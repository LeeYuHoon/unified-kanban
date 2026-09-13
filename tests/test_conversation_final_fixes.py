from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from test_conversation_events import NOW, authority_scope
from test_transcript_projection import NOW as PROJECTION_NOW
from test_transcript_projection import SECRET, authorized, codex_user, write_raw_lines

from kanban_adapter import conversation as core
from kanban_adapter import transcript_projection as projection


def _child(authority, locator, parent, index: int):
    pending = authority.begin_binding(
        board=parent.board,
        task=f"child-{index}",
        provider=parent.provider,
        schema_pin=parent.schema_pin,
        profile_root_identity=parent.profile_root_identity,
        locator=locator,
        source_identity=parent.source_identity,
        session=f"child-session-{index}",
        turn_start=parent.turn_start,
        binding_version=1,
        generation=1,
        policy_version=1,
        producer_execution=f"child-execution-{index}",
        now_ns=NOW,
    )
    child = authority.seal_binding(
        pending,
        turn_end=parent.turn_end,
        source_identity=parent.source_identity,
        expected_generation=1,
        now_ns=NOW,
    )
    lifecycle = core.TrustedChildLifecycleV1(
        parent.producer_execution,
        child.producer_execution,
        child.session,
        "reviewer",
        NOW,
        NOW + 1,
        "succeeded",
    )
    return child, lifecycle


def test_same_issuer_cannot_reset_execution_lineage_with_fresh_graphs() -> None:
    authority, policies, locator, parent = authority_scope()
    first_graph_ref = None
    accepted = 0
    for index in range(core.CHILD_MAX_DEPTH + 2):
        child, lifecycle = _child(authority, locator, parent, index)
        graph = authority.create_child_graph()
        if index:
            with pytest.raises(PermissionError, match="lineage|graph"):
                authority.issue_child_edge(
                    parent,
                    child,
                    lifecycle,
                    graph,
                    policy_store=policies,
                    now_ns=NOW,
                    depth=1,
                    expires_at_ns=NOW + 10,
                )
            break
        edge = authority.issue_child_edge(
            parent,
            child,
            lifecycle,
            graph,
            policy_store=policies,
            now_ns=NOW,
            depth=1,
            expires_at_ns=NOW + 10,
        )
        first_graph_ref = edge.graph_ref
        authority.validate_child_route(
            edge,
            parent_binding=parent,
            child_binding=child,
            requested_board=parent.board,
            requested_parent_task=parent.task,
            requested_child_task=child.task,
            policy_store=policies,
            now_ns=NOW,
        )
        accepted += 1
        parent = child
    assert accepted == 1
    assert first_graph_ref is not None


def test_blank_lines_advance_and_paginated_concat_reaches_valid_message(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    valid = json.dumps(codex_user("VISIBLE"), separators=(",", ":")).encode()
    offsets = write_raw_lines(source, [b"", b"", valid])
    scope = authorized(
        tmp_path,
        source,
        provider="codex",
        start=core.SourceBoundaryV1(0),
        end=core.SourceBoundaryV1(offsets[-1]),
    )
    locator, projector, authority, policies, binding, grant = scope
    cursor = None
    texts: list[str | None] = []
    malformed = 0
    pages = 0
    while True:
        page = projection.project_page(
            locator,
            projector,
            authority=authority,
            policy_store=policies,
            binding=binding,
            grant=grant,
            cursor=cursor,
            limits=projection.ProjectionLimits(max_records=1),
            now_ns=PROJECTION_NOW,
        )
        pages += 1
        malformed += page.malformed_count
        texts.extend(event.text for event in page.events)
        cursor = page.next_cursor
        if cursor is None:
            break
        assert pages < 5
    assert texts == ["VISIBLE"]
    assert malformed == 2
    assert pages == 3


def test_owner_receipt_and_index_fifo_are_rejected_without_blocking(
    tmp_path: Path,
) -> None:
    path = tmp_path / "index"
    index = core.ConversationIndex(path, max_bytes=8192, secret=SECRET, scope="scope")
    event = core.ConversationEventV1(
        "rp_" + "a" * 64, "ev_" + "b" * 64, 0, "user_message", text="safe"
    )
    assert index.append_once(event)
    owner = tmp_path / "index.owner"
    owner.unlink()
    os.mkfifo(owner, 0o600)

    started = time.monotonic()
    with pytest.raises(PermissionError, match="receipt|private"):
        core.ConversationIndex(path, max_bytes=8192, secret=SECRET, scope="scope")
    assert time.monotonic() - started < 0.5

    started = time.monotonic()
    with pytest.raises(PermissionError, match="receipt|private"):
        index.append_once(
            core.ConversationEventV1(
                "rp_" + "c" * 64,
                "ev_" + "d" * 64,
                1,
                "user_message",
                text="next",
            )
        )
    assert time.monotonic() - started < 0.5

    fifo_index = tmp_path / "fifo-index"
    os.mkfifo(fifo_index, 0o600)
    started = time.monotonic()
    with pytest.raises(PermissionError, match="regular"):
        core.ConversationIndex(
            fifo_index, max_bytes=8192, secret=SECRET, scope="fifo"
        )
    assert time.monotonic() - started < 0.5

    cache_root = tmp_path / "cache"
    cache_root.mkdir(mode=0o700)
    cache = core.IndexCacheController(cache_root, retention_ns=1)
    retained = core.ConversationIndex(
        cache_root / "expired", max_bytes=8192, secret=SECRET, scope="expired"
    )
    assert retained.append_once(event)
    cache.register(retained, completed_at_ns=0)
    (cache_root / "expired").rename(cache_root / "retained")
    os.mkfifo(cache_root / "expired", 0o600)
    started = time.monotonic()
    assert cache.prune(now_ns=2) == 0
    assert time.monotonic() - started < 0.5
    assert (cache_root / "expired").is_fifo()
