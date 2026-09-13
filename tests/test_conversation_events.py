from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kanban_adapter import conversation
from kanban_adapter.conversation import (
    API_EVENT_LIMIT,
    API_REQUEST_DEADLINE_MS,
    API_RESPONSE_BYTES,
    CHILD_MAX_DEPTH,
    CHILD_MAX_FANOUT,
    CHILD_MAX_GRAPH_NODES,
    INDEX_CARD_BYTES,
    INDEX_GLOBAL_BYTES,
    INDEX_RETENTION_DAYS,
    JSONL_LINE_BYTES,
    JSONL_RECORD_LIMIT,
    JSONL_SCAN_BYTES,
    JSONL_SCAN_DEADLINE_MS,
    POST_REDACTION_TEXT_BYTES,
    PRE_REDACTION_TEXT_BYTES,
    SOURCE_READ_BOARD_LIMIT,
    SOURCE_READ_PROCESS_LIMIT,
    SOURCE_READ_TASK_LIMIT,
    CollectionPolicyV1,
    ConversationEventV1,
    ConversationIndex,
    PrivateFixtureAuthority,
    SourceBoundaryV1,
    SourceIdentityV1,
    SourceLocator,
    TrustedChildLifecycleV1,
    append_event_once,
    issue_projection_grant,
    opaque_event_id,
    opaque_identifier,
    opaque_replay_id,
    opaque_tool_call_ref,
    pair_child_events,
    sanitize_event_text,
    sanitize_tool_name,
    shared_replay_id,
    split_mcp_tool_name,
)

SECRET = b"test-only-mac-key-with-at-least-32-bytes"
NOW = 1_800_000_000_000_000_000


def authority_scope(*, task: str = "task", execution: str = "execution"):
    authority = PrivateFixtureAuthority.create(SECRET, issuer_id="fixture")
    store = authority.create_policy_store()
    policy = CollectionPolicyV1(
        version=1,
        generation=1,
        activated_at_ns=NOW - 1,
        expires_at_ns=NOW + 1_000_000,
        enabled_boards={"board": frozenset({"codex"})},
    )
    store.replace(policy)
    locator = SourceLocator("codex", Path("/private/tmp/fixture-root"), Path("source.jsonl"), os.getuid())
    source_identity = SourceIdentityV1(1, 2, 100, 3)
    pending = authority.begin_binding(
        board="board",
        task=task,
        provider="codex",
        schema_pin="openai/codex@rust-v0.145.0",
        profile_root_identity=SourceIdentityV1(1, 9, 0, 4),
        locator=locator,
        source_identity=source_identity,
        session=f"session-{task}",
        turn_start=SourceBoundaryV1(0, 1),
        binding_version=1,
        generation=1,
        policy_version=1,
        producer_execution=execution,
        now_ns=NOW,
    )
    binding = authority.seal_binding(
        pending,
        turn_end=SourceBoundaryV1(100, 2),
        source_identity=source_identity,
        expected_generation=1,
        now_ns=NOW,
    )
    return authority, store, locator, binding


def event(*, replay: str = "a", kind: str = "user_message", text: str | None = "safe") -> ConversationEventV1:
    return ConversationEventV1(
        replay_id="rp_" + replay * 64,
        event_id="ev_" + "b" * 64,
        seq=0,
        kind=kind,
        text=text,
    )


def test_policy_is_default_deny_and_requires_exact_board_provider_version() -> None:
    disabled = CollectionPolicyV1.disabled(version=1)
    assert not disabled.allows("board", "codex", 1, NOW)
    enabled = CollectionPolicyV1(
        version=2,
        activated_at_ns=NOW - 1,
        expires_at_ns=NOW + 1,
        enabled_boards={"board": frozenset({"codex"})},
        minimum_binding_version=2,
    )
    assert enabled.allows("board", "codex", 2, NOW)
    assert not enabled.allows("board", "claude", 2, NOW)
    assert not enabled.allows("board", "codex", 1, NOW)


def test_binding_requires_authoritative_pending_start_and_one_shot_generation_cas() -> None:
    authority, _store, _locator, binding = authority_scope()
    assert binding.turn_start == SourceBoundaryV1(0, 1)
    assert binding.turn_end == SourceBoundaryV1(100, 2)
    assert binding.schema_pin == "openai/codex@rust-v0.145.0"
    with pytest.raises(RuntimeError, match="one-shot"):
        authority.reseal_for_test(binding, SourceBoundaryV1(100, 2), now_ns=NOW)


def test_product_and_direct_grant_issuance_remain_disabled() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        PrivateFixtureAuthority.production()
    with pytest.raises(RuntimeError, match="disabled"):
        issue_projection_grant()


def test_replay_and_opaque_identifiers_are_scoped_and_hide_raw_ids() -> None:
    _authority, _store, _locator, binding = authority_scope()
    replay = shared_replay_id(binding, source_event_id="raw-source", kind="tool_call", secret=SECRET)
    assert replay.startswith("rp_") and "raw-source" not in replay
    assert opaque_replay_id(binding, "raw-source").startswith("rp_")
    assert opaque_event_id(binding, "raw-source").startswith("ev_")
    assert opaque_tool_call_ref(binding, "raw-call").startswith("tc_")
    assert "raw" not in opaque_identifier("raw-session", scope="session", secret=SECRET)


def test_event_dto_is_allowlisted_strict_typed_immutable_and_revalidated() -> None:
    metadata = {"server": "github", "tool": "search"}
    item = ConversationEventV1(
        replay_id="rp_" + "a" * 64,
        event_id="ev_" + "b" * 64,
        seq=0,
        kind="mcp_call",
        timestamp="2026-09-11T08:00:00Z",
        mcp=metadata,
        tool_call_ref="tc_" + "c" * 64,
    )
    metadata["server"] = "mutated"
    assert item.to_public_dict()["mcp"]["server"] == "github"
    with pytest.raises((TypeError, ValueError)):
        ConversationEventV1("rp_bad", "ev_" + "b" * 64, 0, "tool_call")
    object.__setattr__(item, "timestamp", {"raw": "secret"})
    with pytest.raises((TypeError, ValueError)):
        item.to_public_dict()


def test_identifier_sanitizers_reject_paths_controls_and_opaque_runs() -> None:
    assert sanitize_tool_name("Read") == "Read"
    assert sanitize_tool_name("../secret") is None
    assert sanitize_tool_name("tool\nname") is None
    assert split_mcp_tool_name("mcp__github__search") == ("github", "search")
    assert split_mcp_tool_name("mcp__bad/path__search") is None


def test_redaction_happens_before_utf8_truncation_and_is_best_effort() -> None:
    raw = "mail me@example.com token sk-abcdefghijklmnopqrstuvwxyz " + "가" * 30_000
    result = sanitize_event_text(raw)
    assert "me@example.com" not in (result.text or "")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in (result.text or "")
    assert len((result.text or "").encode()) <= POST_REDACTION_TEXT_BYTES
    assert result.state in {"applied", "partially_redacted"}


def test_redactor_failure_discards_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conversation, "_apply_redactions", lambda _value: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sanitize_event_text("private").state == "failed_closed"
    assert sanitize_event_text("private").text is None


def test_content_free_index_never_serializes_event_text_and_replays_once(tmp_path: Path) -> None:
    path = tmp_path / "index.jsonl"
    index = ConversationIndex(path, max_bytes=INDEX_CARD_BYTES, secret=SECRET, scope="binding")
    item = event(text="TOP SECRET")
    assert append_event_once(index, item)
    assert not append_event_once(index, item)
    assert "TOP SECRET" not in path.read_text()
    assert set(json.loads(path.read_text())) == {"schema_version", "payload", "mac"}


def test_index_rejects_symlinked_parent_without_writing_outside(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "private"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        ConversationIndex(link / "index.jsonl", max_bytes=1024, secret=SECRET, scope="binding")
    assert list(outside.iterdir()) == []


def test_index_identity_cas_preserves_foreign_successor(tmp_path: Path) -> None:
    path = tmp_path / "index.jsonl"
    index = ConversationIndex(path, max_bytes=4096, secret=SECRET, scope="binding")
    assert index.append_once(event())
    path.unlink()
    path.write_text("foreign\n")
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="identity"):
        index.append_once(event(replay="c"))
    assert path.read_text() == "foreign\n"


def test_index_rejects_malformed_existing_json_and_card_budget(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text("{not json}\n")
    malformed.chmod(0o600)
    with pytest.raises(ValueError, match="authenticated|strict JSON"):
        ConversationIndex(malformed, max_bytes=1024, secret=SECRET, scope="binding")
    tiny = ConversationIndex(tmp_path / "tiny.jsonl", max_bytes=64, secret=SECRET, scope="binding")
    with pytest.raises(RuntimeError, match="budget"):
        tiny.append_once(event())


def test_untrusted_child_pairing_never_invents_navigation() -> None:
    pair = pair_child_events(
        {"role": "reviewer", "status": "started"},
        {"role": "reviewer", "status": "succeeded"},
        secret=SECRET,
    )
    assert pair.child_ref is None
    assert pair.child_session_ref is None
    assert pair.completeness == "partial"


def test_typed_child_edge_enforces_scope_cycle_depth_fanout_and_nodes() -> None:
    authority, store, _locator, parent = authority_scope()
    _other_authority, _other_store, _other_locator, _other_binding = authority_scope()
    # 동일 issuer 안에서 child binding을 별도 task/execution으로 발급한다.
    locator = SourceLocator("codex", Path("/private/tmp/fixture-root"), Path("source.jsonl"), os.getuid())
    identity = SourceIdentityV1(1, 2, 100, 3)
    pending = authority.begin_binding(
        board="board", task="child", provider="codex", schema_pin=parent.schema_pin,
        profile_root_identity=parent.profile_root_identity, locator=locator,
        source_identity=identity, session="child-session", turn_start=SourceBoundaryV1(0, 1),
        binding_version=1, generation=1, policy_version=1,
        producer_execution="child-execution", now_ns=NOW,
    )
    child = authority.seal_binding(
        pending, turn_end=SourceBoundaryV1(100, 2), source_identity=identity,
        expected_generation=1, now_ns=NOW,
    )
    lifecycle = TrustedChildLifecycleV1(
        "execution", "child-execution", "child-session", "reviewer", NOW, NOW + 1, "succeeded"
    )
    graph = authority.create_child_graph(max_nodes=3)
    edge = authority.issue_child_edge(
        parent, child, lifecycle, graph, policy_store=store, now_ns=NOW,
        depth=1, expires_at_ns=NOW + 10,
    )
    authority.validate_child_route(
        edge, parent_binding=parent, child_binding=child, requested_board="board",
        requested_parent_task="task", requested_child_task="child",
        policy_store=store, now_ns=NOW,
    )
    graph.add("child-execution", "third", depth=2)
    with pytest.raises(ValueError, match="cycle"):
        graph.add("third", "execution", depth=3)
    with pytest.raises(ValueError, match="node"):
        graph.add("third", "fourth", depth=3)


def test_budget_constants_match_approved_integer_contract() -> None:
    assert (
        JSONL_LINE_BYTES, JSONL_SCAN_BYTES, JSONL_RECORD_LIMIT, JSONL_SCAN_DEADLINE_MS,
        PRE_REDACTION_TEXT_BYTES, POST_REDACTION_TEXT_BYTES,
        API_REQUEST_DEADLINE_MS, API_EVENT_LIMIT, API_RESPONSE_BYTES,
        SOURCE_READ_PROCESS_LIMIT, SOURCE_READ_BOARD_LIMIT, SOURCE_READ_TASK_LIMIT,
        INDEX_GLOBAL_BYTES, INDEX_CARD_BYTES, INDEX_RETENTION_DAYS,
        CHILD_MAX_DEPTH, CHILD_MAX_FANOUT, CHILD_MAX_GRAPH_NODES,
    ) == (
        1_048_576, 8_388_608, 5_000, 2_000, 262_144, 65_536,
        3_000, 500, 1_048_576, 4, 2, 1, 268_435_456, 1_048_576, 30, 4, 32, 128,
    )
