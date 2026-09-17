"""네이티브 종료의 요청 상관관계와 재시도 안전성을 검증한다."""
import pytest

from kanban_adapter import claude_hook as hook
from kanban_adapter import claude_pending_final as pending
from kanban_adapter import conversation_runtime as runtime
from test_claude_file_provenance_v2 import _setup, _history, _request, _final, _page


@pytest.mark.parametrize("event", ["stop", "session-end"])
@pytest.mark.parametrize("bad_id", ["missing", None, 42, [], "different", ""])
def test_native_stop_rejects_uncorrelated_id_then_correct_stop_recovers(tmp_path, monkeypatch, event, bad_id):
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    state_path = hook._state_path(cache, "native")
    before = state_path.read_bytes()
    with source.open("ab") as stream:
        stream.write(_final())
    wrong = dict(payload, model="wrong-model", last_assistant_message="OLD_TURN_FINAL")
    if bad_id == "missing":
        wrong.pop("prompt_id")
    else:
        wrong["prompt_id"] = bad_id
    hook.handle_event(event, wrong, adapter=adapter, cache_dir=cache)
    assert state_path.exists(), "상관관계 없는 종료는 현재 lifecycle을 지우면 안 된다"
    assert state_path.read_bytes() == before
    assert not any(command[0] == "done" for command in commands)
    assert not (cache / "pending-final").exists()
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")
    hook.handle_event("stop", dict(payload, last_assistant_message="current final"), adapter=adapter, cache_dir=cache)
    assert not state_path.exists()
    assert sum(command[0] == "done" for command in commands) == 1
    assert [entry["text"] for entry in _page(service)["events"]] == ["same request", "current final"]


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("event", ["stop", "session-end"])
def test_native_identity_is_required_without_collection_but_legacy_can_finish(tmp_path, monkeypatch, provider, native, event):
    monkeypatch.delenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", raising=False)
    monkeypatch.setattr(hook.HermesCliBackend, "resolve_board", lambda self, **kwargs: "demo")
    commands = []
    def adapter(argv, cwd):
        commands.append(argv)
        return "t_native" if argv[0] == "start" else ""
    payload = dict(session_id="native", cwd=str(tmp_path), prompt="request")
    if native:
        payload["prompt_id"] = "550e8400-e29b-41d4-a716-446655440000"
    cache = tmp_path / "cache"
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache, source=provider)
    state_path = hook._state_path(cache, "native")
    before = state_path.read_bytes()
    stop = dict(payload, model="wrong-model")
    stop.pop("prompt_id", None)
    hook.handle_event(event, stop, adapter=adapter, cache_dir=cache, source=provider)
    if native:
        assert state_path.read_bytes() == before
        assert not any(command[0] == "done" for command in commands)
        hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache, source=provider)
    assert not state_path.exists()
    assert sum(command[0] == "done" for command in commands) == 1


@pytest.mark.parametrize("status", ["rejected", "expired", "unknown", "unavailable"])
def test_v2_unready_queue_retains_lifecycle_then_recovers(tmp_path, monkeypatch, status):
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    state_path = hook._state_path(cache, "native")
    before = state_path.read_bytes()
    with source.open("ab") as stream:
        stream.write(_final())
    with monkeypatch.context() as scoped:
        if status == "unavailable":
            scoped.setattr(runtime, "get_conversation_service", lambda: None)
        else:
            scoped.setattr(pending, "run_once", lambda *args: status)
        scoped.setattr(pending, "launch", lambda *args: pytest.fail("거절 작업은 실행하지 않는다"))
        hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    assert state_path.exists()
    assert state_path.read_bytes() == before
    assert not any(command[0] == "done" for command in commands)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    assert not state_path.exists()
    assert sum(command[0] == "done" for command in commands) == 1
    assert [entry["text"] for entry in _page(service)["events"]] == ["same request", "current final"]
