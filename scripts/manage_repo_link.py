#!/usr/bin/env python3
"""Race-resistant management for repository-owned symlinks."""

import argparse
import ctypes
import json
import os
import secrets
import stat
import sys
from pathlib import Path

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_TRUSTED_ROOT_ALIASES = {"etc", "tmp", "var"}


def _close_noexcept(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def _identity(item):
    return (item.st_dev, item.st_ino, stat.S_IFMT(item.st_mode), item.st_mtime_ns, item.st_size)


def _directory_identity(item):
    return (item.st_dev, item.st_ino, stat.S_IFMT(item.st_mode))


def _result(target, changed, entry):
    identity, link_target = entry if entry is not None else (None, None)
    return {"path": os.path.abspath(target), "changed": changed, "identity": list(identity) if identity is not None else None, "link_target": link_target}


def _canonicalize_trusted_root_alias(path):
    parts = path.parts
    if len(parts) < 2 or parts[0] != "/" or parts[1] not in _TRUSTED_ROOT_ALIASES:
        return path
    alias = Path("/") / parts[1]
    try:
        info = os.lstat(alias)
    except FileNotFoundError:
        return path
    if not stat.S_ISLNK(info.st_mode):
        return path
    raw_target = os.readlink(alias)
    resolved = Path(os.path.normpath(os.path.join(str(alias.parent), raw_target)))
    expected = Path("/private") / parts[1]
    if resolved != expected:
        raise RuntimeError("refusing unexpected root alias: %s -> %s" % (alias, raw_target))
    return expected.joinpath(*parts[2:])


def _open_parent(target, *, create=False):
    target = os.path.abspath(target)
    path = Path(target)
    transaction_dir = os.environ.get("UNIFIED_KANBAN_TRANSACTION_DIR")
    transaction_fd = os.environ.get("UNIFIED_KANBAN_TRANSACTION_FD")
    if transaction_dir and transaction_fd and path.parent == Path(transaction_dir):
        return os.dup(int(transaction_fd)), path.name
    path = _canonicalize_trusted_root_alias(path)
    if path.name in {"", ".", ".."}:
        raise RuntimeError("link target must name an absolute leaf")
    directory_fd = os.open("/", _DIR_FLAGS)
    try:
        for component in path.parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise RuntimeError("unsafe link parent component: %s" % path.parent)
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise
                created = False
                try:
                    os.mkdir(component, 0o700, dir_fd=directory_fd)
                    created = True
                except FileExistsError:
                    pass
                if created:
                    os.fsync(directory_fd)
                child = os.open(component, _DIR_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
        opened = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise RuntimeError("link parent is not a directory: %s" % path.parent)
        return directory_fd, path.name
    except BaseException:
        os.close(directory_fd)
        raise


def _verify_parent(target, directory_fd):
    check_fd, _ = _open_parent(target)
    try:
        if _directory_identity(os.fstat(check_fd)) != _directory_identity(os.fstat(directory_fd)):
            raise RuntimeError("link parent changed during operation: %s" % Path(target).parent)
    finally:
        os.close(check_fd)


def _entry(directory_fd, name):
    try:
        item = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    link_target = os.readlink(name, dir_fd=directory_fd) if stat.S_ISLNK(item.st_mode) else None
    return (_identity(item), link_target)


def _write_all(fd, encoded):
    view = memoryview(encoded)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short write while persisting link receipt")
        view = view[count:]


def _publish_exclusive(directory_fd, source, target):
    os.link(
        source,
        target,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )
    os.fsync(directory_fd)
    os.unlink(source, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _rename_exclusive(directory_fd, source, destination):
    if sys.platform != "darwin":
        raise RuntimeError("exclusive managed-link retirement requires macOS")
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = libc.renameatx_np
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    if renameatx_np(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        0x00000004,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "%s -> %s" % (source, destination))
    os.fsync(directory_fd)


def _discard_expected(directory_fd, name, expected, retired_prefix):
    retired = ".%s.%s" % (retired_prefix, secrets.token_hex(16))
    _rename_exclusive(directory_fd, name, retired)
    moved = _entry(directory_fd, retired)
    if moved != expected:
        try:
            _rename_exclusive(directory_fd, retired, name)
        except OSError as error:
            raise RuntimeError(
                "managed-link cleanup changed; foreign successor retained"
            ) from error
        raise RuntimeError("managed-link cleanup identity changed")
    os.unlink(retired, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _write_receipt(path, result):
    receipt = Path(path)
    if not receipt.is_absolute():
        raise ValueError("receipt path must be absolute")
    directory_fd, name = _open_parent(receipt)
    temp = None
    encoded = (json.dumps({"entries": [result]}, sort_keys=True) + "\n").encode()
    try:
        while True:
            temp = ".%s.%s" % (name, secrets.token_hex(16))
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=directory_fd)
                break
            except FileExistsError:
                continue
        try:
            _write_all(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory_fd)
        _verify_parent(receipt, directory_fd)
        _publish_exclusive(directory_fd, temp, name)
        temp = None
        _verify_parent(receipt, directory_fd)
    finally:
        if temp is not None:
            try:
                os.unlink(temp, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileNotFoundError:
                pass
        _close_noexcept(directory_fd)


def _install(source, target, receipt=None):
    source = os.path.abspath(source)
    target = os.path.abspath(target)
    directory_fd, name = _open_parent(target)
    created = False
    installed = None
    candidate = None
    candidate_entry = None
    try:
        current = _entry(directory_fd, name)
        if current is not None:
            if current[1] == source:
                result = _result(target, False, current)
                if receipt is not None:
                    _write_receipt(receipt, result)
                return result
            kind = "foreign symlink" if current[1] is not None else "non-symlink"
            raise RuntimeError("Refusing to replace %s: %s" % (kind, target))
        _verify_parent(target, directory_fd)
        while True:
            candidate = ".unified-kanban-link-candidate-%s" % secrets.token_hex(16)
            try:
                os.symlink(source, candidate, dir_fd=directory_fd)
                break
            except FileExistsError:
                continue
        candidate_entry = _entry(directory_fd, candidate)
        if candidate_entry is None or candidate_entry[1] != source:
            raise RuntimeError("link candidate changed during install: %s" % target)
        os.fsync(directory_fd)
        _verify_parent(target, directory_fd)
        try:
            os.link(
                candidate,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            installed = candidate_entry
            created = True
            os.fsync(directory_fd)
        except FileExistsError:
            current = _entry(directory_fd, name)
            if current is None or current[1] != source:
                raise RuntimeError("link target changed during install: %s" % target)
            result = _result(target, False, current)
            if receipt is not None:
                _write_receipt(receipt, result)
            return result
        if _entry(directory_fd, name) != installed:
            raise RuntimeError("installed link changed before receipt: %s" % target)
        _discard_expected(
            directory_fd,
            candidate,
            candidate_entry,
            "unified-kanban-link-candidate-retired",
        )
        candidate = None
        _verify_parent(target, directory_fd)
        result = _result(target, True, installed)
        if receipt is not None:
            _write_receipt(receipt, result)
        return result
    except BaseException:
        if created and installed is not None and _entry(directory_fd, name) == installed:
            failed = ".unified-kanban-link-failed-%s" % secrets.token_hex(16)
            os.rename(name, failed, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
            if _entry(directory_fd, failed) == installed:
                _discard_expected(
                    directory_fd, failed, installed, "unified-kanban-link-retired"
                )
            else:
                try:
                    _publish_exclusive(directory_fd, failed, name)
                except FileExistsError:
                    pass
        raise
    finally:
        try:
            if (
                candidate is not None
                and candidate_entry is not None
                and _entry(directory_fd, candidate) == candidate_entry
            ):
                _discard_expected(
                    directory_fd,
                    candidate,
                    candidate_entry,
                    "unified-kanban-link-candidate-retired",
                )
        finally:
            _close_noexcept(directory_fd)


def install(source, target):
    return _install(source, target)


def _uninstall(expected, target, receipt=None):
    expected = os.path.abspath(expected)
    target = os.path.abspath(target)
    try:
        directory_fd, name = _open_parent(target)
    except FileNotFoundError:
        result = _result(target, False, None)
        if receipt is not None:
            _write_receipt(receipt, result)
        return result
    quarantine = None
    current = None
    try:
        current = _entry(directory_fd, name)
        if current is None:
            result = _result(target, False, None)
            if receipt is not None:
                _write_receipt(receipt, result)
            return result
        if current[1] != expected:
            print("Leaving foreign link/path untouched: %s" % target, file=sys.stderr)
            result = _result(target, False, current)
            if receipt is not None:
                _write_receipt(receipt, result)
            return result
        _verify_parent(target, directory_fd)
        quarantine = ".unified-kanban-unlink-%s" % secrets.token_hex(16)
        os.rename(name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        moved = _entry(directory_fd, quarantine)
        if moved != current:
            if _entry(directory_fd, name) is None:
                os.rename(quarantine, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
                quarantine = None
            raise RuntimeError("link changed during uninstall; foreign entry preserved: %s" % target)
        _verify_parent(target, directory_fd)
        result = _result(target, True, None)
        if receipt is not None:
            _write_receipt(receipt, result)
        if _entry(directory_fd, name) is not None or _entry(directory_fd, quarantine) != current:
            raise RuntimeError("quarantined link changed before removal: %s" % target)
        _discard_expected(
            directory_fd, quarantine, current, "unified-kanban-unlink-retired"
        )
        quarantine = None
        return result
    except BaseException:
        if quarantine is not None and current is not None and _entry(directory_fd, quarantine) == current and _entry(directory_fd, name) is None:
            _publish_exclusive(directory_fd, quarantine, name)
            quarantine = None
        raise
    finally:
        try:
            if quarantine is not None and _entry(directory_fd, quarantine) is not None:
                print("Preserved a concurrently changed link entry at %s/%s" % (Path(target).parent, quarantine), file=sys.stderr)
        finally:
            _close_noexcept(directory_fd)


def uninstall(expected, target):
    return _uninstall(expected, target)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--receipt")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            _install(args.source, args.target, args.receipt)
        else:
            _uninstall(args.source, args.target, args.receipt)
    except (OSError, RuntimeError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
