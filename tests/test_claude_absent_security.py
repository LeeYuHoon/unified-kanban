from __future__ import annotations

import errno
import json
import os

import pytest

from kanban_adapter import claude_absent as absent
from kanban_adapter import claude_hook as hook
from kanban_adapter import claude_pending_final as queue
from kanban_adapter import claude_file_provenance as file_provenance
from kanban_adapter.conversation import CollectionPolicyV1
from test_claude_missing_transcript import PROMPT_ID, _harness, _native_turn


def _start(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "launch", lambda path: None)
    values = _harness(tmp_path, monkeypatch)
    source, service, commands, adapter, payload, cache = values
    payload["prompt_id"] = PROMPT_ID
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    return values


def _unpublished(service, monkeypatch):
    monkeypatch.setattr(service.bindings, "put", lambda **kw: pytest.fail("binding publication attempted"))
    monkeypatch.setattr(service.authority, "begin_binding", lambda **kw: pytest.fail("binding authority attempted"))


def _job_state(service, cache, status):
    job, = (cache / "pending-final").glob("*.json")
    body = json.loads(job.read_bytes())
    assert body["status"] == status
    assert body["attempts"] == 1
    assert body["mac"] == queue._mac(service, {k: v for k, v in body.items() if k != "mac"})
    assert body["kwargs"]["prompt_id"] == PROMPT_ID
    assert body["kwargs"]["task"] == "t_native"
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")
    return job, body


def _guard_errors(monkeypatch, module, name):
    original = getattr(module, name)
    errors = []
    def checked(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except (OSError, ValueError) as error:
            errors.append(str(error))
            raise
    monkeypatch.setattr(module, name, checked)
    return errors


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
def test_malformed_absent_fields_reject_without_completion_or_binding(tmp_path, monkeypatch, final_event, value, field):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    records = [json.loads(line) for line in source.read_text().splitlines()]
    _unpublished(service, monkeypatch)
    errors = _guard_errors(monkeypatch, absent, "_range")
    record = records[1]
    if field.startswith("message."):
        record["message"][field.split(".")[1]] = value
    elif field.startswith("block."):
        record["message"]["content"][0][field.split(".")[1]] = value
    else:
        record[field] = value
    source.write_text("".join(json.dumps(row) + "\n" for row in records))
    hook.handle_event(final_event, payload, adapter=adapter, cache_dir=cache)
    # null은 유효한 스트리밍 데이터지만 public-final 표시는 아니다.
    streaming = field == "message.stop_reason" and value is None
    job, body = _job_state(service, cache, "pending" if streaming else "rejected")
    assert hook._state_path(cache, "native").exists() is (not streaming)
    assert sum(command[0] == "done" for command in commands) == int(streaming)
    if streaming:
        assert errors == []
        assert body["first_open"]["captured_eof"] == source.stat().st_size
    else:
        expected = {
            "type": "invalid absent snapshot type",
            "sessionId": "invalid absent snapshot identity field",
            "promptId": "invalid absent snapshot identity field",
            "uuid": "invalid absent snapshot identity field",
            "parentUuid": "unbound message parent" if value is None else "invalid absent snapshot parent",
            "message": "invalid absent snapshot message",
            "message.role": "invalid absent snapshot message",
            "message.content": "invalid absent snapshot content",
            "message.stop_reason": "invalid absent snapshot stop reason",
            "block.type": "invalid absent snapshot content block",
            "block.text": "invalid absent snapshot text",
            "block.id": "invalid absent snapshot tool identity",
            "block.name": "invalid absent snapshot tool identity",
            "block.tool_use_id": "invalid absent snapshot tool identity",
        }[field]
        assert errors == [expected]
        assert "first_open" not in body
        before = job.read_bytes()
        assert queue.run_once(service, job) == "rejected"
        assert job.read_bytes() == before


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
def test_uncertain_absent_source_retains_rejected_lifecycle_without_binding(tmp_path, monkeypatch, failure):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    _unpublished(service, monkeypatch)
    errors = {name: _guard_errors(monkeypatch, absent, name)
              for name in ("_final_authorize", "_ancestry", "prepare_final")}
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
        monkeypatch.setattr(file_provenance, "JSONL_SCAN_BYTES", 8)
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    missing = failure == "absent"
    job, body = _job_state(service, cache, "pending" if missing else "rejected")
    assert "first_open" not in body
    assert hook._state_path(cache, "native").exists() is (not missing)
    assert sum(c[0] == "done" for c in commands) == int(missing)
    if not missing:
        expected = {
            "wrong-prompt": "first prompt boundary mismatch",
            "wrong-session": "absent snapshot session mismatch",
            "symlink": None,
            "hardlink": "source owner/single link requirement failed",
            "mode": "source mode must be owner-private",
            "root-swap": "absent-source ancestry identity changed",
            "ancestor-swap": "absent-source ancestry identity changed",
            "duplicate-prompt": "duplicate prompt boundary",
            "receipt": "absent preparation scope mismatch",
            "policy": "conversation collection policy is disabled",
            "oversize": "file snapshot byte budget exceeded",
        }[failure]
        observed = [error for group in errors.values() for error in group]
        assert observed
        if failure == "symlink":
            assert any(error.startswith(f"[Errno {errno.ELOOP}]") for error in observed)
        else:
            assert expected in observed
        before = job.read_bytes()
        assert queue.run_once(service, job) == "rejected"
        assert job.read_bytes() == before


def test_legacy_delayed_tail_never_backfills_snapshot(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    first = source.read_bytes().splitlines(keepends=True)[0]
    source.write_bytes(first + b'{"type": "assistant"')
    state = json.loads(hook._state_path(cache, "native").read_bytes())
    kwargs = dict(board="demo", task="t_native", prepared=state["conversation_prepared"],
                  task_receipt=state["observation_receipt"], prompt_id=PROMPT_ID)
    binding = absent.seal(service, **kwargs)
    assert binding.turn_end.byte_offset == len(first)
    _native_turn(source)
    assert absent.seal(service, **kwargs) is None
    assert service.bindings.get("demo", "t_native")[0] == binding
    page = service.get_parent_page(principal_id="owner", board="demo", task="t_native", cursor=None, limit=20)
    assert page["completeness"] == "partial"
    assert all(e.get("text") != "public final answer" for e in page["events"])


@pytest.mark.parametrize("target_final", [False, True])
def test_delayed_tail_waits_for_target_final_and_excludes_next_turn(tmp_path, monkeypatch, target_final):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    request, final = source.read_bytes().splitlines(keepends=True)
    prefix = final[:20] if target_final else b""
    source.write_bytes(request + prefix)
    with monkeypatch.context() as patcher:
        _unpublished(service, patcher)
        hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
        job, body = _job_state(service, cache, "pending")
    assert not hook._state_path(cache, "native").exists()
    assert sum(c[0] == "done" for c in commands) == 1
    pin = body["first_open"]
    assert pin["captured_eof"] == len(request + prefix)
    assert pin["mac"] == absent._mac(service, {k: v for k, v in pin.items() if k != "mac"})
    next_rows = [json.loads(request), json.loads(final)]
    next_rows[0].update(promptId="650e8400-e29b-41d4-a716-446655440000", uuid="next-user")
    next_rows[1].update(uuid="next-assistant", parentUuid="next-user")
    next_rows[1]["message"]["content"][0]["text"] = "foreign next final"
    next_bytes = b"".join((json.dumps(row) + "\n").encode() for row in next_rows)
    with source.open("ab") as stream:
        if target_final:
            stream.write(final[len(prefix):])
        stream.write(next_bytes)
    if not target_final:
        with monkeypatch.context() as patcher:
            _unpublished(service, patcher)
            assert queue.run_once(service, job) == "pending"
        with pytest.raises(FileNotFoundError):
            service.bindings.get("demo", "t_native")
    else:
        assert queue.run_once(service, job) == "ready"
        binding, _ = service.bindings.get("demo", "t_native")
        assert binding.turn_end.byte_offset == len(request + final)
        page = service.get_parent_page(principal_id="owner", board="demo", task="t_native", cursor=None, limit=20)
        assert [e["text"] for e in page["events"]] == ["public new turn", "public final answer"]
        assert page["completeness"] == "partial"
        before = job.read_bytes()
        with source.open("ab") as stream:
            stream.write(next_bytes)
        assert queue.run_once(service, job) == "ready"
        assert job.read_bytes() == before
        assert service.bindings.get("demo", "t_native")[0] == binding
    assert json.loads(job.read_bytes())["first_open"] == pin


def test_next_turn_is_excluded_at_exact_prompt_boundary(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    first = source.read_bytes()
    _native_turn(source, "650e8400-e29b-41d4-a716-446655440000")
    source.write_bytes(first + source.read_bytes())
    hook.handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    binding, _ = service.bindings.get("demo", "t_native")
    assert binding.turn_end.byte_offset == len(first)


@pytest.mark.parametrize("failure", ["policy", "replacement"])
def test_legacy_snapshot_races_still_prevent_publication(tmp_path, monkeypatch, failure):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    state = json.loads(hook._state_path(cache, "native").read_bytes())
    _native_turn(source)
    original = service._digest_range
    calls = []
    def race(fd, start, end):
        calls.append((start, end))
        digest = original(fd, start, end)
        if failure == "policy":
            current = service.policies.load()
            service.policies.replace(CollectionPolicyV1(
                version=2, generation=3, activated_at_ns=current.activated_at_ns,
                expires_at_ns=current.expires_at_ns,
                enabled_boards={"demo": frozenset({"claude"})}), expected_generation=2)
        else:
            source.rename(source.with_suffix(".old"))
            _native_turn(source)
        return digest
    monkeypatch.setattr(service, "_digest_range", race)
    monkeypatch.setattr(service.bindings, "put", lambda **kw: pytest.fail("legacy race published"))
    expected = ("policy lease cannot upgrade a reader" if failure == "policy"
                else "absent snapshot source replaced after pin")
    with pytest.raises(PermissionError, match=expected):
        absent.seal(service, board="demo", task="t_native",
                    prepared=state["conversation_prepared"],
                    task_receipt=state["observation_receipt"], prompt_id=PROMPT_ID)
    assert calls == [(0, source.stat().st_size)]
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


def test_policy_change_during_snapshot_prevents_publication(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    _unpublished(service, monkeypatch)
    errors = _guard_errors(monkeypatch, absent, "prepare_final")
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
    # 작업 도중의 협력 writer는 게시 자체가 거절되어 기존 generation이 유지된다.
    assert errors == ["policy lease cannot upgrade a reader"]
    assert service.policies.load().generation == 2
    _job_state(service, cache, "rejected")
    assert hook._state_path(cache, "native").exists()
    assert not any(c[0] == "done" for c in commands)


def test_replacement_after_first_pin_denied(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _start(tmp_path, monkeypatch)
    _native_turn(source)
    _unpublished(service, monkeypatch)
    errors = _guard_errors(monkeypatch, absent, "prepare_final")
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
    assert errors == ["file snapshot replaced after pin"]
    _job_state(service, cache, "rejected")
    assert hook._state_path(cache, "native").exists()
    assert not any(c[0] == "done" for c in commands)
