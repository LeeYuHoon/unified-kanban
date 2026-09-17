"""영구 선택자의 명시적 동의와 프로필 격리를 임시 파일로 검증한다."""
import json
import os
from pathlib import Path

import pytest

from kanban_adapter import conversation_runtime as runtime


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.delenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", raising=False)
    monkeypatch.setattr(runtime, "_cached_path", None)
    monkeypatch.setattr(runtime, "_cached_service", None)


def selector(tmp_path, **changes):
    home = tmp_path / "profile"
    parent = home / "unified-kanban-conversation"
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = parent / "activation.json"
    payload = dict(schema_version=1, enabled=True,
                   runtime_config=str(parent / "runtime.json"), launcher_profiles={})
    payload.update(changes)
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path, payload


def test_clean_environment_resolves_explicit_private_selector(tmp_path, monkeypatch):
    _, payload = selector(tmp_path)
    built = []
    service = object()
    monkeypatch.setattr(runtime, "_build", lambda path: built.append(path) or service)
    assert runtime.get_conversation_service() is service
    assert built == [Path(str(payload["runtime_config"]))]


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"enabled": 1},
    {"enabled": "false"}, {"runtime_config": "relative.json"},
    {"runtime_config": "/tmp/../runtime.json"}, {"runtime_config": "/tmp/\u0000bad"},
    {"extra": "not-allowed"}, {"launcher_profiles": []},
    {"launcher_profiles": {"foreign": "/tmp/ref"}},
    {"launcher_profiles": {"codex": "relative"}},
])
def test_configured_malformed_selector_is_not_absence(tmp_path, monkeypatch, changes):
    selector(tmp_path, **changes)
    monkeypatch.setattr(runtime, "_build", lambda _: pytest.fail("runtime must not open"))
    with pytest.raises((ValueError, TypeError)):
        runtime.get_conversation_service()


@pytest.mark.parametrize("damage", ["file-mode", "parent-mode", "hardlink", "symlink", "oversize", "duplicate"])
def test_configured_private_selector_failure_is_not_absence(tmp_path, monkeypatch, damage):
    path, _ = selector(tmp_path)
    if damage == "file-mode":
        path.chmod(0o644)
    elif damage == "parent-mode":
        path.parent.chmod(0o755)
    elif damage == "hardlink":
        os.link(path, path.with_name("alias"))
    elif damage == "symlink":
        target = path.with_name("target")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "oversize":
        path.write_text(" " * 65537)
    else:
        path.write_text('{"schema_version":1,"enabled":false,"enabled":true,"runtime_config":"/tmp/runtime.json","launcher_profiles":{}}')
    monkeypatch.setattr(runtime, "_build", lambda _: pytest.fail("runtime must not open"))
    with pytest.raises((OSError, ValueError, RuntimeError)):
        runtime.get_conversation_service()


@pytest.mark.parametrize("parent_mode", [0o700, 0o755])
def test_prepared_runtime_without_selector_is_disabled(tmp_path, monkeypatch, parent_mode):
    path, payload = selector(tmp_path)
    path.parent.chmod(parent_mode)
    Path(str(payload["runtime_config"])).write_text("not a runtime")
    path.unlink()
    monkeypatch.setattr(runtime, "_build", lambda _: pytest.fail("runtime must not open"))
    assert runtime.get_conversation_service() is None


def test_disabled_selector_and_other_profile_do_not_build(tmp_path, monkeypatch):
    selector(tmp_path, enabled=False)
    monkeypatch.setattr(runtime, "_build", lambda _: pytest.fail("runtime must not open"))
    assert runtime.get_conversation_service() is None
    selector(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
    assert runtime.get_conversation_service() is None


@pytest.mark.parametrize("value", ["", "relative", "/tmp/../runtime.json"])
def test_invalid_explicit_override_never_falls_back(tmp_path, monkeypatch, value):
    selector(tmp_path)
    monkeypatch.setenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", value)
    monkeypatch.setattr(runtime, "_build", lambda _: pytest.fail("runtime must not open"))
    with pytest.raises(ValueError):
        runtime.get_conversation_service()


def test_explicit_override_precedes_selector(tmp_path, monkeypatch):
    path, _ = selector(tmp_path)
    path.write_text("bad selector")
    selected = tmp_path / "explicit.json"
    monkeypatch.setenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", str(selected))
    monkeypatch.setattr(runtime, "_build", lambda path: path)
    assert runtime.get_conversation_service() == selected


def test_fixture_hook_requests_receipt_without_config_environment(tmp_path, monkeypatch):
    from kanban_adapter import claude_hook as hook
    from test_claude_missing_transcript import _harness, PROMPT_ID

    _, _, commands, adapter, payload, cache = _harness(tmp_path, monkeypatch)
    monkeypatch.delenv("UNIFIED_KANBAN_CONVERSATION_CONFIG")
    selector(tmp_path)
    payload["prompt_id"] = PROMPT_ID
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert any(arg.startswith("--conversation-receipt-fd=") for arg in commands[0])
    state = json.loads(hook._state_path(cache, "native").read_text())
    assert state["conversation_prepared"]["mode"] == "claude-absent-v1"


def test_no_raw_environment_activation_branches_remain():
    import inspect
    from kanban_adapter import claude_hook as hook

    assert "UNIFIED_KANBAN_CONVERSATION_CONFIG" not in inspect.getsource(hook)


@pytest.mark.parametrize("damage", ["owner", "fifo", "parent-symlink", "file-swap", "parent-swap"])
def test_owner_type_and_namespace_races_fail_closed(tmp_path, monkeypatch, damage):
    from kanban_adapter import conversation_activation as activation

    path, _ = selector(tmp_path)
    if damage == "owner":
        uid = os.getuid()
        monkeypatch.setattr(activation.os, "getuid", lambda: uid + 1)
    elif damage == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif damage == "parent-symlink":
        moved = path.parent.with_name("moved")
        path.parent.rename(moved)
        path.parent.symlink_to(moved, target_is_directory=True)
    else:
        real_read = activation.os.read
        def racing_read(fd, count):
            data = real_read(fd, count)
            if damage == "file-swap":
                path.rename(path.with_name("old"))
                path.write_bytes(data)
                path.chmod(0o600)
            else:
                path.parent.rename(path.parent.with_name("old"))
                path.parent.mkdir(mode=0o700)
            return data
        monkeypatch.setattr(activation.os, "read", racing_read)
    monkeypatch.setattr(runtime, "_build", lambda _: pytest.fail("runtime must not open"))
    with pytest.raises((OSError, RuntimeError)):
        runtime.get_conversation_service()


def test_invalid_selector_is_checked_before_cached_service(tmp_path, monkeypatch):
    path, _ = selector(tmp_path)
    sentinel = object()
    monkeypatch.setattr(runtime, "_build", lambda _: sentinel)
    assert runtime.get_conversation_service() is sentinel
    path.write_text("malformed")
    with pytest.raises(ValueError):
        runtime.get_conversation_service()


def test_unset_profile_uses_only_home_default(tmp_path, monkeypatch):
    from kanban_adapter.conversation_activation import resolve_runtime_config

    path, payload = selector(tmp_path)
    path.parent.parent.rename(tmp_path / ".hermes")
    monkeypatch.delenv("HERMES_HOME")
    assert resolve_runtime_config() == Path(str(payload["runtime_config"]))


@pytest.mark.parametrize("source", ["claude-code", "codex"])
@pytest.mark.parametrize("receipt_kind", ["malformed", "wrong-board"])
def test_selector_capture_failure_keeps_original_board(tmp_path, monkeypatch, source, receipt_kind):
    from kanban_adapter import claude_hook as hook

    selector(tmp_path)
    mapping = ["original"]
    monkeypatch.setattr(hook.HermesCliBackend, "resolve_board", lambda self, **kw: mapping[0])
    calls = []
    def adapter(argv, cwd):
        calls.append(argv)
        if argv[0] == "start":
            fd = int(next(a.split("=", 1)[1] for a in argv if a.startswith("--conversation-receipt-fd=")))
            os.write(fd, b"{" if receipt_kind == "malformed" else b'{"board":"wrong","task":"t_same"}')
            return "t_same"
        assert argv[argv.index("--board") + 1] == "original"
        return ""
    payload = dict(session_id="session", cwd=str(tmp_path), prompt="hello")
    cache = tmp_path / "cache"
    with pytest.raises(ValueError if receipt_kind == "malformed" else RuntimeError):
        hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache, source=source)
    mapping[0] = "wrong"
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache, source=source)
    assert [args[0] for args in calls] == ["start", "update", "done"]
    assert all(args[args.index("--board") + 1] == "original" for args in calls)
