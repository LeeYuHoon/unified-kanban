"""합성 비공개 데이터만 사용해 실제 프롬프트 및 진입 경로의 진단을 검증한다."""
import io
import json
import os

import pytest

from kanban_adapter import claude_hook as hook, claude_hook_entry as entry, codex_hook
from kanban_adapter.backend import HermesCliBackend

CANARY = "SYNTHETIC_PRIVATE_CANARY"


def fail_private(*args, **kwargs):
    raise PermissionError(CANARY, "/private/" + CANARY)


@pytest.fixture
def diagnostic(monkeypatch):
    records = []
    monkeypatch.setenv("UNIFIED_KANBAN_DIAGNOSTIC_FD", "3")
    monkeypatch.setattr(entry.os, "write", lambda fd, raw: records.append(json.loads(raw)))
    return records


def test_codex_entry_original_exception_metadata(monkeypatch, diagnostic, capsys):
    monkeypatch.setattr(codex_hook, "handle_event", fail_private)
    assert codex_hook.main(["prompt"], stdin=io.StringIO("{}")) == 0
    record = diagnostic[-1]
    assert record["exception_class"] == "PermissionError"
    assert record["source_basename"] == "test_collection_failure_diagnostics.py"
    assert record["source_function"] == "fail_private"
    assert isinstance(record["source_line"], int)
    assert CANARY not in json.dumps(diagnostic) + str(capsys.readouterr())


def test_prompt_preparation_original_exception(monkeypatch, tmp_path, capsys):
    from kanban_adapter import conversation_runtime
    # 실제 안전 경로 해석기를 실행하고, 런타임 캡처는 아래에서 격리한다.
    monkeypatch.setenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", str(tmp_path / "synthetic.json"))
    monkeypatch.delenv("UNIFIED_KANBAN_DIAGNOSTIC_FD", raising=False)
    monkeypatch.setattr(HermesCliBackend, "resolve_board", lambda *a, **kw: "demo")
    monkeypatch.setattr(hook, "token_snapshot", lambda *a: {})
    monkeypatch.setattr(conversation_runtime, "capture_hook_start", fail_private)
    def adapter(args, cwd):
        fd = int(next(a.split("=", 1)[1] for a in args if a.startswith("--conversation-receipt-fd=")))
        os.write(fd, json.dumps({"board": "demo", "task": "t_12345678", "private": CANARY}).encode())
        return "t_12345678"
    with pytest.raises(PermissionError):
        hook.handle_event("prompt", {"session_id": "s", "cwd": str(tmp_path), "prompt": CANARY,
                          "transcript_path": str(tmp_path / CANARY)}, adapter=adapter, cache_dir=tmp_path / "cache")
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.err.splitlines()]
    record = next(r for r in records if r["error"] == "conversation-prepare-failed")
    assert record["exception_class"] == "PermissionError"
    assert record["source_function"] == "fail_private"
    assert CANARY not in captured.out + captured.err


@pytest.mark.parametrize("missing", ["receipt", "prepared"])
def test_enabled_preserve_missing_is_observable(monkeypatch, tmp_path, diagnostic, missing):
    # 상태가 없으면 이 합성 설정의 절대 경로를 읽기 전에 반환한다.
    monkeypatch.setenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", str(tmp_path / "synthetic.json"))
    state = {"observation_receipt": {}, "conversation_prepared": {}}
    state.pop("observation_receipt" if missing == "receipt" else "conversation_prepared")
    assert hook._preserve_conversation(state, "p", source="codex", cache=tmp_path) is True
    assert diagnostic[-1]["error"] == "conversation-" + missing + "-missing"


def test_preserve_error_is_sanitized(monkeypatch, tmp_path, diagnostic):
    from kanban_adapter import codex_pending_final, conversation_runtime
    monkeypatch.setattr(conversation_runtime, "get_conversation_service", lambda: object())
    monkeypatch.setattr(codex_pending_final, "enqueue", fail_private)
    state = {"task_id": "t_12345678", "observation_receipt": {"board": "demo"},
             "conversation_prepared": {"mode": "codex-file-provenance-v1", "turn_id": "p"}}
    assert hook._preserve_conversation(state, "p", source="codex", cache=tmp_path) is False
    assert diagnostic[-1]["error"] == "conversation-preserve-failed"
    assert diagnostic[-1]["exception_class"] == "PermissionError"
    assert CANARY not in json.dumps(diagnostic)


@pytest.mark.parametrize("missing", ["transcript", "prepared", "absent"])
def test_prompt_missing_is_observable(monkeypatch, tmp_path, capsys, missing):
    from kanban_adapter import conversation_runtime
    # 실제 안전 경로 해석기를 실행하고, 런타임 캡처는 아래에서 격리한다.
    monkeypatch.setenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", str(tmp_path / "synthetic.json"))
    monkeypatch.delenv("UNIFIED_KANBAN_DIAGNOSTIC_FD", raising=False)
    monkeypatch.setattr(HermesCliBackend, "resolve_board", lambda *a, **kw: "demo")
    monkeypatch.setattr(hook, "token_snapshot", lambda *a: {})
    monkeypatch.setattr(conversation_runtime, "capture_hook_start", lambda **kw: None)
    def adapter(args, cwd):
        fd = int(next(a.split("=", 1)[1] for a in args if a.startswith("--conversation-receipt-fd=")))
        os.write(fd, b'{"board":"demo","task":"t_12345678"}')
        return "t_12345678"
    payload = {"session_id": "s", "cwd": str(tmp_path), "prompt": CANARY}
    if missing != "transcript":
        payload["transcript_path"] = str(tmp_path / CANARY)
    if missing == "absent":
        def absent(**kwargs):
            raise FileNotFoundError(CANARY)
        monkeypatch.setattr(conversation_runtime, "capture_hook_start", absent)
        monkeypatch.setattr(conversation_runtime, "get_conversation_service", lambda: None)
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=tmp_path / "cache")
    captured = capsys.readouterr()
    if missing == "absent":
        assert '"exception_class": "FileNotFoundError"' in captured.err
        assert "conversation-prepare-failed" in captured.err
    else:
        assert "conversation-" + missing + "-missing" in captured.err
    assert CANARY not in captured.out + captured.err
