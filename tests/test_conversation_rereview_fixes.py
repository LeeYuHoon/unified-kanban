from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from test_conversation_events import NOW, authority_scope
from test_transcript_projection import NOW as PROJECTION_NOW
from test_transcript_projection import SECRET, authorized, codex_user, write_jsonl

from kanban_adapter import conversation as core
from kanban_adapter import transcript_projection as projection


def _event(ch: str = "a") -> core.ConversationEventV1:
    return core.ConversationEventV1(
        "rp_" + ch * 64, "ev_" + ch * 64, 0, "user_message", text="safe"
    )


def _scope(root: Path, records: list[object], *, provider: str = "codex"):
    root.mkdir(mode=0o700)
    source = root / "source.jsonl"
    offsets = write_jsonl(source, records)
    return source, authorized(
        root,
        source,
        provider=provider,
        start=core.SourceBoundaryV1(0),
        end=core.SourceBoundaryV1(offsets[-1]),
    )


def _page(scope, **kwargs):
    locator, projector, authority, policies, binding, grant = scope
    return projection.project_page(
        locator,
        projector,
        authority=authority,
        policy_store=policies,
        binding=binding,
        grant=grant,
        now_ns=PROJECTION_NOW,
        **kwargs,
    )


def test_initial_foreign_private_file_is_never_recovered_or_modified(tmp_path: Path) -> None:
    path = tmp_path / "index"
    original = b"FOREIGN_PRIVATE_DOCUMENT"
    path.write_bytes(original)
    path.chmod(0o600)

    with pytest.raises(ValueError, match="authenticated|partial"):
        core.ConversationIndex(path, max_bytes=8192, secret=SECRET, scope="scope")

    assert path.read_bytes() == original


def test_retention_requires_authenticated_index_and_preserves_foreign_successor(
    tmp_path: Path,
) -> None:
    cache = core.IndexCacheController(tmp_path, retention_ns=10)
    owned = core.ConversationIndex(
        tmp_path / "old", max_bytes=8192, secret=SECRET, scope="scope"
    )
    assert owned.append_once(_event())
    cache.register(owned, completed_at_ns=PROJECTION_NOW - 20)
    (tmp_path / "old").rename(tmp_path / "retained")
    successor = tmp_path / "old"
    successor.write_bytes(b"FOREIGN_SUCCESSOR")
    successor.chmod(0o600)

    assert cache.prune(now_ns=PROJECTION_NOW) == 0
    assert successor.read_bytes() == b"FOREIGN_SUCCESSOR"
    assert (tmp_path / "retained").exists()

    with pytest.raises(TypeError):
        cache.register("old", completed_at_ns=PROJECTION_NOW - 20)  # type: ignore[arg-type]


def test_pagination_preserves_every_intra_record_event_across_event_and_byte_caps(
    tmp_path: Path,
) -> None:
    record = {
        "type": "assistant",
        "sessionId": "session",
        "message": {
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "FIRST"},
                {"type": "text", "text": "SECOND"},
            ],
        },
    }
    _, scope = _scope(tmp_path / "events", [record], provider="claude")
    first = _page(scope, limits=projection.ProjectionLimits(max_events=1))
    assert first.next_cursor is not None
    second = _page(
        scope, cursor=first.next_cursor, limits=projection.ProjectionLimits(max_events=1)
    )
    assert [event.text for event in first.events + second.events] == ["FIRST", "SECOND"]
    assert [event.seq for event in first.events + second.events] == [0, 1]
    assert second.next_cursor is None

    _, baseline_scope = _scope(tmp_path / "baseline", [record], provider="claude")
    baseline = _page(baseline_scope)
    one_event_size = len(
        json.dumps(
            dataclasses.replace(baseline, events=baseline.events[:1], completeness="partial", truncated=True).to_public_dict(),
            ensure_ascii=True,
            sort_keys=True,
        ).encode()
    )
    _, byte_scope = _scope(tmp_path / "bytes", [record], provider="claude")
    page = _page(
        byte_scope,
        limits=projection.ProjectionLimits(max_response_bytes=one_event_size + 256),
    )
    pages = [page]
    while pages[-1].next_cursor is not None:
        pages.append(
            _page(
                byte_scope,
                cursor=pages[-1].next_cursor,
                limits=projection.ProjectionLimits(max_response_bytes=one_event_size + 256),
            )
        )
    assert len(pages) > 1
    assert [event.text for item in pages for event in item.events] == [
        event.text for event in baseline.events
    ]


def test_every_page_path_obeys_exact_wire_budget_and_propagates_text_clipping(
    tmp_path: Path,
) -> None:
    _, scope = _scope(tmp_path / "normal", [codex_user("A" * 10_000)])
    page = _page(scope)
    assert page.truncated is True
    assert page.events[0].redaction == "partially_redacted"
    assert page.dropped_count == 0
    wire = json.dumps(page.to_public_dict(), ensure_ascii=True, sort_keys=True).encode()
    assert len(wire) <= projection.API_RESPONSE_BYTES

    source, rotated_scope = _scope(tmp_path / "rotated", [codex_user("x")])
    source.write_text("{}\n")
    with pytest.raises(projection.ProjectionError, match="minimum|envelope|cap"):
        _page(rotated_scope, limits=projection.ProjectionLimits(max_response_bytes=1))


def test_duplicate_retry_establishes_file_and_directory_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "index"
    index = core.ConversationIndex(path, max_bytes=8192, secret=SECRET, scope="scope")
    assert index.append_once(_event("b"))
    real_fsync = core.os.fsync
    calls = 0

    def fail_once(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected file fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(core.os, "fsync", fail_once)
    with pytest.raises(OSError, match="injected"):
        index.append_once(_event())
    synced: list[int] = []
    monkeypatch.setattr(core.os, "fsync", lambda fd: synced.append(fd))

    assert index.append_once(_event()) is False
    assert len(synced) == 2


def test_authority_owned_graph_computes_depth_and_rejects_overdeep_signed_route() -> None:
    authority, policies, locator, parent = authority_scope()
    graph = authority.create_child_graph()
    accepted = 0
    for index in range(core.CHILD_MAX_DEPTH + 1):
        pending = authority.begin_binding(
            board=parent.board,
            task=f"child{index}",
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
        if index == core.CHILD_MAX_DEPTH:
            with pytest.raises(ValueError, match="depth"):
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
            depth=index + 1,
            expires_at_ns=NOW + 10,
        )
        assert edge.depth == index + 1
        accepted += 1
        parent = child
    assert accepted == core.CHILD_MAX_DEPTH


def test_projection_never_reads_outside_producer_sealed_byte_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.jsonl"
    offsets = write_jsonl(source, [codex_user("BEFORE"), codex_user("INSIDE"), codex_user("AFTER")])
    scope = authorized(
        tmp_path,
        source,
        provider="codex",
        start=core.SourceBoundaryV1(offsets[1]),
        end=core.SourceBoundaryV1(offsets[2]),
    )
    start = scope[4].turn_start.byte_offset
    end = scope[4].turn_end.byte_offset
    reads: list[tuple[int, int]] = []
    real_pread = projection.os.pread

    def bounded_pread(fd: int, count: int, offset: int) -> bytes:
        reads.append((offset, count))
        assert start <= offset
        assert offset + count <= end
        return real_pread(fd, count, offset)

    monkeypatch.setattr(projection.os, "pread", bounded_pread)
    page = _page(scope)
    assert [event.text for event in page.events] == ["INSIDE"]
    assert reads
