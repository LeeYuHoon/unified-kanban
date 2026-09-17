"""이전 요청의 인증된 공개 final만 카드 완료 결과로 전달한다."""
import json

import pytest

from kanban_adapter import claude_hook as hook
from test_superseded_final_lifecycle import scenario
from test_codex_authenticated_prepare import append


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
@pytest.mark.parametrize("delayed", [False, True])
def test_superseded_card_gets_exact_final(tmp_path, monkeypatch, provider, delayed):
    source, service, commands, cache, send, final, next_request, next_final, queue, root, texts = scenario(tmp_path, monkeypatch, provider)
    results = []
    original = hook._complete

    def complete(*args, adapter, **kwargs):
        def capture(argv, cwd):
            if argv[0] == "done":
                path = next(a.split("=", 1)[1] for a in argv if a.startswith("--result-file="))
                with open(path) as stream:
                    results.append((argv[argv.index("--task") + 1], stream.read()))
            return adapter(argv, cwd)
        return original(*args, adapter=capture, **kwargs)

    monkeypatch.setattr(hook, "_complete", complete)
    send("prompt")
    if not delayed:
        append(source, final + next_request + next_final)
    send("prompt", "next")
    if delayed:
        # 완료 뒤 지연된 final은 대화만 복구하며 이미 완료된 카드를 다시 쓰지 않는다.
        expected = [("t_1", "Superseded by a new user prompt after a missing Stop event")]
        assert results == expected
        append(source, final + next_request + next_final)
        job, = (cache / root).glob("*.json")
        assert queue.run_once(service, job) == "ready"
        assert results == expected
        page = service.get_parent_page(principal_id="owner", board="demo", task="t_1", cursor=None, limit=20)
        assert [e["text"] for e in page["events"]] == texts
        return
    assert results == [("t_1", texts[-1])]


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_hook_final_does_not_store_truncated_projection(tmp_path, monkeypatch, provider):
    source, service, commands, cache, send, final, next_request, next_final, queue, root, texts = scenario(tmp_path, monkeypatch, provider)
    send("prompt")
    state = json.loads(hook._state_path(cache, "native").read_bytes())
    append(source, final + next_request)
    send("prompt", "next")
    monkeypatch.setattr(service, "_project_parent_page", lambda **kw: {
        "events": [{"kind": "final_assistant", "text": "clipped", "redaction": "partially_redacted"}],
        "next_cursor": None,
    })
    assert service.get_hook_final(board="demo", task="t_1", task_receipt=state["observation_receipt"]) is None


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_hook_final_rejects_binding_changed_between_auth_and_projection(tmp_path, monkeypatch, provider):
    from dataclasses import replace
    source, service, commands, cache, send, final, next_request, next_final, queue, root, texts = scenario(tmp_path, monkeypatch, provider)
    send("prompt")
    state = json.loads(hook._state_path(cache, "native").read_bytes())
    append(source, final + next_request)
    send("prompt", "next")
    binding, locator = service.bindings.get("demo", "t_1")
    calls = []

    def get(board, task):
        calls.append(task)
        return (binding if len(calls) == 1 else replace(binding, producer_execution="foreign")), locator

    monkeypatch.setattr(service.bindings, "get", get)
    projected = []
    import kanban_adapter.conversation_integration as integration
    original = integration.project_page
    monkeypatch.setattr(integration, "project_page", lambda *a, **kw: projected.append(True) or original(*a, **kw))
    with pytest.raises(PermissionError, match="hook binding changed"):
        service.get_hook_final(board="demo", task="t_1", task_receipt=state["observation_receipt"])
    assert not projected


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
@pytest.mark.parametrize("attack", ["receipt", "membership", "source", "execution"])
def test_hook_final_rejects_unauthenticated_result(tmp_path, monkeypatch, provider, attack):
    from test_conversation_integration_security_fixes import _receipt
    source, service, commands, cache, send, final, next_request, next_final, queue, root, texts = scenario(tmp_path, monkeypatch, provider)
    send("prompt")
    state = json.loads(hook._state_path(cache, "native").read_bytes())
    append(source, final + next_request)
    send("prompt", "next")
    receipt = state["observation_receipt"]
    if attack == "receipt":
        receipt["nonce"] = "foreign"
    elif attack == "membership":
        monkeypatch.setattr(service, "task_membership", lambda *args: False)
    elif attack == "source":
        raw = source.read_bytes()
        source.rename(source.with_suffix(".old"))
        source.write_bytes(raw)
        source.chmod(0o600)
    else:
        receipt = _receipt(b"k" * 32, board="demo", task="t_1", created_at=3, nonce="foreign")
    try:
        result = service.get_hook_final(board="demo", task="t_1", task_receipt=receipt)
    except (PermissionError, FileNotFoundError):
        result = None
    assert result is None


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_supersede_retry_upgrades_saved_fallback(tmp_path, monkeypatch, provider):
    source, service, commands, cache, send, final, next_request, next_final, queue, root, texts = scenario(tmp_path, monkeypatch, provider)
    original = hook._complete
    results = []

    def complete(*args, adapter, **kwargs):
        def capture(argv, cwd):
            if argv[0] == "done":
                path = next(a.split("=", 1)[1] for a in argv if a.startswith("--result-file="))
                with open(path) as stream:
                    results.append(stream.read())
                if len(results) == 1:
                    raise RuntimeError("테스트 완료 실패")
            return adapter(argv, cwd)
        return original(*args, adapter=capture, **kwargs)

    monkeypatch.setattr(hook, "_complete", complete)
    send("prompt")
    with pytest.raises(RuntimeError, match="테스트 완료 실패"):
        send("prompt", "next")
    append(source, final + next_request + next_final)
    send("prompt", "next")
    assert results[-1] == texts[-1]
