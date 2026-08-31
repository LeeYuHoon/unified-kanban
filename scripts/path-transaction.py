#!/usr/bin/env python3
"""setup과 uninstall이 다루는 호스트 경로를 위한, identity에 결속된 rollback receipt."""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

_MAX_FILE_BYTES = 8 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_TRUSTED_ROOT_ALIASES = {"etc", "tmp", "var"}
_TOKEN_LEDGER_FD_ENV = "UNIFIED_KANBAN_TOKEN_LEDGER_FD"


class _ReceiptPublishConflict(RuntimeError):
    recovery_name: str | None = None
    recovery_snapshot: dict[str, Any] | None = None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting transaction receipt")
        view = view[written:]


def _identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_mtime_ns, info.st_size]


def _mac_xattrs(fd: int) -> dict[str, str]:
    if sys.platform != "darwin":
        return {}
    libc = ctypes.CDLL(None, use_errno=True)
    flistxattr = libc.flistxattr
    flistxattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    flistxattr.restype = ctypes.c_ssize_t
    fgetxattr = libc.fgetxattr
    fgetxattr.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    fgetxattr.restype = ctypes.c_ssize_t
    size = flistxattr(fd, None, 0, 0)
    if size < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if size == 0:
        return {}
    names_buffer = ctypes.create_string_buffer(size)
    if flistxattr(fd, names_buffer, size, 0) != size:
        raise RuntimeError("file xattr names changed during snapshot")
    result: dict[str, str] = {}
    for raw_name in names_buffer.raw[:size].split(b"\0"):
        if not raw_name:
            continue
        value_size = fgetxattr(fd, raw_name, None, 0, 0, 0)
        if value_size < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), os.fsdecode(raw_name))
        value_buffer = ctypes.create_string_buffer(value_size)
        if fgetxattr(fd, raw_name, value_buffer, value_size, 0, 0) != value_size:
            raise RuntimeError("file xattr changed during snapshot")
        result[os.fsdecode(raw_name)] = base64.b64encode(
            value_buffer.raw[:value_size]
        ).decode("ascii")
    return dict(sorted(result.items()))


def _mac_acl(fd: int) -> str | None:
    if sys.platform != "darwin":
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    acl_get_fd_np = libc.acl_get_fd_np
    acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    acl_get_fd_np.restype = ctypes.c_void_p
    acl_to_text = libc.acl_to_text
    acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
    acl_to_text.restype = ctypes.c_void_p
    acl_free = libc.acl_free
    acl_free.argtypes = [ctypes.c_void_p]
    acl_free.restype = ctypes.c_int
    acl = acl_get_fd_np(fd, 0x00000100)
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return None
        raise OSError(error, os.strerror(error))
    text = None
    try:
        length = ctypes.c_ssize_t()
        text = acl_to_text(acl, ctypes.byref(length))
        if not text:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return base64.b64encode(ctypes.string_at(text, length.value)).decode("ascii")
    finally:
        if text:
            acl_free(text)
        acl_free(acl)


def _clear_mac_acl(fd: int) -> None:
    if sys.platform != "darwin":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    acl_init = libc.acl_init
    acl_init.argtypes = [ctypes.c_int]
    acl_init.restype = ctypes.c_void_p
    acl_free = libc.acl_free
    acl_free.argtypes = [ctypes.c_void_p]
    acl_set_fd_np = libc.acl_set_fd_np
    acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    acl_set_fd_np.restype = ctypes.c_int
    acl = acl_init(1)
    if not acl:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    try:
        if acl_set_fd_np(fd, acl, 0x00000100) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    finally:
        acl_free(acl)


def _make_file_private(fd: int) -> None:
    """이미 검증된 일반(regular) inode에서 소유자가 아닌 주체의 실효 접근 권한을 제거한다."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        fchflags = libc.fchflags
        fchflags.argtypes = [ctypes.c_int, ctypes.c_uint]
        fchflags.restype = ctypes.c_int
        if fchflags(fd, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        _clear_mac_acl(fd)
    os.fchmod(fd, 0o600)
    current = os.fstat(fd)
    if stat.S_IMODE(current.st_mode) != 0o600:
        raise RuntimeError("private file mode verification failed")
    if sys.platform == "darwin":
        if int(getattr(current, "st_flags", 0)) != 0:
            raise RuntimeError("private file flags verification failed")
        if _mac_acl(fd) not in {None, ""}:
            raise RuntimeError("private file ACL verification failed")


def _file_metadata(info: os.stat_result, fd: int) -> dict[str, Any]:
    return {
        "uid": info.st_uid,
        "gid": info.st_gid,
        "atime_ns": info.st_atime_ns,
        "mtime_ns": info.st_mtime_ns,
        "flags": int(getattr(info, "st_flags", 0)),
        "xattrs": _mac_xattrs(fd),
        "acl": _mac_acl(fd),
    }


def _apply_file_metadata(fd: int, metadata: dict[str, Any]) -> None:
    os.fchown(fd, int(metadata["uid"]), int(metadata["gid"]))
    os.fchmod(fd, int(metadata["mode"]))
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        fremovexattr = libc.fremovexattr
        fremovexattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        fremovexattr.restype = ctypes.c_int
        fsetxattr = libc.fsetxattr
        fsetxattr.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        fsetxattr.restype = ctypes.c_int
        for name in _mac_xattrs(fd):
            if fremovexattr(fd, os.fsencode(name), 0) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), name)
        for name, encoded in metadata["xattrs"].items():
            value = base64.b64decode(encoded, validate=True)
            value_buffer = ctypes.create_string_buffer(value)
            if fsetxattr(
                fd,
                os.fsencode(name),
                value_buffer,
                len(value),
                0,
                0,
            ) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), name)
        acl_text = metadata.get("acl")
        acl_free = libc.acl_free
        acl_free.argtypes = [ctypes.c_void_p]
        if acl_text is not None:
            acl_from_text = libc.acl_from_text
            acl_from_text.argtypes = [ctypes.c_char_p]
            acl_from_text.restype = ctypes.c_void_p
            raw_acl = base64.b64decode(acl_text, validate=True)
            acl = acl_from_text(raw_acl)
        else:
            acl_init = libc.acl_init
            acl_init.argtypes = [ctypes.c_int]
            acl_init.restype = ctypes.c_void_p
            acl = acl_init(1)
        if not acl:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        acl_set_fd_np = libc.acl_set_fd_np
        acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        acl_set_fd_np.restype = ctypes.c_int
        try:
            if acl_set_fd_np(fd, acl, 0x00000100) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        finally:
            acl_free(acl)
        os.utime(
            fd,
            ns=(int(metadata["atime_ns"]), int(metadata["mtime_ns"])),
        )
        fchflags = libc.fchflags
        fchflags.argtypes = [ctypes.c_int, ctypes.c_uint]
        fchflags.restype = ctypes.c_int
        if fchflags(fd, int(metadata["flags"])) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))


def _directory_identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)]


def _created_directory_identity(info: os.stat_result) -> list[int]:
    return [
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode),
        info.st_uid,
        info.st_gid,
    ]


def _canonicalize_trusted_root_alias(path: Path) -> Path:
    """macOS의 고정된 루트 별칭만 해석하며, 사용자가 제어하는 부모 경로는 절대 따라가지 않는다."""
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
        raise RuntimeError(f"refusing unexpected root alias: {alias} -> {raw_target}")
    return expected.joinpath(*parts[2:])


def _open_parent(path: Path, *, create: bool = False) -> tuple[int, str]:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise RuntimeError(f"transaction path must be an absolute leaf path: {path}")
    transaction_dir = os.environ.get("UNIFIED_KANBAN_TRANSACTION_DIR")
    transaction_fd = os.environ.get("UNIFIED_KANBAN_TRANSACTION_FD")
    if transaction_dir and transaction_fd and path.parent == Path(transaction_dir):
        return os.dup(int(transaction_fd)), path.name
    path = _canonicalize_trusted_root_alias(path)
    fd = os.open("/", _DIR_FLAGS)
    try:
        for component in path.parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise RuntimeError(f"unsafe transaction parent component: {path.parent}")
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                created = False
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                if created:
                    os.fsync(fd)
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, path.name
    except BaseException:
        os.close(fd)
        raise


def _verify_bound_parent(path: Path, directory_fd: int) -> None:
    expected = _directory_identity(os.fstat(directory_fd))
    check_fd, _ = _open_parent(path)
    try:
        if _directory_identity(os.fstat(check_fd)) != expected:
            raise RuntimeError(f"transaction parent changed: {path.parent}")
    finally:
        os.close(check_fd)


def _parent_identity(path: Path) -> list[int]:
    fd, _ = _open_parent(path)
    try:
        return _directory_identity(os.fstat(fd))
    finally:
        os.close(fd)


def _verify_parent(entry: dict[str, Any]) -> None:
    path = Path(entry["path"])
    if _parent_identity(path) != entry["parent_identity"]:
        raise RuntimeError(f"transaction parent changed: {path.parent}")


def _snapshot_at(directory_fd: int, name: str, display: Path) -> dict[str, Any]:
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return {"kind": "absent"}
    identity = _identity(before)
    if stat.S_ISLNK(before.st_mode):
        return {"kind": "symlink", "identity": identity, "target": os.readlink(name, dir_fd=directory_fd)}
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"transaction path must be absent, regular, or symlink: {display}")
    if before.st_size > _MAX_FILE_BYTES:
        raise RuntimeError(f"transaction file exceeds {_MAX_FILE_BYTES} bytes: {display}")
    fd = os.open(name, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if _identity(opened) != identity or not stat.S_ISREG(opened.st_mode):
            raise RuntimeError(f"transaction path changed during snapshot: {display}")
        metadata = _file_metadata(opened, fd)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, _MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_FILE_BYTES:
                raise RuntimeError(f"transaction file exceeds {_MAX_FILE_BYTES} bytes: {display}")
        data = b"".join(chunks)
    finally:
        os.close(fd)
    return {
        "kind": "file",
        "identity": identity,
        "mode": stat.S_IMODE(opened.st_mode),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": base64.b64encode(data).decode("ascii"),
        "metadata": {"mode": stat.S_IMODE(opened.st_mode), **metadata},
    }


def _snapshot(path: Path) -> dict[str, Any]:
    fd, name = _open_parent(path)
    try:
        return _snapshot_at(fd, name, path)
    finally:
        os.close(fd)


def _equivalent(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    if current.get("kind") != expected.get("kind"):
        return False
    if current["kind"] == "absent":
        return True
    if current.get("identity") != expected.get("identity"):
        return False
    if current["kind"] == "symlink":
        return current.get("target") == expected.get("target")
    current_metadata = current.get("metadata")
    expected_metadata = expected.get("metadata")
    metadata_matches = True
    if isinstance(expected_metadata, dict):
        metadata_matches = isinstance(current_metadata, dict) and {
            key: value
            for key, value in current_metadata.items()
            if key != "atime_ns"
        } == {
            key: value
            for key, value in expected_metadata.items()
            if key != "atime_ns"
        }
    return (
        current.get("mode") == expected.get("mode")
        and current.get("sha256") == expected.get("sha256")
        and metadata_matches
    )


def _receipt_token(snapshot: dict[str, Any]) -> str:
    normalized = dict(snapshot)
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict):
        normalized["metadata"] = {
            key: value for key, value in metadata.items() if key != "atime_ns"
        }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    domain = b"unified-kanban-transaction-receipt-v1\0"
    return hashlib.sha256(domain + encoded).hexdigest()


def _record_authority_token(token: str) -> None:
    raw_fd = os.environ.get(_TOKEN_LEDGER_FD_ENV)
    if raw_fd is None:
        return
    fd = int(raw_fd)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise RuntimeError("transaction token ledger must be a private owned regular file")
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_APPEND)
    os.lseek(fd, 0, os.SEEK_END)
    _write_all(fd, f"{token}\n".encode("ascii"))
    os.fsync(fd)


def _authority_tokens(fd: int) -> list[str]:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
        or info.st_size > 1024 * 1024
    ):
        raise RuntimeError("transaction token ledger is invalid")
    raw = os.pread(fd, info.st_size, 0)
    tokens = raw.decode("ascii").splitlines()
    return [
        token
        for token in tokens
        if len(token) == 64
        and all(character in "0123456789abcdef" for character in token)
    ]


def _require_receipt_snapshot(path: Path, expected_token: str) -> dict[str, Any]:
    if len(expected_token) != 64 or any(
        character not in "0123456789abcdef" for character in expected_token
    ):
        raise RuntimeError("transaction receipt token is malformed")
    snapshot = _snapshot(path)
    if not hmac.compare_digest(_receipt_token(snapshot), expected_token):
        raise RuntimeError("transaction receipt identity changed")
    return snapshot


def _current_ledger_authority_token(path: Path) -> str:
    raw_fd = os.environ.get(_TOKEN_LEDGER_FD_ENV)
    if raw_fd is None:
        raise RuntimeError("transaction token ledger is required for authority recovery")
    for token in reversed(_authority_tokens(int(raw_fd))):
        try:
            _require_receipt_snapshot(path, token)
        except RuntimeError:
            continue
        return token
    raise RuntimeError("no transaction authority token matches the current receipt")


def _close_noexcept(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _chmod_expected_at(
    directory_fd: int, name: str, expected_identity: list[int], mode: int
) -> None:
    opened = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_fd)
    try:
        if _identity(os.fstat(opened)) != expected_identity:
            raise RuntimeError(f"transaction quarantine changed before chmod: {name}")
        if mode == 0o600:
            _make_file_private(opened)
        else:
            os.fchmod(opened, mode)
    finally:
        os.close(opened)


def _private_quarantine_matches(current: dict[str, Any], before: dict[str, Any]) -> bool:
    metadata = current.get("metadata")
    return (
        before.get("kind") == "file"
        and current.get("kind") == "file"
        and current.get("identity") == before.get("identity")
        and current.get("mode") == 0o600
        and current.get("sha256") == before.get("sha256")
        and isinstance(metadata, dict)
        and metadata.get("flags") == 0
        and metadata.get("acl") in {None, ""}
    )


def _random_name(prefix: str) -> str:
    return f".{prefix}.{secrets.token_hex(16)}"


def _rename_durable(source: str, destination: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
    os.rename(
        source,
        destination,
        src_dir_fd=src_dir_fd,
        dst_dir_fd=dst_dir_fd,
    )
    os.fsync(src_dir_fd)
    if dst_dir_fd != src_dir_fd:
        os.fsync(dst_dir_fd)


def _unlink_durable(name: str, *, dir_fd: int) -> None:
    os.unlink(name, dir_fd=dir_fd)
    os.fsync(dir_fd)


def _rmdir_durable(name: str, *, dir_fd: int) -> None:
    os.rmdir(name, dir_fd=dir_fd)
    os.fsync(dir_fd)


def _create_temp(directory_fd: int, prefix: str, data: bytes, mode: int) -> tuple[str, list[int]]:
    while True:
        name = _random_name(prefix)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=directory_fd)
            break
        except FileExistsError:
            continue
    try:
        _write_all(fd, data)
        os.fsync(fd)
        identity = _identity(os.fstat(fd))
        os.fsync(directory_fd)
    except BaseException:
        os.close(fd)
        _unlink_durable(name, dir_fd=directory_fd)
        raise
    os.close(fd)
    return name, identity


def _apply_snapshot_metadata_at(
    directory_fd: int,
    name: str,
    snapshot: dict[str, Any],
    expected_identity: list[int],
    *,
    private_mode: bool = False,
) -> None:
    metadata = snapshot.get("metadata")
    if not isinstance(metadata, dict):
        return
    fd = os.open(name, os.O_RDWR | _NOFOLLOW, dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError("metadata target must be a regular file")
        if _identity(opened)[:2] != expected_identity[:2]:
            raise RuntimeError("metadata target identity changed")
        applied = dict(metadata)
        if private_mode:
            applied["mode"] = 0o600
            applied["flags"] = 0
            applied["acl"] = None
        _apply_file_metadata(fd, applied)
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_temp_exclusive(
    directory_fd: int,
    temp: str,
    name: str,
    expected: dict[str, Any],
) -> None:
    """동시에 생성된 leaf를 대체하지 않으면서 일반 파일 temp를 게시한다."""
    if not _equivalent(_snapshot_at(directory_fd, temp, Path(name)), expected):
        raise RuntimeError("publication temp changed before link")
    os.link(
        temp,
        name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )
    os.fsync(directory_fd)
    published = _snapshot_at(directory_fd, name, Path(name))
    if not _equivalent(published, expected):
        raise RuntimeError(
            "publication destination changed during link; foreign inode retained"
        )
    _unlink_exact_receipt(
        directory_fd, temp, Path(name), expected, "published temp"
    )


def _swap_names_atomic(directory_fd: int, first: str, second: str) -> None:
    """교환과 내구성(durability)을 뒤섞지 않고 두 엔트리를 원자적으로 교환한다."""
    if sys.platform != "darwin":
        raise RuntimeError("atomic transaction receipt exchange requires macOS")
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
        os.fsencode(first),
        directory_fd,
        os.fsencode(second),
        0x00000002,  # <sys/stdio.h>에 정의된 RENAME_SWAP
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{first} <-> {second}")


def _swap_names(directory_fd: int, first: str, second: str) -> None:
    """두 디렉터리 엔트리를 원자적으로 교환하고 그 교환을 내구성 있게 만든다."""
    _swap_names_atomic(directory_fd, first, second)
    os.fsync(directory_fd)


def _minimal_identity_at(
    directory_fd: int, name: str
) -> list[int] | None:
    """leaf의 identity만 읽으며, 외부(foreign) 대상의 타입, 내용, ACL, 크기는 절대 조사하지 않는다."""
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _identity(info)


def _restore_unknown_displaced(
    directory_fd: int,
    canonical: str,
    quarantine: str,
    path: Path,
    placeholder: dict[str, Any],
) -> OSError | None:
    """밀려난(displaced) leaf를 읽거나 분류하지 않고 교환을 되돌린다."""
    placeholder_identity = placeholder["identity"]
    last_error: OSError | None = None
    for _attempt in range(3):
        canonical_identity = _minimal_identity_at(directory_fd, canonical)
        quarantine_identity = _minimal_identity_at(directory_fd, quarantine)
        if quarantine_identity == placeholder_identity:
            try:
                os.fsync(directory_fd)
            except OSError as error:
                return error
            return None
        if canonical_identity != placeholder_identity or quarantine_identity is None:
            raise RuntimeError(f"foreign successor recovery identities changed: {path}")
        try:
            _swap_names_atomic(directory_fd, canonical, quarantine)
        except OSError as error:
            last_error = error
            continue
        if _minimal_identity_at(directory_fd, quarantine) != placeholder_identity:
            raise RuntimeError(f"foreign successor recovery changed identity: {path}")
        try:
            os.fsync(directory_fd)
        except OSError as error:
            return error
        return None
    raise RuntimeError(f"foreign successor recovery exchange failed: {path}") from last_error


def _restore_swapped_canonical(
    directory_fd: int,
    canonical: str,
    quarantine: str,
    path: Path,
    displaced: dict[str, Any],
    placeholder: dict[str, Any],
) -> OSError | None:
    """실패한 원자적 교환을 재시도하면서, 밀려난 canonical 엔트리를 복원한다."""
    last_error: OSError | None = None
    for _attempt in range(3):
        current_canonical = _snapshot_at(directory_fd, canonical, path)
        current_quarantine = _snapshot_at(directory_fd, quarantine, path)
        if _equivalent(current_canonical, displaced) and _equivalent(
            current_quarantine, placeholder
        ):
            try:
                os.fsync(directory_fd)
            except OSError as error:
                return error
            return None
        if not _equivalent(current_canonical, placeholder) or not _equivalent(
            current_quarantine, displaced
        ):
            raise RuntimeError(
                f"foreign successor recovery identities changed: {path}"
            )
        try:
            _swap_names_atomic(directory_fd, canonical, quarantine)
        except OSError as error:
            last_error = error
            continue
        restored_canonical = _snapshot_at(directory_fd, canonical, path)
        restored_quarantine = _snapshot_at(directory_fd, quarantine, path)
        if not _equivalent(restored_canonical, displaced) or not _equivalent(
            restored_quarantine, placeholder
        ):
            raise RuntimeError(f"foreign successor recovery changed identity: {path}")
        try:
            os.fsync(directory_fd)
        except OSError as error:
            return error
        return None
    raise RuntimeError(f"foreign successor recovery exchange failed: {path}") from last_error


def _rename_exclusive(directory_fd: int, source: str, destination: str) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("exclusive receipt rename requires macOS renameatx_np")
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
        0x00000004,  # RENAME_EXCL 플래그
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{source} -> {destination}")
    os.fsync(directory_fd)


def _retain_published_receipt_and_restore_foreign(
    directory_fd: int,
    canonical: str,
    displaced: str,
    installed: dict[str, Any],
    foreign: dict[str, Any],
    path: Path,
) -> str:
    authority = f".{canonical}.recovery.{secrets.token_hex(16)}"
    _rename_exclusive(directory_fd, canonical, authority)
    if not _equivalent(_snapshot_at(directory_fd, authority, path), installed):
        raise RuntimeError("retained recovery receipt identity changed")
    try:
        _rename_exclusive(directory_fd, displaced, canonical)
    except BaseException:
        if _snapshot_at(directory_fd, canonical, path)["kind"] == "absent":
            _rename_exclusive(directory_fd, authority, canonical)
        raise
    if not _equivalent(_snapshot_at(directory_fd, canonical, path), foreign):
        raise RuntimeError("foreign transaction receipt was not restored")
    return authority


def _receipt_file_snapshot(
    info: os.stat_result, encoded: bytes, fd: int | None = None
) -> dict[str, Any]:
    snapshot = {
        "kind": "file",
        "identity": _identity(info),
        "mode": stat.S_IMODE(info.st_mode),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "data": base64.b64encode(encoded).decode("ascii"),
    }
    if fd is not None:
        snapshot["metadata"] = {
            "mode": stat.S_IMODE(info.st_mode),
            **_file_metadata(info, fd),
        }
    return snapshot


def _write_receipt(
    path: Path,
    payload: dict[str, Any],
    *,
    exclusive: bool = False,
    expected: dict[str, Any] | None = None,
    record_token_before_publish: bool = False,
) -> dict[str, Any]:
    directory_fd, name = _open_parent(path)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temp: str | None = None
    temp_snapshot: dict[str, Any] | None = None
    previous: str | None = None
    try:
        if exclusive:
            temp, installed_identity = _create_temp(
                directory_fd, f"{name}.receipt", encoded, 0o600
            )
            temp_info = os.stat(temp, dir_fd=directory_fd, follow_symlinks=False)
            if _identity(temp_info) != installed_identity:
                raise _ReceiptPublishConflict("transaction receipt temp identity changed")
            installed = _snapshot_at(directory_fd, temp, path)
            temp_snapshot = installed
            if record_token_before_publish:
                _record_authority_token(_receipt_token(installed))
            try:
                _verify_bound_parent(path, directory_fd)
                _publish_temp_exclusive(directory_fd, temp, name, installed)
                temp = None
                temp_snapshot = None
                if not _equivalent(
                    _snapshot_at(directory_fd, name, path), installed
                ):
                    raise RuntimeError("created receipt identity changed")
            except Exception as error:
                raise _ReceiptPublishConflict(
                    "transaction receipt changed immediately after creation"
                ) from error
            return installed
        if expected is None:
            raise RuntimeError("receipt replacement requires expected producer identity")
        temp, installed_identity = _create_temp(
            directory_fd, f"{name}.receipt", encoded, 0o600
        )
        temp_info = os.stat(temp, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(temp_info) != installed_identity:
            raise _ReceiptPublishConflict("transaction receipt temp identity changed")
        installed = _snapshot_at(directory_fd, temp, path)
        temp_snapshot = installed
        if record_token_before_publish:
            _record_authority_token(_receipt_token(installed))
        _verify_bound_parent(path, directory_fd)
        previous = temp
        temp = None
        temp_snapshot = None
        try:
            _swap_names(directory_fd, previous, name)
        except BaseException:
            if not _equivalent(
                _snapshot_at(directory_fd, name, path), installed
            ):
                temp = previous
                previous = None
            raise
        try:
            _verify_bound_parent(path, directory_fd)
            moved_previous = _snapshot_at(directory_fd, previous, path)
            if not _equivalent(moved_previous, expected):
                if _equivalent(_snapshot_at(directory_fd, name, path), installed):
                    try:
                        _swap_names(directory_fd, previous, name)
                    except BaseException as recovery_error:
                        authority = _retain_published_receipt_and_restore_foreign(
                            directory_fd,
                            name,
                            previous,
                            installed,
                            moved_previous,
                            path,
                        )
                        previous = None
                        conflict = _ReceiptPublishConflict(
                            "transaction receipt changed before atomic publication"
                        )
                        conflict.recovery_name = authority
                        conflict.recovery_snapshot = installed
                        raise conflict from recovery_error
                    if _equivalent(
                        _snapshot_at(directory_fd, previous, path), installed
                    ):
                        _unlink_exact_receipt(
                            directory_fd,
                            previous,
                            path,
                            installed,
                            "swapped-back candidate receipt",
                        )
                        previous = None
                raise RuntimeError(
                    "transaction receipt changed before atomic publication"
                )
            if not _equivalent(_snapshot_at(directory_fd, name, path), installed):
                raise RuntimeError("published receipt identity changed")
        except Exception as error:
            if isinstance(error, _ReceiptPublishConflict):
                raise
            raise _ReceiptPublishConflict(
                "transaction receipt changed immediately after publication"
            ) from error
        _unlink_exact_receipt(
            directory_fd,
            previous,
            path,
            expected,
            "previous transaction receipt",
        )
        previous = None
        return installed
    finally:
        if temp is not None:
            _remove_at(directory_fd, temp, path, temp_snapshot)
        _close_noexcept(directory_fd)


def _read_json_file(
    path: Path, label: str, expected: dict[str, Any] | None = None
) -> Any:
    directory_fd, name = _open_parent(path)
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"{label} must be a regular non-symlink file")
        if stat.S_IMODE(before.st_mode) & 0o077:
            raise RuntimeError(f"{label} must not be group/world accessible")
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd)
        try:
            opened = os.fstat(fd)
            if _identity(opened) != _identity(before):
                raise RuntimeError(f"{label} changed during validation")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_FILE_BYTES:
                    raise RuntimeError(f"{label} exceeds {_MAX_FILE_BYTES} bytes")
                chunks.append(chunk)
            raw = b"".join(chunks)
            opened_after = os.fstat(fd)
            if _identity(opened_after) != _identity(opened):
                raise RuntimeError(f"{label} changed during read")
            opened_snapshot = _receipt_file_snapshot(opened_after, raw, fd)
            if expected is not None and not _equivalent(opened_snapshot, expected):
                raise RuntimeError(f"{label} identity does not match producer receipt")
            return json.loads(raw.decode("utf-8"))
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(directory_fd)


def _read_receipt(
    path: Path, expected: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = _read_json_file(path, "transaction receipt", expected)
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("entries"), list):
        raise RuntimeError("transaction receipt schema is invalid")
    created = payload.get("created_parents", [])
    if not isinstance(created, list) or any(
        not isinstance(entry, dict)
        or set(entry) != {"path", "identity"}
        or not isinstance(entry["path"], str)
        or not isinstance(entry["identity"], list)
        or len(entry["identity"]) != 6
        or any(not isinstance(value, int) for value in entry["identity"])
        for entry in created
    ):
        raise RuntimeError("transaction created-parent schema is invalid")
    return payload


def _read_operation_receipt(
    path: Path, expected: dict[str, Any]
) -> list[dict[str, Any]]:
    payload = _read_json_file(path, "operation receipt", expected)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("operation receipt entries must be a non-empty list")
    return entries


def _remove_at(
    directory_fd: int,
    name: str,
    display: Path,
    expected: dict[str, Any] | None,
) -> None:
    if expected is None:
        return
    current = _snapshot_at(directory_fd, name, display)
    if current["kind"] == "absent":
        return
    if not _equivalent(current, expected):
        raise RuntimeError(f"temporary path changed before cleanup: {display}")
    _unlink_exact_receipt(
        directory_fd, name, display, expected, "temporary transaction file"
    )


def _restore_bound(path: Path, directory_fd: int, name: str, before: dict[str, Any], expected: dict[str, Any]) -> None:
    if not _equivalent(_snapshot_at(directory_fd, name, path), expected):
        raise RuntimeError(f"path changed before transaction restore: {path}")
    data: bytes | None = None
    temp: str | None = None
    temp_snapshot: dict[str, Any] | None = None
    if before["kind"] == "file":
        data = base64.b64decode(before["data"], validate=True)
        if hashlib.sha256(data).hexdigest() != before["sha256"]:
            raise RuntimeError(f"transaction receipt content checksum mismatch: {path}")
        temp, temp_identity = _create_temp(
            directory_fd, f"{name}.rollback", data, int(before["mode"])
        )
        _apply_snapshot_metadata_at(directory_fd, temp, before, temp_identity)
        temp_snapshot = _snapshot_at(directory_fd, temp, path)
    quarantine: str | None = None
    quarantine_placeholder: dict[str, Any] | None = None
    if expected["kind"] != "absent":
        quarantine, _quarantine_identity = _create_temp(
            directory_fd, f"{name}.quarantine", b"", 0o600
        )
        quarantine_placeholder = _snapshot_at(directory_fd, quarantine, path)
    moved = False
    installed_baseline = False
    try:
        _verify_bound_parent(path, directory_fd)
        if expected["kind"] != "absent":
            if quarantine is None or quarantine_placeholder is None:
                raise RuntimeError(f"transaction restore quarantine missing: {path}")
            _swap_names_atomic(directory_fd, name, quarantine)
            moved = True
            try:
                displaced_identity = _minimal_identity_at(directory_fd, quarantine)
                canonical_identity = _minimal_identity_at(directory_fd, name)
                os.fsync(directory_fd)
                if canonical_identity != quarantine_placeholder["identity"]:
                    raise RuntimeError(
                        f"rollback placeholder changed during transaction restore: {path}"
                    )
                if displaced_identity != expected["identity"]:
                    raise RuntimeError(f"path changed during transaction restore: {path}")
                displaced = _snapshot_at(directory_fd, quarantine, path)
                canonical_placeholder = _snapshot_at(directory_fd, name, path)
                if not _equivalent(canonical_placeholder, quarantine_placeholder):
                    raise RuntimeError(
                        f"rollback placeholder changed during transaction restore: {path}"
                    )
                if not _equivalent(displaced, expected):
                    raise RuntimeError(f"path changed during transaction restore: {path}")
            except BaseException as exchange_error:
                recovery_error = _restore_unknown_displaced(
                    directory_fd,
                    name,
                    quarantine,
                    path,
                    quarantine_placeholder,
                )
                moved = False
                _remove_at(
                    directory_fd,
                    quarantine,
                    path,
                    quarantine_placeholder,
                )
                quarantine = None
                quarantine_placeholder = None
                if recovery_error is not None:
                    raise exchange_error from recovery_error
                raise
            if expected["kind"] == "file":
                _chmod_expected_at(
                    directory_fd, quarantine, expected["identity"], 0o600
                )
                if not _private_quarantine_matches(
                    _snapshot_at(directory_fd, quarantine, path), expected
                ):
                    raise RuntimeError(f"path changed during transaction restore: {path}")
            elif not _equivalent(
                _snapshot_at(directory_fd, quarantine, path), expected
            ):
                raise RuntimeError(f"path changed during transaction restore: {path}")
            _unlink_exact_receipt(
                directory_fd,
                name,
                path,
                canonical_placeholder,
                "rollback swap placeholder",
            )
        if before["kind"] == "file":
            if temp is None:
                raise RuntimeError(f"transaction restore temp missing: {path}")
            if temp_snapshot is None:
                raise RuntimeError(f"transaction restore temp snapshot missing: {path}")
            _publish_temp_exclusive(directory_fd, temp, name, temp_snapshot)
            temp = None
            temp_snapshot = None
            installed_baseline = True
        elif before["kind"] == "symlink":
            os.symlink(before["target"], name, dir_fd=directory_fd)
            os.fsync(directory_fd)
            installed_baseline = True
        _verify_bound_parent(path, directory_fd)
        restored = _snapshot_at(directory_fd, name, path)
        if before["kind"] == "absent":
            if restored["kind"] != "absent":
                raise RuntimeError(f"transaction restore did not remove path: {path}")
        else:
            restored_expected = {
                **before,
                "identity": restored.get("identity"),
            }
            if not _equivalent(restored, restored_expected):
                raise RuntimeError(f"transaction restore verification failed: {path}")
            if before["kind"] == "file":
                _apply_snapshot_metadata_at(
                    directory_fd, name, before, restored["identity"]
                )
                verify_fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_fd)
                try:
                    verify_info = os.fstat(verify_fd)
                    if (
                        not stat.S_ISREG(verify_info.st_mode)
                        or _identity(verify_info)[:2] != restored["identity"][:2]
                    ):
                        raise RuntimeError(
                            f"transaction restore identity changed during metadata verification: {path}"
                        )
                    verified_metadata = {
                        "mode": stat.S_IMODE(verify_info.st_mode),
                        **_file_metadata(verify_info, verify_fd),
                    }
                    if verified_metadata != before.get("metadata"):
                        raise RuntimeError(
                            f"transaction restore metadata verification failed: {path}"
                        )
                finally:
                    os.close(verify_fd)
        if moved:
            if quarantine is None:
                raise RuntimeError(f"rollback quarantine missing: {path}")
            quarantine_snapshot = _snapshot_at(directory_fd, quarantine, path)
            _unlink_exact_receipt(
                directory_fd,
                quarantine,
                path,
                quarantine_snapshot,
                "rollback quarantine",
            )
            moved = False
    except BaseException:
        if installed_baseline:
            cleanup_snapshot = _snapshot_at(directory_fd, name, path)
            cleanup_expected = {
                **before,
                "identity": cleanup_snapshot.get("identity"),
            }
            if _equivalent(cleanup_snapshot, cleanup_expected):
                _unlink_exact_receipt(
                    directory_fd,
                    name,
                    path,
                    cleanup_snapshot,
                    "failed rollback replacement",
                )
        if (
            moved
            and quarantine is not None
            and _snapshot_at(directory_fd, name, path)["kind"] == "absent"
        ):
            if expected["kind"] == "file":
                _apply_snapshot_metadata_at(
                    directory_fd, quarantine, expected, expected["identity"]
                )
            _publish_temp_exclusive(
                directory_fd, quarantine, name, expected
            )
            if expected["kind"] == "file":
                _apply_snapshot_metadata_at(
                    directory_fd, name, expected, expected["identity"]
                )
            moved = False
        raise
    finally:
        if temp is not None:
            _remove_at(directory_fd, temp, path, temp_snapshot)
        if quarantine is not None and not moved and quarantine_placeholder is not None:
            _remove_at(
                directory_fd,
                quarantine,
                path,
                quarantine_placeholder,
            )


def _restore(path: Path, before: dict[str, Any], expected: dict[str, Any] | None = None) -> None:
    directory_fd, name = _open_parent(path)
    try:
        current = _snapshot_at(directory_fd, name, path)
        _restore_bound(path, directory_fd, name, before, current if expected is None else expected)
    finally:
        os.close(directory_fd)


def _ensure_parent(path: Path, created: list[dict[str, Any]]) -> list[int]:
    physical = _canonicalize_trusted_root_alias(path)
    fd = os.open("/", _DIR_FLAGS)
    current = Path("/")
    try:
        for component in physical.parent.parts[1:]:
            child_created = False
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                    child_created = True
                except FileExistsError:
                    pass
                if child_created:
                    os.fsync(fd)
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            current /= component
            identity = _created_directory_identity(os.fstat(child))
            if child_created:
                created.append({"path": str(current), "identity": identity})
            os.close(fd)
            fd = child
        return _directory_identity(os.fstat(fd))
    finally:
        os.close(fd)


def _remove_created_parents(created: list[dict[str, Any]]) -> None:
    for entry in reversed(created):
        path = Path(entry["path"])
        fd, name = _open_parent(path)
        try:
            retired = _random_name(f"{name}.retired")
            _rename_exclusive(fd, name, retired)
            current = os.stat(retired, dir_fd=fd, follow_symlinks=False)
            if _created_directory_identity(current) != entry["identity"]:
                _rename_exclusive(fd, retired, name)
                raise RuntimeError(f"transaction-created parent changed: {path}")
            _rmdir_durable(retired, dir_fd=fd)
        finally:
            os.close(fd)


def begin(receipt: Path, paths: list[Path]) -> str:
    if not paths or any(not path.is_absolute() for path in paths):
        raise RuntimeError("transaction paths must be non-empty absolute paths")
    if len({str(path) for path in paths}) != len(paths):
        raise RuntimeError("transaction paths must be unique")
    created: list[dict[str, Any]] = []
    try:
        parent_identities = [_ensure_parent(path, created) for path in paths]
        entries = [
            {
                "path": str(path),
                "parent_identity": parent_identity,
                "before": _snapshot(path),
                "managed": False,
                "installed": None,
                "order": None,
            }
            for path, parent_identity in zip(paths, parent_identities)
        ]
        installed = _write_receipt(
            receipt,
            {
                "version": 1,
                "nonce": secrets.token_hex(16),
                "next_order": 0,
                "entries": entries,
                "created_parents": created,
            },
            exclusive=True,
            record_token_before_publish=True,
        )
    except BaseException:
        _remove_created_parents(created)
        raise
    return _receipt_token(installed)


def _verify_operation_entry(operation: dict[str, Any], current: dict[str, Any]) -> None:
    if set(operation) - {"path", "changed", "identity", "link_target", "sha256", "mode"}:
        raise RuntimeError("operation receipt entry has unknown fields")
    if not isinstance(operation.get("path"), str) or not isinstance(operation.get("changed"), bool):
        raise RuntimeError(  # noqa: TRY004 - persisted schema faults are runtime errors
            "operation receipt entry schema is invalid"
        )
    identity = operation.get("identity")
    if identity is not None and (not isinstance(identity, list) or len(identity) != 5 or any(not isinstance(value, int) for value in identity)):
        raise RuntimeError("operation receipt identity is invalid")
    if identity is None:
        if current.get("kind") != "absent":
            raise RuntimeError(f"operation result no longer absent: {operation['path']}")
        return
    if current.get("identity") != identity:
        raise RuntimeError(f"operation identity no longer present: {operation['path']}")
    if "link_target" in operation and current.get("target") != operation["link_target"]:
        raise RuntimeError(f"operation symlink target changed: {operation['path']}")
    if "sha256" in operation and current.get("sha256") != operation["sha256"]:
        raise RuntimeError(f"operation file content changed: {operation['path']}")
    if "mode" in operation and current.get("mode") != operation["mode"]:
        raise RuntimeError(f"operation file mode changed: {operation['path']}")


def _unlink_exact_receipt(
    directory_fd: int,
    name: str,
    path: Path,
    expected: dict[str, Any],
    label: str,
) -> None:
    quarantine = _random_name(f"{name}.quarantine")
    try:
        _rename_durable(
            name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
        )
    except BaseException as error:
        moved_after_error = _snapshot_at(directory_fd, quarantine, path)
        canonical_after_error = _snapshot_at(directory_fd, name, path)
        if _equivalent(moved_after_error, expected):
            if canonical_after_error.get("kind") != "absent":
                raise RuntimeError(
                    f"{label} retirement lost canonical authority; owned inode retained"
                ) from error
            try:
                _rename_exclusive(directory_fd, quarantine, name)
            except OSError as restore_error:
                raise RuntimeError(
                    f"{label} retirement failed after rename; owned inode retained"
                ) from restore_error
        raise
    moved = _snapshot_at(directory_fd, quarantine, path)
    if not _equivalent(moved, expected):
        try:
            _rename_exclusive(directory_fd, quarantine, name)
        except FileExistsError as error:
            raise RuntimeError(
                f"{label} changed during removal; replacement retained in quarantine"
            ) from error
        raise RuntimeError(f"{label} changed during removal")
    retired = _random_name(f"{name}.retired")
    _rename_exclusive(directory_fd, quarantine, retired)
    retired_snapshot = _snapshot_at(directory_fd, retired, path)
    if not _equivalent(retired_snapshot, expected):
        try:
            _rename_exclusive(directory_fd, retired, quarantine)
        except OSError as error:
            raise RuntimeError(
                f"{label} changed during retirement; replacement retained"
            ) from error
        raise RuntimeError(f"{label} changed during retirement")
    _unlink_durable(retired, dir_fd=directory_fd)


def _discard_operation_receipt(path: Path, expected: dict[str, Any]) -> None:
    fd, name = _open_parent(path)
    try:
        if _equivalent(_snapshot_at(fd, name, path), expected):
            _unlink_exact_receipt(
                fd, name, path, expected, "operation receipt"
            )
    finally:
        _close_noexcept(fd)


def _discard_operation_receipt_noexcept(
    path: Path, expected: dict[str, Any]
) -> None:
    try:
        _discard_operation_receipt(path, expected)
    except (OSError, RuntimeError):
        pass


def checkpoint(
    receipt: Path, operation_receipt: Path, expected_token: str
) -> str:
    receipt_snapshot = _require_receipt_snapshot(receipt, expected_token)
    payload = _read_receipt(receipt, receipt_snapshot)
    if not _equivalent(_snapshot(receipt), receipt_snapshot):
        raise RuntimeError("transaction receipt changed during checkpoint read")
    operation_snapshot = _snapshot(operation_receipt)
    if operation_snapshot["kind"] != "file" or operation_snapshot["mode"] != 0o600:
        raise RuntimeError("operation receipt must be a private regular file")
    operations = _read_operation_receipt(operation_receipt, operation_snapshot)
    by_path = {entry["path"]: entry for entry in payload["entries"]}
    if any(not isinstance(operation, dict) for operation in operations):
        raise RuntimeError("operation receipt entries must be objects")
    paths = [operation.get("path") for operation in operations]
    if any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths) or any(path not in by_path for path in paths):
        raise RuntimeError("operation receipt paths must be unique and snapshotted")
    validated = []
    for operation in operations:
        entry = by_path[operation["path"]]
        _verify_parent(entry)
        current = _snapshot(Path(entry["path"]))
        if entry.get("managed") and not operation["changed"] and not _equivalent(current, entry["installed"]):
            raise RuntimeError(f"transaction path changed after prior checkpoint: {operation['path']}")
        _verify_operation_entry(operation, current)
        prior = entry["installed"] if entry.get("managed") else entry["before"]
        validated.append((entry, operation, current, prior))
    for entry, operation, current, _prior in validated:
        if operation["changed"]:
            entry["managed"] = True
            entry["installed"] = current
            entry["order"] = payload["next_order"]
            payload["next_order"] += 1
    try:
        installed = _write_receipt(
            receipt,
            payload,
            expected=receipt_snapshot,
            record_token_before_publish=True,
        )
    except BaseException as error:
        try:
            persisted = (
                not isinstance(error, _ReceiptPublishConflict)
                and _read_receipt(receipt) == payload
            )
        except Exception:  # noqa: BLE001 - any validation failure means not persisted
            persisted = False
        if not persisted:
            recovery_name = getattr(error, "recovery_name", None)
            recovery_snapshot = getattr(error, "recovery_snapshot", None)
            _rollback_payload(
                receipt,
                payload,
                receipt_snapshot,
                remove_receipt=recovery_name is None,
            )
            if recovery_name is not None and recovery_snapshot is not None:
                recovery_fd, _receipt_name = _open_parent(receipt)
                try:
                    _unlink_exact_receipt(
                        recovery_fd,
                        recovery_name,
                        receipt,
                        recovery_snapshot,
                        "retained recovery receipt",
                    )
                finally:
                    os.close(recovery_fd)
        else:
            _discard_operation_receipt_noexcept(
                operation_receipt, operation_snapshot
            )
            return _current_ledger_authority_token(receipt)
        raise
    _discard_operation_receipt_noexcept(operation_receipt, operation_snapshot)
    return _receipt_token(installed)


def replace_file(
    receipt: Path,
    source: Path,
    target: Path,
    operation_receipt: Path,
    absent_mode: int | None = None,
    target_mode: int | None = None,
) -> None:
    payload = _read_receipt(receipt)
    matches = [entry for entry in payload["entries"] if entry["path"] == str(target)]
    if len(matches) != 1 or matches[0].get("managed"):
        raise RuntimeError("replace-file target must be one unmanaged snapshotted path")
    entry = matches[0]
    baseline = entry["before"]
    target_fd, target_name = _open_parent(target)
    temp: str | None = None
    quarantine: str | None = None
    installed: dict[str, Any] | None = None
    installed_identity: list[int] | None = None
    published_identity: list[int] | None = None
    try:
        if _directory_identity(os.fstat(target_fd)) != entry["parent_identity"]:
            raise RuntimeError(f"transaction parent changed: {target.parent}")
        current = _snapshot_at(target_fd, target_name, target)
        if not _equivalent(current, baseline):
            raise RuntimeError(f"replace-file target changed after transaction begin: {target}")
        candidate = _snapshot(source)
        if candidate["kind"] != "file":
            raise RuntimeError("replace-file source must be a regular non-symlink file")
        if (
            baseline["kind"] == "file"
            and baseline["sha256"] == candidate["sha256"]
            and (target_mode is None or int(baseline["mode"]) == target_mode)
        ):
            _write_receipt(operation_receipt, {"entries": [{"path": str(target), "changed": False, "identity": baseline["identity"], "sha256": baseline["sha256"], "mode": baseline["mode"]}]}, exclusive=True)
            return
        data = base64.b64decode(candidate["data"], validate=True)
        mode = int(
            target_mode
            if target_mode is not None
            else baseline["mode"]
            if baseline["kind"] == "file"
            else candidate["mode"]
            if absent_mode is None
            else absent_mode
        )
        temp, installed_identity = _create_temp(target_fd, f"{target_name}.install", data, mode)
        _apply_snapshot_metadata_at(
            target_fd,
            temp,
            baseline if baseline["kind"] == "file" else candidate,
            installed_identity,
            private_mode=True,
        )
        installed_identity = _identity(
            os.stat(temp, dir_fd=target_fd, follow_symlinks=False)
        )
        installed = _snapshot_at(target_fd, temp, target)
        _verify_bound_parent(target, target_fd)
        if not _equivalent(_snapshot_at(target_fd, target_name, target), baseline):
            raise RuntimeError(f"replace-file target changed before commit: {target}")
        if baseline["kind"] != "absent":
            quarantine = _random_name(f"{target_name}.original")
            _rename_durable(
                target_name, quarantine, src_dir_fd=target_fd, dst_dir_fd=target_fd
            )
            if baseline["kind"] == "file":
                _chmod_expected_at(target_fd, quarantine, baseline["identity"], 0o600)
                if not _private_quarantine_matches(
                    _snapshot_at(target_fd, quarantine, target), baseline
                ):
                    raise RuntimeError(f"replace-file target changed during commit: {target}")
            elif not _equivalent(
                _snapshot_at(target_fd, quarantine, target), baseline
            ):
                raise RuntimeError(f"replace-file target changed during commit: {target}")
        _publish_temp_exclusive(target_fd, temp, target_name, installed)
        temp = None
        installed = None
        published_identity = installed_identity
        final_snapshot = baseline if baseline["kind"] == "file" else candidate
        _apply_snapshot_metadata_at(
            target_fd, target_name, final_snapshot, installed_identity
        )
        if target_mode is not None or (
            baseline["kind"] != "file" and absent_mode is not None
        ):
            _chmod_expected_at(target_fd, target_name, installed_identity, mode)
        observed = _snapshot_at(target_fd, target_name, target)
        if observed.get("identity") != installed_identity or observed.get("sha256") != candidate["sha256"]:
            raise RuntimeError(f"replace-file target changed immediately after commit: {target}")
        # 위의 검증 읽기는 atime을 앞으로 진행시킬 수 있다. 여전히 identity에
        # 결속되어 있는, 설치된 inode를 통해 완전한 최종 메타데이터를 다시
        # 적용하고, 권한을 반환하기 전에 그 내용을 다시 읽지 않는다.
        _apply_snapshot_metadata_at(
            target_fd, target_name, final_snapshot, installed_identity
        )
        if target_mode is not None or (
            baseline["kind"] != "file" and absent_mode is not None
        ):
            _chmod_expected_at(target_fd, target_name, installed_identity, mode)
        _verify_bound_parent(target, target_fd)
        _write_receipt(operation_receipt, {"entries": [{"path": str(target), "changed": True, "identity": installed_identity, "sha256": candidate["sha256"], "mode": mode}]}, exclusive=True)
        if quarantine is not None:
            quarantine_snapshot = _snapshot_at(target_fd, quarantine, target)
            _unlink_exact_receipt(
                target_fd,
                quarantine,
                target,
                quarantine_snapshot,
                "replaced-file quarantine",
            )
            quarantine = None
    except BaseException:
        current = _snapshot_at(target_fd, target_name, target)
        if (
            published_identity is None
            and installed is not None
            and installed_identity is not None
            and current.get("identity") == installed_identity
        ):
            # 상위 디렉터리 fsync(또는 그 이후 검증)가 실패하기 전에 link(2)가
            # operation이 소유한 inode를 이미 커밋했을 수 있다.
            # 그 커밋된 배치 상태를 생산자 identity로부터 복구하여, 보상 처리가
            # 우리의 게시물만 제거하고 보관해 둔 원본을 정확히 그대로
            # 복원하도록 한다.
            published_identity = installed_identity
        owns_publication = (
            published_identity is not None
            and current.get("identity") == published_identity
        )
        if owns_publication:
            failed = _random_name(f"{target_name}.failed-replace")
            _rename_durable(
                target_name, failed, src_dir_fd=target_fd, dst_dir_fd=target_fd
            )
            if (
                _snapshot_at(target_fd, failed, target).get("identity")
                == published_identity
            ):
                failed_snapshot = _snapshot_at(target_fd, failed, target)
                _unlink_exact_receipt(
                    target_fd,
                    failed,
                    target,
                    failed_snapshot,
                    "failed replacement",
                )
            else:
                try:
                    failed_snapshot = _snapshot_at(target_fd, failed, target)
                    _publish_temp_exclusive(
                        target_fd, failed, target_name, failed_snapshot
                    )
                except FileExistsError:
                    pass
        if quarantine is not None and _snapshot_at(target_fd, target_name, target)["kind"] == "absent":
            quarantined = _snapshot_at(target_fd, quarantine, target)
            owned = (
                _equivalent(quarantined, baseline)
                or _private_quarantine_matches(quarantined, baseline)
                if baseline["kind"] == "file"
                else _equivalent(quarantined, baseline)
            )
            if owned:
                if baseline["kind"] == "file":
                    _apply_snapshot_metadata_at(
                        target_fd, quarantine, baseline, baseline["identity"]
                    )
                _publish_temp_exclusive(
                    target_fd, quarantine, target_name, baseline
                )
                if baseline["kind"] == "file":
                    _apply_snapshot_metadata_at(
                        target_fd, target_name, baseline, baseline["identity"]
                    )
                quarantine = None
        raise
    finally:
        if temp is not None:
            _remove_at(target_fd, temp, target, installed)
        _close_noexcept(target_fd)


def remove_file(receipt: Path, target: Path, operation_receipt: Path) -> None:
    payload = _read_receipt(receipt)
    matches = [entry for entry in payload["entries"] if entry["path"] == str(target)]
    if len(matches) != 1 or matches[0].get("managed"):
        raise RuntimeError("remove-file target must be one unmanaged snapshotted path")
    entry = matches[0]
    baseline = entry["before"]
    directory_fd, name = _open_parent(target)
    quarantine: str | None = None
    try:
        if _directory_identity(os.fstat(directory_fd)) != entry["parent_identity"]:
            raise RuntimeError(f"transaction parent changed: {target.parent}")
        if not _equivalent(_snapshot_at(directory_fd, name, target), baseline):
            raise RuntimeError(f"remove-file target changed after transaction begin: {target}")
        if baseline["kind"] == "absent":
            _write_receipt(operation_receipt, {"entries": [{"path": str(target), "changed": False, "identity": None}]}, exclusive=True)
            return
        _verify_bound_parent(target, directory_fd)
        quarantine = _random_name(f"{name}.remove")
        _rename_durable(
            name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
        )
        if baseline["kind"] == "file":
            _chmod_expected_at(directory_fd, quarantine, baseline["identity"], 0o600)
            if not _private_quarantine_matches(
                _snapshot_at(directory_fd, quarantine, target), baseline
            ):
                raise RuntimeError(f"remove-file target changed during removal: {target}")
        elif not _equivalent(
            _snapshot_at(directory_fd, quarantine, target), baseline
        ):
            raise RuntimeError(f"remove-file target changed during removal: {target}")
        _verify_bound_parent(target, directory_fd)
        _write_receipt(operation_receipt, {"entries": [{"path": str(target), "changed": True, "identity": None}]}, exclusive=True)
        quarantine_snapshot = _snapshot_at(directory_fd, quarantine, target)
        _unlink_exact_receipt(
            directory_fd,
            quarantine,
            target,
            quarantine_snapshot,
            "removed-file quarantine",
        )
        quarantine = None
    except BaseException:
        if quarantine is not None and _snapshot_at(directory_fd, name, target)["kind"] == "absent":
            quarantined = _snapshot_at(directory_fd, quarantine, target)
            owned = (
                _equivalent(quarantined, baseline)
                or _private_quarantine_matches(quarantined, baseline)
                if baseline["kind"] == "file"
                else _equivalent(quarantined, baseline)
            )
            if owned:
                if baseline["kind"] == "file":
                    _apply_snapshot_metadata_at(
                        directory_fd, quarantine, baseline, baseline["identity"]
                    )
                _publish_temp_exclusive(
                    directory_fd, quarantine, name, baseline
                )
                if baseline["kind"] == "file":
                    _apply_snapshot_metadata_at(
                        directory_fd, name, baseline, baseline["identity"]
                    )
                quarantine = None
        raise
    finally:
        _close_noexcept(directory_fd)


def export_before(receipt: Path, target: Path, destination: Path) -> None:
    payload = _read_receipt(receipt)
    matches = [entry for entry in payload["entries"] if entry["path"] == str(target)]
    if len(matches) != 1:
        raise RuntimeError("export-before target must be one snapshotted path")
    before = matches[0]["before"]
    if before["kind"] == "absent":
        return
    if before["kind"] != "file":
        raise RuntimeError("export-before target must be absent or a regular file")
    data = base64.b64decode(before["data"], validate=True)
    if hashlib.sha256(data).hexdigest() != before["sha256"]:
        raise RuntimeError("export-before snapshot checksum mismatch")
    fd, name = _open_parent(destination)
    try:
        out = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        try:
            _write_all(out, data)
            os.fsync(out)
        finally:
            os.close(out)
        os.fsync(fd)
    finally:
        os.close(fd)


def before_kind(receipt: Path, target: Path) -> str:
    payload = _read_receipt(receipt)
    matches = [entry for entry in payload["entries"] if entry["path"] == str(target)]
    if len(matches) != 1:
        raise RuntimeError("before-kind target must be one snapshotted path")
    kind = matches[0]["before"].get("kind")
    if kind not in {"absent", "file", "symlink"}:
        raise RuntimeError("before-kind snapshot has an unsupported path kind")
    return kind


def _rollback_payload(
    receipt: Path,
    payload: dict[str, Any],
    receipt_expected: dict[str, Any],
    *,
    remove_receipt: bool = True,
) -> None:
    managed = sorted((entry for entry in payload["entries"] if entry.get("managed")), key=lambda entry: entry["order"], reverse=True)
    bound: list[tuple[dict[str, Any], Path, int, str]] = []
    created_bound: list[tuple[dict[str, Any], Path, int, str, int]] = []
    try:
        allowed: dict[str, set[str]] = {
            entry["path"]: set() for entry in payload.get("created_parents", [])
        }
        for entry in managed:
            physical = _canonicalize_trusted_root_alias(Path(entry["path"]))
            if str(physical.parent) in allowed:
                allowed[str(physical.parent)].add(physical.name)
        for child in payload.get("created_parents", []):
            child_path = Path(child["path"])
            if str(child_path.parent) in allowed:
                allowed[str(child_path.parent)].add(child_path.name)
        for entry in payload.get("created_parents", []):
            path = Path(entry["path"])
            parent_fd, name = _open_parent(path)
            try:
                directory_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
            except BaseException:
                os.close(parent_fd)
                raise
            if _created_directory_identity(os.fstat(directory_fd)) != entry["identity"]:
                os.close(directory_fd)
                os.close(parent_fd)
                raise RuntimeError(f"transaction-created parent changed: {path}")
            unexpected = set(os.listdir(directory_fd)) - allowed[str(path)]
            if unexpected:
                os.close(directory_fd)
                os.close(parent_fd)
                raise RuntimeError(f"transaction-created parent has foreign contents: {path}")
            created_bound.append((entry, path, parent_fd, name, directory_fd))
        for entry in managed:
            path = Path(entry["path"])
            fd, name = _open_parent(path)
            if _directory_identity(os.fstat(fd)) != entry["parent_identity"]:
                os.close(fd)
                raise RuntimeError(f"transaction parent changed: {path.parent}")
            if not _equivalent(_snapshot_at(fd, name, path), entry["installed"]):
                os.close(fd)
                raise RuntimeError(f"path changed after transaction checkpoint: {path}")
            bound.append((entry, path, fd, name))
        for entry, path, fd, name in bound:
            _restore_bound(path, fd, name, entry["before"], entry["installed"])
        for _entry, path, parent_fd, name, directory_fd in reversed(created_bound):
            if os.listdir(directory_fd):
                raise RuntimeError(f"transaction-created parent is not empty after rollback: {path}")
            retired = _random_name(f"{name}.retired")
            _rename_exclusive(parent_fd, name, retired)
            current = os.stat(retired, dir_fd=parent_fd, follow_symlinks=False)
            if _created_directory_identity(current) != _created_directory_identity(os.fstat(directory_fd)):
                _rename_exclusive(parent_fd, retired, name)
                raise RuntimeError(f"transaction-created parent changed before removal: {path}")
            _rmdir_durable(retired, dir_fd=parent_fd)
    finally:
        for _entry, _path, fd, _name in bound:
            os.close(fd)
        for _entry, _path, parent_fd, _name, directory_fd in created_bound:
            os.close(directory_fd)
            os.close(parent_fd)
    if not remove_receipt:
        return
    receipt_fd, receipt_name = _open_parent(receipt)
    try:
        if not _equivalent(
            _snapshot_at(receipt_fd, receipt_name, receipt), receipt_expected
        ):
            raise RuntimeError("transaction receipt changed before rollback completion")
        _unlink_exact_receipt(
            receipt_fd,
            receipt_name,
            receipt,
            receipt_expected,
            "transaction receipt",
        )
    finally:
        os.close(receipt_fd)


def rollback(receipt: Path, expected_token: str) -> None:
    receipt_snapshot = _require_receipt_snapshot(receipt, expected_token)
    payload = _read_receipt(receipt, receipt_snapshot)
    if not _equivalent(_snapshot(receipt), receipt_snapshot):
        raise RuntimeError("transaction receipt changed during rollback read")
    _rollback_payload(receipt, payload, receipt_snapshot)


def _ledger_authority_token(receipt: Path, ledger_fd: int) -> str:
    for token in reversed(_authority_tokens(ledger_fd)):
        try:
            _require_receipt_snapshot(receipt, token)
        except RuntimeError:
            continue
        return token
    raise RuntimeError("no transaction authority token matches the current receipt")


def rollback_from_ledger(receipt: Path, ledger_fd: int) -> None:
    rollback(receipt, _ledger_authority_token(receipt, ledger_fd))


def rollback_operation_from_ledger(
    receipt: Path, operation_receipt: Path, ledger_fd: int
) -> None:
    """트랜잭션 manifest가 한 번도 checkpoint하지 않은 operation 하나를 되돌린다.

    stage receipt는 manifest receipt가 아니다. 스냅샷된 entries도, 생성된
    parents도, 자체 ledger 토큰도 지니지 않으므로 결코 스스로의 rollback 권한이
    될 수 없다. 따라서 복구는 :func:`checkpoint`가 하는 것과 정확히 같은 방식으로
    - ledger가 여전히 승인하는 manifest에 대해 - 이를 검증하고, 변경된 각 경로를
    manifest baseline으로부터 복원한다. manifest는 호출자가 이후에 롤백할 수
    있도록 손대지 않고 남겨 두므로, 중단된 operation과 그 이전에 checkpoint된
    stage들이 하나의 순서로 되감긴다.
    """
    expected_token = _ledger_authority_token(receipt, ledger_fd)
    receipt_snapshot = _require_receipt_snapshot(receipt, expected_token)
    payload = _read_receipt(receipt, receipt_snapshot)
    if not _equivalent(_snapshot(receipt), receipt_snapshot):
        raise RuntimeError("transaction receipt changed during operation recovery")
    operation_snapshot = _snapshot(operation_receipt)
    if operation_snapshot["kind"] != "file" or operation_snapshot["mode"] != 0o600:
        raise RuntimeError("operation receipt must be a private regular file")
    operations = _read_operation_receipt(operation_receipt, operation_snapshot)
    by_path = {entry["path"]: entry for entry in payload["entries"]}
    if any(not isinstance(operation, dict) for operation in operations):
        raise RuntimeError("operation receipt entries must be objects")
    paths = [operation.get("path") for operation in operations]
    if (
        any(not isinstance(path, str) for path in paths)
        or len(set(paths)) != len(paths)
        or any(path not in by_path for path in paths)
    ):
        raise RuntimeError("operation receipt paths must be unique and snapshotted")
    restorable: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for operation in operations:
        entry = by_path[operation["path"]]
        _verify_parent(entry)
        current = _snapshot(Path(entry["path"]))
        _verify_operation_entry(operation, current)
        if entry.get("managed"):
            # checkpoint가 이미 이 배치 상태를 채택했으므로 manifest가 이를
            # 소유하며 manifest 롤백이 이를 복원한다. 여기서 stage receipt는
            # 잔재일 뿐이고, checkpoint된 설치 상태와 어긋나는 receipt는
            # 우리가 처리할 대상이 아니다.
            if not _equivalent(current, entry["installed"]):
                raise RuntimeError(
                    "operation receipt disagrees with the checkpointed installation: "
                    f"{operation['path']}"
                )
            continue
        if operation["changed"]:
            restorable.append((entry, current))
    for entry, current in reversed(restorable):
        _restore(Path(entry["path"]), entry["before"], current)
    _discard_operation_receipt(operation_receipt, operation_snapshot)


def _remove_receipt(path: Path, expected_token: str) -> None:
    expected = _require_receipt_snapshot(path, expected_token)
    _read_receipt(path, expected)
    fd, name = _open_parent(path)
    try:
        if not _equivalent(_snapshot_at(fd, name, path), expected):
            raise RuntimeError("transaction receipt changed before commit")
        _unlink_exact_receipt(fd, name, path, expected, "transaction receipt")
    finally:
        os.close(fd)


def _clear_private_tree(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
            try:
                if _directory_identity(os.fstat(child_fd)) != _directory_identity(info):
                    raise RuntimeError(f"private transaction child changed while opening: {name}")
                _clear_private_tree(child_fd)
                if os.listdir(child_fd):
                    raise RuntimeError(f"private transaction child is not empty: {name}")
                retired = _random_name(f"{name}.retired")
                _rename_exclusive(directory_fd, name, retired)
                current = os.stat(retired, dir_fd=directory_fd, follow_symlinks=False)
                if _directory_identity(current) != _directory_identity(os.fstat(child_fd)):
                    _rename_exclusive(directory_fd, retired, name)
                    raise RuntimeError(f"private transaction child changed before removal: {name}")
            finally:
                os.close(child_fd)
            _rmdir_durable(retired, dir_fd=directory_fd)
        else:
            retired = _random_name(f"{name}.retired")
            _rename_exclusive(directory_fd, name, retired)
            current = os.stat(retired, dir_fd=directory_fd, follow_symlinks=False)
            if _identity(current) != _identity(info):
                _rename_exclusive(directory_fd, retired, name)
                raise RuntimeError(f"private transaction leaf changed before removal: {name}")
            _unlink_durable(retired, dir_fd=directory_fd)


def cleanup_private_dir(path: Path, inherited_fd: int) -> None:
    retained_identity = _created_directory_identity(os.fstat(inherited_fd))
    _clear_private_tree(inherited_fd)
    parent_fd, name = _open_parent(path)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _created_directory_identity(current) != retained_identity:
            raise RuntimeError(f"private transaction directory changed: {path}")
        if os.listdir(inherited_fd):
            raise RuntimeError(f"private transaction directory is not empty: {path}")
        retired = _random_name(f"{name}.retired")
        _rename_exclusive(parent_fd, name, retired)
        moved = os.stat(retired, dir_fd=parent_fd, follow_symlinks=False)
        if _created_directory_identity(moved) != retained_identity:
            _rename_exclusive(parent_fd, retired, name)
            raise RuntimeError(f"private transaction directory changed: {path}")
        _rmdir_durable(retired, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("begin", "checkpoint", "before-kind", "export-before", "replace-file", "remove-file", "rollback", "rollback-ledger", "commit", "cleanup-private-dir"))
    parser.add_argument("receipt", type=Path)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--absent-mode", type=lambda value: int(value, 8))
    parser.add_argument("--target-mode", type=lambda value: int(value, 8))
    args = parser.parse_args()
    if not args.receipt.is_absolute():
        raise SystemExit("transaction receipt must be an absolute path")
    if args.action == "cleanup-private-dir":
        if len(args.paths) != 1:
            raise SystemExit("cleanup-private-dir requires one inherited directory fd")
        cleanup_private_dir(args.receipt, int(str(args.paths[0])))
    elif args.action == "begin":
        print(begin(args.receipt, args.paths))
    elif args.action == "checkpoint":
        if len(args.paths) != 2:
            raise SystemExit("checkpoint requires OPERATION_RECEIPT RECEIPT_TOKEN")
        print(checkpoint(args.receipt, args.paths[0], str(args.paths[1])))
    elif args.action == "replace-file":
        if len(args.paths) != 3:
            raise SystemExit("replace-file requires SOURCE TARGET OPERATION_RECEIPT")
        if args.absent_mode is not None and not 0 <= args.absent_mode <= 0o777:
            raise SystemExit("--absent-mode must be an octal permission mode")
        if args.target_mode is not None and not 0 <= args.target_mode <= 0o777:
            raise SystemExit("--target-mode must be an octal permission mode")
        replace_file(
            args.receipt,
            args.paths[0],
            args.paths[1],
            args.paths[2],
            args.absent_mode,
            args.target_mode,
        )
    elif args.action == "remove-file":
        if len(args.paths) != 2:
            raise SystemExit("remove-file requires TARGET OPERATION_RECEIPT")
        remove_file(args.receipt, args.paths[0], args.paths[1])
    elif args.action == "export-before":
        if len(args.paths) != 2:
            raise SystemExit("export-before requires TARGET DESTINATION")
        export_before(args.receipt, args.paths[0], args.paths[1])
    elif args.action == "before-kind":
        if len(args.paths) != 1:
            raise SystemExit("before-kind requires TARGET")
        print(before_kind(args.receipt, args.paths[0]))
    elif args.action == "rollback":
        if len(args.paths) != 1:
            raise SystemExit("rollback requires RECEIPT_TOKEN")
        rollback(args.receipt, str(args.paths[0]))
    elif args.action == "rollback-ledger":
        if len(args.paths) != 1:
            raise SystemExit("rollback-ledger requires one inherited ledger fd")
        rollback_from_ledger(args.receipt, int(str(args.paths[0])))
    else:
        if len(args.paths) != 1:
            raise SystemExit("commit requires RECEIPT_TOKEN")
        _remove_receipt(args.receipt, str(args.paths[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
