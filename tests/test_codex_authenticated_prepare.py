"""합성 원본과 실제 서비스로 준비/봉인 경로를 검증한다."""
import hashlib

import pytest

from kanban_adapter import codex_file_provenance as native
from test_codex_file_provenance import marker, row
from test_codex_target_turn_projection import message as public_message
from test_conversation_integration_security_fixes import _service, _receipt


def message(role, ordinal, turn="target"):
    record = public_message(turn, role=role, phase="final_answer" if role == "assistant" else None, text="public")
    return row(record["type"], record["payload"], ordinal)


def setup(tmp_path):
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    source = root / "rollout.jsonl"
    history = (row("session_meta", {"id": "native"}, 39)
               + marker("task_started", "old", 40) + marker("task_complete", "old", 41))
    source.write_bytes(history + marker("task_started", ordinal=42))
    source.chmod(0o600)
    service, key = _service(tmp_path, provider="codex", root=root)
    receipt = _receipt(key, board="demo", task="task", created_at=2)
    args = dict(board="demo", task="task", task_receipt=receipt, turn_id="target")
    return source, service, args, history


def append(source, raw):
    with source.open("ab") as stream:
        stream.write(raw)


def test_after_hook_request_pending_until_terminal_and_real_service_projection(tmp_path):
    source, service, args, history = setup(tmp_path)
    prepared = native.prepare(service, session="native", source_path=source, **args)
    assert native.seal(service, prepared=prepared, **args) == {"status": "pending", "binding": None}
    append(source, message("user", 43) + message("assistant", 44))
    assert native.seal(service, prepared=prepared, **args)["status"] == "pending"
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "task")
    append(source, marker("task_complete", ordinal=45))
    end = source.stat().st_size
    append(source, marker("task_started", "next", 46) + message("user", 47, "next"))
    result = native.seal(service, prepared=prepared, **args)
    assert result["status"] == "ready"
    binding = result["binding"]
    assert service.bindings.get("demo", "task")[0] == binding
    assert binding.codex_target_turn_id == "target"
    assert (binding.turn_start.byte_offset, binding.turn_start.ordinal) == (len(history), 42)
    assert (binding.turn_end.byte_offset, binding.turn_end.ordinal) == (end, 46)
    assert binding.source_range_digest == hashlib.sha256(source.read_bytes()[len(history):end]).hexdigest()
    page = service.get_parent_page(principal_id="owner", board="demo", task="task", cursor=None, limit=20)
    assert [(e["kind"], e["text"]) for e in page["events"]] == [("user_message", "public"), ("final_assistant", "public")]
    with pytest.raises(PermissionError, match="immutable"):
        native.seal(service, prepared=prepared, **args)
    assert service.bindings.get("demo", "task")[0] == binding


@pytest.mark.parametrize("attack", ["prefix", "inode", "ancestor", "receipt", "policy", "membership", "turn", "ordinal", "terminal"])
@pytest.mark.parametrize("terminal", [False, True])
def test_retry_revalidates_authority_and_original_source(tmp_path, attack, terminal):
    from kanban_adapter.conversation import CollectionPolicyV1
    source, service, args, _ = setup(tmp_path)
    prepared = native.prepare(service, session="native", source_path=source, **args)
    assert native.seal(service, prepared=prepared, **args)["status"] == "pending"
    append(source, message("user", 43) + message("assistant", 44))
    if terminal:
        append(source, marker("task_complete", ordinal=45))
    if attack == "prefix":
        source.write_bytes(source.read_bytes().replace(b'"old"', b'"bad"'))
    elif attack == "inode":
        raw = source.read_bytes()
        source.rename(source.with_suffix(".old"))
        source.write_bytes(raw)
        source.chmod(0o600)
    elif attack == "ancestor":
        moved = source.parent.with_name("moved")
        source.parent.rename(moved)
        source.parent.mkdir(mode=0o700)
        (moved / source.name).rename(source)
    elif attack == "receipt":
        args["task_receipt"] = {**args["task_receipt"], "auth_tag": "0" * 64}
    elif attack == "policy":
        service.policies.replace(CollectionPolicyV1.disabled(version=2, generation=3), expected_generation=2)
    elif attack == "membership":
        service.task_membership = lambda *_: False
    elif attack == "turn":
        args["turn_id"] = "wrong"
    elif attack == "ordinal":
        source.write_bytes(source.read_bytes().replace(b'"ordinal": 43', b'"ordinal": 73'))
    elif attack == "terminal":
        append(source, marker("task_complete", "wrong", 46 if terminal else 45))
        if terminal:
            source.write_bytes(source.read_bytes().replace(b'"task_complete", "turn_id": "target"', b'"task_complete", "turn_id": "wrong"'))
    with pytest.raises(PermissionError):
        native.seal(service, prepared=prepared, **args)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "task")


@pytest.mark.parametrize("field", ["mode", "schema_pin", "board", "task", "provider", "session", "turn_id", "allowed_root", "relative_path", "ancestors", "source_identity", "captured_eof", "prefix_digest", "start_offset", "start_ordinal", "receipt_nonce", "receipt_created_at_ns", "policy_generation", "policy_version"])
def test_preparation_fields_are_all_authenticated(tmp_path, field):
    source, service, args, _ = setup(tmp_path)
    prepared = native.prepare(service, session="native", source_path=source, **args)
    prepared[field] = "tampered"
    with pytest.raises(PermissionError, match="authentication"):
        native.seal(service, prepared=prepared, **args)


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_wrong_turn_message_cannot_satisfy_readiness(tmp_path, role):
    source, service, args, _ = setup(tmp_path)
    prepared = native.prepare(service, session="native", source_path=source, **args)
    append(source, message("user", 43, "wrong" if role == "user" else "target") +
           message("assistant", 44, "wrong" if role == "assistant" else "target") +
           marker("task_complete", ordinal=45) + marker("task_started", "next", 46) +
           message(role, 47))
    assert native.seal(service, prepared=prepared, **args)["status"] == "pending"


def test_unprojectable_final_must_not_publish_request_only_binding(tmp_path):
    import json
    source, service, args, _ = setup(tmp_path)
    prepared = native.prepare(service, session="native", source_path=source, **args)
    bad = json.loads(message("assistant", 44))
    bad["payload"]["unknown_private"] = "private"
    append(source, message("user", 43) + json.dumps(bad).encode() + b"\n" + marker("task_complete", ordinal=45))
    assert native.seal(service, prepared=prepared, **args)["status"] == "pending"
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "task")


@pytest.mark.parametrize("phase", ["prepare", "seal"])
def test_replacement_during_snapshot_is_rejected(tmp_path, monkeypatch, phase):
    source, service, args, _ = setup(tmp_path)
    prepared = native.prepare(service, session="native", source_path=source, **args)
    original = native.parse_turn
    def replace(*a, **kw):
        result = original(*a, **kw)
        raw = source.read_bytes()
        source.rename(source.with_suffix(".old"))
        source.write_bytes(raw)
        source.chmod(0o600)
        return result
    monkeypatch.setattr(native, "parse_turn", replace)
    with pytest.raises(PermissionError):
        if phase == "prepare":
            native.prepare(service, session="native", source_path=source, **args)
        else:
            native.seal(service, prepared=prepared, **args)


def test_start_and_request_appended_after_prepare(tmp_path):
    source, service, args, history = setup(tmp_path)
    source.write_bytes(history)
    prepared = native.prepare(service, session="native", source_path=source, **args)
    append(source, marker("task_started", ordinal=42) + message("user", 43) +
           message("assistant", 44) + marker("task_complete", ordinal=45))
    assert native.seal(service, prepared=prepared, **args)["status"] == "ready"


def test_null_phase_request_supported_by_public_projector(tmp_path):
    import json
    source, service, args, _ = setup(tmp_path)
    request = json.loads(message("user", 43))
    request["payload"]["phase"] = None
    append(source, json.dumps(request).encode() + b"\n")
    prepared = native.prepare(service, session="native", source_path=source, **args)
    append(source, message("assistant", 44) + marker("task_complete", ordinal=45))
    assert native.seal(service, prepared=prepared, **args)["status"] == "ready"


@pytest.mark.parametrize("attack", ["receipt", "policy", "membership", "outside", "symlink"])
def test_prepare_rejects_untrusted_authority_and_source(tmp_path, attack):
    from kanban_adapter.conversation import CollectionPolicyV1
    source, service, args, _ = setup(tmp_path)
    if attack == "receipt":
        args["task_receipt"] = {**args["task_receipt"], "auth_tag": "0" * 64}
    elif attack == "policy":
        service.policies.replace(CollectionPolicyV1.disabled(version=2, generation=3), expected_generation=2)
    elif attack == "membership":
        service.task_membership = lambda *_: False
    elif attack == "outside":
        source = tmp_path / "outside.jsonl"
        source.write_bytes(b"")
        source.chmod(0o600)
    elif attack == "symlink":
        original = source.with_suffix(".old")
        source.rename(original)
        source.symlink_to(original)
    with pytest.raises((PermissionError, ValueError, OSError)):
        native.prepare(service, session="native", source_path=source, **args)


def test_partial_terminal_retries_and_sealed_terminal_digest(tmp_path):
    source, service, args, _ = setup(tmp_path)
    prepared = native.prepare(service, session="native", source_path=source, **args)
    append(source, message("user", 43) + message("assistant", 44) + marker("task_complete", ordinal=45)[:-1])
    assert native.seal(service, prepared=prepared, **args)["status"] == "pending"
    append(source, b"\n")
    binding = native.seal(service, prepared=prepared, **args)["binding"]
    source.write_bytes(source.read_bytes().replace(b'"task_complete", "turn_id": "target"', b'"task_complete", "turn_id": "wrong!"'))
    page = service.get_parent_page(principal_id="owner", board="demo", task="task", cursor=None, limit=20)
    assert page["events"] == [] and page["source_availability"] == "rotated"
    assert service.bindings.get("demo", "task")[0] == binding
