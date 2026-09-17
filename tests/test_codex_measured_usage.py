"""본문 없는 Codex 0.154 사용량 기록을 검증하며 공급자 호출이나 운영 상태 접근은 하지 않는다."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kanban_adapter.usage import usage_comment, usage_event_id
from kanban_adapter.token_usage import codex_token_events, token_snapshot


def record(response="response-private", at="2026-09-17T01:45:40.406Z", turn="turn-a"):
    return {"timestamp": at, "ordinal": 13, "type": "token_usage_record", "payload": {
        "thread_id": "session-a", "session_id": "session-a", "turn_id": turn,
        "root_turn_id": turn, "response_id": response,
        "usage": {"input_tokens": 16081, "cached_input_tokens": 11904,
                  "cache_write_input_tokens": 0, "output_tokens": 11,
                  "reasoning_output_tokens": 0, "total_tokens": 16092},
        "turn_token_usage": {"total_tokens": 999999},
        "thread_token_usage": {"total_tokens": 9999999},
    }}


def boundary(kind, turn="turn-a", at="2026-09-17T01:45:48.648Z"):
    return {"timestamp": at, "type": "event_msg", "payload": {"type": kind, "turn_id": turn}}


def rows():
    r = record()
    return [
        {"type": "session_meta", "payload": {"id": "session-a"}},
        boundary("task_started", at="2026-09-17T01:45:27.936Z"),
        {"type": "turn_context", "payload": {"turn_id": "turn-a", "model": "fixture-model"}},
        r,
        {"timestamp": "2026-09-17T01:45:40.407Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": r["payload"]["usage"],
                                              "last_token_usage": r["payload"]["usage"]}}},
        boundary("task_complete"),
    ]


def write_rows(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in data))


def request_comment(event):
    request_hash = event["request_hash"]
    return usage_comment(source="codex", model="fixture-model", usage={},
        tokens=event["tokens"], usage_at=event["usage_at"], usage_timing="request",
        request_hash=request_hash, event_id=usage_event_id("codex", "t_abcdef12", request_hash))


def test_measured_request_projects_into_existing_schema2_without_snapshot(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    write_rows(rollout, rows())
    event, = codex_token_events(rollout, session="session-a", turn="turn-a", root=tmp_path)
    result = json.loads(request_comment(event).partition("\n")[2])
    assert result.get("usage_timing") == "request"
    assert result["usage_at"] == 1789609540
    assert "completion_at" not in result
    assert result["tokens"] == {"input": 4177, "cache_read": 11904, "cache_write": 0,
                                "output": 11, "reasoning": 0, "total": 16092, "requests": 1}
    request_hash = hashlib.sha256(json.dumps(
        ["codex", "session-a", "turn-a", "response-private"], separators=(",", ":")
    ).encode()).hexdigest()[:16]
    assert result["request_hash"] == request_hash
    assert result["event_id"] == usage_event_id("codex", "t_abcdef12", request_hash)
    assert "response-private" not in json.dumps(result)
    assert result["model"] == "fixture-model"
    assert event["source_timestamp"] == "2026-09-17T01:45:40.406Z"


def test_duplicate_requests_and_replayed_turns_are_counted_once(tmp_path):
    path = tmp_path / "rollout.jsonl"
    data = rows()
    write_rows(path, data[:4] + [record(), record("request-2")] + data[4:] + data)
    events = codex_token_events(path, session="session-a", turn="turn-a", root=tmp_path)
    assert events is not None
    assert len(events) == 2
    assert sum(event["tokens"]["total"] for event in events) == 32184


@pytest.mark.parametrize("case", ["unclosed", "before-start", "after-end", "foreign-meta",
    "missing-meta", "missing-start", "missing-time", "naive-time", "bad-time",
    "foreign-session", "foreign-thread", "foreign-turn", "foreign-root", "missing-response",
    "bool-count", "negative-count", "too-large-count", "cache-exceeds-input"])
def test_invalid_native_evidence_never_falls_back_to_cumulative(tmp_path, case):
    rollout = tmp_path / "rollout.jsonl"
    data = rows()
    if case == "unclosed": data.pop()
    elif case == "before-start": data[3]["timestamp"] = "2026-09-17T01:45:00Z"
    elif case == "after-end": data[3]["timestamp"] = "2026-09-17T01:46:00Z"
    elif case == "foreign-meta": data[0]["payload"]["id"] = "foreign"
    elif case == "missing-meta": data.pop(0)
    elif case == "missing-start": data.pop(1)
    elif case == "missing-time": data[3].pop("timestamp")
    elif case == "naive-time": data[3]["timestamp"] = "2026-09-17T01:45:40"
    elif case == "bad-time": data[3]["timestamp"] = True
    elif case.startswith("foreign-"):
        key = {"foreign-session": "session_id", "foreign-thread": "thread_id",
               "foreign-turn": "turn_id", "foreign-root": "root_turn_id"}[case]
        data[3]["payload"][key] = "foreign"
    elif case == "missing-response": data[3]["payload"].pop("response_id")
    else:
        data[3]["payload"]["usage"]["input_tokens"] = {
            "bool-count": True, "negative-count": -1, "too-large-count": 10**30,
            "cache-exceeds-input": 1}[case]
    write_rows(rollout, data)
    assert codex_token_events(rollout, session="session-a", turn="turn-a", root=tmp_path) == []


def test_two_turns_same_response_id_and_late_rows_remain_scoped(tmp_path):
    path = tmp_path / "rollout.jsonl"
    late = record("late-a")
    data = rows() + [boundary("task_started", "turn-b", "2026-09-17T01:46:00Z"),
        late, record(turn="turn-b", at="2026-09-17T01:46:01Z"),
        boundary("task_complete", "turn-b", "2026-09-17T01:46:02Z"), late]
    write_rows(path, data)
    a = codex_token_events(path, session="session-a", turn="turn-a", root=tmp_path)
    b = codex_token_events(path, session="session-a", turn="turn-b", root=tmp_path)
    assert a is not None and b is not None
    assert len(a) == len(b) == 1
    assert a[0]["request_hash"] != b[0]["request_hash"]
    assert a[0]["tokens"]["total"] == b[0]["tokens"]["total"] == 16092


def test_kst_midnight_uses_each_request_timestamp_not_completion(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    path = tmp_path / "rollout.jsonl"
    data = [rows()[0], boundary("task_started", at="2026-09-17T14:59:00Z"),
        record("before", "2026-09-17T14:59:59.999Z"),
        record("after", "2026-09-17T15:00:00.001Z"),
        boundary("task_complete", at="2026-09-17T15:01:00Z")]
    write_rows(path, data)
    events = codex_token_events(path, session="session-a", turn="turn-a", root=tmp_path)
    assert events is not None
    by_day = {}
    for event in events:
        day = datetime.fromtimestamp(event["usage_at"], ZoneInfo("Asia/Seoul")).date().isoformat()
        by_day[day] = by_day.get(day, 0) + event["tokens"]["total"]
    assert by_day == {"2026-09-17": 16092, "2026-09-18": 16092}


@pytest.mark.parametrize("value", [None, 0, 13])
def test_measured_cache_write_distinguishes_absent_zero_and_known(tmp_path, value):
    path = tmp_path / "rollout.jsonl"
    data = rows()
    if value is None:
        data[3]["payload"]["usage"].pop("cache_write_input_tokens")
    else:
        data[3]["payload"]["usage"]["cache_write_input_tokens"] = value
    write_rows(path, data)
    events = codex_token_events(path, session="session-a", turn="turn-a", root=tmp_path)
    assert events is not None and len(events) == 1
    assert events[0]["tokens"]["cache_write"] == value
    assert events[0]["tokens"]["total"] == 16092  # 원본의 총량을 따르며 캐시나 추론 토큰을 더하지 않는다.


def test_measured_reader_keeps_runtime_path_guard(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.jsonl"
    write_rows(outside, rows())
    link = root / "link.jsonl"
    link.symlink_to(outside)
    for path, error in [(outside, "outside"), (link, "symlink")]:
        with pytest.raises(ValueError, match=error):
            codex_token_events(path, session="session-a", turn="turn-a", root=root)


def test_unflushed_completion_line_does_not_seal_measured_batch(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_rows(path, rows())
    path.write_text(path.read_text().rstrip("\n"))
    assert codex_token_events(path, session="session-a", turn="turn-a", root=tmp_path) == []


def test_missing_hook_scope_does_not_infer_session_or_turn(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_rows(path, rows())
    assert codex_token_events(path, session="", turn="turn-a", root=tmp_path) == []
    assert codex_token_events(path, session="session-a", turn="", root=tmp_path) == []


def test_legacy_snapshot_does_not_count_duplicate_token_count_requests(tmp_path):
    path = tmp_path / "rollout.jsonl"
    snapshot = rows()[4]
    write_rows(path, [snapshot, snapshot, snapshot])
    assert codex_token_events(path, session="session-a", turn="turn-a", root=tmp_path) is None
    tokens = token_snapshot("codex", path, root=tmp_path)
    assert tokens["requests"] == 1
    assert tokens["total"] == 16092
    assert tokens["cache_write"] == 0


def test_boundary_bearing_legacy_turn_preserves_none_contract(tmp_path):
    path = tmp_path / "rollout.jsonl"
    data = rows()
    write_rows(path, data[:3] + data[4:])

    assert codex_token_events(
        path, session="session-a", turn="turn-a", root=tmp_path,
    ) is None


def test_completed_turn_without_a_model_request_has_no_usage(tmp_path):
    path = tmp_path / "rollout.jsonl"
    data = rows()
    write_rows(path, data[:3] + data[-1:])

    assert codex_token_events(
        path, session="session-a", turn="turn-a", root=tmp_path,
    ) == []


def test_invalid_duplicate_cannot_hide_behind_an_earlier_valid_request(tmp_path):
    path = tmp_path / "rollout.jsonl"
    conflicting = record()
    conflicting["payload"]["usage"]["input_tokens"] = True
    write_rows(path, rows()[:4] + [conflicting] + rows()[4:])
    assert codex_token_events(path, session="session-a", turn="turn-a", root=tmp_path) == []


def test_contradictory_session_metadata_is_not_repaired_by_later_header(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_rows(path, [{"type": "session_meta", "payload": {"id": "foreign"}}] + rows())
    assert codex_token_events(path, session="session-a", turn="turn-a", root=tmp_path) == []


def test_conflicting_replay_does_not_arbitrarily_select_usage(tmp_path):
    path = tmp_path / "rollout.jsonl"
    conflicting = record()
    conflicting["payload"]["usage"]["output_tokens"] = 999
    write_rows(path, rows()[:4] + [conflicting] + rows()[4:])
    assert codex_token_events(path, session="session-a", turn="turn-a", root=tmp_path) == []


def prior_native_turn():
    earlier = record("earlier", "2026-09-17T01:44:01Z", turn="turn-before")
    return [
        boundary("task_started", "turn-before", "2026-09-17T01:44:00Z"),
        earlier,
        boundary("task_complete", "turn-before", "2026-09-17T01:44:02Z"),
    ]


@pytest.mark.parametrize("kind", ["empty", "legacy"])
def test_target_turn_classification_ignores_earlier_native_usage(tmp_path, kind):
    path = tmp_path / "rollout.jsonl"
    target = [boundary("task_started", at="2026-09-17T01:45:27.936Z")]
    if kind == "legacy":
        target.append(rows()[4])
    target.append(boundary("task_complete"))
    write_rows(path, [rows()[0], *prior_native_turn(), *target])

    result = codex_token_events(path, session="session-a", turn="turn-a", root=tmp_path)
    if kind == "legacy":
        assert result is None
    else:
        assert result == []


@pytest.mark.parametrize("fault", [
    "info", "total", "bool", "negative", "too-large", "cache-exceeds-input", "no-counts",
])
def test_malformed_target_legacy_evidence_is_not_authorized(tmp_path, fault):
    path = tmp_path / "rollout.jsonl"
    token_count = rows()[4]
    if fault == "info":
        token_count["payload"]["info"] = "bad"
    elif fault == "total":
        token_count["payload"]["info"]["total_token_usage"] = "bad"
    elif fault == "no-counts":
        token_count["payload"]["info"]["total_token_usage"] = {}
    else:
        token_count["payload"]["info"]["total_token_usage"]["input_tokens"] = {
            "bool": True,
            "negative": -1,
            "too-large": 10**30,
            "cache-exceeds-input": 1,
        }[fault]
    write_rows(path, rows()[:3] + [token_count] + rows()[-1:])

    assert codex_token_events(
        path, session="session-a", turn="turn-a", root=tmp_path,
    ) == []
