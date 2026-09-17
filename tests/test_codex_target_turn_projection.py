"""인증된 목표 턴 계약은 기존 공개 투영과 별도로 검증한다."""
from dataclasses import asdict, replace
from pathlib import Path
import hashlib
import json
import os

import pytest

from kanban_adapter import conversation as core
from kanban_adapter import transcript_projection as projection
from kanban_adapter.conversation_integration import (
    BindingStore, ConversationService, OwnerPolicyFile, ProductionConversationAuthority,
)


def message(turn="target", *, role="user", phase=None, text="same text"):
    payload = {
        "type": "message", "id": "message-id", "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        "internal_chat_message_metadata_passthrough": {
            "turn_id": turn, "create_time": 1, "content_item_kinds": ["text"],
        },
    }
    if phase is not None:
        payload["phase"] = phase
    return {"type": "response_item", "payload": payload}


def setup_scope(tmp_path, records, *, native=True, **overrides):
    source = tmp_path / "source" / "rollout.jsonl"
    source.parent.mkdir(mode=0o700, exist_ok=True)
    for i, record in enumerate(records, 70):
        record["ordinal"] = i
        record["timestamp"] = "2026-09-11T08:00:00Z"
    raw = b"".join(json.dumps(r).encode() + b"\n" for r in records)
    source.write_bytes(raw)
    source.chmod(0o600)
    identity = core.SourceIdentityV1.from_stat(source.stat())
    locator = core.SourceLocator("codex", source.parent, source.relative_to(source.parent), os.getuid())
    authority = ProductionConversationAuthority.for_test(kernel_secret=b"k" * 32)
    kwargs = dict(
        board="demo", task="task", provider="codex",
        schema_pin=core.CODEX_TARGET_TURN_SCHEMA_PIN if native else projection.CodexProjector.schema_pin,
        profile_root_identity=core.SourceIdentityV1.from_stat(source.parent.stat()), locator=locator,
        source_identity=identity, session="session", turn_start=core.SourceBoundaryV1(0, 70),
        binding_version=1, generation=1, policy_version=1, producer_execution="receipt-nonce", now_ns=100,
    )
    if native:
        kwargs["codex_target_turn_id"] = "target"
    kwargs.update(overrides)
    pending = authority.begin_binding(**kwargs)
    binding = authority.seal_binding(
        pending, turn_end=core.SourceBoundaryV1(len(raw), 70 + len(records)),
        source_identity=identity, expected_generation=1, now_ns=101,
        source_range_digest=hashlib.sha256(raw).hexdigest(),
    )
    policies = OwnerPolicyFile(tmp_path / "private" / "policy.json", secret=b"p" * 32)
    policies.replace(core.CollectionPolicyV1(
        version=1, generation=2, activated_at_ns=99, expires_at_ns=10_000,
        enabled_boards={"demo": frozenset({"codex"})},
    ), expected_generation=1)
    store = BindingStore(tmp_path / "private" / "bindings", secret=b"b" * 32)
    store.put(binding=binding, locator=locator)
    service = ConversationService(
        authority=authority, policies=policies, bindings=store,
        task_membership=lambda board, task: (board, task) == ("demo", "task"),
        principal_board_grants={"owner": frozenset({"demo"})}, clock_ns=lambda: 200,
    )
    return authority, pending, binding, locator, store, service


def test_authenticated_target_roundtrip_and_service_selects_exact_native_messages(tmp_path):
    records = [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "mirror"}},
        message("wrong"), message(),
        message(role="assistant", phase="analysis", text="private"),
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "call"}},
        message(),
        message("wrong", role="assistant", phase="final_answer"),
        message(role="assistant", phase="final_answer", text="done"),
        {"type": "event_msg", "payload": {"type": "agent_message", "phase": "final_answer", "message": "mirror"}},
    ]
    authority, pending, binding, locator, store, service = setup_scope(tmp_path, records)
    loaded, loaded_locator = store.get("demo", "task")
    assert loaded == binding and loaded_locator == locator
    assert pending.codex_target_turn_id == loaded.codex_target_turn_id == "target"
    authority._validate_binding(loaded)
    cursor = None
    events = []
    for _ in range(10):
        page = service.get_parent_page(principal_id="owner", board="demo", task="task", cursor=cursor, limit=1)
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert [(e["kind"], e["text"]) for e in events] == [
        ("user_message", "same text"), ("user_message", "same text"), ("final_assistant", "done"),
    ]
    assert len({e["event_id"] for e in events}) == 3
    assert "target" not in json.dumps(events)
    assert [e["seq"] for e in events] == [0, 1, 2]
    with pytest.raises(PermissionError):
        authority._validate_binding(replace(loaded, codex_target_turn_id="wrong"))


@pytest.mark.parametrize("updates", [
    {"codex_target_turn_id": None}, {"codex_target_turn_id": ""},
    {"codex_target_turn_id": 1}, {"codex_target_turn_id": True},
    {"codex_target_turn_id": "bad\x00id"},
    {"schema_pin": projection.CodexProjector.schema_pin}, {"provider": "claude"},
])
def test_incomplete_or_cross_contract_target_authority_rejected(tmp_path, updates):
    with pytest.raises(ValueError):
        setup_scope(tmp_path, [message()], **updates)


@pytest.mark.parametrize("updates", [
    {"turn_start": core.SourceBoundaryV1(0, None)},
    {"turn_end": core.SourceBoundaryV1(1, None)},
    {"source_range_digest": "0" * 64},
])
def test_native_binding_requires_global_ordinals_and_real_digest(tmp_path, updates):
    _, _, binding, _, _, _ = setup_scope(tmp_path, [message()])
    with pytest.raises(ValueError):
        replace(binding, **updates)


@pytest.mark.parametrize("role,phase", [
    ("assistant", None), ("assistant", "analysis"), ("assistant", "commentary"),
    ("system", "final_answer"), ("developer", None), ("tool", None), ("user", "analysis"),
])
def test_nonpublic_target_messages_dropped(tmp_path, role, phase):
    _, _, _, _, _, service = setup_scope(tmp_path, [message(role=role, phase=phase)])
    page = service.get_parent_page(principal_id="owner", board="demo", task="task", cursor=None, limit=10)
    assert not page["events"]


@pytest.mark.parametrize("where", ["outer", "payload", "metadata", "block"])
def test_unknown_matching_message_fields_never_publish(tmp_path, where):
    record = message()
    target = {"outer": record, "payload": record["payload"],
              "metadata": record["payload"]["internal_chat_message_metadata_passthrough"],
              "block": record["payload"]["content"][0]}[where]
    target["unknown_private_field"] = "PRIVATE"
    _, _, _, _, _, service = setup_scope(tmp_path, [record])
    page = service.get_parent_page(principal_id="owner", board="demo", task="task", cursor=None, limit=10)
    assert not page["events"]


@pytest.mark.parametrize("metadata", [None, {}, {"turn_id": "wrong"}, {"turn_id": None}, {"turn_id": True}])
def test_absent_or_wrong_target_metadata_cannot_fallback(tmp_path, metadata):
    record = message()
    record["payload"]["internal_chat_message_metadata_passthrough"] = metadata
    _, _, _, _, _, service = setup_scope(tmp_path, [record])
    page = service.get_parent_page(principal_id="owner", board="demo", task="task", cursor=None, limit=10)
    assert not page["events"]


def test_null_phase_user_is_public(tmp_path):
    record = message()
    record["payload"]["phase"] = None
    _, _, _, _, _, service = setup_scope(tmp_path, [record])
    page = service.get_parent_page(principal_id="owner", board="demo", task="task", cursor=None, limit=10)
    assert [e["kind"] for e in page["events"]] == ["user_message"]


@pytest.mark.parametrize("line_type,payload_type", [
    ("event_msg", "user_message"), ("event_msg", "agent_message"),
    ("event_msg", "item_completed"), ("response_item", "function_call"),
    ("response_item", "custom_tool_call"), ("response_item", "function_call_output"),
    ("response_item", "custom_tool_call_output"), ("response_item", "reasoning"),
    ("token_usage_record", "usage"),
])
def test_nonmessage_records_cannot_publish_even_with_matching_metadata(tmp_path, line_type, payload_type):
    record = message()
    record["type"] = line_type
    record["payload"].update(type=payload_type, phase="final_answer", message="PRIVATE", call_id="call", name="shell")
    _, _, _, _, _, service = setup_scope(tmp_path, [record])
    page = service.get_parent_page(principal_id="owner", board="demo", task="task", cursor=None, limit=10)
    assert not page["events"]


def test_binding_contract_downgrade_invalidates_authentication(tmp_path):
    authority, _, binding, _, _, _ = setup_scope(tmp_path, [message()])
    downgraded = replace(binding, schema_pin=projection.CodexProjector.schema_pin, codex_target_turn_id=None)
    with pytest.raises(PermissionError, match="binding receipt"):
        authority._validate_binding(downgraded)


def test_target_binding_cannot_use_legacy_projector(tmp_path):
    authority, _, binding, locator, _, service = setup_scope(tmp_path, [message()])
    policies = authority.create_policy_store()
    policies.replace(service.policies.load())
    principal = authority.issue_principal_scope(principal="owner", board="demo", task="task", expires_at_ns=1000)
    grant = authority.issue_projection_grant(policies, binding, principal, now_ns=200, ttl_ns=500)
    with pytest.raises(PermissionError, match="schema pin"):
        projection.project_page(locator, projection.CodexProjector(), binding=binding, grant=grant,
                                authority=authority, policy_store=policies, now_ns=200)


def legacy_fingerprint(module):
    # 파일 I/O가 없는 고정 합성 식별자로 변경 전 구현의 golden 값을 재현한다.
    authority = module.PrivateFixtureAuthority.create(b"legacy-test-secret" * 2, issuer_id="legacy-fixture")
    identity = module.SourceIdentityV1(1, 2, 100, 4)
    locator = module.SourceLocator("codex", Path("/synthetic"), Path("rollout.jsonl"), 501)
    pending = authority.begin_binding(
        board="demo", task="task", provider="codex", schema_pin=projection.CodexProjector.schema_pin,
        profile_root_identity=module.SourceIdentityV1(1, 3, 0, 4), locator=locator,
        source_identity=identity, session="session", turn_start=module.SourceBoundaryV1(0, 70),
        binding_version=1, generation=1, policy_version=1, producer_execution="nonce", now_ns=100,
    )
    binding = authority.seal_binding(pending, turn_end=module.SourceBoundaryV1(100, 73),
                                    source_identity=identity, expected_generation=1, now_ns=101)
    policies = authority.create_policy_store()
    policies.replace(module.CollectionPolicyV1(version=1, generation=1, activated_at_ns=99,
                     expires_at_ns=1000, enabled_boards={"demo": frozenset({"codex"})}))
    principal = authority.issue_principal_scope(principal="owner", board="demo", task="task", expires_at_ns=1000)
    grant = authority.issue_projection_grant(policies, binding, principal, now_ns=200, ttl_ns=500)
    cursor = authority.issue_cursor(binding, grant, offset=50, next_seq=1, last_ordinal=71, now_ns=200)
    payload = {"pending_receipt": pending.pending_receipt, "binding": binding.scope_dict(include_auth=True),
               "replay": binding.replay_scope().hex(), "grant": asdict(grant), "cursor": cursor,
               "event": module.opaque_event_id(binding, "50:71")}
    assert "codex_target_turn_id" not in payload["binding"]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_legacy_serialization_macs_event_ids_and_cursor_golden():
    # 변경 전 HEAD 12251c68a0773f5f33c5634945a353aa48df82fa에서 직접 실행한 결과다.
    assert legacy_fingerprint(core) == "fcea5b5622110e5466ca86c7ee05ec7335520da1454763335e79f8a3b1a3bc14"


def test_legacy_projector_service_behavior_and_cursor_still_work(tmp_path):
    records = [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "mirror"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "call"}},
        message("wrong", role="assistant", phase="final_answer"),
    ]
    _, pending, binding, _, store, service = setup_scope(tmp_path, records, native=False)
    assert pending.codex_target_turn_id is None
    assert "codex_target_turn_id" not in binding.scope_dict(include_auth=True)
    assert store.get("demo", "task")[0] == binding
    cursor = None
    events = []
    for _ in range(5):
        page = service.get_parent_page(principal_id="owner", board="demo", task="task", cursor=cursor, limit=1)
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert [e["kind"] for e in events] == ["user_message", "tool_call", "final_assistant"]


def test_target_tamper_after_valid_sidecar_mac_still_fails_authority(tmp_path):
    _, _, binding, locator, store, service = setup_scope(tmp_path, [message()])
    # 별도 sidecar 키를 가진 공격자도 binding MAC 권한은 얻지 못한다.
    store._path("demo", "task").unlink()
    store.put(binding=replace(binding, codex_target_turn_id="wrong"), locator=locator)
    with pytest.raises(PermissionError, match="binding receipt"):
        service.get_parent_page(principal_id="owner", board="demo", task="task", cursor=None, limit=10)
