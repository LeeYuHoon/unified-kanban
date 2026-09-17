"""격리된 서명 정책에서 보드/provider별 opt-in 시점을 검증한다."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kanban_adapter.conversation import CollectionPolicyV1, SourceBoundaryV1, SourceIdentityV1, SourceLocator
from kanban_adapter.conversation_integration import (
    BindingStore, ConversationService, OwnerPolicyFile, ProductionConversationAuthority,
)
from kanban_adapter.conversation_policy_cli import main
from test_conversation_product_integration import private_file


@pytest.fixture
def sealed(tmp_path):
    secret = tmp_path / "secret"
    private_file(secret, b"p" * 32)
    store = OwnerPolicyFile(tmp_path / "policy.json", secret=b"p" * 32)
    store.replace(CollectionPolicyV1(
        version=7, generation=2, activated_at_ns=99, expires_at_ns=10_000,
        enabled_boards={"demo": frozenset({"codex"})},
    ), expected_generation=1)
    source = tmp_path / "source" / "rollout.jsonl"
    records = [
        {"timestamp": "2026-09-11T08:00:00Z", "ordinal": 1, "type": "event_msg",
         "payload": {"type": "user_message", "message": "request"}},
        {"timestamp": "2026-09-11T08:00:01Z", "ordinal": 2, "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer", "message": "final"}},
    ]
    private_file(source, b"".join(json.dumps(row).encode() + b"\n" for row in records))
    identity = SourceIdentityV1.from_stat(source.stat())
    locator = SourceLocator("codex", source.parent, Path(source.name), os.getuid())
    authority = ProductionConversationAuthority.for_test(kernel_secret=b"k" * 32)
    pending = authority.begin_binding(
        board="demo", task="t_12345678", provider="codex", schema_pin="openai/codex@rust-v0.145.0",
        profile_root_identity=SourceIdentityV1.from_stat(source.parent.stat()), locator=locator,
        source_identity=identity, session="session", turn_start=SourceBoundaryV1(0, 1),
        binding_version=1, generation=1, policy_version=7, producer_execution="execution", now_ns=100,
    )
    binding = authority.seal_binding(pending, turn_end=SourceBoundaryV1(identity.size, 3),
                                     source_identity=identity, expected_generation=1, now_ns=101)
    bindings = BindingStore(tmp_path / "bindings", secret=b"b" * 32)
    bindings.put(binding=binding, locator=locator)
    service = ConversationService(authority=authority, policies=store, bindings=bindings,
        task_membership=lambda board, task: True, principal_board_grants={"owner": frozenset({"demo"})},
        clock_ns=lambda: 400, kernel_secret=b"k" * 32)
    def apply(*args):
        return main(["--policy-file", str(store.path), "--secret-file", str(secret), *args])
    def page(**args):
        return service.get_parent_page(principal_id="owner", board="demo", task="t_12345678",
                                       cursor=args.get("cursor"), limit=args.get("limit", 10))
    return store, bindings, service, apply, page


def test_add_board_preserves_sealed_binding(sealed):
    store, bindings, service, apply, page = sealed
    before = page()
    binding_bytes = bindings._path("demo", "t_12345678").read_bytes()
    assert apply("enable", "--board", "other", "--provider", "claude",
                 "--expires-at-ns", "10000", "--now-ns", "300") == 0
    assert page()["events"] == before["events"]
    assert bindings._path("demo", "t_12345678").read_bytes() == binding_bytes
    assert store.load().version == 7


def test_add_provider_rejects_pre_activation_card_before_source_open(sealed):
    from test_conversation_product_integration import task_receipt
    store, _, service, apply, _ = sealed
    assert apply("enable", "--board", "demo", "--provider", "claude",
                 "--expires-at-ns", "10000", "--now-ns", "300") == 0
    # 생성 시점도 MAC에 포함된 진짜 fixture 영수증을 쓴다.
    import hashlib
    import hmac
    receipt = task_receipt(b"k" * 32, "t_12345678")
    receipt["created_at_ns"] = 200
    body = {k: v for k, v in receipt.items() if k != "auth_tag"}
    receipt["auth_tag"] = hmac.new(b"k" * 32, b"observation-receipt\0" +
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    with pytest.raises(PermissionError, match="predates"):
        service.capture_hook_start(board="demo", task="t_12345678", provider="claude",
            session="session", source_path=Path("/nonexistent/source"), task_receipt=receipt)
    with pytest.raises(PermissionError, match="provider"):
        service._verify_task_receipt(receipt, board="demo", task="t_12345678", policy=store.load())
    assert store.load().activation_for("demo", "codex") == 99


@pytest.mark.parametrize("module_name,provider", [
    ("claude_absent", "claude"), ("claude_file_provenance", "claude"),
    ("codex_file_provenance", "codex"),
])
def test_native_preparation_rejects_pre_scope_receipt(sealed, monkeypatch, module_name, provider):
    import importlib
    import hashlib
    import hmac
    from test_conversation_product_integration import task_receipt
    _, _, service, apply, _ = sealed
    # Codex는 기존 쌍이므로 해제 후 다시 켜 새 epoch를 만든다.
    assert apply("disable", "--board", "demo", "--provider", provider, "--now-ns", "250") == 0
    assert apply("enable", "--board", "demo", "--provider", provider,
                 "--expires-at-ns", "10000", "--now-ns", "300") == 0
    receipt = task_receipt(b"k" * 32, "t_unbound1")
    receipt["created_at_ns"] = 200
    body = {k: v for k, v in receipt.items() if k != "auth_tag"}
    receipt["auth_tag"] = hmac.new(b"k" * 32, b"observation-receipt\0" +
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    def no_locator(*args):
        pytest.fail("pre-scope receipt reached source locator")
    monkeypatch.setattr(service, "_locator", no_locator)
    native_id = {"turn_id": "target"} if provider == "codex" else {
        "prompt_id": "550e8400-e29b-41d4-a716-446655440000"}
    module = importlib.import_module("kanban_adapter." + module_name)
    with pytest.raises(PermissionError, match="predates"):
        module.capture(service, board="demo", task="t_unbound1", session="session",
                       source_path=Path("/nonexistent/source"), task_receipt=receipt, **native_id)


def test_scope_append_does_not_reauthorize_pending_preparation(tmp_path, monkeypatch):
    from dataclasses import replace
    from test_claude_file_provenance_v2 import _setup, _history, _request, PROMPT_ID
    from kanban_adapter import claude_hook as hook, claude_file_provenance as v2
    _, service, _, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    state = json.loads(hook._state_path(cache, "native").read_text())
    policy = service.policies.load()
    assert policy.pair_activated_at_ns is not None
    # 기존 쌍 cutoff가 같아도 미봉인 preparation의 세대 인증은 유지한다.
    service.policies.replace(replace(policy, generation=policy.generation + 1,
        schema_version=2,
        enabled_boards={**policy.enabled_boards, "other": frozenset({"claude"})},
        pair_activated_at_ns={**policy.pair_activated_at_ns, "other": {"claude": policy.activated_at_ns}}),
        expected_generation=policy.generation)
    with pytest.raises(PermissionError, match="policy changed"):
        v2.seal(service, board="demo", task="t_native", prepared=state["conversation_prepared"],
                task_receipt=state["observation_receipt"], prompt_id=PROMPT_ID)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


def test_disable_unrelated_and_reenable_denies_old_binding(sealed):
    store, _, _, apply, page = sealed
    before = page()["events"]
    assert apply("enable", "--board", "other", "--provider", "claude",
                 "--expires-at-ns", "10000", "--now-ns", "250") == 0
    assert apply("disable", "--board", "other", "--now-ns", "300") == 0
    assert page()["events"] == before
    assert not store.load().allows("other", "claude", 1, 400)
    assert apply("disable", "--board", "demo", "--now-ns", "310") == 0
    assert apply("enable", "--board", "demo", "--provider", "codex",
                 "--expires-at-ns", "10000", "--now-ns", "320") == 0
    with pytest.raises(PermissionError):
        page()
    assert store.load().activation_for("demo", "codex") == 320


def test_migration_preserves_legacy_canonical_cas_and_rejects_old_reader(sealed):
    from kanban_adapter.conversation_integration import _canonical, _tag
    store, _, _, apply, page = sealed
    legacy = store.path.read_bytes()
    payload = json.loads(legacy)["payload"]
    assert payload["schema_version"] == 1
    assert "pair_activated_at_ns" not in payload
    assert legacy == _canonical({"payload": payload, "mac": _tag("po", b"p" * 32, payload)}) + b"\n"
    assert apply("enable", "--board", "other", "--provider", "claude",
                 "--expires-at-ns", "10000", "--now-ns", "300") == 0
    envelope = json.loads(store.path.read_bytes())
    assert envelope["payload"]["schema_version"] == 2
    assert envelope["mac"] != _tag("po", b"p" * 32, envelope["payload"])
    # 접두사만 바꿔도 구형 검증기를 통과하면 안 된다.
    assert envelope["mac"].split("_", 1)[1] != _tag("po", b"p" * 32, envelope["payload"]).split("_", 1)[1]
    assert store.load().activation_for("demo", "codex") == 99
    assert store.load().activation_for("other", "claude") == 300
    assert page()["events"]


@pytest.mark.parametrize("mutation", [
    {"schema_version": 999}, {"schema_version": True}, {"unexpected": 1},
    {"pair_activated_at_ns": None}, {"pair_activated_at_ns": {}},
    {"pair_activated_at_ns": {"demo": {"codex": True}}},
    {"pair_activated_at_ns": {"demo": {"codex": -1}}},
    {"pair_activated_at_ns": {"demo": {"codex": 98}}},
    {"pair_activated_at_ns": {"demo": {"codex": 10000}}},
    {"pair_activated_at_ns": {"demo": {"claude": 99}}},
    {"pair_activated_at_ns": {"demo": {"codex": 99}, "extra": {"codex": 99}}},
    {"enabled_boards": {"demo": ["unknown"]}, "pair_activated_at_ns": {"demo": {"unknown": 99}}},
    {"enabled_boards": {"demo": "codex"}},
    {"enabled_boards": {"demo": ["codex", "codex"]}},
])
def test_signed_unknown_schema_or_malformed_map_is_rejected(sealed, mutation):
    from kanban_adapter.conversation_integration import _canonical, _tag
    store, _, _, _, _ = sealed
    payload = json.loads(store.path.read_bytes())["payload"]
    payload.update(schema_version=2, pair_activated_at_ns={"demo": {"codex": 99}})
    payload.update(mutation)
    # 구조 오류와 MAC 오류를 분리하기 위해 해당 wire 형식으로 직접 서명한다.
    mac = _tag("po2", b"p" * 32, ["collection-policy-v2", payload]) if payload["schema_version"] == 2 else _tag("po", b"p" * 32, payload)
    store.path.write_bytes(_canonical({"payload": payload, "mac": mac}) + b"\n")
    with pytest.raises((ValueError, TypeError, PermissionError)):
        store.load()


def test_append_and_disable_do_not_renew_expiry(sealed):
    store, _, _, apply, _ = sealed
    before = store.path.read_bytes()
    assert apply("enable", "--board", "other", "--provider", "claude",
                 "--expires-at-ns", "20000", "--now-ns", "300") == 1
    assert store.path.read_bytes() == before
    assert apply("enable", "--board", "other", "--provider", "claude",
                 "--expires-at-ns", "20000", "--now-ns", "11000") == 1
    assert store.path.read_bytes() == before
    assert apply("disable", "--board", "demo", "--now-ns", "11000") == 0
    assert store.load().expires_at_ns == 10000


def test_pair_disable_preserves_other_provider(sealed):
    store, _, _, apply, page = sealed
    before = page()["events"]
    assert apply("enable", "--board", "demo", "--provider", "claude",
                 "--expires-at-ns", "10000", "--now-ns", "250") == 0
    assert apply("disable", "--board", "demo", "--provider", "claude", "--now-ns", "300") == 0
    assert page()["events"] == before
    assert store.load().activation_for("demo", "claude") is None


def test_disable_cli_alias_revokes_canonical_provider(sealed):
    store, _, _, apply, page = sealed
    before = page()["events"]
    assert apply("enable", "--board", "demo", "--provider", "claude-code",
                 "--expires-at-ns", "10000", "--now-ns", "250") == 0
    assert store.load().activation_for("demo", "claude") == 250
    # enable과 같은 별칭으로 해제해도 실제 canonical 권한이 남으면 안 된다.
    assert apply("disable", "--board", "demo", "--provider", "claude-code", "--now-ns", "300") == 0
    assert store.load().activation_for("demo", "claude") is None
    assert page()["events"] == before


def test_explicit_migrate_is_scope_neutral_and_cannot_downgrade(sealed):
    from dataclasses import replace
    store, _, _, apply, page = sealed
    before = page()["events"]
    assert apply("migrate") == 0
    migrated = store.load()
    assert migrated.schema_version == 2
    assert migrated.generation == 3
    assert migrated.enabled_boards == {"demo": frozenset({"codex"})}
    assert migrated.activated_at_ns == 99
    assert migrated.minimum_binding_version == 1
    assert page()["events"] == before
    raw = store.path.read_bytes()
    with pytest.raises(ValueError, match="downgrade"):
        store.replace(replace(migrated, schema_version=1, generation=4), expected_generation=3)
    assert store.path.read_bytes() == raw


def test_duplicate_signed_map_key_is_rejected(sealed):
    store, _, _, apply, _ = sealed
    assert apply("enable", "--board", "other", "--provider", "claude",
                 "--expires-at-ns", "10000", "--now-ns", "300") == 0
    raw = store.path.read_text()
    assert '"codex":99' in raw
    store.path.write_text(raw.replace('"codex":99', '"codex":0,"codex":99'))
    with pytest.raises(ValueError, match="duplicate"):
        store.load()


def test_fresh_page_survives_append_but_stale_cursor_denied(sealed):
    _, _, _, apply, page = sealed
    cursor = page(limit=1)["next_cursor"]
    assert cursor
    assert apply("enable", "--board", "other", "--provider", "claude",
                 "--expires-at-ns", "10000", "--now-ns", "300") == 0
    with pytest.raises(PermissionError):
        page(cursor=cursor, limit=1)
    assert len(page()["events"]) == 2


def test_existing_generation_one_policy_cannot_renew_expiry(sealed):
    from kanban_adapter.conversation_integration import _canonical, _tag
    store, _, _, apply, _ = sealed
    payload = json.loads(store.path.read_bytes())["payload"]
    payload["generation"] = 1
    store.path.write_bytes(_canonical({"payload": payload, "mac": _tag("po", b"p" * 32, payload)}) + b"\n")
    before = store.path.read_bytes()
    assert apply("enable", "--board", "other", "--provider", "claude",
                 "--expires-at-ns", "20000", "--now-ns", "300") == 1
    assert store.path.read_bytes() == before


def test_pair_map_is_immutable_and_future_pair_is_denied(sealed, monkeypatch):
    from kanban_adapter import conversation_policy_cli
    store, _, _, apply, _ = sealed
    monkeypatch.setattr(conversation_policy_cli.time, "time_ns", lambda: 500)
    assert apply("enable", "--board", "other", "--provider", "claude", "--expires-at-ns", "10000") == 0
    policy = store.load()
    assert policy.activation_for("other", "claude") == 500
    assert not policy.allows("other", "claude", 1, 499)
    assert policy.allows("other", "claude", 1, 500)
    with pytest.raises(TypeError):
        policy.pair_activated_at_ns["other"]["claude"] = 0


def test_map_tampering_and_legacy_extension_never_pass_authentication(sealed):
    from kanban_adapter.conversation_integration import _canonical, _tag
    store, _, _, apply, _ = sealed
    legacy = json.loads(store.path.read_bytes())["payload"]
    assert apply("enable", "--board", "other", "--provider", "claude", "--expires-at-ns", "10000", "--now-ns", "300") == 0
    envelope = json.loads(store.path.read_bytes())
    envelope["payload"]["pair_activated_at_ns"]["other"]["claude"] = 99
    store.path.write_bytes(_canonical(envelope) + b"\n")
    with pytest.raises(PermissionError, match="MAC"):
        store.load()
    legacy["pair_activated_at_ns"] = {"demo": {"codex": 99}}
    store.path.write_bytes(_canonical({"payload": legacy, "mac": _tag("po", b"p" * 32, legacy)}) + b"\n")
    with pytest.raises(ValueError, match="fields"):
        store.load()
