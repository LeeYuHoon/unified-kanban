#!/usr/bin/env python3
"""Atomically merge or remove repository-owned Claude and Codex hook entries."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import stat
import sys
import tempfile
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


FileIdentity = tuple[int, int, int, int]
MANAGED_MARKER = "# unified-kanban-managed-v1"


def _identity(info: os.stat_result) -> FileIdentity:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def read_settings(
    path: Path,
) -> tuple[dict[str, Any], bytes, int, FileIdentity | None]:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return {}, b"", 0o600, None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("Claude settings must be a non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("Claude settings changed during read")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read()
        payload = json.loads(raw)
    finally:
        os.close(fd)
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
                    and hook.get("timeout") == 30
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
    fd, name = tempfile.mkstemp(dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            installed_identity = _identity(os.fstat(handle.fileno()))
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            if expected_identity is not None:
                raise RuntimeError("hook settings changed before write")
        else:
            if expected_identity is None or _identity(current) != expected_identity:
                raise RuntimeError("hook settings changed before write")
        os.replace(temp, path)
        temp = None
        if _identity(os.lstat(path)) != installed_identity:
            raise RuntimeError("hook settings changed immediately after write")
        return installed_identity
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def write_atomic(
    path: Path,
    settings: dict[str, Any],
    mode: int,
    expected_identity: FileIdentity | None,
) -> FileIdentity:
    content = (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return write_bytes_atomic(path, content, mode, expected_identity)


def _open_lock(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        os.close(fd)
        raise RuntimeError("hook settings lock must be a regular file")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def apply_batch(
    action: str,
    specs: list[tuple[Path, Path, dict[str, str]]],
) -> None:
    for path, hook_bin, _events in specs:
        if not path.is_absolute() or not hook_bin.is_absolute():
            raise ValueError("hook settings and binary paths must be absolute")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    lock_paths = sorted({
        path.parent / ".unified-kanban-hooks.lock" for path, _hook, _events in specs
    })
    lock_fds: list[int] = []
    written: list[tuple[Path, bytes, int, FileIdentity | None, FileIdentity]] = []
    try:
        for lock_path in lock_paths:
            lock_fds.append(_open_lock(lock_path))

        records = []
        for path, hook_bin, events in specs:
            settings, original_bytes, mode, identity = read_settings(path)
            validate_groups(settings, events)
            records.append(
                (path, hook_bin, events, settings, original_bytes, mode, identity)
            )

        if action == "validate":
            return

        for path, hook_bin, events, settings, original_bytes, mode, identity in records:
            if action == "install":
                merge(settings, hook_bin, events)
            elif action == "uninstall":
                unmerge(settings, hook_bin, events)
            else:
                raise ValueError(f"unsupported batch action: {action}")
            new_identity = write_atomic(path, settings, mode, identity)
            written.append((path, original_bytes, mode, identity, new_identity))
    except Exception as primary:
        rollback_errors = []
        for path, original_bytes, mode, original_identity, new_identity in reversed(written):
            try:
                current = os.lstat(path)
                if _identity(current) != new_identity:
                    raise RuntimeError(f"{path} changed before rollback")
                if original_identity is None:
                    path.unlink()
                else:
                    write_bytes_atomic(path, original_bytes, mode, new_identity)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(
                f"batch operation failed: {primary}; rollback failed: "
                + "; ".join(rollback_errors)
            ) from primary
        raise
    finally:
        for fd in reversed(lock_fds):
            os.close(fd)


def main() -> int:
    """Validate arguments and apply one atomic multi-provider hook operation."""
    if (
        len(sys.argv) == 6
        and sys.argv[1] in {"batch-install", "batch-uninstall", "batch-validate"}
    ):
        action = sys.argv[1].removeprefix("batch-")
        specs = [
            (Path(sys.argv[2]).expanduser(), Path(sys.argv[3]).expanduser(), CLAUDE_EVENTS),
            (Path(sys.argv[4]).expanduser(), Path(sys.argv[5]).expanduser(), CODEX_EVENTS),
        ]
        if action == "uninstall":
            specs = [spec for spec in specs if os.path.lexists(spec[0])]
            if not specs:
                return 0
        apply_batch(action, specs)
        return 0
    if (
        len(sys.argv) != 5
        or sys.argv[1] not in {"install", "uninstall"}
        or sys.argv[4] not in {"claude", "codex"}
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
    settings_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(
        settings_path.parent / ".unified-kanban-hooks.lock",
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        settings, _original_bytes, mode, identity = read_settings(settings_path)
        if action == "install":
            merge(settings, hook_bin, events)
        else:
            unmerge(settings, hook_bin, events)
        write_atomic(settings_path, settings, mode, identity)
    finally:
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
