"""새 Claude 영수증에 연결된 최초 열기 스냅샷은 항상 LIMITED PARTIAL이다.

식별자가 없거나 불확실한 소스는 제공하지 않으며 기존 inode 연속성은 유지한다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_conversation_integration_security_fixes import _receipt, _service

from kanban_adapter import claude_hook as hook
from kanban_adapter import conversation_runtime as runtime


def _harness(tmp_path, monkeypatch):
    monkeypatch.setattr(hook.HermesCliBackend, "resolve_board", lambda self, **kwargs: "demo")
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    source = root / "new-project" / "native.jsonl"
    service, kernel = _service(tmp_path, provider="claude", root=root)
    monkeypatch.setenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", str(tmp_path / "config"))
    monkeypatch.setattr(runtime, "get_conversation_service", lambda: service)
    commands = []

    def adapter(argv, cwd):
        commands.append(argv)
        if argv[0] == "start":
            receipt = _receipt(kernel, board="demo", task="t_native", created_at=2)
            fd = int(next(a.split("=", 1)[1] for a in argv if a.startswith("--conversation-receipt-fd=")))
            os.write(fd, json.dumps(receipt).encode())
            return "t_native"
        return ""

    payload = {"session_id": "native", "cwd": str(tmp_path), "prompt": "public new turn", "transcript_path": str(source)}
    cache = tmp_path / "cache"
    return source, service, commands, adapter, payload, cache


def _write_source(source, text="public new turn"):
    source.parent.mkdir(mode=0o700, exist_ok=True)
    source.write_text(json.dumps({"type": "user", "sessionId": "native", "message": {"role": "user", "content": text}}) + "\n")
    source.chmod(0o600)


@pytest.mark.parametrize("final_event", ["stop", "session-end"])
def test_absent_first_transcript_preserves_state_and_closes_card(tmp_path, monkeypatch, final_event):
    source, service, commands, adapter, payload, cache = _harness(tmp_path, monkeypatch)
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    state_path = hook._state_path(cache, "native")
    state = json.loads(state_path.read_text())
    assert state["task_id"] == "t_native"
    assert "observation_receipt" in state
    assert "conversation_prepared" not in state
    # 후보 경로에 놓인 무관한 과거 기록은 시작 증거가 아니다.
    _write_source(source, "preexisting substituted content")
    hook.handle_event(final_event, payload, adapter=adapter, cache_dir=cache)
    assert not state_path.exists()
    assert any(command[0] == "done" for command in commands)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


PROMPT_ID = "550e8400-e29b-41d4-a716-446655440000"


def _native_turn(source, prompt_id=PROMPT_ID):
    source.parent.mkdir(mode=0o700, exist_ok=True)
    records = [
        {"type": "user", "sessionId": "native", "promptId": prompt_id,
         "uuid": "user-one", "parentUuid": None,
         "message": {"role": "user", "content": "public new turn"}},
        {"type": "assistant", "sessionId": "native", "uuid": "assistant-one",
         "parentUuid": "user-one", "message": {"role": "assistant",
         "stop_reason": "end_turn", "content": [{"type": "text", "text": "public final answer"}]}},
    ]
    source.write_text("".join(json.dumps(record) + "\n" for record in records))
    source.chmod(0o600)


def test_first_native_turn_sealing_requires_missing_producer_boundary(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _harness(tmp_path, monkeypatch)
    payload["prompt_id"] = PROMPT_ID
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    _native_turn(source)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    binding, _ = service.bindings.get("demo", "t_native")
    assert binding.turn_start.byte_offset == 0
    assert binding.turn_end.byte_offset == source.stat().st_size
    page = service.get_parent_page(principal_id="owner", board="demo", task="t_native", cursor=None, limit=20)
    assert page["completeness"] == "partial"
    assert [(e["kind"], e["text"]) for e in page["events"]] == [
        ("user_message", "public new turn"), ("final_assistant", "public final answer")]
    assert any(command[0] == "done" for command in commands)


@pytest.mark.parametrize("final_event", ["stop", "session-end"])
def test_existing_start_evidence_seals_only_appended_turn(tmp_path, monkeypatch, final_event):
    source, service, commands, adapter, payload, cache = _harness(tmp_path, monkeypatch)
    _write_source(source, "prior turn must not be bound")
    prior_size = source.stat().st_size
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    with source.open("a") as stream:
        stream.write(json.dumps({"type": "user", "sessionId": "native", "message": {"role": "user", "content": "new turn"}}) + "\n")
    hook.handle_event(final_event, payload, adapter=adapter, cache_dir=cache)
    binding, _ = service.bindings.get("demo", "t_native")
    assert binding.turn_start.byte_offset == prior_size
    assert binding.turn_end.byte_offset == source.stat().st_size
    assert not hook._state_path(cache, "native").exists()
    assert any(command[0] == "done" for command in commands)


def test_existing_start_inode_rejects_substituted_transcript(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _harness(tmp_path, monkeypatch)
    _write_source(source, "prior turn")
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    source.rename(source.with_suffix(".old"))
    _write_source(source, "substituted old content")
    with pytest.raises(PermissionError, match="identity changed"):
        hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")
    assert hook._state_path(cache, "native").exists()
