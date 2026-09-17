"""부재 시작 준비는 최초 inode를 저장하고 final 전에는 발행하지 않는다."""
import json

import pytest

from kanban_adapter import claude_absent as absent
from test_claude_missing_transcript import PROMPT_ID, _native_turn
from test_conversation_integration_security_fixes import _receipt, _service


def setup_source(tmp_path):
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    source = root / "new-project" / "native.jsonl"
    service, kernel = _service(tmp_path, provider="claude", root=root)
    receipt = _receipt(kernel, board="demo", task="t_native", created_at=2)
    kwargs = dict(board="demo", task="t_native", task_receipt=receipt, prompt_id=PROMPT_ID)
    proof = absent.capture(service, session="native", source_path=source, **kwargs)
    _native_turn(source)
    first, final = source.read_bytes().splitlines(keepends=True)
    source.write_bytes(first)
    return source, service, kwargs, proof, final


def test_final_pin_cannot_downgrade_to_legacy_request_only_seal(tmp_path):
    source, service, kwargs, proof, final = setup_source(tmp_path)
    pin = absent.prepare_final(service, prepared=proof, **kwargs)
    with pytest.raises(PermissionError, match="mode"):
        absent.seal(service, prepared=pin, **kwargs)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


def test_request_first_open_then_same_inode_final(tmp_path):
    source, service, kwargs, proof, final = setup_source(tmp_path)
    assert callable(getattr(absent, "prepare_final", None)), "missing authenticated first-open preparation API"
    pin = absent.prepare_final(service, prepared=proof, **kwargs)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")
    assert absent.seal_final(service, prepared=pin, **kwargs) == {"status": "pending", "binding": None}
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")
    with source.open("ab") as stream:
        stream.write(final)
    result = absent.seal_final(service, prepared=json.loads(json.dumps(pin)), **kwargs)
    assert result["status"] == "ready"
    assert service.bindings.get("demo", "t_native")[0] == result["binding"]
    page = service.get_parent_page(principal_id="owner", board="demo", task="t_native", cursor=None, limit=20)
    assert page["completeness"] == "partial"
    assert [(e["kind"], e["text"]) for e in page["events"]] == [
        ("user_message", "public new turn"), ("final_assistant", "public final answer")]

@pytest.mark.parametrize("failure", ["inode", "prefix", "ancestry", "policy", "receipt", "prompt", "mac", "mode", "membership", "hardlink", "permissions"])
def test_retry_rejects_changed_provenance_without_publish(tmp_path, monkeypatch, failure):
    import os
    from kanban_adapter.conversation import CollectionPolicyV1
    source, service, kwargs, proof, final = setup_source(tmp_path)
    pin = absent.prepare_final(service, prepared=proof, **kwargs)
    with source.open("ab") as stream:
        stream.write(final)
    if failure == "inode":
        source.rename(source.with_suffix(".old"))
        _native_turn(source)
    elif failure == "prefix":
        source.write_bytes(source.read_bytes().replace(b"public new turn", b"public OLD turn"))
    elif failure == "ancestry":
        source.parent.rename(source.parent.with_name("old-project"))
        _native_turn(source)
    elif failure == "policy":
        service.policies.replace(CollectionPolicyV1.disabled(version=2, generation=3), expected_generation=2)
    elif failure == "receipt":
        kwargs["task_receipt"] = {**kwargs["task_receipt"], "nonce": "other"}
    elif failure == "prompt":
        kwargs["prompt_id"] = "650e8400-e29b-41d4-a716-446655440000"
    elif failure in {"mac", "mode"}:
        pin[failure] = "tampered"
    elif failure == "membership":
        monkeypatch.setattr(service, "task_membership", lambda *args: False)
    elif failure == "hardlink":
        os.link(source, source.with_suffix(".link"))
    elif failure == "permissions":
        source.chmod(0o644)
    with pytest.raises((PermissionError, ValueError)):
        absent.seal_final(service, prepared=pin, **kwargs)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


@pytest.mark.parametrize("phase", ["first-open", "retry"])
@pytest.mark.parametrize("failure", ["parent", "prior-prompt", "session"])
def test_absent_initial_native_guards_survive_final_preparation(tmp_path, phase, failure):
    source, service, kwargs, proof, final = setup_source(tmp_path)
    request = source.read_bytes()
    if phase == "retry":
        source.write_bytes(b"")
        pin = absent.prepare_final(service, prepared=proof, **kwargs)
    if failure == "parent":
        request = request.replace(b'"parentUuid": null', b'"parentUuid": "prior"')
    elif failure == "prior-prompt":
        request = request.replace(PROMPT_ID.encode(), b"650e8400-e29b-41d4-a716-446655440000") + request
    elif failure == "session":
        request = request.replace(b'"native"', b'"foreign"')
    source.write_bytes(request + final)
    with pytest.raises(PermissionError):
        if phase == "first-open":
            absent.prepare_final(service, prepared=proof, **kwargs)
        else:
            absent.seal_final(service, prepared=pin, **kwargs)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


@pytest.mark.parametrize("partial", [False, True])
def test_published_binding_is_never_replaced_or_falsely_ready(tmp_path, partial):
    source, service, kwargs, proof, final = setup_source(tmp_path)
    pin = absent.prepare_final(service, prepared=proof, **kwargs)
    if partial:
        binding = absent.seal(service, prepared=proof, **kwargs)
    else:
        with source.open("ab") as stream:
            stream.write(final)
        binding = absent.seal_final(service, prepared=pin, **kwargs)["binding"]
    with source.open("ab") as stream:
        stream.write(final)
    with pytest.raises(PermissionError, match="immutable"):
        absent.seal_final(service, prepared=pin, **kwargs)
    with pytest.raises(PermissionError, match="immutable"):
        absent.prepare_final(service, prepared=proof, **kwargs)
    assert service.bindings.get("demo", "t_native")[0] == binding


@pytest.mark.parametrize("prefix_length", [0, 15])
def test_first_open_before_complete_request_stays_pinned(tmp_path, prefix_length):
    source, service, kwargs, proof, final = setup_source(tmp_path)
    request = source.read_bytes()
    source.write_bytes(request[:prefix_length])
    pin = absent.prepare_final(service, prepared=proof, **kwargs)
    assert absent.seal_final(service, prepared=pin, **kwargs)["status"] == "pending"
    with source.open("ab") as stream:
        stream.write(request[prefix_length:] + final)
    assert absent.seal_final(service, prepared=pin, **kwargs)["status"] == "ready"
