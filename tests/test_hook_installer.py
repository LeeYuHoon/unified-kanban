from __future__ import annotations

import importlib.util
import json
import os
import shlex
import stat
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install-claude-hooks.py"
SPEC = importlib.util.spec_from_file_location("hook_installer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def _settings_pair(tmp_path: Path) -> tuple[Path, Path, bytes, bytes]:
    claude = tmp_path / ".claude/settings.json"
    codex = tmp_path / ".codex/hooks.json"
    claude.parent.mkdir(parents=True)
    codex.parent.mkdir(parents=True)
    claude_raw = b'{  "model" : "opus", "hooks" : {} }'
    codex_raw = b'{\n  "hooks": {}\n}\n'
    claude.write_bytes(claude_raw)
    codex.write_bytes(codex_raw)
    os.chmod(claude, 0o640)
    os.chmod(codex, 0o600)
    return claude, codex, claude_raw, codex_raw


def test_batch_install_rolls_back_exact_bytes_and_mode_when_second_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, codex, claude_raw, codex_raw = _settings_pair(tmp_path)
    real_write = installer.write_atomic

    def fail_second(path, settings, mode, expected_identity):
        if path == codex:
            raise OSError("simulated second write failure")
        return real_write(path, settings, mode, expected_identity)

    monkeypatch.setattr(installer, "write_atomic", fail_second)
    with pytest.raises(OSError, match="second write failure"):
        installer.apply_batch(
            "install",
            [
                (claude, tmp_path / "claude-hook", installer.CLAUDE_EVENTS),
                (codex, tmp_path / "codex-hook", installer.CODEX_EVENTS),
            ],
        )

    assert claude.read_bytes() == claude_raw
    assert codex.read_bytes() == codex_raw
    assert stat.S_IMODE(os.stat(claude).st_mode) == 0o640
    assert stat.S_IMODE(os.stat(codex).st_mode) == 0o600


def test_batch_reports_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, codex, claude_raw, _codex_raw = _settings_pair(tmp_path)
    real_write = installer.write_atomic
    real_write_bytes = installer.write_bytes_atomic

    def fail_second(path, settings, mode, expected_identity):
        if path == codex:
            raise OSError("second write failed")
        return real_write(path, settings, mode, expected_identity)

    def fail_rollback(path, content, mode, expected_identity):
        if path == claude and content == claude_raw:
            raise OSError("rollback write failed")
        return real_write_bytes(path, content, mode, expected_identity)

    monkeypatch.setattr(installer, "write_atomic", fail_second)
    monkeypatch.setattr(installer, "write_bytes_atomic", fail_rollback)
    with pytest.raises(RuntimeError, match="rollback failed.*rollback write failed"):
        installer.apply_batch(
            "install",
            [
                (claude, tmp_path / "claude-hook", installer.CLAUDE_EVENTS),
                (codex, tmp_path / "codex-hook", installer.CODEX_EVENTS),
            ],
        )


def test_post_tool_use_event_is_claude_only() -> None:
    assert installer.CLAUDE_EVENTS["PostToolUse"] == "post-tool-use"
    # Codex 0.145 ships native PostToolUse/SubagentStart/SessionEnd hooks.
    assert installer.CODEX_EVENTS == {
        "UserPromptSubmit": "prompt",
        "PostToolUse": "post-tool-use",
        "SubagentStart": "subagent-start",
        "Stop": "stop",
        "SessionEnd": "session-end",
    }


def test_batch_install_and_uninstall_post_tool_use_symmetrically(
    tmp_path: Path,
) -> None:
    claude, codex, _claude_raw, _codex_raw = _settings_pair(tmp_path)
    claude_hook = tmp_path / "claude-hook"
    codex_hook = tmp_path / "codex-hook"
    specs = [
        (claude, claude_hook, installer.CLAUDE_EVENTS),
        (codex, codex_hook, installer.CODEX_EVENTS),
    ]

    installer.apply_batch("install", specs)

    claude_settings = json.loads(claude.read_text(encoding="utf-8"))
    commands = [
        item["command"]
        for group in claude_settings["hooks"]["PostToolUse"]
        for item in group["hooks"]
    ]
    assert commands == [
        f"{shlex.quote(str(claude_hook))} post-tool-use {installer.MANAGED_MARKER}"
    ]
    codex_settings = json.loads(codex.read_text(encoding="utf-8"))
    assert [
        hook["command"]
        for group in codex_settings["hooks"]["PostToolUse"]
        for hook in group["hooks"]
    ] == [f"{codex_hook} post-tool-use {installer.MANAGED_MARKER}"]
    assert [
        hook["command"]
        for group in codex_settings["hooks"]["SubagentStart"]
        for hook in group["hooks"]
    ] == [f"{codex_hook} subagent-start {installer.MANAGED_MARKER}"]

    installer.apply_batch("uninstall", specs)

    assert json.loads(claude.read_text(encoding="utf-8")) == {"model": "opus"}
    assert json.loads(codex.read_text(encoding="utf-8")) == {}


def test_merge_normalizes_duplicate_managed_commands_and_timeout(tmp_path: Path) -> None:
    hook = tmp_path / "hook"
    command = f"{hook} prompt {installer.MANAGED_MARKER}"
    settings = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": command, "timeout": 1}]},
                {"hooks": [{"type": "command", "command": command, "timeout": 30}]},
            ]
        }
    }

    installer.merge(settings, hook, installer.CLAUDE_EVENTS)

    managed = [
        item
        for group in settings["hooks"]["UserPromptSubmit"]
        for item in group["hooks"]
        if item.get("command") == command
    ]
    assert managed == [{"type": "command", "command": command, "timeout": 30}]
