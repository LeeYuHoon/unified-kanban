"""기존 전사 파일의 생성 이후 실패 경계를 합성 이벤트로 검증한다."""
import json
import os


import pytest

from kanban_adapter import claude_hook as hook
from kanban_adapter import conversation_runtime as runtime
from test_claude_missing_transcript import _harness, _write_source


@pytest.mark.parametrize("error_type", [OSError, ValueError, RuntimeError])
def test_capture_failure_keeps_created_task_tracked(tmp_path, monkeypatch, error_type):
    source, service, commands, adapter, payload, cache = _harness(tmp_path, monkeypatch)
    _write_source(source)
    primary = error_type("private-canary")

    def fail(**kwargs):
        raise primary

    monkeypatch.setattr(runtime, "capture_hook_start", fail)
    with pytest.raises(error_type) as caught:
        hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert caught.value is primary
    # 영수증은 신규 생성 여부를 증명하지 않으므로 기존 카드도 삭제하지 않는다.
    state = json.loads(hook._state_path(cache, payload["session_id"]).read_text())
    assert state["task_id"] == "t_native"
    assert [command[0] for command in commands] == ["start"]
    assert "private-canary" not in json.dumps(state)
    assert state["conversation_status"] == "unavailable"
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert [command[0] for command in commands] == ["start"]
    hook.handle_event("stop", dict(payload, prompt_id="stale"), adapter=adapter, cache_dir=cache)
    assert [command[0] for command in commands] == ["start"]
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    assert [command[0] for command in commands] == ["start", "update", "done"]


@pytest.mark.parametrize("receipt_bytes", [b"", b"{", b'{"task":"t_foreign","board":"foreign"}'])
def test_bad_receipt_does_not_leave_untracked_task(tmp_path, monkeypatch, receipt_bytes):
    source, service, commands, _, payload, cache = _harness(tmp_path, monkeypatch)
    _write_source(source)

    def adapter(argv, cwd):
        commands.append(argv)
        if argv[0] == "start":
            fd = int(next(a.split("=", 1)[1] for a in argv if a.startswith("--conversation-receipt-fd=")))
            os.write(fd, receipt_bytes)
            return "t_native"
        return ""

    with pytest.raises((ValueError, RuntimeError)):
        hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    # 잘못된 영수증의 다른 보드/카드에는 보상 쓰기를 하지 않는다.
    assert [command[0] for command in commands] == ["start"]
    state = json.loads(hook._state_path(cache, payload["session_id"]).read_text())
    assert state["conversation_status"] == "unavailable"
    assert "observation_receipt" not in state
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    hook.handle_event("stop", dict(payload, prompt_id="stale"), adapter=adapter, cache_dir=cache)
    assert [command[0] for command in commands] == ["start"]
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    assert [command[0] for command in commands] == ["start", "update", "done"]
