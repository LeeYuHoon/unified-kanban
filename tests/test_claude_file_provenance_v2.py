"""기존 파일의 네이티브 요청 경계를 실제 hook과 projection으로 검증한다."""
import json

import pytest

from kanban_adapter import claude_file_provenance as v2

from kanban_adapter import claude_hook as hook
from test_claude_missing_transcript import PROMPT_ID, _harness

OLD_ID = "650e8400-e29b-41d4-a716-446655440000"


def _row(kind, uuid, parent, text, prompt=None):
    row = {"type": kind, "sessionId": "native", "uuid": uuid, "parentUuid": parent,
           "message": {"role": kind, "content": text if kind == "user" else
                       [{"type": "text", "text": text}], "stop_reason": "end_turn"}}
    if prompt is not None:
        row["promptId"] = prompt
    return json.dumps(row).encode() + b"\n"


def _history():
    return (_row("user", "old-u", None, "same request", OLD_ID) +
            _row("assistant", "old-a", "old-u", "prior private final"))


def _request():
    return _row("user", "new-u", "old-a", "same request", PROMPT_ID)


def _final():
    return _row("assistant", "new-a", "new-u", "current final")


def _setup(tmp_path, monkeypatch, raw):
    values = _harness(tmp_path, monkeypatch)
    source, service, commands, adapter, payload, cache = values
    payload["prompt_id"] = PROMPT_ID
    payload["prompt"] = "same request"
    source.parent.mkdir(mode=0o700)
    source.write_bytes(raw)
    source.chmod(0o600)
    return values


def _page(service):
    return service.get_parent_page(principal_id="owner", board="demo", task="t_native", cursor=None, limit=20)


def test_after_hook_request_waits_at_authenticated_eof(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history())
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    state = json.loads(hook._state_path(cache, "native").read_text())
    prepared = state.get("conversation_prepared")
    assert prepared is not None
    assert prepared["start_offset"] == len(_history())
    assert prepared["request_uuid"] is None
    with source.open("ab") as stream:
        stream.write(_request() + _final())
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    assert [e["text"] for e in _page(service)["events"]] == ["same request", "current final"]


@pytest.mark.parametrize("phase", ["capture", "seal"])
def test_replacement_during_native_parse_never_publishes(tmp_path, monkeypatch, phase):
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    if phase == "seal":
        hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    original = v2._range
    def replace(*args):
        value = original(*args)
        raw = source.read_bytes()
        source.rename(source.with_suffix(".old"))
        source.write_bytes(raw)
        source.chmod(0o600)
        return value
    monkeypatch.setattr(v2, "_range", replace)
    if phase == "capture":
        with pytest.raises(PermissionError):
            hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    else:
        hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
        with pytest.raises(FileNotFoundError):
            service.bindings.get("demo", "t_native")


def test_before_hook_request_is_included_by_native_id(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    with source.open("ab") as stream:
        stream.write(_final())
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    binding, _ = service.bindings.get("demo", "t_native")
    assert binding.binding_version == 2
    assert binding.turn_start.byte_offset == len(_history())
    assert [e["text"] for e in _page(service)["events"]] == ["same request", "current final"]


@pytest.mark.parametrize("before", [True, False])
@pytest.mark.parametrize("attack", ["prefix", "inode", "ancestor", "parent", "prompt", "mac", "receipt", "policy", "downgrade"])
def test_uncertain_existing_provenance_never_binds(tmp_path, monkeypatch, before, attack):
    from kanban_adapter.conversation import CollectionPolicyV1
    source, service, commands, adapter, payload, cache = _setup(
        tmp_path, monkeypatch, _history() + (_request() if before else b""))
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    state_path = hook._state_path(cache, "native")
    state = json.loads(state_path.read_text())
    assert state["conversation_prepared"]["mode"] == v2.MODE
    with source.open("ab") as stream:
        stream.write((b"" if before else _request()) + _final())
    if attack == "prefix":
        source.write_bytes(source.read_bytes().replace(b"prior private final", b"other private final"))
    elif attack == "inode":
        raw = source.read_bytes()
        source.rename(source.with_suffix(".old"))
        source.write_bytes(raw)
        source.chmod(0o600)
    elif attack == "ancestor":
        old = source.parent.with_name("moved")
        source.parent.rename(old)
        source.parent.mkdir(mode=0o700)
        (old / source.name).rename(source)  # 파일 inode는 같아도 조상 교체를 거부한다
    elif attack == "parent":
        source.write_bytes(source.read_bytes().replace(b'"parentUuid": "new-u"', b'"parentUuid": "old-a"'))
    elif attack == "prompt":
        source.write_bytes(source.read_bytes().replace(PROMPT_ID.encode(), OLD_ID.encode()))
    elif attack == "mac":
        state["conversation_prepared"]["start_offset"] = 0
        state_path.write_text(json.dumps(state))
    elif attack == "downgrade":
        state["conversation_prepared"].pop("mode")
        state_path.write_text(json.dumps(state))
    elif attack == "receipt":
        state["observation_receipt"]["nonce"] = "changed"
        state_path.write_text(json.dumps(state))
    elif attack == "policy":
        service.policies.replace(CollectionPolicyV1.disabled(version=2, generation=3), expected_generation=2)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")
    # 거절된 인증 작업은 현재 카드와 재시도 상태를 보존한다.
    assert not any(command[0] == "done" for command in commands)
    assert state_path.exists()


@pytest.mark.parametrize("field", ["board", "task", "session", "prompt_id", "policy_version", "policy_generation", "ancestors", "source_identity", "prefix_digest", "request_uuid", "captured_eof", "receipt_nonce", "mode"])
def test_every_preparation_authority_field_is_authenticated(tmp_path, monkeypatch, field):
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    state = json.loads(hook._state_path(cache, "native").read_text())
    prepared = state["conversation_prepared"]
    prepared[field] = "tampered"
    with pytest.raises(PermissionError, match="authentication"):
        v2.seal(service, board="demo", task="t_native", prepared=prepared,
                task_receipt=state["observation_receipt"], prompt_id=PROMPT_ID)


def test_next_prompt_and_delayed_final_cannot_expand_immutable_binding(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    state = json.loads(hook._state_path(cache, "native").read_text())
    # 과거 request-only binding의 불변성은 명시적 legacy 봉인으로 재현한다.
    v2.seal(service, board="demo", task="t_native", prepared=state["conversation_prepared"],
            task_receipt=state["observation_receipt"], prompt_id=PROMPT_ID)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    binding, _ = service.bindings.get("demo", "t_native")
    assert [e["text"] for e in _page(service)["events"]] == ["same request"]
    with source.open("ab") as stream:
        stream.write(_final() + _row("user", "later-u", "new-a", "later request", "750e8400-e29b-41d4-a716-446655440000"))
    assert v2.seal(service, board="demo", task="t_native", prepared=state["conversation_prepared"],
                   task_receipt=state["observation_receipt"], prompt_id=PROMPT_ID) is None
    assert service.bindings.get("demo", "t_native")[0] == binding
    assert _page(service)["completeness"] == "partial"


def test_next_prompt_is_excluded_from_initial_seal(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    with source.open("ab") as stream:
        stream.write(_final() + _row("user", "later-u", "new-a", "later request", "750e8400-e29b-41d4-a716-446655440000"))
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    assert [e["text"] for e in _page(service)["events"]] == ["same request", "current final"]


def test_missing_request_never_selects_identical_prior_text(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history())
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_shared_legacy_eof_invariant_is_not_relaxed(tmp_path, provider):
    from test_conversation_integration_security_fixes import _service, _receipt
    source_root = tmp_path / "approved"
    source_root.mkdir(mode=0o700)
    source = source_root / "native.jsonl"
    raw = _history() if provider == "claude" else b'{"type":"session_meta","payload":{"id":"native"}}\n'
    source.write_bytes(raw)
    source.chmod(0o600)
    service, secret = _service(tmp_path, provider=provider, root=source_root)
    receipt = _receipt(secret, board="demo", task="t_native", created_at=2)
    prepared = service.capture_hook_start(board="demo", task="t_native", provider=provider,
        session="native", source_path=source, task_receipt=receipt)
    assert prepared["start_offset"] == len(raw)
    prepared["start_offset"] = 0
    with pytest.raises(PermissionError, match="captured source metadata"):
        service.seal_hook_binding(board="demo", task="t_native", prepared=prepared, task_receipt=receipt)
