from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
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


def test_changed_hook_receipt_binds_rendered_content(tmp_path: Path) -> None:
    claude, _codex, _claude_raw, _codex_raw = _settings_pair(tmp_path)
    receipt = tmp_path / "operation.json"

    installer.apply_batch(
        "install",
        [(claude, tmp_path / "claude-hook", installer.CLAUDE_EVENTS)],
        receipt_path=receipt,
    )

    payload = json.loads(receipt.read_text(encoding="utf-8"))
    [entry] = payload["entries"]
    assert entry["changed"] is True
    assert entry["sha256"] == hashlib.sha256(claude.read_bytes()).hexdigest()


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


def test_hook_temp_stays_private_during_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "settings.json"
    observed_modes: list[int] = []
    real_write = installer.os.write

    def observe_mode(fd: int, data) -> int:
        observed_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_write(fd, data)

    monkeypatch.setattr(installer.os, "write", observe_mode)
    installer.write_bytes_atomic(settings, b'{"installed": true}\n', 0o644, None)

    assert observed_modes
    assert set(observed_modes) == {0o600}
    assert stat.S_IMODE(settings.stat().st_mode) == 0o644


def test_read_settings_rejects_oversized_regular_file(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    with settings.open("wb") as stream:
        stream.truncate(installer.MAX_SETTINGS_BYTES + 1)

    with pytest.raises(ValueError, match="settings exceed"):
        installer.read_settings(settings)


def test_hook_quarantine_is_private_immediately(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks": {}}', encoding="utf-8")
    settings.chmod(0o644)
    directory_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        expected = installer._entry_identity(directory_fd, settings.name)
        assert expected is not None
        quarantine = installer._quarantine_expected(
            directory_fd, settings.name, expected, "settings.original"
        )
        info = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
        assert stat.S_IMODE(info.st_mode) == 0o600
        installer._publish_exclusive(directory_fd, quarantine, settings.name)
    finally:
        os.close(directory_fd)


def test_write_restores_original_when_quarantine_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "settings.json"
    original = b'{"hooks": {}}\n'
    settings.write_bytes(original)
    _payload, _raw, mode, expected = installer.read_settings(settings)
    assert expected is not None
    baseline = settings.stat()
    real_fsync = os.fsync
    calls = 0

    def fail_quarantine_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected quarantine fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(installer.os, "fsync", fail_quarantine_fsync)

    with pytest.raises(OSError, match="quarantine fsync failure"):
        installer.write_bytes_atomic(
            settings,
            b'{"hooks": {"installed": []}}\n',
            mode,
            expected,
        )

    restored = settings.stat()
    assert (restored.st_dev, restored.st_ino) == (baseline.st_dev, baseline.st_ino)
    assert settings.read_bytes() == original
    assert not list(tmp_path.glob(".settings.json.original.*"))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS quarantine privacy")
def test_hook_quarantine_clears_extended_acl_and_bsd_flags(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks": {}}', encoding="utf-8")
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read", settings],
        check=True,
    )
    os.chflags(settings, stat.UF_HIDDEN)
    _payload, _raw, _mode, expected = installer.read_settings(settings)
    assert expected is not None
    baseline = installer._ORIGINAL_METADATA[expected]
    directory_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        quarantine = installer._quarantine_expected(
            directory_fd, settings.name, expected, "settings.original"
        )
        info = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
        opened = os.open(quarantine, os.O_RDONLY, dir_fd=directory_fd)
        try:
            acl = installer._metadata_helper()._mac_acl(opened)
        finally:
            os.close(opened)
        installer._restore_metadata_expected(directory_fd, quarantine, expected)
        installer._publish_exclusive(directory_fd, quarantine, settings.name)
    finally:
        os.close(directory_fd)

    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_flags == 0
    assert acl in {None, ""}
    assert baseline["acl"] not in {None, ""}
    assert baseline["flags"] == stat.UF_HIDDEN


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS metadata contract")
def test_hook_replacement_preserves_complete_baseline_metadata(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks": {}}\n', encoding="utf-8")
    settings.chmod(0o640)
    baseline_atime = 1_700_001_000_123_456_789
    baseline_mtime = 1_700_002_000_987_654_321
    os.utime(settings, ns=(baseline_atime, baseline_mtime))
    subprocess.run(
        ["/usr/bin/xattr", "-w", "com.example.hook-test", "retained", settings],
        check=True,
    )
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read", settings],
        check=True,
    )
    os.chflags(settings, stat.UF_HIDDEN)
    _payload, _raw, mode, expected = installer.read_settings(settings)
    assert expected is not None
    baseline = dict(installer._ORIGINAL_METADATA[expected])

    installed = installer.write_bytes_atomic(
        settings,
        b'{"hooks": {"installed": []}}\n',
        mode,
        expected,
    )

    opened = os.open(settings, os.O_RDONLY)
    try:
        info = os.fstat(opened)
        observed = installer._capture_metadata(opened, info)
    finally:
        os.close(opened)
    assert installer._identity(info) == installed
    assert settings.read_bytes() == b'{"hooks": {"installed": []}}\n'
    assert observed == baseline


def test_write_does_not_overwrite_leaf_created_after_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text('{"before": true}\n', encoding="utf-8")
    expected = installer._identity(settings.lstat())
    real_link = installer.os.link
    injected = False

    def create_foreign_before_publish(src, dst, **kwargs):
        nonlocal injected
        if dst == settings.name and not injected:
            injected = True
            fd = installer.os.open(
                dst,
                installer.os.O_WRONLY | installer.os.O_CREAT | installer.os.O_EXCL,
                0o600,
                dir_fd=kwargs["dst_dir_fd"],
            )
            try:
                installer.os.write(fd, b"foreign\n")
            finally:
                installer.os.close(fd)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(installer.os, "link", create_foreign_before_publish)

    with pytest.raises(FileExistsError):
        installer.write_bytes_atomic(settings, b'{"installed": true}\n', 0o600, expected)

    assert settings.read_bytes() == b"foreign\n"
    quarantines = list(tmp_path.glob(".settings.json.original.*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b'{"before": true}\n'
    assert stat.S_IMODE(quarantines[0].stat().st_mode) == 0o600


def test_batch_reports_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, codex, _claude_raw, _codex_raw = _settings_pair(tmp_path)
    real_write = installer.write_atomic
    real_link = installer.os.link
    settings_publishes = 0

    def fail_second(path, settings, mode, expected_identity):
        if path == codex:
            raise OSError("second write failed")
        return real_write(path, settings, mode, expected_identity)

    def fail_rollback_link(src, dst, **kwargs):
        nonlocal settings_publishes
        if dst == "settings.json":
            settings_publishes += 1
        if settings_publishes == 2 and str(src).startswith(".settings.json.temp."):
            raise OSError("rollback link failed")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(installer, "write_atomic", fail_second)
    monkeypatch.setattr(installer.os, "link", fail_rollback_link)
    with pytest.raises(RuntimeError, match="rollback failed.*rollback link failed"):
        installer.apply_batch(
            "install",
            [
                (claude, tmp_path / "claude-hook", installer.CLAUDE_EVENTS),
                (codex, tmp_path / "codex-hook", installer.CODEX_EVENTS),
            ],
        )


def test_receipt_failure_restores_single_path_without_original_quarantines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, codex, claude_raw, codex_raw = _settings_pair(tmp_path)
    receipt = tmp_path / "operation.json"
    original = claude.stat()
    original_atime_ns = original.st_atime_ns
    original_mtime_ns = original.st_mtime_ns
    xattr_name = b"user.unified-kanban-test"
    setxattr = getattr(os, "setxattr", None)
    getxattr = getattr(os, "getxattr", None)
    if setxattr is not None and getxattr is not None:
        setxattr(claude, xattr_name, b"retained")
    original_flags = getattr(original, "st_flags", 0)
    test_flag = getattr(stat, "UF_HIDDEN", 0)
    has_flags = bool(test_flag and hasattr(os, "chflags"))
    if has_flags:
        os.chflags(claude, original_flags | test_flag)
        original_flags |= test_flag
    if sys.platform == "darwin":
        subprocess.run(
            ["/bin/chmod", "+a", "everyone allow read", claude],
            check=True,
        )
    metadata_fd = os.open(claude, os.O_RDONLY)
    try:
        original_acl = installer._metadata_helper()._mac_acl(metadata_fd)
    finally:
        os.close(metadata_fd)

    def fail_receipt(_path, _entries):
        raise OSError("injected receipt failure")

    monkeypatch.setattr(installer, "_write_receipt", fail_receipt)
    with pytest.raises(OSError, match="receipt failure"):
        installer.apply_batch(
            "install",
            [(claude, tmp_path / "claude-hook", installer.CLAUDE_EVENTS)],
            receipt,
        )

    restored = claude.stat()
    assert restored.st_ino == original.st_ino
    assert (restored.st_uid, restored.st_gid) == (original.st_uid, original.st_gid)
    assert restored.st_atime_ns == original_atime_ns
    assert restored.st_mtime_ns == original_mtime_ns
    metadata_fd = os.open(claude, os.O_RDONLY)
    try:
        assert installer._metadata_helper()._mac_acl(metadata_fd) == original_acl
    finally:
        os.close(metadata_fd)
    assert claude.read_bytes() == claude_raw
    assert codex.read_bytes() == codex_raw
    assert stat.S_IMODE(restored.st_mode) == 0o640
    assert stat.S_IMODE(codex.stat().st_mode) == 0o600
    if getxattr is not None:
        assert getxattr(claude, xattr_name) == b"retained"
    if has_flags:
        assert claude.stat().st_flags == original_flags
        os.chflags(claude, original_flags & ~test_flag)
    assert not receipt.exists()
    assert not list(tmp_path.rglob("*.original.*"))


def test_receipt_backed_multi_path_operation_is_rejected_before_mutation(
    tmp_path: Path,
) -> None:
    claude, codex, claude_raw, codex_raw = _settings_pair(tmp_path)

    with pytest.raises(ValueError, match="exactly one settings path"):
        installer.apply_batch(
            "install",
            [
                (claude, tmp_path / "claude-hook", installer.CLAUDE_EVENTS),
                (codex, tmp_path / "codex-hook", installer.CODEX_EVENTS),
            ],
            tmp_path / "operation.json",
        )

    assert claude.read_bytes() == claude_raw
    assert codex.read_bytes() == codex_raw


def test_transaction_mode_does_not_recreate_replaced_settings_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction = tmp_path / "transaction"
    transaction.mkdir(mode=0o700)
    transaction_fd = os.open(
        transaction, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    parent = tmp_path / ".claude"
    moved = tmp_path / ".claude-moved"
    parent.mkdir(mode=0o700)
    settings = parent / "settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    parent.rename(moved)
    monkeypatch.setenv("UNIFIED_KANBAN_TRANSACTION_DIR", str(transaction))
    monkeypatch.setenv("UNIFIED_KANBAN_TRANSACTION_FD", str(transaction_fd))
    try:
        with pytest.raises(FileNotFoundError):
            installer.apply_batch(
                "install",
                [(settings, tmp_path / "hook", installer.CLAUDE_EVENTS)],
                transaction / "operation.json",
            )
    finally:
        os.close(transaction_fd)

    assert not parent.exists()
    assert json.loads((moved / "settings.json").read_text(encoding="utf-8")) == {}


def test_post_receipt_parent_close_failure_does_not_fail_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    receipt = tmp_path / "operation.json"
    real_open_parent = installer._open_parent
    real_close = installer.os.close
    parent_fds = set()
    failures = 0

    def track_parent(path, **kwargs):
        result = real_open_parent(path, **kwargs)
        if path == settings:
            parent_fds.add(result[0])
        return result

    def fail_parent_close(fd):
        nonlocal failures
        if failures == 0 and receipt.exists() and fd in parent_fds:
            failures += 1
            raise OSError("injected final close failure")
        return real_close(fd)

    monkeypatch.setattr(installer, "_open_parent", track_parent)
    monkeypatch.setattr(installer.os, "close", fail_parent_close)
    installer.apply_batch(
        "install",
        [(settings, tmp_path / "hook", installer.CLAUDE_EVENTS)],
        receipt,
    )

    assert failures == 1
    assert receipt.exists()
    assert json.loads(settings.read_text(encoding="utf-8"))["hooks"]


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
    ] == [f"{shlex.quote(str(codex_hook))} post-tool-use {installer.MANAGED_MARKER}"]
    assert [
        hook["command"]
        for group in codex_settings["hooks"]["SubagentStart"]
        for hook in group["hooks"]
    ] == [f"{shlex.quote(str(codex_hook))} subagent-start {installer.MANAGED_MARKER}"]

    installer.apply_batch("uninstall", specs)

    assert json.loads(claude.read_text(encoding="utf-8")) == {"model": "opus"}
    assert json.loads(codex.read_text(encoding="utf-8")) == {}


def test_merge_normalizes_duplicate_managed_commands_and_timeout(tmp_path: Path) -> None:
    hook = tmp_path / "hook"
    command = f"{shlex.quote(str(hook))} prompt {installer.MANAGED_MARKER}"
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


def test_unmerge_removes_owned_command_even_when_timeout_drifted(tmp_path: Path) -> None:
    hook = tmp_path / "hook"
    command = f"{shlex.quote(str(hook))} prompt {installer.MANAGED_MARKER}"
    settings = {
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [{"type": "command", "command": command, "timeout": 60}]
            }]
        }
    }

    installer.unmerge(settings, hook, installer.CLAUDE_EVENTS)

    assert settings == {}


def test_batch_receipt_distinguishes_changed_and_byte_identical_no_op(
    tmp_path: Path,
) -> None:
    claude, codex, _claude_raw, _codex_raw = _settings_pair(tmp_path)
    specs = [
        (claude, tmp_path / "claude-hook", installer.CLAUDE_EVENTS),
        (codex, tmp_path / "codex-hook", installer.CODEX_EVENTS),
    ]

    changed = installer.apply_batch("install", specs)
    unchanged = installer.apply_batch("install", specs)

    assert [entry["changed"] for entry in changed["entries"]] == [True, True]
    assert all(entry["identity"] is not None for entry in changed["entries"])
    assert [entry["changed"] for entry in unchanged["entries"]] == [False, False]
    assert [entry["identity"] for entry in unchanged["entries"]] == [
        entry["identity"] for entry in changed["entries"]
    ]
