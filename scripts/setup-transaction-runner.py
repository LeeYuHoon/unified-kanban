#!/usr/bin/env python3
"""Run setup with a recoverable, single-writer filesystem transaction."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from types import ModuleType

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


def _load_transaction_module(repo_root: Path) -> ModuleType:
    path = repo_root / "scripts" / "path-transaction.py"
    spec = importlib.util.spec_from_file_location("unified_kanban_path_transaction", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load path transaction helper")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _open_transaction_directory(
    module: ModuleType, path: Path, created_parents: list[dict[str, object]]
) -> tuple[int, bool]:
    module._ensure_parent(path, created_parents)
    parent_fd, name = module._open_parent(path)
    created = False
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            os.close(directory_fd)
            raise RuntimeError("setup transaction directory must be private and owned")
        return directory_fd, created
    finally:
        os.close(parent_fd)


def _open_private_file(directory_fd: int, name: str) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise RuntimeError(f"setup transaction {name} must be a private single-link file")
    return fd


def _write_parent_receipt(directory_fd: int, created: list[dict[str, object]]) -> None:
    fd = os.open(
        "runner-parents.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        data = json.dumps(created, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("short write publishing setup parent receipt")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory_fd)


def _read_parent_receipt(directory_fd: int) -> list[dict[str, object]]:
    fd = os.open(
        "runner-parents.json",
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 or info.st_nlink != 1:
            raise RuntimeError("setup parent receipt is invalid")
        raw = os.pread(fd, info.st_size, 0)
    finally:
        os.close(fd)
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise RuntimeError("setup parent receipt schema is invalid")
    return payload


def _cleanup_empty_created_parents(module: ModuleType, created: list[dict[str, object]]) -> None:
    for entry in reversed(created):
        path = Path(str(entry["path"]))
        parent_fd, name = module._open_parent(path)
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if module._created_directory_identity(current) != entry["identity"]:
                raise RuntimeError(f"setup-created parent changed: {path}")
            child_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
            try:
                if os.listdir(child_fd):
                    break
            finally:
                os.close(child_fd)
            retired = module._random_name(f"{name}.retired")
            module._rename_exclusive(parent_fd, name, retired)
            moved = os.stat(retired, dir_fd=parent_fd, follow_symlinks=False)
            if module._created_directory_identity(moved) != entry["identity"]:
                module._rename_exclusive(parent_fd, retired, name)
                raise RuntimeError(f"setup-created parent changed during cleanup: {path}")
            module._rmdir_durable(retired, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)


def _prepare_transaction(
    module: ModuleType, path: Path
) -> tuple[int, int, int, list[dict[str, object]]]:
    created_parents: list[dict[str, object]] = []
    directory_fd, created = _open_transaction_directory(module, path, created_parents)
    lock_fd = -1
    ledger_fd = -1
    try:
        lock_fd = _open_private_file(directory_fd, "lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another setup transaction is active") from exc

        if not created:
            names = set(os.listdir(directory_fd))
            if not {"manifest.json", "token-ledger", "runner-parents.json"} <= names:
                raise RuntimeError(
                    "stale setup transaction has no recoverable manifest; preserving it"
                )
            created_parents = _read_parent_receipt(directory_fd)
            ledger_fd = _open_private_file(directory_fd, "token-ledger")
            os.environ["UNIFIED_KANBAN_TRANSACTION_DIR"] = str(path)
            os.environ["UNIFIED_KANBAN_TRANSACTION_FD"] = str(directory_fd)
            os.environ["UNIFIED_KANBAN_TOKEN_LEDGER_FD"] = str(ledger_fd)
            try:
                # A stage receipt records one operation the manifest may never
                # have checkpointed, so it is recovered against the manifest's
                # ledger authority and never treated as a manifest receipt of
                # its own. Later stages unwind first, then the manifest.
                manifest = path / "manifest.json"
                stages = sorted(
                    (
                        name
                        for name in names
                        if re.fullmatch(r"stage-[1-9][0-9]*\.json", name)
                    ),
                    key=lambda name: int(name[6:-5]),
                    reverse=True,
                )
                for stage in stages:
                    module.rollback_operation_from_ledger(
                        manifest, path / stage, ledger_fd
                    )
                module.rollback_from_ledger(manifest, ledger_fd)
            except BaseException as exc:
                raise RuntimeError(
                    f"stale setup transaction rollback failed; foreign paths were preserved: {exc}"
                ) from exc
            module.cleanup_private_dir(path, directory_fd)
            os.close(ledger_fd)
            ledger_fd = -1
            os.close(lock_fd)
            lock_fd = -1
            os.close(directory_fd)
            directory_fd, created = _open_transaction_directory(module, path, [])
            if not created:
                raise RuntimeError("recovered setup transaction directory was replaced")
            lock_fd = _open_private_file(directory_fd, "lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _write_parent_receipt(directory_fd, created_parents)
        else:
            _write_parent_receipt(directory_fd, created_parents)

        ledger_fd = _open_private_file(directory_fd, "token-ledger")
        if os.fstat(ledger_fd).st_size != 0:
            raise RuntimeError("new setup transaction ledger is not empty")
        os.fsync(directory_fd)
        return directory_fd, ledger_fd, lock_fd, created_parents
    except BaseException:
        if ledger_fd >= 0:
            os.close(ledger_fd)
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)
        raise


def main() -> int:
    if len(sys.argv) < 4:
        raise SystemExit("usage: setup-transaction-runner.py REPO_ROOT STATE_DIR SETUP [ARGS...]")
    repo_root = Path(sys.argv[1]).resolve(strict=True)
    state_dir = Path(sys.argv[2])
    setup = Path(sys.argv[3]).resolve(strict=True)
    if not state_dir.is_absolute():
        raise RuntimeError("setup state directory must be absolute")
    module = _load_transaction_module(repo_root)
    transaction_dir = state_dir / "setup-transaction"
    directory_fd, ledger_fd, lock_fd, created_parents = _prepare_transaction(
        module, transaction_dir
    )
    try:
        os.dup2(directory_fd, 9)
        os.dup2(ledger_fd, 10)
        environment = dict(os.environ)
        environment.update(
            {
                "UNIFIED_KANBAN_SETUP_TRANSACTION_CHILD": "1",
                "UNIFIED_KANBAN_TRANSACTION_DIR": str(transaction_dir),
                "UNIFIED_KANBAN_TRANSACTION_FD": "9",
                "UNIFIED_KANBAN_TOKEN_LEDGER_FD": "10",
            }
        )
        result = subprocess.run(
            ["bash", str(setup), *sys.argv[4:]],
            env=environment,
            pass_fds=(9, 10),
            check=False,
        )
        if result.returncode != 0:
            try:
                os.lstat(transaction_dir)
            except FileNotFoundError:
                if not os.listdir(directory_fd):
                    _cleanup_empty_created_parents(module, created_parents)
        return result.returncode
    finally:
        for fd in (10, 9, ledger_fd, lock_fd, directory_fd):
            try:
                os.close(fd)
            except OSError:
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"setup transaction runner failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
