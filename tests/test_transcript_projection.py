from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from kanban_adapter.conversation import (
    JSONL_LINE_BYTES,
    JSONL_RECORD_LIMIT,
    JSONL_SCAN_BYTES,
    JSONL_SCAN_DEADLINE_MS,
    CollectionPolicyV1,
    PrivateFixtureAuthority,
    SourceBoundaryV1,
    SourceIdentityV1,
    SourceLocator,
    open_verified_jsonl_fd,
    open_verified_root,
    open_verified_sqlite_source,
)
from kanban_adapter.transcript_projection import (
    ClaudeProjector,
    CodexProjector,
    HermesProjector,
    LegacyObservationResolver,
    ProjectionError,
    ProjectionLimits,
    project_page,
)

SECRET = b"test-only-mac-key-with-at-least-32-bytes"
NOW = 1_800_000_000_000_000_000


def write_raw_lines(path: Path, lines: list[bytes]) -> list[int]:
    offsets = [0]
    with path.open("wb") as stream:
        for line in lines:
            stream.write(line + b"\n")
            offsets.append(stream.tell())
    path.chmod(0o600)
    return offsets


def write_jsonl(path: Path, records: list[object]) -> list[int]:
    return write_raw_lines(
        path,
        [json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode() for record in records],
    )


def locator(root: Path, source: Path, provider: str = "codex") -> SourceLocator:
    return SourceLocator(provider, root, source.relative_to(root), os.getuid())


def authorized(
    root: Path,
    source: Path,
    *,
    provider: str,
    start: SourceBoundaryV1,
    end: SourceBoundaryV1,
    now_ns: int = NOW,
):
    source_locator = locator(root, source, provider)
    with open_verified_root(source_locator) as root_cap:
        root_identity = root_cap.identity
        with open_verified_jsonl_fd(source_locator, root_capability=root_cap) as verified:
            source_identity = verified.identity
    authority = PrivateFixtureAuthority.create(SECRET, issuer_id="fixture")
    policies = authority.create_policy_store()
    policies.replace(
        CollectionPolicyV1(
            version=1,
            generation=1,
            activated_at_ns=now_ns - 1,
            expires_at_ns=now_ns + 10_000_000_000,
            enabled_boards={"board": frozenset({provider})},
        )
    )
    projector = CodexProjector() if provider == "codex" else ClaudeProjector()
    pending = authority.begin_binding(
        board="board", task="task", provider=provider, schema_pin=projector.schema_pin,
        profile_root_identity=root_identity, locator=source_locator,
        source_identity=source_identity, session="session", turn_start=start,
        binding_version=1, generation=1, policy_version=1,
        producer_execution="execution", now_ns=now_ns,
    )
    binding = authority.seal_binding(
        pending, turn_end=end, source_identity=source_identity,
        expected_generation=1, now_ns=now_ns,
        source_range_digest=hashlib.sha256(
            source.read_bytes()[start.byte_offset:end.byte_offset]
        ).hexdigest(),
    )
    principal = authority.issue_principal_scope(
        principal="owner", board="board", task="task", expires_at_ns=now_ns + 1_000_000_000
    )
    grant = authority.issue_projection_grant(
        policies, binding, principal, now_ns=now_ns, ttl_ns=500_000_000
    )
    return source_locator, projector, authority, policies, binding, grant


def codex_user(text: str, ordinal: int | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "timestamp": "2026-09-11T08:00:00Z",
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }
    if ordinal is not None:
        value["ordinal"] = ordinal
    return value


def test_missing_auth_denies_before_source_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.jsonl"
    offsets = write_jsonl(source, [codex_user("safe", 1)])
    scope = authorized(tmp_path, source, provider="codex", start=SourceBoundaryV1(0, 1), end=SourceBoundaryV1(offsets[-1], 2))
    source_locator, projector, authority, policies, binding, grant = scope
    opened = False

    def forbidden(_locator: SourceLocator):
        nonlocal opened
        opened = True
        raise AssertionError("opened")

    monkeypatch.setattr("kanban_adapter.transcript_projection.open_verified_root", forbidden)
    forged = dataclasses.replace(grant, task="other")
    with pytest.raises(PermissionError):
        project_page(source_locator, projector, binding=binding, grant=forged, authority=authority, policy_store=policies, now_ns=NOW)
    assert opened is False


def test_verified_opener_reads_only_pinned_regular_single_link_file(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    source = nested / "turn.jsonl"
    source.write_text('{}\n')
    source.chmod(0o600)
    with open_verified_jsonl_fd(locator(tmp_path, source)) as verified:
        assert stat.S_ISREG(os.fstat(verified.fd).st_mode)
        assert verified.identity.inode_tuple() == (source.stat().st_dev, source.stat().st_ino)
        assert os.read(verified.fd, 3) == b"{}\n"


def test_verified_opener_rejects_traversal_symlink_hardlink_owner_and_type(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.jsonl"
    source.write_text('{}\n')
    source.chmod(0o600)
    with pytest.raises(ValueError):
        SourceLocator("codex", root, Path("../source.jsonl"), os.getuid())
    symlink = root / "symlink.jsonl"
    symlink.symlink_to(source)
    with pytest.raises(OSError):
        open_verified_jsonl_fd(locator(root, symlink))
    hardlink = root / "hard.jsonl"
    os.link(source, hardlink)
    with pytest.raises(PermissionError, match="link"):
        open_verified_jsonl_fd(locator(root, source))
    directory = root / "directory.jsonl"
    directory.mkdir()
    with pytest.raises(PermissionError, match="regular"):
        open_verified_jsonl_fd(locator(root, directory))
    public = root / "public.jsonl"
    public.write_text("{}\n")
    public.chmod(0o644)
    with pytest.raises(PermissionError, match="mode"):
        open_verified_jsonl_fd(locator(root, public))


def test_retained_root_capability_survives_component_swap_without_following_successor(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    source = nested / "turn.jsonl"
    source.write_text('{"old":true}\n')
    source.chmod(0o600)
    sealed_identity = SourceIdentityV1.from_stat(source.stat())
    source_locator = locator(root, source)
    with open_verified_root(source_locator) as root_cap:
        nested.rename(root / "retained")
        nested.mkdir()
        (nested / "turn.jsonl").write_text('{"new":true}\n')
        (nested / "turn.jsonl").chmod(0o600)
        with open_verified_jsonl_fd(source_locator, root_capability=root_cap) as verified:
            assert verified.identity != sealed_identity
            assert os.read(verified.fd, 64) == b'{"new":true}\n'


def test_sqlite_source_is_explicitly_disabled_without_inode_wal_proof(tmp_path: Path) -> None:
    disabled = open_verified_sqlite_source(SourceLocator("hermes", tmp_path, Path("db.sqlite"), os.getuid()))
    assert disabled.source_availability == "disabled"
    assert disabled.connection is None


def test_codex_native_projection_exact_range_redacts_and_omits_raw_tool_result(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    records = [
        codex_user("mail me@example.com", 10),
        {"timestamp": "2026-09-11T08:00:01Z", "ordinal": 11, "type": "response_item", "payload": {"type": "function_call", "name": "Read", "call_id": "raw-call", "arguments": "{\"secret\":1}"}},
        {"timestamp": "2026-09-11T08:00:02Z", "ordinal": 12, "type": "response_item", "payload": {"type": "function_call_output", "call_id": "raw-call", "output": "RAW RESULT"}},
        {"timestamp": "2026-09-11T08:00:03Z", "ordinal": 13, "type": "event_msg", "payload": {"type": "agent_message", "phase": "final_answer", "message": "done sk-abcdefghijklmnopqrstuvwxyz"}},
    ]
    offsets = write_jsonl(source, records)
    scope = authorized(tmp_path, source, provider="codex", start=SourceBoundaryV1(0, 10), end=SourceBoundaryV1(offsets[-1], 14))
    source_locator, projector, authority, policies, binding, grant = scope
    page = project_page(source_locator, projector, binding=binding, grant=grant, authority=authority, policy_store=policies, now_ns=NOW)
    assert [event.kind for event in page.events] == ["user_message", "tool_call", "tool_result", "final_assistant"]
    rendered = json.dumps(page.to_public_dict())
    assert "me@example.com" not in rendered and "RAW RESULT" not in rendered and "raw-call" not in rendered


def test_projection_rejects_binding_with_wrong_turn_source_or_profile_root_before_open(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    offsets = write_jsonl(source, [codex_user("safe", 1)])
    scope = authorized(tmp_path, source, provider="codex", start=SourceBoundaryV1(0, 1), end=SourceBoundaryV1(offsets[-1], 2))
    source_locator, projector, authority, policies, binding, grant = scope
    for forged in (
        dataclasses.replace(binding, turn_start=SourceBoundaryV1(1, 1)),
        dataclasses.replace(binding, source_identity=dataclasses.replace(binding.source_identity, size=0)),
        dataclasses.replace(binding, profile_root_identity=dataclasses.replace(binding.profile_root_identity, inode=1)),
    ):
        with pytest.raises(PermissionError, match="binding"):
            project_page(source_locator, projector, binding=forged, grant=grant, authority=authority, policy_store=policies, now_ns=NOW)


def test_claude_unknown_compaction_sidechain_team_inner_role_and_nonfinal_are_not_promoted(tmp_path: Path) -> None:
    source = tmp_path / "claude.jsonl"
    records = [
        {"uuid": "1", "sessionId": "session", "type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "compact"}},
        {"uuid": "2", "sessionId": "session", "type": "user", "isSidechain": True, "message": {"role": "user", "content": "side"}},
        {"uuid": "3", "sessionId": "session", "type": "user", "teamName": "team", "message": {"role": "user", "content": "team"}},
        {"uuid": "4", "sessionId": "session", "type": "assistant", "message": {"role": "developer", "content": [{"type": "text", "text": "wrong"}]}},
        {"uuid": "5", "sessionId": "session", "type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use", "content": [{"type": "text", "text": "intermediate"}]}},
    ]
    offsets = write_jsonl(source, records)
    scope = authorized(tmp_path, source, provider="claude", start=SourceBoundaryV1(0), end=SourceBoundaryV1(offsets[-1]))
    source_locator, projector, authority, policies, binding, grant = scope
    page = project_page(source_locator, projector, binding=binding, grant=grant, authority=authority, policy_store=policies, now_ns=NOW)
    assert page.events == ()
    assert page.completeness == "partial"
    assert page.dropped_count == len(records)


def test_malformed_duplicate_json_and_invalid_utf8_are_bounded_and_reported(tmp_path: Path) -> None:
    source = tmp_path / "bad.jsonl"
    lines = [
        b'{"timestamp":"2026-09-11T08:00:00Z","type":"event_msg","payload":{"type":"user_message","message":"safe","message":"leak"}}',
        b"\xff\xfe",
        json.dumps(codex_user("visible", 3), separators=(",", ":")).encode(),
    ]
    offsets = write_raw_lines(source, lines)
    scope = authorized(tmp_path, source, provider="codex", start=SourceBoundaryV1(0), end=SourceBoundaryV1(offsets[-1]))
    source_locator, projector, authority, policies, binding, grant = scope
    page = project_page(source_locator, projector, binding=binding, grant=grant, authority=authority, policy_store=policies, now_ns=NOW)
    assert [event.text for event in page.events] == ["visible"]
    assert page.malformed_count == 2 and page.completeness == "partial"


def test_overlong_jsonl_line_fails_closed_with_bounded_scan(tmp_path: Path) -> None:
    source = tmp_path / "big.jsonl"
    source.write_bytes(b'{"padding":"' + b"x" * JSONL_LINE_BYTES + b'"}\n')
    source.chmod(0o600)
    scope = authorized(tmp_path, source, provider="codex", start=SourceBoundaryV1(0), end=SourceBoundaryV1(source.stat().st_size))
    source_locator, projector, authority, policies, binding, grant = scope
    with pytest.raises(ProjectionError, match="line"):
        project_page(source_locator, projector, binding=binding, grant=grant, authority=authority, policy_store=policies, now_ns=NOW)


def test_record_byte_deadline_and_response_budgets_produce_partial_lower_bounds(tmp_path: Path) -> None:
    assert (JSONL_SCAN_BYTES, JSONL_RECORD_LIMIT, JSONL_SCAN_DEADLINE_MS) == (8_388_608, 5_000, 2_000)
    source = tmp_path / "many.jsonl"
    records = [codex_user(str(number), number) for number in range(10)]
    offsets = write_jsonl(source, records)
    scope = authorized(tmp_path, source, provider="codex", start=SourceBoundaryV1(0, 0), end=SourceBoundaryV1(offsets[-1], 10))
    source_locator, projector, authority, policies, binding, grant = scope
    page = project_page(source_locator, projector, binding=binding, grant=grant, authority=authority, policy_store=policies, now_ns=NOW, limits=ProjectionLimits(max_records=4, max_events=2))
    assert len(page.events) == 2 and page.truncated and page.next_cursor is not None
    assert all(not count["exact"] for count in page.counts.values())


def test_signed_cursor_continues_inside_exact_turn_and_cannot_rollback(tmp_path: Path) -> None:
    source = tmp_path / "paged.jsonl"
    records = [codex_user(str(number), number) for number in range(4)]
    offsets = write_jsonl(source, records)
    scope = authorized(tmp_path, source, provider="codex", start=SourceBoundaryV1(0, 0), end=SourceBoundaryV1(offsets[-1], 4))
    source_locator, projector, authority, policies, binding, grant = scope
    first = project_page(source_locator, projector, binding=binding, grant=grant, authority=authority, policy_store=policies, now_ns=NOW, limits=ProjectionLimits(max_events=2))
    second = project_page(source_locator, projector, binding=binding, grant=grant, authority=authority, policy_store=policies, now_ns=NOW + 1, cursor=first.next_cursor, limits=ProjectionLimits(max_events=2))
    assert [event.text for event in (*first.events, *second.events)] == ["0", "1", "2", "3"]
    assert second.next_cursor is None
    with pytest.raises(PermissionError):
        project_page(source_locator, projector, binding=binding, grant=grant, authority=authority, policy_store=policies, now_ns=NOW + 2, cursor=first.next_cursor)


def test_same_inode_safe_suffix_append_preserves_the_sealed_projection(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    offsets = write_jsonl(source, [codex_user("safe", 1)])
    scope = authorized(tmp_path, source, provider="codex", start=SourceBoundaryV1(0, 1), end=SourceBoundaryV1(offsets[-1], 2))
    source_locator, projector, authority, policies, binding, grant = scope
    source.write_text(source.read_text() + "{}\n")
    page = project_page(source_locator, projector, binding=binding, grant=grant, authority=authority, policy_store=policies, now_ns=NOW)
    assert page.source_availability == "available"
    assert [event.text for event in page.events] == ["safe"]


def test_sealed_digest_verification_is_charged_to_projection_scan_budget(
    monkeypatch, tmp_path: Path,
) -> None:
    source = tmp_path / "large.jsonl"
    records = [
        {"timestamp": "2026-09-12T00:00:00Z", "ordinal": 1, "type": "event_msg",
         "payload": {"type": "user_message", "message": "x" * 8_900}},
    ]
    offsets = write_jsonl(source, records)
    locator, projector, authority, policies, binding, grant = authorized(
        tmp_path, source, provider="codex",
        start=SourceBoundaryV1(0, 1), end=SourceBoundaryV1(offsets[-1], 2),
    )

    original = os.pread
    consumed = 0

    def bounded(fd: int, count: int, offset: int) -> bytes:
        nonlocal consumed
        consumed += count
        return original(fd, count, offset)

    monkeypatch.setattr("kanban_adapter.transcript_projection.os.pread", bounded)
    page = project_page(
        locator, projector, binding=binding, grant=grant, authority=authority,
        policy_store=policies, limits=ProjectionLimits(max_scan_bytes=128), now_ns=NOW,
    )
    assert consumed <= 128
    assert page.truncated is True


def test_legacy_resolver_never_opens_source_and_returns_content_free_states() -> None:
    resolver = LegacyObservationResolver()
    assert resolver.classify(has_trusted_binding=False) == "unavailable"
    assert resolver.classify(has_trusted_binding=True) == "trusted_binding_required"


def test_hermes_projector_remains_disabled_and_declares_unsupported_capability() -> None:
    projector = HermesProjector()
    assert projector.schema_pin.startswith("disabled")
    assert projector.capabilities == {"all": "unsupported"}
    with pytest.raises(ProjectionError, match="disabled"):
        projector.project({})
