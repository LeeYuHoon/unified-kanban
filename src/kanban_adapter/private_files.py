"""Directory-FD based private file capabilities used by hook state machines."""

from __future__ import annotations

import ctypes
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

Identity = tuple[int, int]
_RENAME_SWAP = 0x00000002
_RENAME_EXCL = 0x00000004


def _identity(info: os.stat_result) -> Identity:
    return info.st_dev, info.st_ino


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def open_directory(path: Path, *, create: bool = False, mode: int = 0o700) -> int:
    """Open an absolute directory one non-symlink component at a time."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("private directory must be absolute")
    parts = path.parts
    # Darwin exposes these stable system aliases as root-level symlinks. Map
    # them to their canonical namespace before traversal rather than following
    # a symlink component with openat(). User-controlled symlinks remain fatal.
    if sys.platform == "darwin" and len(parts) > 1 and parts[1] in {"var", "tmp", "etc"}:
        parts = (os.sep, "private", *parts[1:])
    fd = os.open(parts[0], _directory_flags())
    try:
        for component in parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError("private directory has an unsafe component")
            try:
                child = os.open(component, _directory_flags(), dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(component, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_parent(path: Path) -> int:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise ValueError("private file must name an absolute leaf")
    return open_directory(path.parent)


@dataclass
class Receipt:
    """Open ownership/CAS receipt pinning both a directory and regular inode."""

    directory_fd: int
    file_fd: int
    name: str
    _closed: bool = False

    @property
    def identity(self) -> Identity:
        return _identity(os.fstat(self.file_fd))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self.file_fd, self.directory_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> Receipt:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple) and len(other) == 2:
            return self.identity == other
        return super().__eq__(other)


class CommittedPublicationError(RuntimeError):
    """Publication installed a new inode but a post-install check failed."""

    def __init__(self, message: str, receipt: Receipt) -> None:
        super().__init__(message)
        self.receipt = receipt


class NamespaceAuthorityError(RuntimeError):
    """A public canonical name could not be restored to a proven inode."""


def _renameatx(directory_fd: int, source: str, destination: str, flags: int) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("race-resistant hook state requires macOS")
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = libc.renameatx_np
    renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameatx_np.restype = ctypes.c_int
    if renameatx_np(directory_fd, os.fsencode(source), directory_fd, os.fsencode(destination), flags) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{source} -> {destination}")


def _swap_names(directory_fd: int, first: str, second: str) -> None:
    _renameatx(directory_fd, first, second, _RENAME_SWAP)


def _rename_exclusive(directory_fd: int, source: str, destination: str) -> None:
    _renameatx(directory_fd, source, destination, _RENAME_EXCL)


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("private file write made no progress")
        written += count


def _random_name(label: str) -> str:
    safe = "".join(c for c in label if c.isalnum() or c in "_-") or "private"
    return f".{safe}.{secrets.token_hex(16)}"


def _validate_receipt(receipt: Receipt, name: str | None = None) -> os.stat_result:
    opened = os.fstat(receipt.file_fd)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise RuntimeError("private file must be a singly-linked regular file")
    entry = os.stat(name or receipt.name, dir_fd=receipt.directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(entry.st_mode)
        or entry.st_nlink != 1
        or _identity(entry) != _identity(opened)
    ):
        raise RuntimeError("private file identity or link count changed")
    return opened


def _same_directory(first_fd: int, second_fd: int) -> bool:
    return _identity(os.fstat(first_fd)) == _identity(os.fstat(second_fd))


def validate_directory(path: Path, directory_fd: int) -> None:
    """Require the retained directory to remain at its canonical pathname."""
    current = open_directory(path)
    try:
        if not _same_directory(current, directory_fd):
            raise NamespaceAuthorityError(
                "private directory no longer occupies its canonical pathname"
            )
    finally:
        os.close(current)


def _receipt(directory_fd: int, file_fd: int, name: str) -> Receipt:
    return Receipt(os.dup(directory_fd), file_fd, name)


def _retire_name(receipt: Receipt, name: str) -> None:
    """Remove exactly ``name`` through the receipt's retained directory FD."""
    _validate_receipt(receipt, name)
    retired = _random_name(f"{name}.retired")
    moved = False
    try:
        _rename_exclusive(receipt.directory_fd, name, retired)
        moved = True
        _validate_receipt(receipt, retired)
        os.unlink(retired, dir_fd=receipt.directory_fd)
        moved = False
        after = os.fstat(receipt.file_fd)
        if not stat.S_ISREG(after.st_mode) or after.st_nlink != 0:
            raise RuntimeError("private file changed during retirement")
        os.fsync(receipt.directory_fd)
    except BaseException:
        if moved:
            try:
                _rename_exclusive(receipt.directory_fd, retired, name)
            except OSError as error:
                raise RuntimeError("private file cleanup changed; foreign successor retained") from error
        raise


def retire_expected(
    path: Path,
    expected: Receipt | Identity,
    *,
    directory_fd: int | None = None,
) -> None:
    """Retire a singly-linked inode using a retained receipt and directory."""
    owned: Receipt | None = None
    if isinstance(expected, Receipt):
        receipt = expected
    else:  # Compatibility for callers outside the hook transaction.
        parent = os.dup(directory_fd) if directory_fd is not None else _open_parent(path)
        try:
            fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=parent)
            owned = Receipt(parent, fd, path.name)
            if owned.identity != expected:
                raise RuntimeError("private file cleanup identity changed")
            receipt = owned
        except BaseException:
            if owned is None:
                os.close(parent)
            else:
                owned.close()
            raise
    try:
        _retire_name(receipt, path.name)
    finally:
        receipt.close()


def detach_expected(path: Path, expected: Receipt, *, directory_fd: int) -> Receipt:
    """Move canonical state to a fresh private capability before a side effect."""
    if not _same_directory(expected.directory_fd, directory_fd):
        expected.close()
        raise NamespaceAuthorityError("private file receipt belongs to a different directory")
    _validate_receipt(expected, path.name)
    retired = _random_name(f"{path.name}.detached")
    _rename_exclusive(directory_fd, path.name, retired)
    expected.name = retired
    try:
        _validate_receipt(expected, retired)
        os.fsync(directory_fd)
    except BaseException:
        try:
            _rename_exclusive(directory_fd, retired, path.name)
            expected.name = path.name
            _validate_receipt(expected, path.name)
        except BaseException as restore_error:
            expected.close()
            raise NamespaceAuthorityError(
                "detached state could not restore canonical authority"
            ) from restore_error
        raise
    return expected


def restore_detached(path: Path, receipt: Receipt) -> Receipt:
    """Restore detached state after the external operation failed."""
    try:
        _validate_receipt(receipt, receipt.name)
        validate_directory(path.parent, receipt.directory_fd)
        old_name = receipt.name
        _rename_exclusive(receipt.directory_fd, old_name, path.name)
    except BaseException:
        receipt.close()
        raise
    receipt.name = path.name
    try:
        _validate_receipt(receipt, path.name)
        validate_directory(path.parent, receipt.directory_fd)
    except BaseException as error:
        try:
            _validate_receipt(receipt, path.name)
            _rename_exclusive(receipt.directory_fd, path.name, old_name)
            receipt.name = old_name
            _validate_receipt(receipt, old_name)
            os.fsync(receipt.directory_fd)
        except BaseException as rollback_error:
            receipt.close()
            raise NamespaceAuthorityError(
                "restored state could not recover detached authority"
            ) from rollback_error
        receipt.close()
        raise NamespaceAuthorityError(
            "restored state parent lost canonical authority"
        ) from error
    try:
        os.fsync(receipt.directory_fd)
    except BaseException as error:
        try:
            _validate_receipt(receipt, path.name)
            _rename_exclusive(receipt.directory_fd, path.name, old_name)
            receipt.name = old_name
            _validate_receipt(receipt, old_name)
            os.fsync(receipt.directory_fd)
        except BaseException as rollback_error:
            receipt.close()
            raise NamespaceAuthorityError(
                "restored state could not recover detached authority"
            ) from rollback_error
        receipt.close()
        raise NamespaceAuthorityError("restored state lost canonical authority") from error
    try:
        validate_directory(path.parent, receipt.directory_fd)
    except BaseException as error:
        try:
            _validate_receipt(receipt, path.name)
            _rename_exclusive(receipt.directory_fd, path.name, old_name)
            receipt.name = old_name
            _validate_receipt(receipt, old_name)
            os.fsync(receipt.directory_fd)
        except BaseException as rollback_error:
            receipt.close()
            raise NamespaceAuthorityError(
                "restored state could not recover detached authority"
            ) from rollback_error
        receipt.close()
        raise NamespaceAuthorityError(
            "restored state parent lost canonical authority"
        ) from error
    return receipt


def discard_detached(receipt: Receipt) -> None:
    """Delete a fresh private detached capability after external success."""
    try:
        _validate_receipt(receipt, receipt.name)
        os.unlink(receipt.name, dir_fd=receipt.directory_fd)
        if os.fstat(receipt.file_fd).st_nlink != 0:
            raise RuntimeError("detached state remained linked after discard")
        os.fsync(receipt.directory_fd)
    finally:
        receipt.close()


def create_private_text(
    directory: Path,
    content: str,
    *,
    label: str,
    directory_fd: int | None = None,
) -> tuple[Path, Receipt]:
    """Create a fresh owner-only text file relative to a retained directory."""
    parent = os.dup(directory_fd) if directory_fd is not None else open_directory(directory)
    name = _random_name(label)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    receipt: Receipt | None = None
    try:
        fd = os.open(name, flags, 0o600, dir_fd=parent)
        os.fchmod(fd, 0o600)
        _write_all(fd, content.encode("utf-8"))
        os.fsync(fd)
        receipt = Receipt(parent, fd, name)
        parent = fd = -1
        _validate_receipt(receipt)
        return directory / name, receipt
    except BaseException:
        if receipt is not None:
            try:
                _retire_name(receipt, name)
            except BaseException:
                pass
            receipt.close()
        elif fd >= 0:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=parent)
            except OSError:
                pass
        raise
    finally:
        if parent >= 0:
            os.close(parent)


def create_anonymous_text(
    directory: Path,
    content: str,
    *,
    label: str,
    directory_fd: int | None = None,
) -> Receipt:
    """Create, write, and unlink an owner-only inode retained only by its FD."""
    parent = os.dup(directory_fd) if directory_fd is not None else open_directory(directory)
    name = _random_name(label)
    flags = (
        os.O_RDWR | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    fd = -1
    receipt: Receipt | None = None
    try:
        fd = os.open(name, flags, 0o600, dir_fd=parent)
        os.fchmod(fd, 0o600)
        _write_all(fd, content.encode("utf-8"))
        os.fsync(fd)
        receipt = Receipt(parent, fd, name)
        parent = fd = -1
        _validate_receipt(receipt, name)
        os.unlink(name, dir_fd=receipt.directory_fd)
        opened = os.fstat(receipt.file_fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 0:
            raise RuntimeError("anonymous private file remained linked")
        os.lseek(receipt.file_fd, 0, os.SEEK_SET)
        os.fsync(receipt.directory_fd)
        return receipt
    except BaseException:
        if receipt is not None:
            try:
                if os.fstat(receipt.file_fd).st_nlink == 1:
                    os.unlink(name, dir_fd=receipt.directory_fd)
            except OSError:
                pass
            receipt.close()
        elif fd >= 0:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=parent)
            except OSError:
                pass
        raise
    finally:
        if parent >= 0:
            os.close(parent)


def read_bytes(path: Path, *, directory_fd: int | None = None) -> tuple[bytes, Receipt]:
    """Read a canonical singly-linked file while retaining parent and file FDs."""
    parent = os.dup(directory_fd) if directory_fd is not None else _open_parent(path)
    fd = -1
    receipt: Receipt | None = None
    try:
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=parent)
        receipt = Receipt(parent, fd, path.name)
        parent = fd = -1
        _validate_receipt(receipt)
        chunks: list[bytes] = []
        while chunk := os.read(receipt.file_fd, 64 * 1024):
            chunks.append(chunk)
        _validate_receipt(receipt)
        return b"".join(chunks), receipt
    except BaseException:
        if receipt is not None:
            receipt.close()
        elif fd >= 0:
            os.close(fd)
        if receipt is None and parent >= 0:
            os.close(parent)
        raise


def atomic_publish(
    path: Path,
    content: bytes,
    *,
    expected_identity: Receipt | Identity | None = None,
    directory_fd: int | None = None,
) -> Receipt:
    """Publish exclusively or CAS-replace a receipt-pinned canonical inode."""
    parent = os.dup(directory_fd) if directory_fd is not None else _open_parent(path)
    staged_name = _random_name(f"{path.name}.staged")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    staged: Receipt | None = None
    consumed_expected: Receipt | None = None
    owned_expected: Receipt | None = None
    committed = False
    try:
        fd = os.open(staged_name, flags, 0o600, dir_fd=parent)
        staged = Receipt(os.dup(parent), fd, staged_name)
        os.fchmod(fd, 0o600)
        _write_all(fd, content)
        os.fsync(fd)
        _validate_receipt(staged)

        if expected_identity is None:
            _rename_exclusive(parent, staged_name, path.name)
            staged.name = path.name
            committed = True
        else:
            expected = expected_identity
            if not isinstance(expected, Receipt):
                old_fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
                expected = Receipt(os.dup(parent), old_fd, path.name)
                owned_expected = expected
                if expected.identity != expected_identity:
                    expected.close()
                    raise RuntimeError("private file identity changed before replacement")
            elif not _same_directory(expected.directory_fd, parent):
                raise RuntimeError("private file receipt belongs to a different directory")
            _validate_receipt(expected)
            _swap_names(parent, staged_name, path.name)
            staged.name = path.name
            committed = True
            try:
                _validate_receipt(expected, staged_name)
            except BaseException:
                try:
                    _swap_names(parent, staged_name, path.name)
                except OSError as error:
                    # The canonical name is no longer proven to denote the
                    # staged inode.  Do not expose an invalid committed
                    # receipt, and let the identity-checked finally block
                    # preserve every foreign entry.
                    committed = False
                    expected.close()
                    raise NamespaceAuthorityError(
                        "private file replacement could not restore canonical namespace"
                    ) from error
                staged.name = staged_name
                committed = False
                try:
                    _validate_receipt(expected, path.name)
                except BaseException as verification_error:
                    expected.close()
                    raise NamespaceAuthorityError(
                        "private file replacement restored an untrusted canonical entry"
                    ) from verification_error
                raise RuntimeError("private file changed during atomic replacement; foreign successor preserved")
            consumed_expected = expected
            _retire_name(expected, staged_name)
            expected.close()
            owned_expected = None
            consumed_expected = None

        try:
            _validate_receipt(staged)
            os.fsync(parent)
        except BaseException as error:
            raise CommittedPublicationError(
                "private file was installed but durability verification failed", staged
            ) from error
        result = staged
        staged = None
        return result
    except CommittedPublicationError:
        staged = None
        raise
    except BaseException as error:
        if committed and staged is not None:
            try:
                _validate_receipt(staged)
            except BaseException as verification_error:
                committed = False
                raise RuntimeError(
                    "private file publication lost canonical namespace authority"
                ) from verification_error
            receipt = staged
            staged = None
            raise CommittedPublicationError(
                "private file was installed before publication failed", receipt
            ) from error
        raise
    finally:
        if staged is not None and not committed:
            try:
                _retire_name(staged, staged.name)
            except BaseException:
                pass
            finally:
                staged.close()
        if consumed_expected is not None:
            consumed_expected.close()
        if owned_expected is not None:
            owned_expected.close()
        os.close(parent)
