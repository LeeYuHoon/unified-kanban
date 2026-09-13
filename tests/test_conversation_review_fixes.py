from __future__ import annotations

import dataclasses
import json
import os
import stat
import time
from pathlib import Path

import pytest

import kanban_adapter.conversation as core
from kanban_adapter.conversation import (
    CHILD_MAX_GRAPH_NODES,
    CollectionPolicyV1,
    ConversationEventV1,
    ConversationIndex,
    IndexCacheController,
    PrivateFixtureAuthority,
    PrivatePolicyStore,
    SourceBoundaryV1,
    SourceIdentityV1,
    SourceLocator,
    SourceReadLimiter,
    TrustedChildLifecycleV1,
    open_verified_jsonl_fd,
    open_verified_root,
)
from kanban_adapter.transcript_projection import (
    ClaudeProjector,
    CodexProjector,
    ProjectionLimits,
    project_page,
)

SECRET = b"review-fix-test-secret-at-least-32-bytes"
NOW_NS = 2_000_000_000_000_000_000


def _write_lines(path: Path, records: list[object]) -> list[int]:
    offsets = [0]
    with path.open("wb") as stream:
        for record in records:
            data = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
            stream.write(data)
            offsets.append(offsets[-1] + len(data))
    path.chmod(0o600)
    return offsets


def _codex_user(text: str, ordinal: int, timestamp: str = "2026-09-11T08:00:00Z") -> dict:
    return {
        "timestamp": timestamp,
        "ordinal": ordinal,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


def _scope(
    root: Path,
    source: Path,
    *,
    provider: str,
    start: SourceBoundaryV1,
    end: SourceBoundaryV1,
    now_ns: int = NOW_NS,
):
    locator = SourceLocator(provider, root, source.relative_to(root), os.getuid())
    root_cap = open_verified_root(locator)
    try:
        with open_verified_jsonl_fd(locator, root_capability=root_cap) as verified:
            identity = SourceIdentityV1.from_stat(os.fstat(verified.fd))
    finally:
        root_cap.close()
    authority = PrivateFixtureAuthority.create(SECRET, issuer_id="fixture-authority")
    policies = authority.create_policy_store()
    policies.replace(
        CollectionPolicyV1(
            version=1,
            generation=1,
            activated_at_ns=now_ns - 1_000,
            expires_at_ns=now_ns + 10_000_000_000,
            enabled_boards={"board": frozenset({provider})},
        )
    )
    pending = authority.begin_binding(
        board="board",
        task="task",
        provider=provider,
        schema_pin=(
            CodexProjector.schema_pin if provider == "codex" else ClaudeProjector.schema_pin
        ),
        profile_root_identity=SourceIdentityV1.from_stat(root.stat()),
        locator=locator,
        source_identity=identity,
        session="session",
        turn_start=start,
        binding_version=1,
        generation=1,
        policy_version=1,
        producer_execution="execution",
        now_ns=now_ns,
    )
    binding = authority.seal_binding(
        pending,
        turn_end=end,
        source_identity=identity,
        expected_generation=1,
        now_ns=now_ns,
    )
    principal = authority.issue_principal_scope(
        principal="owner",
        board="board",
        task="task",
        expires_at_ns=now_ns + 5_000_000_000,
    )
    grant = authority.issue_projection_grant(
        policies,
        binding,
        principal,
        now_ns=now_ns,
        ttl_ns=1_000_000_000,
    )
    return locator, authority, policies, binding, grant


def test_issued_cursor_cannot_escape_before_or_after_sealed_range_and_terminal_has_none(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rollout.jsonl"
    records = [
        _codex_user("BEFORE", 1),
        _codex_user("IN-1", 10),
        _codex_user("IN-2", 11),
        _codex_user("AFTER", 20),
    ]
    offsets = _write_lines(source, records)
    scope = _scope(
        tmp_path,
        source,
        provider="codex",
        start=SourceBoundaryV1(offsets[1], 10),
        end=SourceBoundaryV1(offsets[3], 12),
    )
    locator, authority, policies, binding, grant = scope
    first = project_page(
        locator,
        CodexProjector(),
        binding=binding,
        grant=grant,
        authority=authority,
        policy_store=policies,
        now_ns=NOW_NS,
        limits=ProjectionLimits(max_events=1),
    )
    assert [event.text for event in first.events] == ["IN-1"]
    assert first.next_cursor is not None
    second = project_page(
        locator,
        CodexProjector(),
        binding=binding,
        grant=grant,
        authority=authority,
        policy_store=policies,
        now_ns=NOW_NS + 1,
        cursor=first.next_cursor,
        limits=ProjectionLimits(max_events=1),
    )
    assert [event.text for event in second.events] == ["IN-2"]
    assert second.next_cursor is None
    assert second.completeness == "partial"  # 미지원 skill/child category를 정직하게 표시
    with pytest.raises(PermissionError, match="invalid|rollback"):
        project_page(
            locator,
            CodexProjector(),
            binding=binding,
            grant=grant,
            authority=authority,
            policy_store=policies,
            now_ns=NOW_NS + 2,
            cursor=first.next_cursor,
        )


def test_claude_positive_allowlist_rejects_meta_compaction_sidechain_team_and_inner_role(
    tmp_path: Path,
) -> None:
    projector = ClaudeProjector()
    source = tmp_path / "claude.jsonl"
    offsets = _write_lines(source, [{"type": "progress"}])
    _, _, _, binding, _ = _scope(
        tmp_path,
        source,
        provider="claude",
        start=SourceBoundaryV1(offsets[0]),
        end=SourceBoundaryV1(offsets[1]),
    )
    base = {
        "uuid": "123e4567-e89b-12d3-a456-426614174000",
        "sessionId": "session",
        "type": "user",
        "message": {"role": "user", "content": "PRIVATE"},
    }
    for extra in (
        {"isMeta": True},
        {"isCompactSummary": True},
        {"isSidechain": True},
        {"teamName": "team"},
        {"message": {"role": "developer", "content": "PRIVATE"}},
    ):
        outcome = projector.project(
            {**base, **extra}, binding=binding, source_event_id="source", seq=0
        )
        assert outcome.events == ()

    tool = {
        "uuid": "123e4567-e89b-12d3-a456-426614174001",
        "sessionId": "session",
        "type": "assistant",
        "message": {
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {"secret": "never"}}],
        },
    }
    outcome = projector.project(tool, binding=binding, source_event_id="tool", seq=0)
    assert [(item.kind, item.tool) for item in outcome.events] == [("tool_call", "Read")]
    assert projector.capabilities["skill"] == "unsupported"
    assert projector.capabilities["child"] == "unsupported"


def test_metadata_is_strict_immutable_and_index_has_exact_mac_schema(tmp_path: Path) -> None:
    with pytest.raises((TypeError, ValueError)):
        ConversationEventV1("rp_" + "a" * 64, "ev_" + "b" * 64, 0, "tool_call", timestamp={"secret": "x"})
    with pytest.raises((TypeError, ValueError)):
        ConversationEventV1("rp_bad", "ev_" + "b" * 64, 0, "tool_call")
    with pytest.raises((TypeError, ValueError)):
        ConversationEventV1("rp_" + "a" * 64, "ev_" + "b" * 64, 0, "tool_result", tool_call_ref="raw")

    metadata = {"server": "github", "tool": "search"}
    event = ConversationEventV1(
        "rp_" + "a" * 64,
        "ev_" + "b" * 64,
        0,
        "mcp_call",
        timestamp="2026-09-11T08:00:00Z",
        mcp=metadata,
        tool_call_ref="tc_" + "c" * 64,
    )
    metadata["server"] = "mutated"
    assert event.to_public_dict()["mcp"]["server"] == "github"

    path = tmp_path / "index.jsonl"
    index = ConversationIndex(path, max_bytes=4096, secret=SECRET, scope="binding-ref")
    assert index.append_once(event)
    envelope = json.loads(path.read_text())
    assert set(envelope) == {"schema_version", "payload", "mac"}
    assert envelope["mac"].startswith("ix_")
    envelope["payload"]["timestamp"] = {"secret": "x"}
    path.write_text(json.dumps(envelope) + "\n")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="schema|MAC|timestamp"):
        ConversationIndex(path, max_bytes=4096, secret=SECRET, scope="binding-ref")


def test_anchor_walk_rejects_ancestor_symlink_and_fifo_without_blocking(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    allowed = actual / "sessions"
    allowed.mkdir(parents=True)
    source = allowed / "source.jsonl"
    source.write_text("{}\n")
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError):
        open_verified_root(SourceLocator("codex", link / "sessions", Path("source.jsonl"), os.getuid()))

    fifo = allowed / "fifo.jsonl"
    os.mkfifo(fifo)
    locator = SourceLocator("codex", allowed, Path("fifo.jsonl"), os.getuid())
    root_cap = open_verified_root(locator)
    started = time.monotonic()
    try:
        with pytest.raises(PermissionError, match="regular"):
            open_verified_jsonl_fd(locator, root_capability=root_cap)
    finally:
        root_cap.close()
    assert time.monotonic() - started < 0.5


def test_private_issuer_one_shot_seal_full_receipt_scope_live_policy_and_expiry(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    offsets = _write_lines(source, [_codex_user("ok", 1)])
    locator, authority, policies, binding, grant = _scope(
        tmp_path,
        source,
        provider="codex",
        start=SourceBoundaryV1(0, 1),
        end=SourceBoundaryV1(offsets[-1], 2),
    )
    with pytest.raises(RuntimeError, match="one-shot"):
        authority.reseal_for_test(binding, SourceBoundaryV1(offsets[-1], 3), now_ns=NOW_NS)
    replaced = dataclasses.replace(binding, producer_receipt="pr_" + "0" * 64)
    with pytest.raises(PermissionError, match="receipt|binding"):
        authority.validate_projection_access(policies, replaced, grant, locator, now_ns=NOW_NS)
    extended = dataclasses.replace(grant, expires_at_ns=grant.expires_at_ns + 1_000_000_000)
    with pytest.raises(PermissionError, match="full scope"):
        authority.validate_projection_access(policies, binding, extended, locator, now_ns=NOW_NS)
    forged_store = PrivatePolicyStore(authority.issuer_id, object())
    with pytest.raises(PermissionError, match="authoritative"):
        authority.validate_projection_access(
            forged_store, binding, grant, locator, now_ns=NOW_NS
        )
    policies.disable("board", expected_generation=1)
    with pytest.raises(PermissionError, match="policy"):
        authority.validate_projection_access(policies, binding, grant, locator, now_ns=NOW_NS)
    with pytest.raises(PermissionError, match="expired"):
        authority.validate_projection_access(policies, binding, grant, locator, now_ns=NOW_NS + 20_000_000_000)
    with pytest.raises(RuntimeError, match="disabled"):
        PrivateFixtureAuthority.production()


def test_codex_rust_v0145_native_schema_projects_user_final_and_nested_tool(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    records = [
        _codex_user("hello", 10),
        {
            "timestamp": "2026-09-11T08:00:01Z",
            "ordinal": 11,
            "type": "response_item",
            "payload": {"type": "function_call", "name": "Read", "arguments": "{\"path\":\"secret\"}", "call_id": "call-1"},
        },
        {
            "timestamp": "2026-09-11T08:00:02Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call-1", "output": "RAW_RESULT"},
        },
        {
            "timestamp": "2026-09-11T08:00:03Z",
            "ordinal": 13,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
                "phase": "final_answer",
            },
        },
    ]
    offsets = _write_lines(source, records)
    locator, authority, policies, binding, grant = _scope(
        tmp_path,
        source,
        provider="codex",
        start=SourceBoundaryV1(0, 10),
        end=SourceBoundaryV1(offsets[-1], 14),
    )
    page = project_page(
        locator,
        CodexProjector(),
        binding=binding,
        grant=grant,
        authority=authority,
        policy_store=policies,
        now_ns=NOW_NS,
    )
    assert [event.kind for event in page.events] == ["user_message", "tool_call", "tool_result", "final_assistant"]
    rendered = json.dumps(page.to_public_dict())
    assert "RAW_RESULT" not in rendered
    assert "arguments" not in rendered
    assert [event.seq for event in page.events] == [0, 1, 2, 3]


def test_journal_lock_reload_replay_recovery_and_foreign_successor_nonmutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "index.jsonl"
    event = ConversationEventV1(
        "rp_" + "1" * 64,
        "ev_" + "2" * 64,
        0,
        "tool_call",
        tool="Read",
        tool_call_ref="tc_" + "3" * 64,
    )
    first = ConversationIndex(path, max_bytes=8192, secret=SECRET, scope="scope")
    assert first.append_once(event)
    second = ConversationIndex(path, max_bytes=8192, secret=SECRET, scope="scope")
    duplicate = dataclasses.replace(event, replay_id="rp_" + "3" * 64, event_id="ev_" + "4" * 64, seq=1)
    assert first.append_once(duplicate)
    assert second.append_once(duplicate) is False
    assert path.read_text().count(duplicate.replay_id) == 1

    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    recovered = ConversationIndex(path, max_bytes=8192, secret=SECRET, scope="scope")
    assert recovered.append_once(dataclasses.replace(event, replay_id="rp_" + "5" * 64, event_id="ev_" + "6" * 64, seq=2))
    assert path.read_bytes().endswith(b"\n")

    partial_path = tmp_path / "partial-write.jsonl"
    partial_index = ConversationIndex(partial_path, max_bytes=8192, secret=SECRET, scope="partial")
    assert partial_index.append_once(
        dataclasses.replace(
            event, replay_id="rp_" + "a" * 64, event_id="ev_" + "b" * 64
        )
    )
    real_write = core.os.write
    write_calls = 0

    def interrupted_write(fd: int, data: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return real_write(fd, data[: max(1, len(data) // 2)])
        raise OSError("synthetic interrupted append")

    monkeypatch.setattr(core.os, "write", interrupted_write)
    with pytest.raises(OSError, match="interrupted"):
        partial_index.append_once(event)
    monkeypatch.setattr(core.os, "write", real_write)
    partial_recovered = ConversationIndex(
        partial_path, max_bytes=8192, secret=SECRET, scope="partial"
    )
    assert partial_recovered.append_once(event)

    fsync_path = tmp_path / "fsync-write.jsonl"
    fsync_index = ConversationIndex(fsync_path, max_bytes=8192, secret=SECRET, scope="fsync")
    assert fsync_index.append_once(
        dataclasses.replace(
            event, replay_id="rp_" + "c" * 64, event_id="ev_" + "d" * 64
        )
    )
    real_fsync = core.os.fsync
    fsync_calls = 0

    def interrupted_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise OSError("synthetic fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(core.os, "fsync", interrupted_fsync)
    with pytest.raises(OSError, match="fsync"):
        fsync_index.append_once(event)
    monkeypatch.setattr(core.os, "fsync", real_fsync)
    fsync_recovered = ConversationIndex(
        fsync_path, max_bytes=8192, secret=SECRET, scope="fsync"
    )
    assert fsync_recovered.append_once(event) is False

    foreign = tmp_path / "foreign"
    foreign.write_text("foreign")
    foreign.chmod(0o644)
    os.replace(foreign, path)
    with pytest.raises((PermissionError, RuntimeError)):
        recovered.append_once(dataclasses.replace(event, replay_id="rp_" + "7" * 64, event_id="ev_" + "8" * 64, seq=3))
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert path.read_text() == "foreign"


def test_full_envelope_budget_partial_lower_bounds_concurrency_and_retention(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    records = [_codex_user("A" * 1000, number) for number in range(10, 15)]
    offsets = _write_lines(source, records)
    locator, authority, policies, binding, grant = _scope(
        tmp_path,
        source,
        provider="codex",
        start=SourceBoundaryV1(0, 10),
        end=SourceBoundaryV1(offsets[-1], 15),
    )
    page = project_page(
        locator,
        CodexProjector(),
        binding=binding,
        grant=grant,
        authority=authority,
        policy_store=policies,
        now_ns=NOW_NS,
        limits=ProjectionLimits(max_response_bytes=2200),
    )
    assert len(json.dumps(page.to_public_dict(), ensure_ascii=True, sort_keys=True).encode()) <= 2200
    assert page.completeness == "partial"
    assert page.truncated is True
    assert all(count["exact"] is False for count in page.counts.values())

    limiter = SourceReadLimiter(process_limit=1, board_limit=1, task_limit=1)
    with (
        limiter.admit("board", "task", deadline_ms=50),
        pytest.raises(TimeoutError),
        limiter.admit("board", "task", deadline_ms=1),
    ):
        pass

    cache = IndexCacheController(tmp_path / "cache", global_bytes=512, retention_ns=10)
    cache.root.mkdir(mode=0o700)
    expired = ConversationIndex(
        cache.root / "expired", max_bytes=8192, secret=SECRET, scope="expired"
    )
    assert expired.append_once(
        ConversationEventV1(
            "rp_" + "6" * 64, "ev_" + "7" * 64, 0, "user_message", text="old"
        )
    )
    cache.register(expired, completed_at_ns=NOW_NS - 20)
    # macOS에는 identity-CAS unlink가 없으므로 안전하지 않은 retention 삭제는 생략한다.
    assert cache.prune(now_ns=NOW_NS) == 0
    assert expired.path.exists()
    with pytest.raises(RuntimeError, match="global"):
        cache.reserve(513)
    bounded_root = tmp_path / "bounded-cache"
    bounded_root.mkdir(mode=0o700)
    bounded = IndexCacheController(bounded_root, global_bytes=128, retention_ns=10)
    bounded_index = ConversationIndex(
        bounded_root / "card.jsonl",
        max_bytes=8192,
        secret=SECRET,
        scope="bounded",
        cache_controller=bounded,
    )
    with pytest.raises(RuntimeError, match="global"):
        bounded_index.append_once(
            ConversationEventV1(
                "rp_" + "8" * 64,
                "ev_" + "9" * 64,
                0,
                "user_message",
                text="safe",
            )
        )


def test_typed_child_lifecycle_single_edge_scope_expiry_cycle_and_node_limit(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    offsets = _write_lines(source, [_codex_user("ok", 1)])
    locator, authority, policies, parent, _grant = _scope(
        tmp_path,
        source,
        provider="codex",
        start=SourceBoundaryV1(0, 1),
        end=SourceBoundaryV1(offsets[-1], 2),
    )
    child_pending = authority.begin_binding(
        board="board",
        task="child-task",
        provider="codex",
        schema_pin=CodexProjector.schema_pin,
        profile_root_identity=parent.profile_root_identity,
        locator=locator,
        source_identity=parent.source_identity,
        session="child-session",
        turn_start=SourceBoundaryV1(0, 1),
        binding_version=1,
        generation=1,
        policy_version=1,
        producer_execution="child-execution",
        now_ns=NOW_NS,
    )
    child = authority.seal_binding(
        child_pending,
        turn_end=SourceBoundaryV1(offsets[-1], 2),
        source_identity=parent.source_identity,
        expected_generation=1,
        now_ns=NOW_NS,
    )
    lifecycle = TrustedChildLifecycleV1(
        parent_execution="execution",
        child_execution="child-execution",
        child_session="child-session",
        role="reviewer",
        started_at_ns=NOW_NS,
        stopped_at_ns=NOW_NS + 1,
        status="succeeded",
    )
    graph = authority.create_child_graph(max_nodes=CHILD_MAX_GRAPH_NODES)
    edge = authority.issue_child_edge(
        parent, child, lifecycle, graph, policy_store=policies, now_ns=NOW_NS,
        depth=1, expires_at_ns=NOW_NS + 100,
    )
    authority.validate_child_route(
        edge,
        parent_binding=parent,
        child_binding=child,
        requested_board="board",
        requested_parent_task="task",
        requested_child_task="child-task",
        policy_store=policies,
        now_ns=NOW_NS,
    )
    with pytest.raises(PermissionError, match="scope"):
        authority.validate_child_route(
            edge,
            parent_binding=parent,
            child_binding=child,
            requested_board="other",
            requested_parent_task="task",
            requested_child_task="child-task",
            policy_store=policies,
            now_ns=NOW_NS,
        )
    with pytest.raises(PermissionError, match="expired"):
        authority.validate_child_route(
            edge,
            parent_binding=parent,
            child_binding=child,
            requested_board="board",
            requested_parent_task="task",
            requested_child_task="child-task",
            policy_store=policies,
            now_ns=NOW_NS + 100,
        )
    with pytest.raises(RuntimeError, match="already"):
        graph.add("execution", "child-execution", depth=1)
    with pytest.raises(ValueError, match="cycle"):
        graph.add("child-execution", "execution", depth=2)
    with pytest.raises(ValueError, match="node"):
        tiny = authority.create_child_graph(max_nodes=2)
        tiny.add("a", "b", depth=1)
        tiny.add("b", "c", depth=2)
