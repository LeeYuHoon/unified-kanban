#!/usr/bin/env python3
"""Hermes 업데이터를 위한, 경쟁 조건에 강한 엄격 상태 마커."""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import secrets
import stat
import sys
from pathlib import Path

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_TRUSTED_ROOT_ALIASES = {"etc", "tmp", "var"}


def _swap_names(directory_fd: int, first: str, second: str) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("atomic state marker exchange requires macOS")
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
        0x00000002,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{first} <-> {second}")


def _rename_exclusive(directory_fd: int, source: str, destination: str) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("exclusive state marker rename requires macOS")
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
        raise OSError(error, os.strerror(error), f"{source} -> {destination}")


def _retire_expected(
    directory_fd: int, name: str, expected_identity: tuple[int, int]
) -> None:
    retired = f".{name}.retired.{secrets.token_hex(16)}"
    _rename_exclusive(directory_fd, name, retired)
    moved = os.stat(retired, dir_fd=directory_fd, follow_symlinks=False)
    if (moved.st_dev, moved.st_ino) != expected_identity:
        try:
            _rename_exclusive(directory_fd, retired, name)
        except OSError as error:
            raise RuntimeError(
                "state marker cleanup changed; foreign successor retained"
            ) from error
        raise RuntimeError("state marker cleanup identity changed")
    os.unlink(retired, dir_fd=directory_fd)


def _write_all(fd: int, data: bytes) -> None:
    """양수를 반환한 short write를 성공으로 받아들이지 않고, 모든 바이트를 쓰거나 실패한다."""
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("state marker write made no progress")
        written += count


def _canonicalize_trusted_root_alias(path: Path) -> Path:
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


def _open_directory(path: Path, *, create: bool = False) -> int:
    if not path.is_absolute():
        raise ValueError("state directory must be absolute")
    path = _canonicalize_trusted_root_alias(path)
    directory_fd = os.open("/", _DIR_FLAGS)
    try:
        opened_root = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened_root.st_mode):
            raise RuntimeError("filesystem root is not a directory")
        for component in path.parts[1:]:
            if component in {"", ".", ".."}:
                raise RuntimeError(f"unsafe state directory component: {path}")
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    # 업데이터 상태 능력(capability)에는 소유자 전용 순회 권한이 필요하다.
                    os.mkdir(component, 0o700, dir_fd=directory_fd)  # nosemgrep (의도된 소유자 전용 권한)
                except FileExistsError:
                    pass
                child = os.open(component, _DIR_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _open_parent(path: Path) -> tuple[int, str]:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise ValueError("state marker must name an absolute leaf")
    return _open_directory(path.parent), path.name


def _ensure_directory(path: Path) -> None:
    fd = _open_directory(path, create=True)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("state directory must be an owner-only directory")
    finally:
        os.close(fd)


def _open_append_log(path: Path) -> int:
    directory_fd = _open_directory(path.parent, create=True)
    fd = -1
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        try:
            before = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if before is None:
            fd = os.open(
                path.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
        else:
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeError("dashboard log must be a regular non-symlink file")
            fd = os.open(path.name, flags, dir_fd=directory_fd)
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError("dashboard log changed before open")
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError("dashboard log must be a regular non-symlink file")
        current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError("dashboard log changed during open")
        result = fd
        fd = -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(directory_fd)


def _exec_append_log(path: Path, command: list[str]) -> None:
    if not command:
        raise ValueError("dashboard log exec requires a command")
    log_fd = _open_append_log(path)
    try:
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
    finally:
        if log_fd not in {1, 2}:
            os.close(log_fd)
    os.execvpe(command[0], command, os.environ)


def _lock_entry(directory_fd: int, name: str) -> tuple[tuple[int, int], str] | None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        raise RuntimeError("update lock must be a symlink")
    target = os.readlink(name, dir_fd=directory_fd)
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeError("update lock changed during read")
    return (info.st_dev, info.st_ino), target


def _acquire_lock(path: Path, token: str) -> None:
    directory_fd, name = _open_parent(path)
    try:
        os.symlink(token, name, dir_fd=directory_fd)
        entry = _lock_entry(directory_fd, name)
        if entry is None or entry[1] != token:
            raise RuntimeError("update lock changed immediately after creation")
    finally:
        os.close(directory_fd)


def _read_lock(path: Path) -> str:
    directory_fd, name = _open_parent(path)
    try:
        entry = _lock_entry(directory_fd, name)
        if entry is None:
            raise FileNotFoundError(path)
        return entry[1]
    finally:
        os.close(directory_fd)


def _release_lock(path: Path, token: str) -> None:
    directory_fd, name = _open_parent(path)
    quarantine: str | None = None
    try:
        entry = _lock_entry(directory_fd, name)
        if entry is None or entry[1] != token:
            raise RuntimeError("update lock identity changed")
        identity = entry[0]
        quarantine = f".{name}.release.{secrets.token_hex(16)}"
        _rename_exclusive(directory_fd, name, quarantine)
        moved = _lock_entry(directory_fd, quarantine)
        if moved is None or moved != entry:
            if _lock_entry(directory_fd, name) is None:
                _rename_exclusive(directory_fd, quarantine, name)
                quarantine = None
            raise RuntimeError("update lock changed during release")
        _retire_expected(directory_fd, quarantine, identity)
        quarantine = None
    finally:
        os.close(directory_fd)


def _read(path: Path, kind: str) -> tuple[list[str], tuple[int, int]] | None:
    try:
        directory_fd, name = _open_parent(path)
    except FileNotFoundError:
        # 한 번도 생성된 적 없는 상태 디렉터리에는 마커가 없으며, 이는
        # 마커 파일이 존재하지 않는 것과 동일한 관찰 결과다. 이를 부재로
        # 보고해 주어야 호출자가 어떤 상태도 생성하기 전에 자신이 할 일이
        # 없다고 판단할 수 있다.
        return None
    try:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError("state marker must be a regular non-symlink file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            opened = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError("state marker changed before read")
            data = os.read(fd, 256)
            if os.read(fd, 1):
                raise RuntimeError("state marker is too large")
            after = os.fstat(fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino) or (
                opened.st_dev, opened.st_ino
            ) != (current.st_dev, current.st_ino):
                raise RuntimeError("state marker changed during read")
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)
    if data == b"cleared\n":
        return None
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("state marker is not ASCII") from exc
    lines = text.replace("\r\n", "\n").splitlines()
    expected = 1 if kind == "applied" else 2
    if len(lines) != expected or not SHA_RE.fullmatch(lines[0]):
        raise RuntimeError("state marker schema is invalid")
    if kind == "pending" and lines[1] not in {"0", "1"}:
        raise RuntimeError("pending marker dashboard flag is invalid")
    return lines, (opened.st_dev, opened.st_ino)


def _write_bytes(
    path: Path, data: bytes, *, expected_identity: tuple[int, int] | None = None
) -> tuple[int, int]:
    directory_fd, name = _open_parent(path)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    try:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise RuntimeError("state marker must be a regular non-symlink file")
        if expected_identity is not None and (
            before is None or (before.st_dev, before.st_ino) != expected_identity
        ):
            raise RuntimeError("state marker identity does not match removal receipt")
        if before is None:
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        else:
            fd = os.open(name, flags, dir_fd=directory_fd)
            opened = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError("state marker changed before write")
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError("state marker must be a regular non-symlink file")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        _write_all(fd, data)
        os.lseek(fd, 0, os.SEEK_SET)
        if os.read(fd, len(data) + 1) != data:
            raise OSError("state marker verification failed after write")
        os.fsync(fd)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino) or (
            opened.st_dev, opened.st_ino
        ) != (current.st_dev, current.st_ino):
            raise RuntimeError("state marker changed during write")
        os.fsync(directory_fd)
        return opened.st_dev, opened.st_ino
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(directory_fd)


def _write(path: Path, lines: list[str]) -> tuple[int, int]:
    data = ("\n".join(lines) + "\n").encode("ascii")
    directory_fd, name = _open_parent(path)
    temporary = f".{name}.tmp.{secrets.token_hex(16)}"
    fd = -1
    may_unlink_temporary = True
    staged_identity: tuple[int, int] | None = None
    try:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise RuntimeError("state marker must be a regular non-symlink file")

        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        _write_all(fd, data)
        os.lseek(fd, 0, os.SEEK_SET)
        if os.read(fd, len(data) + 1) != data:
            raise OSError("state marker verification failed after write")
        os.fsync(fd)
        staged = os.fstat(fd)
        staged_identity = (staged.st_dev, staged.st_ino)

        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        before_identity = None if before is None else (before.st_dev, before.st_ino)
        current_identity = None if current is None else (current.st_dev, current.st_ino)
        if current_identity != before_identity:
            raise RuntimeError("state marker changed before atomic replacement")

        if before is None:
            os.link(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            _retire_expected(directory_fd, temporary, staged_identity)
            may_unlink_temporary = False
        else:
            assert before_identity is not None
            _swap_names(directory_fd, temporary, name)
            may_unlink_temporary = False
            displaced = os.stat(
                temporary, dir_fd=directory_fd, follow_symlinks=False
            )
            if (displaced.st_dev, displaced.st_ino) != before_identity:
                _swap_names(directory_fd, temporary, name)
                may_unlink_temporary = True
                raise RuntimeError(
                    "state marker changed during atomic replacement; foreign successor preserved"
                )
            may_unlink_temporary = True
            _retire_expected(directory_fd, temporary, before_identity)
            may_unlink_temporary = False
        installed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (installed.st_dev, installed.st_ino) != (staged.st_dev, staged.st_ino):
            raise RuntimeError("state marker changed during atomic replacement")
        os.fsync(directory_fd)
        return installed.st_dev, installed.st_ino
    finally:
        if fd >= 0:
            os.close(fd)
        if may_unlink_temporary:
            try:
                if staged_identity is not None:
                    _retire_expected(directory_fd, temporary, staged_identity)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _remove(path: Path, expected_identity: tuple[int, int]) -> None:
    # 툼스톤(tombstone)은 unlink/rename 경로명 경쟁을 피한다. 읽는 쪽은 이를
    # 부재로 취급하며, 동시에 바꿔치기된 외부 inode는 결코 제거하지 않는다.
    _write_bytes(path, b"cleared\n", expected_identity=expected_identity)


def _parse_identity(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2 or any(not part.isdecimal() for part in parts):
        raise SystemExit("invalid state marker identity receipt")
    return int(parts[0]), int(parts[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "read",
            "read-receipt",
            "write",
            "remove",
            "ensure-dir",
            "exec-append-log",
            "lock-acquire",
            "lock-read",
            "lock-release",
        ),
    )
    parser.add_argument("kind")
    parser.add_argument("path", type=Path)
    parser.add_argument("values", nargs="*")
    args = parser.parse_args()
    if args.action == "exec-append-log":
        if args.kind != "log" or not args.values:
            raise SystemExit(
                "exec-append-log requires: log PATH COMMAND [ARG ...]"
            )
        _exec_append_log(args.path, args.values)
        raise AssertionError("exec returned unexpectedly")
    if args.action == "ensure-dir":
        if args.kind != "directory" or args.values:
            raise SystemExit("ensure-dir requires: directory PATH")
        _ensure_directory(args.path)
        return 0
    if args.action == "lock-read":
        if args.kind != "lock" or args.values:
            raise SystemExit("lock-read requires: lock PATH")
        print(_read_lock(args.path))
        return 0
    if args.action in {"lock-acquire", "lock-release"}:
        if args.kind != "lock" or len(args.values) != 1:
            raise SystemExit(f"{args.action} requires: lock PATH TOKEN")
        if args.action == "lock-acquire":
            _acquire_lock(args.path, args.values[0])
        else:
            _release_lock(args.path, args.values[0])
        return 0
    if args.kind not in {"applied", "pending"}:
        raise SystemExit("state marker kind must be applied or pending")
    if args.action in {"read", "read-receipt"}:
        result = _read(args.path, args.kind)
        if result is None:
            return 3
        lines, identity = result
        if args.action == "read-receipt":
            print(f"{identity[0]}:{identity[1]}")
        print("\n".join(lines))
    elif args.action == "write":
        expected = 1 if args.kind == "applied" else 2
        if len(args.values) != expected or not SHA_RE.fullmatch(args.values[0]):
            raise SystemExit("invalid state marker values")
        if args.kind == "pending" and args.values[1] not in {"0", "1"}:
            raise SystemExit("invalid pending dashboard flag")
        identity = _write(args.path, args.values)
        print(f"{identity[0]}:{identity[1]}")
    else:
        if len(args.values) != 1:
            raise SystemExit("remove requires one state marker identity receipt")
        _remove(args.path, _parse_identity(args.values[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
