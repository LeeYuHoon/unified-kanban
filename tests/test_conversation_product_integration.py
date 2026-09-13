from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import pytest

from kanban_adapter.conversation import (
    CollectionPolicyV1,
    SourceBoundaryV1,
    SourceIdentityV1,
    SourceLocator,
)
from kanban_adapter.conversation_integration import (
    BindingStore,
    ConversationService,
    OwnerPolicyFile,
    ProductionConversationAuthority,
    VerifiedInteractivePrincipal,
)


def private_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)


def task_receipt(secret: bytes, task: str) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "hermes-kanban-observation-receipt-v1",
        "issuer": "hermes-kanban-kernel",
        "key_id": hashlib.sha256(secret).hexdigest()[:24],
        "board": "demo",
        "task": task,
        "observation": True,
        "created_at_ns": 100_000_000_000,
        "nonce": "ab" * 16,
    }
    wire = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return {
        **body,
        "auth_tag": hmac.new(secret, b"observation-receipt\0" + wire, hashlib.sha256).hexdigest(),
    }


def test_production_authority_requires_signed_kernel_receipt() -> None:
    kernel_secret = b"k" * 32
    authority_secret = b"a" * 32
    receipt = ProductionConversationAuthority.issue_kernel_receipt(
        kernel_secret=kernel_secret,
        issuer_id="hermes-kanban-kernel",
        key_id="test-key",
        now_ns=100,
    )

    authority = ProductionConversationAuthority.bootstrap(
        authority_secret=authority_secret,
        kernel_secret=kernel_secret,
        receipt=receipt,
    )
    assert authority.issuer_id == "hermes-kanban-kernel"

    with pytest.raises(PermissionError):
        ProductionConversationAuthority.bootstrap(
            authority_secret=authority_secret,
            kernel_secret=b"x" * 32,
            receipt=receipt,
        )


def test_owner_policy_defaults_deny_and_uses_generation_cas(tmp_path: Path) -> None:
    store = OwnerPolicyFile(tmp_path / "policy.json", secret=b"p" * 32)
    assert store.load().enabled_boards == {}

    enabled = CollectionPolicyV1(
        version=1,
        generation=2,
        activated_at_ns=100,
        expires_at_ns=10_000,
        enabled_boards={"demo": frozenset({"codex"})},
    )
    store.replace(enabled, expected_generation=1)
    assert store.load().allows("demo", "codex", 1, 101)

    with pytest.raises(RuntimeError, match="generation CAS"):
        store.replace(enabled, expected_generation=1)


def test_binding_store_is_content_free_and_rejects_foreign_mac(tmp_path: Path) -> None:
    source = tmp_path / "source" / "rollout.jsonl"
    private_file(source, b'{"type":"event_msg"}\n')
    info = source.stat()
    identity = SourceIdentityV1.from_stat(info)
    locator = SourceLocator("codex", source.parent, Path(source.name), os.getuid())
    authority = ProductionConversationAuthority.for_test(kernel_secret=b"k" * 32)
    policy = authority.create_policy_store()
    policy.replace(CollectionPolicyV1(
        version=1, generation=1, activated_at_ns=1, expires_at_ns=10_000,
        enabled_boards={"demo": frozenset({"codex"})},
    ))
    pending = authority.begin_binding(
        board="demo", task="t_12345678", provider="codex", schema_pin="openai/codex@rust-v0.145.0",
        profile_root_identity=SourceIdentityV1.from_stat(source.parent.stat()), locator=locator,
        source_identity=identity, session="private-session", turn_start=SourceBoundaryV1(0, 0),
        binding_version=1, generation=1, policy_version=1,
        producer_execution="execution-1", now_ns=2,
    )
    binding = authority.seal_binding(
        pending, turn_end=SourceBoundaryV1(identity.size, 1), source_identity=identity,
        expected_generation=1, now_ns=3,
    )
    store = BindingStore(tmp_path / "bindings", secret=b"b" * 32)
    store.put(binding=binding, locator=locator)

    raw = next((tmp_path / "bindings").glob("*.json")).read_text()
    assert "event_msg" not in raw
    assert str(source) not in raw
    assert "private-session" in raw
    loaded_binding, loaded_locator = store.get("demo", "t_12345678")
    assert loaded_binding == binding
    assert loaded_locator == locator

    payload = json.loads(raw)
    payload["mac"] = "bs_" + "0" * 64
    next((tmp_path / "bindings").glob("*.json")).write_text(json.dumps(payload))
    with pytest.raises(PermissionError):
        store.get("demo", "t_12345678")


def test_loopback_process_token_is_not_an_interactive_principal() -> None:
    with pytest.raises(PermissionError):
        VerifiedInteractivePrincipal.create(
            principal_id="local-process",
            auth_method="loopback_process_token",
            board_grants=frozenset({"demo"}),
            authenticated_at_ns=time.time_ns(),
        )

    principal = VerifiedInteractivePrincipal.create(
        principal_id="owner-session",
        auth_method="oauth_session",
        board_grants=frozenset({"demo"}),
        authenticated_at_ns=time.time_ns(),
    )
    assert principal.authorizes("demo")


def test_authenticated_service_projects_exact_bound_source_and_replays(tmp_path: Path) -> None:
    source = tmp_path / "source" / "rollout.jsonl"
    records = [
        {"timestamp": "2026-09-11T08:00:00Z", "ordinal": 1, "type": "event_msg",
         "payload": {"type": "user_message", "message": "hello user@example.com"}},
        {"timestamp": "2026-09-11T08:00:01Z", "ordinal": 2, "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer", "message": "done"}},
    ]
    private_file(source, b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records
    ))
    identity = SourceIdentityV1.from_stat(source.stat())
    locator = SourceLocator("codex", source.parent, Path(source.name), os.getuid())
    authority = ProductionConversationAuthority.for_test(kernel_secret=b"k" * 32)
    pending = authority.begin_binding(
        board="demo", task="t_12345678", provider="codex", schema_pin="openai/codex@rust-v0.145.0",
        profile_root_identity=SourceIdentityV1.from_stat(source.parent.stat()), locator=locator,
        source_identity=identity, session="private-session", turn_start=SourceBoundaryV1(0, 1),
        binding_version=1, generation=1, policy_version=1,
        producer_execution="execution-1", now_ns=100,
    )
    binding = authority.seal_binding(
        pending, turn_end=SourceBoundaryV1(identity.size, 3), source_identity=identity,
        expected_generation=1, now_ns=101,
    )
    policies = OwnerPolicyFile(tmp_path / "private" / "policy.json", secret=b"p" * 32)
    policies.replace(CollectionPolicyV1(
        version=1, generation=2, activated_at_ns=99, expires_at_ns=10_000,
        enabled_boards={"demo": frozenset({"codex"})},
    ), expected_generation=1)
    bindings = BindingStore(tmp_path / "private" / "bindings", secret=b"b" * 32)
    bindings.put(binding=binding, locator=locator)
    membership_calls = []
    service = ConversationService(
        authority=authority, policies=policies, bindings=bindings,
        task_membership=lambda board, task: membership_calls.append((board, task)) or True,
        principal_board_grants={"owner-1": frozenset({"demo"})},
        clock_ns=lambda: 200,
    )

    page = service.get_parent_page(
        principal_id="owner-1", board="demo", task="t_12345678", cursor=None, limit=1,
    )
    assert page["events"][0]["kind"] == "user_message"
    assert "user@example.com" not in json.dumps(page)
    second = service.get_parent_page(
        principal_id="owner-1", board="demo", task="t_12345678",
        cursor=page["next_cursor"], limit=1,
    )
    assert second["events"][0]["kind"] == "final_assistant"
    assert membership_calls == [("demo", "t_12345678"), ("demo", "t_12345678")]


def test_hook_receipt_to_sealed_binding_to_authenticated_reload(tmp_path: Path) -> None:
    authority = ProductionConversationAuthority.for_test(kernel_secret=b"k" * 32)
    policy_file = OwnerPolicyFile(tmp_path / "policy.json", secret=b"p" * 32)
    policy_file.replace(
        CollectionPolicyV1(
            version=1,
            generation=2,
            activated_at_ns=100,
            expires_at_ns=10_000,
            enabled_boards={"demo": frozenset({"codex"})},
        ),
        expected_generation=1,
    )
    kernel_secret = b"k" * 32
    service = ConversationService(
        authority=authority,
        policies=policy_file,
        bindings=BindingStore(tmp_path / "bindings", secret=b"b" * 32),
        task_membership=lambda board, task: board == "demo" and task == "t_12345678",
        principal_board_grants={"owner": frozenset({"demo"})},
        kernel_secret=kernel_secret,
        provider_roots={"codex": tmp_path / "source"},
        clock_ns=lambda: 200,
    )
    source_root = tmp_path / "source"
    source_root.mkdir(mode=0o700)
    source = source_root / "rollout.jsonl"
    private_file(
        source,
        (json.dumps({"type": "session_meta", "payload": {"id": "session-native"}}) + "\n").encode(),
    )
    receipt = task_receipt(kernel_secret, "t_12345678")
    prepared = service.capture_hook_start(
        board="demo",
        task="t_12345678",
        provider="codex",
        session="session-native",
        source_path=source,
        task_receipt=receipt,
    )
    with source.open("ab") as stream:
        stream.write((json.dumps({
            "timestamp": "2026-09-11T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello"},
        }) + "\n").encode())
    assert service.seal_hook_binding(
        board="demo",
        task="t_12345678",
        prepared=prepared,
        task_receipt=receipt,
    ) is not None
    assert service.seal_hook_binding(
        board="demo",
        task="t_12345678",
        prepared=prepared,
        task_receipt=receipt,
    ) is not None
    first = service.get_parent_page(
        principal_id="owner", board="demo", task="t_12345678", cursor=None, limit=10
    )
    second = service.get_parent_page(
        principal_id="owner", board="demo", task="t_12345678", cursor=None, limit=10
    )
    assert first == second
    assert first["source_availability"] == "available", first
    events = first["events"]
    assert isinstance(events, list)
    assert events, first
    assert events[0]["text"] == "hello"


def test_membership_denial_happens_before_binding_source_access(tmp_path: Path) -> None:
    authority = ProductionConversationAuthority.for_test(kernel_secret=b"k" * 32)
    service = ConversationService(
        authority=authority,
        policies=OwnerPolicyFile(tmp_path / "policy.json", secret=b"p" * 32),
        bindings=BindingStore(tmp_path / "missing", secret=b"b" * 32),
        task_membership=lambda _board, _task: False,
        principal_board_grants={"owner-1": frozenset({"demo"})},
    )
    with pytest.raises(PermissionError, match="membership"):
        service.get_parent_page(
            principal_id="owner-1", board="demo", task="t_12345678", cursor=None, limit=10,
        )
    assert not (tmp_path / "missing").exists()
