"""새 요청이 이전 Stop보다 빠를 때 인증된 수집 증거를 보존한다."""
import json
import os

import pytest

from kanban_adapter import claude_hook as hook
from kanban_adapter import claude_pending_final, codex_pending_final
from test_claude_file_provenance_v2 import _setup, _history, _request, _final, _row
from test_codex_pending_final import harness
from test_codex_authenticated_prepare import append, message
from test_codex_file_provenance import marker
from test_conversation_integration_security_fixes import _receipt


def scenario(tmp_path, monkeypatch, provider):
    """두 카드와 동일 문장/상이한 네이티브 ID를 실제 인증 서비스에 연결한다."""
    if provider == "codex":
        source, service, commands, cache, send = harness(tmp_path, monkeypatch)
        final = message("user", 43) + message("assistant", 44) + marker("task_complete", ordinal=45)
        next_request = marker("task_started", "next", 46) + message("user", 47, "next")
        next_final = (message("assistant", 48, "next").replace(b'"public"', b'"B final must stay separate"')
                      + marker("task_complete", "next", 49))
        queue, root = codex_pending_final, "codex-pending-final"
        texts = ["public", "public"]
    else:
        source, service, _, _, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
        commands, tasks = [], {}
        def adapter(argv, cwd):
            commands.append(argv)
            if argv[0] == "start":
                key = argv[argv.index("--idempotency-key") + 1]
                task = tasks.setdefault(key, "t_" + str(len(tasks) + 1))
                receipt = _receipt(b"k" * 32, board="demo", task=task, created_at=2)
                fd = int(next(a.split("=", 1)[1] for a in argv if a.startswith("--conversation-receipt-fd=")))
                os.write(fd, json.dumps(receipt).encode())
                return task
            return ""
        final = _final()
        # 과거와도 겹치지 않는 새 요청 ID를 사용한다.
        next_id = "750e8400-e29b-41d4-a716-446655440000"
        def send(event, turn="target"):
            data = dict(payload, prompt_id=payload["prompt_id"] if turn == "target" else next_id,
                        last_assistant_message="untrusted late Stop A text")
            hook.handle_event(event, data, adapter=adapter, cache_dir=cache)
        next_request = _row("user", "b-u", "new-a", "same request", next_id)
        next_final = _row("assistant", "b-a", "b-u", "B final must stay separate")
        queue, root = claude_pending_final, "pending-final"
        texts = ["same request", "current final"]
    launches = []
    monkeypatch.setattr(queue, "launch", lambda path: launches.append(path))
    return source, service, commands, cache, send, final, next_request, next_final, queue, root, texts


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_prompt_b_before_stop_a_preserves_exact_authenticated_final(tmp_path, monkeypatch, provider):
    source, service, commands, cache, send, final, next_request, next_final, queue, root, texts = scenario(tmp_path, monkeypatch, provider)
    send("prompt")
    state_path = hook._state_path(cache, "native")
    original = json.loads(state_path.read_bytes())
    append(source, final + next_request)
    send("prompt", "next")
    current = state_path.read_bytes()
    assert json.loads(current)["task_id"] == "t_2"
    append(source, next_final)
    send("stop")
    assert state_path.read_bytes() == current
    assert [c[c.index("--task") + 1] for c in commands if c[0] == "done"] == ["t_1"]
    jobs = list((cache / root).glob("*.json"))
    assert len(jobs) == 1, "새 요청은 A 준비/영수증을 지우기 전에 인증된 수집 작업을 남겨야 한다"
    body = json.loads(jobs[0].read_bytes())
    assert body["kwargs"]["prepared"] == original["conversation_prepared"]
    assert body["kwargs"]["task_receipt"] == original["observation_receipt"]
    assert queue.run_once(service, jobs[0]) == "ready"
    binding = service.bindings.get("demo", "t_1")
    page = service.get_parent_page(principal_id="owner", board="demo", task="t_1", cursor=None, limit=20)
    assert [e["text"] for e in page["events"]] == texts
    send("stop")
    assert service.bindings.get("demo", "t_1") == binding
    assert state_path.read_bytes() == current
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_2")


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_superseded_pending_keeps_old_authority_until_delayed_final(tmp_path, monkeypatch, provider):
    source, service, commands, cache, send, final, next_request, next_final, queue, root, texts = scenario(tmp_path, monkeypatch, provider)
    send("prompt")
    state_path = hook._state_path(cache, "native")
    original = json.loads(state_path.read_bytes())
    send("prompt", "next")
    current = state_path.read_bytes()
    assert json.loads(current)["task_id"] == "t_2"
    job, = (cache / root).glob("*.json")
    body = json.loads(job.read_bytes())
    assert body["status"] == "pending"
    assert body["kwargs"]["prepared"] == original["conversation_prepared"]
    assert body["kwargs"]["task_receipt"] == original["observation_receipt"]
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_1")
    append(source, final + next_request + next_final)
    send("stop")
    assert state_path.read_bytes() == current
    assert queue.run_once(service, job) == "ready"
    page = service.get_parent_page(principal_id="owner", board="demo", task="t_1", cursor=None, limit=20)
    assert [e["text"] for e in page["events"]] == texts
    assert [c[c.index("--task") + 1] for c in commands if c[0] == "done"] == ["t_1"]


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
@pytest.mark.parametrize("attack", ["mac", "receipt", "inode", "downgrade", "unavailable", "launch", "expired"])
def test_supersede_cannot_bypass_failed_collection_authority(tmp_path, monkeypatch, provider, attack):
    from kanban_adapter import conversation_runtime as runtime
    source, service, commands, cache, send, final, next_request, next_final, queue, root, texts = scenario(tmp_path, monkeypatch, provider)
    send("prompt")
    state_path = hook._state_path(cache, "native")
    state = json.loads(state_path.read_bytes())
    if attack == "mac":
        state["conversation_prepared"]["prefix_digest"] = "0" * 64
    elif attack == "receipt":
        state["observation_receipt"]["nonce"] = "foreign"
    elif attack == "downgrade":
        state["conversation_prepared"]["mode"] = "legacy"
    elif attack == "inode":
        raw = source.read_bytes()
        source.rename(source.with_suffix(".old"))
        source.write_bytes(raw)
        source.chmod(0o600)
    elif attack == "unavailable":
        monkeypatch.setattr(runtime, "get_conversation_service", lambda: None)
    elif attack == "expired":
        monkeypatch.setattr(queue, "run_once", lambda *args: "expired")
    elif attack == "launch":
        def fail_launch(path):
            raise OSError("테스트 실행 실패")
        monkeypatch.setattr(queue, "launch", fail_launch)
    state_path.write_text(json.dumps(state))
    before = state_path.read_bytes()
    if attack != "launch":
        append(source, final + next_request)
    send("prompt", "next")
    assert state_path.read_bytes() == before
    assert [c[0] for c in commands].count("start") == 1
    assert not any(c[0] == "done" for c in commands)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_1")
