#!/usr/bin/env python3
"""Atomically merge or remove repository-owned Claude and Codex hook entries."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Any

CLAUDE_EVENTS = {
    "UserPromptSubmit": "prompt",
    "PostToolUse": "post-tool-use",
    "Stop": "stop",
    "SessionEnd": "session-end",
}
# Codex CLI 0.145 exposes native PostToolUse (tool_name/tool_input/model),
# SubagentStart (agent_type) and SessionEnd hooks, so Codex records the same
# usage categories as Claude Code apart from Skills, which no Codex hook
# event names.
CODEX_EVENTS = {
    "UserPromptSubmit": "prompt",
    "PostToolUse": "post-tool-use",
    "SubagentStart": "subagent-start",
    "Stop": "stop",
    "SessionEnd": "session-end",
}


FileIdentity = tuple[int, int, int, int, int]
MANAGED_MARKER = "# unified-kanban-managed-v1"
MAX_SETTINGS_BYTES = 8 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_TRUSTED_ROOT_ALIASES = {"etc", "tmp", "var"}
_BOUND_PARENTS: dict[str, int] = {}
_RETAIN_ORIGINAL = False
_LAST_REPLACEMENT: tuple[Path, str | None, FileIdentity | None, FileIdentity] | None = None
_ORIGINAL_METADATA: dict[FileIdentity, dict[str, Any]] = {}
_METADATA_HELPER: Any | None = None


def _identity(info: os.stat_result) -> FileIdentity:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_mtime_ns,
        info.st_size,
    )


def _metadata_helper() -> Any:
    global _METADATA_HELPER
    if _METADATA_HELPER is None:
        helper_path = Path(__file__).with_name("path-transaction.py")
        spec = importlib.util.spec_from_file_location(
            "unified_kanban_path_transaction_metadata", helper_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load transaction metadata helper")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _METADATA_HELPER = module
    return _METADATA_HELPER


def _capture_metadata(fd: int, info: os.stat_result) -> dict[str, Any]:
    return {
        "mode": stat.S_IMODE(info.st_mode),
        **_metadata_helper()._file_metadata(info, fd),
    }


def _restore_metadata_expected(
    directory_fd: int,
    name: str,
    expected: FileIdentity,
) -> None:
    metadata = _ORIGINAL_METADATA.get(expected)
    if metadata is None:
        raise RuntimeError(f"missing retained metadata for {name}")
    opened = os.open(name, os.O_RDWR | _NOFOLLOW, dir_fd=directory_fd)
    try:
        if _identity(os.fstat(opened)) != expected:
            raise RuntimeError(f"file changed before metadata restore: {name}")
        _metadata_helper()._apply_file_metadata(opened, metadata)
        os.fsync(opened)
        if _identity(os.fstat(opened)) != expected:
            raise RuntimeError(f"file changed during metadata restore: {name}")
    finally:
        os.close(opened)


def _apply_retained_metadata_to_replacement(
    directory_fd: int,
    name: str,
    original: FileIdentity,
    replacement: FileIdentity,
) -> None:
    metadata = _ORIGINAL_METADATA.get(original)
    if metadata is None:
        raise RuntimeError(f"missing retained metadata for {name}")
    opened = os.open(name, os.O_RDWR | _NOFOLLOW, dir_fd=directory_fd)
    try:
        opened_info = os.fstat(opened)
        opened_inode = (
            opened_info.st_dev,
            opened_info.st_ino,
            stat.S_IFMT(opened_info.st_mode),
        )
        if opened_inode != replacement[:3]:
            raise RuntimeError(f"replacement changed before metadata apply: {name}")
        _metadata_helper()._apply_file_metadata(opened, metadata)
        os.fsync(opened)
        applied_info = os.fstat(opened)
        applied_inode = (
            applied_info.st_dev,
            applied_info.st_ino,
            stat.S_IFMT(applied_info.st_mode),
        )
        if applied_inode != replacement[:3]:
            raise RuntimeError(f"replacement changed during metadata apply: {name}")
    finally:
        os.close(opened)


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


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


def _open_parent(path: Path, *, create: bool = False) -> tuple[int, str]:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise ValueError("hook path must be an absolute leaf path")
    transaction_dir = os.environ.get("UNIFIED_KANBAN_TRANSACTION_DIR")
    transaction_fd = os.environ.get("UNIFIED_KANBAN_TRANSACTION_FD")
    if transaction_dir and transaction_fd and path.parent == Path(transaction_dir):
        return os.dup(int(transaction_fd)), path.name
    path = _canonicalize_trusted_root_alias(path)
    fd = os.open("/", _DIR_FLAGS)
    try:
        for component in path.parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError("unsafe hook parent component")
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


def _verify_parent(path: Path, fd: int) -> None:
    check, _ = _open_parent(path)
    try:
        if _directory_identity(os.fstat(check)) != _directory_identity(os.fstat(fd)):
            raise RuntimeError(f"hook settings parent changed: {path.parent}")
    finally:
        os.close(check)


def _parent_for(path: Path, *, create: bool = False) -> tuple[int, str, bool]:
    bound = _BOUND_PARENTS.get(str(path))
    if bound is not None:
        return bound, path.name, False
    fd, name = _open_parent(path, create=create)
    return fd, name, True


def _entry_identity(fd: int, name: str) -> FileIdentity | None:
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return _identity(info)


def _chmod_expected(
    directory_fd: int, name: str, expected: FileIdentity, mode: int
) -> None:
    opened = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_fd)
    try:
        if _identity(os.fstat(opened)) != expected:
            raise RuntimeError(f"file changed before chmod: {name}")
        os.fchmod(opened, mode)
    finally:
        os.close(opened)


def _random_name(prefix: str) -> str:
    return f".{prefix}.{os.urandom(16).hex()}"


def _publish_exclusive(directory_fd: int, source: str, target: str) -> None:
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


def _quarantine_expected(
    directory_fd: int, leaf: str, expected: FileIdentity, prefix: str
) -> str:
    quarantine = _random_name(prefix)
    os.rename(leaf, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    try:
        os.fsync(directory_fd)
        opened = os.open(quarantine, os.O_RDWR | _NOFOLLOW, dir_fd=directory_fd)
        try:
            if _identity(os.fstat(opened)) != expected:
                raise RuntimeError(f"{leaf} changed while being quarantined")
            _metadata_helper()._make_file_private(opened)
            os.fsync(opened)
        finally:
            os.close(opened)
    except BaseException:
        try:
            _publish_exclusive(directory_fd, quarantine, leaf)
        except FileExistsError:
            pass
        raise
    return quarantine


def read_settings(
    path: Path,
) -> tuple[dict[str, Any], bytes, int, FileIdentity | None]:
    directory_fd, name, owned = _parent_for(path)
    try:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {}, b"", 0o600, None
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ValueError("Claude settings must be a non-symlink regular file")
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_fd)
        try:
            opened = os.fstat(fd)
            if _identity(opened) != _identity(before):
                raise RuntimeError("Claude settings changed during read")
            _ORIGINAL_METADATA[_identity(opened)] = _capture_metadata(fd, opened)
            chunks = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SETTINGS_BYTES:
                    raise ValueError(
                        f"Claude settings exceed {MAX_SETTINGS_BYTES} bytes"
                    )
                chunks.append(chunk)
            raw = b"".join(chunks)
            payload = json.loads(raw)
        finally:
            os.close(fd)
        _verify_parent(path, directory_fd)
    finally:
        if owned:
            os.close(directory_fd)
    if not isinstance(payload, dict):
        raise ValueError("Claude settings must be a JSON object")
    return payload, raw, stat.S_IMODE(opened.st_mode), _identity(opened)


def validate_groups(
    settings: dict[str, Any], events: dict[str, str]
) -> dict[str, list[Any]]:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude settings hooks must be an object")
    for event in events:
        groups = hooks.get(event)
        if groups is None:
            continue
        if not isinstance(groups, list):
            raise ValueError(f"Claude settings hooks.{event} must be a list")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError(f"Claude settings hooks.{event} entry must be an object")
            commands = group.get("hooks")
            if not isinstance(commands, list):
                raise ValueError(f"Claude settings hooks.{event}.hooks must be a list")
            if any(not isinstance(item, dict) for item in commands):
                raise ValueError(f"Claude settings hooks.{event}.hooks entries must be objects")
    return hooks


def merge(
    settings: dict[str, Any], hook_bin: Path, events: dict[str, str]
) -> None:
    hooks = validate_groups(settings, events)
    for event, argument in events.items():
        command = (
            f"{shlex.quote(str(hook_bin))} {shlex.quote(argument)} {MANAGED_MARKER}"
        )
        groups = hooks.setdefault(event, [])
        normalized_groups = []
        for group in groups:
            kept = [
                hook for hook in group["hooks"]
                if not (
                    hook.get("type") == "command"
                    and hook.get("command") == command
                )
            ]
            if kept:
                updated = dict(group)
                updated["hooks"] = kept
                normalized_groups.append(updated)
        normalized_groups.append({
            "hooks": [{"type": "command", "command": command, "timeout": 30}],
        })
        hooks[event] = normalized_groups


def unmerge(
    settings: dict[str, Any], hook_bin: Path, events: dict[str, str]
) -> None:
    hooks = validate_groups(settings, events)
    for event, argument in events.items():
        groups = hooks.get(event)
        if groups is None:
            continue
        command = (
            f"{shlex.quote(str(hook_bin))} {shlex.quote(argument)} {MANAGED_MARKER}"
        )
        kept_groups = []
        for group in groups:
            kept_hooks = [
                hook
                for hook in group["hooks"]
                if not (
                    hook.get("type") == "command"
                    and hook.get("command") == command
                )
            ]
            if kept_hooks:
                updated = dict(group)
                updated["hooks"] = kept_hooks
                kept_groups.append(updated)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)


def write_bytes_atomic(
    path: Path,
    content: bytes,
    mode: int,
    expected_identity: FileIdentity | None,
) -> FileIdentity:
    global _LAST_REPLACEMENT
    directory_fd, leaf, owned = _parent_for(path, create=True)
    temp = None
    quarantine = None
    installed_identity = None
    try:
        while True:
            temp = _random_name(f"{leaf}.temp")
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=directory_fd)
                break
            except FileExistsError:
                continue
        try:
            view = memoryview(content)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("short write while writing hook settings")
                view = view[count:]
            os.fsync(fd)
            installed_identity = _identity(os.fstat(fd))
        finally:
            os.close(fd)
        os.fsync(directory_fd)
        if _entry_identity(directory_fd, leaf) != expected_identity:
            raise RuntimeError("hook settings changed before write")
        _verify_parent(path, directory_fd)
        if expected_identity is not None:
            quarantine = _quarantine_expected(
                directory_fd, leaf, expected_identity, f"{leaf}.original"
            )
            _chmod_expected(directory_fd, quarantine, expected_identity, 0o600)
        _publish_exclusive(directory_fd, temp, leaf)
        temp = None
        if _entry_identity(directory_fd, leaf) != installed_identity:
            raise RuntimeError("hook settings changed immediately after write")
        if expected_identity is not None:
            _apply_retained_metadata_to_replacement(
                directory_fd,
                leaf,
                expected_identity,
                installed_identity,
            )
        else:
            _chmod_expected(directory_fd, leaf, installed_identity, mode)
        applied_identity = _entry_identity(directory_fd, leaf)
        if applied_identity is None or applied_identity[:3] != installed_identity[:3]:
            raise RuntimeError("hook settings changed after metadata apply")
        installed_identity = applied_identity
        _verify_parent(path, directory_fd)
        if _RETAIN_ORIGINAL:
            _LAST_REPLACEMENT = (path, quarantine, expected_identity, installed_identity)
            quarantine = None
        elif quarantine is not None:
            if expected_identity is None:
                raise RuntimeError("retained hook original lost its expected identity")
            _discard_expected_name(
                directory_fd,
                quarantine,
                expected_identity,
                f"{leaf}.original-retired",
            )
            quarantine = None
        return installed_identity
    except BaseException:
        current_identity = _entry_identity(directory_fd, leaf)
        if (
            installed_identity is not None
            and current_identity is not None
            and current_identity[:3] == installed_identity[:3]
        ):
            failed = _quarantine_expected(
                directory_fd, leaf, current_identity, f"{leaf}.failed"
            )
            _discard_expected_name(
                directory_fd,
                failed,
                current_identity,
                f"{leaf}.failed-retired",
            )
        if quarantine is not None and _entry_identity(directory_fd, leaf) is None:
            if expected_identity is not None:
                _restore_metadata_expected(
                    directory_fd, quarantine, expected_identity
                )
            _publish_exclusive(directory_fd, quarantine, leaf)
            restored = _entry_identity(directory_fd, leaf)
            if restored is None:
                raise RuntimeError("restored hook settings disappeared")
            _chmod_expected(directory_fd, leaf, restored, mode)
            quarantine = None
        raise
    finally:
        if temp is not None:
            try:
                os.unlink(temp, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileNotFoundError:
                pass
        if owned:
            os.close(directory_fd)


def write_atomic(
    path: Path,
    settings: dict[str, Any],
    mode: int,
    expected_identity: FileIdentity | None,
) -> FileIdentity:
    content = (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return write_bytes_atomic(path, content, mode, expected_identity)


def _render(settings: dict[str, Any]) -> bytes:
    return (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_receipt(path: Path, entries: list[dict[str, Any]]) -> None:
    if not path.is_absolute():
        raise ValueError("receipt path must be absolute")
    encoded = (json.dumps({"entries": entries}, sort_keys=True) + "\n").encode("utf-8")
    directory_fd, leaf = _open_parent(path)
    temp = None
    try:
        temp = _random_name(f"{leaf}.receipt")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=directory_fd)
        try:
            view = memoryview(encoded)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("short write while persisting hook receipt")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory_fd)
        _verify_parent(path, directory_fd)
        _publish_exclusive(directory_fd, temp, leaf)
        temp = None
        _verify_parent(path, directory_fd)
    finally:
        if temp is not None:
            try:
                os.unlink(temp, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileNotFoundError:
                pass
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _restore_retained_original(
    replacement: tuple[Path, str | None, FileIdentity | None, FileIdentity],
) -> None:
    path, quarantine, original_identity, installed_identity = replacement
    if quarantine is None or original_identity is None:
        return
    directory_fd = _BOUND_PARENTS[str(path)]
    _verify_parent(path, directory_fd)
    if _entry_identity(directory_fd, path.name) != installed_identity:
        raise RuntimeError(f"{path} changed before retained-original rollback")
    if _entry_identity(directory_fd, quarantine) != original_identity:
        raise RuntimeError(f"retained original changed before rollback: {path}")
    installed_quarantine = _quarantine_expected(
        directory_fd, path.name, installed_identity, f"{path.name}.rollback"
    )
    try:
        _restore_metadata_expected(directory_fd, quarantine, original_identity)
        _publish_exclusive(directory_fd, quarantine, path.name)
        if _entry_identity(directory_fd, path.name) != original_identity:
            raise RuntimeError(f"retained original changed after rollback: {path}")
        _discard_expected_name(
            directory_fd,
            installed_quarantine,
            installed_identity,
            f"{path.name}.rollback-retired",
        )
    except BaseException:
        if _entry_identity(directory_fd, path.name) is None:
            try:
                _publish_exclusive(directory_fd, installed_quarantine, path.name)
            except BaseException:
                pass
        raise


def _discard_expected_name(
    directory_fd: int,
    name: str,
    expected_identity: FileIdentity,
    retired_prefix: str,
) -> None:
    retired = _quarantine_expected(
        directory_fd,
        name,
        expected_identity,
        retired_prefix,
    )
    os.unlink(retired, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _discard_retained_original(
    replacement: tuple[Path, str | None, FileIdentity | None, FileIdentity],
) -> None:
    path, quarantine, original_identity, _installed_identity = replacement
    if quarantine is None or original_identity is None:
        return
    directory_fd = _BOUND_PARENTS[str(path)]
    if _entry_identity(directory_fd, quarantine) != original_identity:
        return
    _discard_expected_name(
        directory_fd,
        quarantine,
        original_identity,
        f"{path.name}.retired",
    )


def _open_lock(path: Path) -> int:
    directory_fd, _name, owned = _parent_for(path)
    fd = os.dup(directory_fd)
    if owned:
        os.close(directory_fd)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def apply_batch(
    action: str,
    specs: list[tuple[Path, Path, dict[str, str]]],
    receipt_path: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    global _RETAIN_ORIGINAL, _LAST_REPLACEMENT
    if receipt_path is not None and action != "validate" and len(specs) != 1:
        raise ValueError("receipt-backed hook operations require exactly one settings path")
    for path, hook_bin, _events in specs:
        if not path.is_absolute() or not hook_bin.is_absolute():
            raise ValueError("hook settings and binary paths must be absolute")

    if action == "validate":
        for path, _hook, events in specs:
            try:
                settings, _raw, _mode, _identity = read_settings(path)
            except FileNotFoundError:
                settings = {}
            validate_groups(settings, events)
        result = {"entries": []}
        if receipt_path is not None:
            _write_receipt(receipt_path, [])
        return result

    parent_fds: list[int] = []
    lock_fds: list[int] = []
    written: list[
        tuple[
            Path,
            bytes,
            int,
            FileIdentity | None,
            FileIdentity,
            tuple[Path, str | None, FileIdentity | None, FileIdentity] | None,
        ]
    ] = []
    try:
        for path, _hook, _events in specs:
            fd, _name = _open_parent(
                path, create=not bool(os.environ.get("UNIFIED_KANBAN_TRANSACTION_FD"))
            )
            _BOUND_PARENTS[str(path)] = fd
            _BOUND_PARENTS[str(path.parent / ".unified-kanban-hooks.lock")] = fd
            parent_fds.append(fd)
        lock_paths = sorted({path.parent / ".unified-kanban-hooks.lock" for path, _hook, _events in specs})
        for lock_path in lock_paths:
            lock_fds.append(_open_lock(lock_path))

        records = []
        for path, hook_bin, events in specs:
            settings, original_bytes, mode, identity = read_settings(path)
            validate_groups(settings, events)
            records.append((path, hook_bin, events, settings, original_bytes, mode, identity))

        receipt_entries = []
        _RETAIN_ORIGINAL = receipt_path is not None
        for path, hook_bin, events, settings, original_bytes, mode, identity in records:
            if action == "install":
                merge(settings, hook_bin, events)
            elif action == "uninstall":
                unmerge(settings, hook_bin, events)
            else:
                raise ValueError(f"unsupported batch action: {action}")
            rendered = _render(settings)
            if rendered == original_bytes:
                receipt_entries.append({"path": str(path), "changed": False, "identity": list(identity) if identity is not None else None})
                continue
            _LAST_REPLACEMENT = None
            new_identity = write_atomic(path, settings, mode, identity)
            retained = _LAST_REPLACEMENT
            written.append((path, original_bytes, mode, identity, new_identity, retained))
            receipt_entries.append(
                {
                    "path": str(path),
                    "changed": True,
                    "identity": list(new_identity),
                    "sha256": hashlib.sha256(rendered).hexdigest(),
                }
            )
        result = {"entries": receipt_entries}
        if receipt_path is not None:
            _write_receipt(receipt_path, receipt_entries)
        for _path, _bytes, _mode, _identity, _new_identity, retained in written:
            if retained is not None:
                _discard_retained_original(retained)
        written.clear()
        return result
    except Exception as primary:
        rollback_errors = []
        for path, _original_bytes, _mode, _original_identity, new_identity, retained in written:
            fd = _BOUND_PARENTS[str(path)]
            try:
                _verify_parent(path, fd)
                if _entry_identity(fd, path.name) != new_identity:
                    raise RuntimeError(f"{path} changed before batch rollback")
                if retained is not None:
                    _retained_path, quarantine, original_identity, _installed = retained
                    if quarantine is None or original_identity is None:
                        raise RuntimeError(f"missing retained original for {path}")
                    if _entry_identity(fd, quarantine) != original_identity:
                        raise RuntimeError(
                            f"retained original changed before batch rollback: {path}"
                        )
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(
                f"batch operation failed: {primary}; rollback preflight failed: "
                + "; ".join(rollback_errors)
            ) from primary
        for path, original_bytes, mode, original_identity, new_identity, retained in reversed(written):
            if retained is not None:
                try:
                    _restore_retained_original(retained)
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
                continue
            installed_quarantine = None
            fd = _BOUND_PARENTS[str(path)]
            try:
                _verify_parent(path, fd)
                installed_quarantine = _quarantine_expected(
                    fd, path.name, new_identity, f"{path.name}.rollback"
                )
                _chmod_expected(fd, installed_quarantine, new_identity, 0o600)
                if original_identity is not None:
                    restored_identity = write_bytes_atomic(
                        path, original_bytes, mode, None
                    )
                    if _entry_identity(fd, path.name) != restored_identity:
                        raise RuntimeError(f"{path} changed after rollback restore")
                _verify_parent(path, fd)
                _discard_expected_name(
                    fd,
                    installed_quarantine,
                    new_identity,
                    f"{path.name}.rollback-retired",
                )
                installed_quarantine = None
            except Exception as rollback_error:
                if installed_quarantine is not None and _entry_identity(fd, path.name) is None:
                    try:
                        _publish_exclusive(fd, installed_quarantine, path.name)
                        installed_quarantine = None
                    except Exception:
                        pass
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(f"batch operation failed: {primary}; rollback failed: " + "; ".join(rollback_errors)) from primary
        raise
    finally:
        _RETAIN_ORIGINAL = False
        _LAST_REPLACEMENT = None
        _ORIGINAL_METADATA.clear()
        for fd in reversed(lock_fds):
            try:
                os.close(fd)
            except OSError:
                pass
        for path, _hook, _events in specs:
            _BOUND_PARENTS.pop(str(path), None)
            _BOUND_PARENTS.pop(str(path.parent / ".unified-kanban-hooks.lock"), None)
        for fd in reversed(parent_fds):
            try:
                os.close(fd)
            except OSError:
                pass


def main() -> int:
    """Validate arguments and apply one atomic multi-provider hook operation."""
    if (
        len(sys.argv) in {6, 8}
        and sys.argv[1] in {"batch-install", "batch-uninstall", "batch-validate"}
        and (len(sys.argv) == 6 or sys.argv[6] == "--receipt")
    ):
        action = sys.argv[1].removeprefix("batch-")
        specs = [
            (Path(sys.argv[2]).expanduser(), Path(sys.argv[3]).expanduser(), CLAUDE_EVENTS),
            (Path(sys.argv[4]).expanduser(), Path(sys.argv[5]).expanduser(), CODEX_EVENTS),
        ]
        if action == "uninstall":
            original_specs = specs
            specs = [spec for spec in specs if os.path.lexists(spec[0])]
            if not specs:
                if len(sys.argv) == 8:
                    _write_receipt(
                        Path(sys.argv[7]),
                        [
                            {"path": str(path), "changed": False, "identity": None}
                            for path, _hook, _events in original_specs
                        ],
                    )
                return 0
        receipt_path = Path(sys.argv[7]) if len(sys.argv) == 8 else None
        apply_batch(action, specs, receipt_path)
        return 0
    if (
        len(sys.argv) not in {5, 7}
        or sys.argv[1] not in {"install", "uninstall"}
        or sys.argv[4] not in {"claude", "codex"}
        or (len(sys.argv) == 7 and sys.argv[5] != "--receipt")
    ):
        print(
            "usage: install-claude-hooks.py install|uninstall SETTINGS HOOK_BIN claude|codex",
            file=sys.stderr,
        )
        return 2
    action = sys.argv[1]
    settings_path = Path(sys.argv[2]).expanduser()
    hook_bin = Path(sys.argv[3]).expanduser()
    events = CLAUDE_EVENTS if sys.argv[4] == "claude" else CODEX_EVENTS
    if not settings_path.is_absolute() or not hook_bin.is_absolute():
        raise ValueError("hook settings and binary paths must be absolute")
    receipt_path = Path(sys.argv[6]) if len(sys.argv) == 7 else None
    if action == "uninstall" and not os.path.lexists(settings_path):
        if receipt_path is not None:
            _write_receipt(
                receipt_path,
                [{"path": str(settings_path), "changed": False, "identity": None}],
            )
        return 0
    apply_batch(action, [(settings_path, hook_bin, events)], receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
