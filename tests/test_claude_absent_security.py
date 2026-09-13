from __future__ import annotations

import json
import os

import pytest

from kanban_adapter import claude_absent as absent
from kanban_adapter import claude_hook as hook
from kanban_adapter.conversation import CollectionPolicyV1
from test_claude_missing_transcript import PROMPT_ID, _harness, _native_turn


def _start(tmp_path, monkeypatch):
    values = _harness(tmp_path, monkeypatch)
    source, service, commands, adapter, payload, cache = values
    payload["prompt_id"] = PROMPT_ID
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    return values


def test_absent_contract_documents_narrower_trust():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert "docs/claude-absent-start-security.md" in (root / "README.md").read_text()
    text = (root / "docs/claude-absent-start-security.md").read_text()
    for clause in ("LIMITED PARTIAL", "동일 UID", "prompt_id", "promptId", "2.1.268", "비동기", "재수집하지"):
        assert clause in text



@pytest.mark.parametrize("final_event", ["stop", "session-end"])
@pytest.mark.parametrize("value", [[], {}, None, True, 17, 1.5])
@pytest.mark.parametrize("field", ["type", "sessionId", "promptId", "uuid", "parentUuid", "message", "message.role", "message.content", "message.stop_reason", "block.type", "block.text", "block.id", "block.name", "block.tool_use_id"])
def test_malformed_absent_fields_close_without_binding(tmp_path, monkeypatch, final_event, value, field):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    records = [json.loads(line) for line in source.read_text().splitlines()]
    record = records[1]
    if field.startswith("message."):
        record["message"][field.split(".")[1]] = value
    elif field.startswith("block."):
        record["message"]["content"][0][field.split(".")[1]] = value
    else:
        record[field] = value
    source.write_text("".join(json.dumps(row) + "\n" for row in records))
    hook.handle_event(final_event, payload, adapter=adapter, cache_dir=cache)
    assert not hook._state_path(cache, "native").exists()
    assert sum(command[0] == "done" for command in commands) == 1
    # 네이티브 스트리밍의 null stop_reason은 유효하며 내용 형식 오류와 구분한다.
    if field == "message.stop_reason" and value is None:
        assert service.bindings.get("demo", "t_native")[0].turn_end.byte_offset == source.stat().st_size
    else:
        with pytest.raises(FileNotFoundError):
            service.bindings.get("demo", "t_native")


def test_old_stop_must_not_complete_current_prompt(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    hook.handle_event("stop", {**payload, "prompt_id": "650e8400-e29b-41d4-a716-446655440000"}, adapter=adapter, cache_dir=cache)
    assert hook._state_path(cache, "native").exists()
    assert not any(c[0] == "done" for c in commands)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


def test_distinct_native_prompt_ids_do_not_reuse_card_key(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    payload["prompt_id"] = "650e8400-e29b-41d4-a716-446655440000"
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    starts = [c for c in commands if c[0] == "start"]
    keys = [c[c.index("--idempotency-key") + 1] for c in starts]
    assert len(set(keys)) == 2


def test_repeated_prompt_does_not_rearm_existing_source(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    original = hook._state_path(cache, "native").read_bytes()
    _native_turn(source)
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert hook._state_path(cache, "native").read_bytes() == original
    assert len([c for c in commands if c[0] == "start"]) == 1


@pytest.mark.parametrize("failure", ["absent", "wrong-prompt", "wrong-session", "symlink", "hardlink", "mode", "root-swap", "ancestor-swap", "duplicate-prompt", "receipt", "policy", "oversize"])
def test_uncertain_absent_source_closes_lifecycle_without_binding(tmp_path, monkeypatch, failure):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    if failure == "absent":
        source.unlink()
    elif failure == "wrong-prompt":
        _native_turn(source, "650e8400-e29b-41d4-a716-446655440000")
    elif failure == "wrong-session":
        source.write_text(source.read_text().replace('"sessionId": "native"', '"sessionId": "foreign"'))
    elif failure in {"symlink", "hardlink"}:
        peer = source.with_suffix(".peer")
        source.rename(peer)
        if failure == "symlink":
            source.symlink_to(peer)
        else:
            os.link(peer, source)
    elif failure == "mode":
        source.chmod(0o644)
    elif failure in {"root-swap", "ancestor-swap"}:
        target = source.parent.parent if failure == "root-swap" else tmp_path
        target.rename(target.with_name(target.name + "-old"))
        target.mkdir(mode=0o700)
        # 상위 디렉터리 교체 시 서비스 보조 파일과 캐시를 보존하여
        # 정책 부재가 아니라 소스 권한 검증 단계까지 도달하게 한다.
        if failure == "ancestor-swap":
            old = target.with_name(target.name + "-old")
            for name in ("policy.json", "cache"):
                (old / name).rename(target / name)
            source.parent.parent.mkdir(mode=0o700)
        _native_turn(source)
    elif failure == "duplicate-prompt":
        source.write_bytes(source.read_bytes() * 2)
    elif failure == "receipt":
        state_path = hook._state_path(cache, "native")
        state = json.loads(state_path.read_text())
        state["observation_receipt"]["nonce"] = "other"
        state_path.write_text(json.dumps(state))
    elif failure == "policy":
        service.policies.replace(CollectionPolicyV1.disabled(version=2, generation=3), expected_generation=2)
    elif failure == "oversize":
        monkeypatch.setattr(absent, "JSONL_SCAN_BYTES", 8)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    assert not hook._state_path(cache, "native").exists()
    assert any(c[0] == "done" for c in commands)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


def test_delayed_tail_and_next_turn_never_backfill_snapshot(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    first = source.read_bytes().splitlines(keepends=True)[0]
    source.write_bytes(first + b'{"type": "assistant"')
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    binding, _ = service.bindings.get("demo", "t_native")
    assert binding.turn_end.byte_offset == len(first)
    _native_turn(source)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    assert service.bindings.get("demo", "t_native")[0] == binding
    page = service.get_parent_page(principal_id="owner", board="demo", task="t_native", cursor=None, limit=20)
    assert page["completeness"] == "partial"
    assert all(e.get("text") != "public final answer" for e in page["events"])


def test_next_turn_is_excluded_at_exact_prompt_boundary(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    first = source.read_bytes()
    _native_turn(source, "650e8400-e29b-41d4-a716-446655440000")
    source.write_bytes(first + source.read_bytes())
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    binding, _ = service.bindings.get("demo", "t_native")
    assert binding.turn_end.byte_offset == len(first)


def test_policy_change_during_snapshot_prevents_publication(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    original = service._digest_range
    def change_policy(fd, start, end):
        value = original(fd, start, end)
        current = service.policies.load()
        service.policies.replace(CollectionPolicyV1(
            version=2, generation=3, activated_at_ns=current.activated_at_ns,
            expires_at_ns=current.expires_at_ns, enabled_boards={"demo": frozenset({"claude"})}),
            expected_generation=2)
        return value
    monkeypatch.setattr(service, "_digest_range", change_policy)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")
    assert any(c[0] == "done" for c in commands)


def test_replacement_after_first_pin_denied(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    original = service._digest_range
    def replace(fd, start, end):
        value = original(fd, start, end)
        source.rename(source.with_suffix(".old"))
        _native_turn(source)
        return value
    monkeypatch.setattr(service, "_digest_range", replace)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")
    assert any(c[0] == "done" for c in commands)
