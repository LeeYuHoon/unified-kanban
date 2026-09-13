"""승인된 여섯 가지 0.154 레코드 형태를 재현하는 합성 비식별 픽스처다.

네이티브 페이로드, 비공개 식별자, 추론 또는 소스 파일은 보존하지 않는다.
정상 픽스처는 범위 내 합성 순번을 사용하며 실제 바인딩 불일치
(네이티브 순번 8..13과 봉인 범위 [0,6))는 별도로 재현한다.
"""
import copy
import json

import pytest

from kanban_adapter.conversation import SourceBoundaryV1
from kanban_adapter.transcript_projection import ProjectionLimits, project_page
from test_transcript_projection import NOW, authorized, write_jsonl

MARKER = "CODEX_NATIVE_FRESH_c481d24a5b3041418da7e9e5176edf92"
REQUEST = "Reply with exactly this marker: " + MARKER
PRIVATE = "SYNTHETIC_PRIVATE_CANARY"


def native_records():
    metadata = {"turn_id": PRIVATE, "create_time": 1.0, "content_item_kinds": ["text"]}
    usage = dict.fromkeys(("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"), 0)
    payloads = [
        ("response_item", {"type": "message", "id": PRIVATE, "role": "user", "content": [{"type": "input_text", "text": REQUEST}], "internal_chat_message_metadata_passthrough": metadata}),
        ("event_msg", {"type": "item_completed", "thread_id": PRIVATE, "turn_id": PRIVATE, "item": {"type": "UserMessage", "id": PRIVATE, "content": [{"type": "text", "text": REQUEST, "text_elements": []}]}, "started_at_ms": 1, "completed_at_ms": 2}),
        ("event_msg", {"type": "item_completed", "thread_id": PRIVATE, "turn_id": PRIVATE, "item": {"type": "AgentMessage", "id": PRIVATE, "content": [{"type": "Text", "text": MARKER}], "phase": "final_answer"}, "started_at_ms": 1, "completed_at_ms": 2}),
        ("response_item", {"type": "message", "id": PRIVATE, "role": "assistant", "content": [{"type": "output_text", "text": MARKER}], "phase": "final_answer", "internal_chat_message_metadata_passthrough": metadata}),
        ("token_usage_record", {**dict.fromkeys(("thread_id", "turn_id", "session_id", "root_turn_id", "response_id"), PRIVATE), "usage": usage, "turn_token_usage": usage, "thread_token_usage": usage}),
        ("event_msg", {"type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage, "model_context_window": 1}, "rate_limits": {"limit_id": PRIVATE, "limit_name": None, "primary": {"used_percent": 0.0, "window_minutes": 1, "resets_at": 1}, "secondary": None, "credits": {"has_credits": False, "unlimited": False, "balance": PRIVATE}, "individual_limit": None, "spend_control_reached": None, "plan_type": PRIVATE, "rate_limit_reached_type": None}}),
    ]
    return [{"timestamp": "2026-09-13T00:00:00Z", "ordinal": i, "type": t, "payload": copy.deepcopy(p)} for i, (t, p) in enumerate(payloads)]


def page_for(tmp_path, records, **kwargs):
    source = tmp_path / "synthetic.jsonl"
    offsets = write_jsonl(source, records)
    loc, projector, authority, policies, binding, grant = authorized(tmp_path, source, provider="codex", start=SourceBoundaryV1(0, 0), end=SourceBoundaryV1(offsets[-1], 6))
    return project_page(loc, projector, binding=binding, grant=grant, authority=authority, policy_store=policies, now_ns=NOW, **kwargs)


def test_synthetic_0154_request_final_only(tmp_path):
    page = page_for(tmp_path, native_records())
    assert [(e.kind, e.text) for e in page.events] == [("user_message", REQUEST), ("final_assistant", MARKER)]
    assert page.malformed_count == 0
    assert page.dropped_count == 4
    assert page.next_cursor is None
    assert page.completeness == "partial"
    rendered = json.dumps(page.to_public_dict())
    assert PRIVATE not in rendered
    assert "internal_chat_message_metadata_passthrough" not in rendered
    assert "token_usage" not in rendered


@pytest.mark.parametrize("where", ["outer", "payload", "metadata", "block"])
def test_unknown_fields_fail_closed(tmp_path, where):
    record = native_records()[0]
    target = {"outer": record, "payload": record["payload"], "metadata": record["payload"]["internal_chat_message_metadata_passthrough"], "block": record["payload"]["content"][0]}[where]
    target["unknown_private_field"] = PRIVATE
    page = page_for(tmp_path, [record])
    assert not page.events
    assert page.malformed_count == 1


@pytest.mark.parametrize("ordinal", [None, True, -1, 1.5, "0", [], {}, 6])
def test_invalid_ordinals_fail_closed(tmp_path, ordinal):
    record = native_records()[0]
    record["ordinal"] = ordinal
    page = page_for(tmp_path, [record])
    assert not page.events
    assert page.malformed_count == 1


@pytest.mark.parametrize("role,phase", [("assistant", "analysis"), ("assistant", None), ("assistant", "commentary"), ("system", "final_answer"), ("developer", None), ("tool", None), ("user", "analysis")])
def test_nonpublic_roles_and_phases_never_promoted(tmp_path, role, phase):
    record = native_records()[3]
    record["payload"].update(role=role, phase=phase)
    record["payload"]["content"][0]["text"] = PRIVATE
    page = page_for(tmp_path, [record])
    assert not page.events
    assert PRIVATE not in json.dumps(page.to_public_dict())


@pytest.mark.parametrize("kind", ["compacted", "turn_context", "session_meta", "unknown", [], None])
def test_unknown_or_private_record_types_never_promoted(tmp_path, kind):
    record = native_records()[3]
    record["type"] = kind
    page = page_for(tmp_path, [record])
    assert not page.events


def test_actual_ordinal_mismatch_must_not_be_silently_rebased(tmp_path):
    records = native_records()
    for record, ordinal in zip(records, range(8, 14)):
        record["ordinal"] = ordinal
    page = page_for(tmp_path, records)
    assert not page.events
    assert page.malformed_count == 6
    assert page.source_availability == "available"
    assert page.completeness == "partial"


def test_ordinal_order_and_event_budget_preserved(tmp_path):
    records = native_records()
    records[3]["ordinal"] = 0
    page = page_for(tmp_path, records)
    assert [e.kind for e in page.events] == ["user_message"]
    assert page.malformed_count == 1
    page = page_for(tmp_path, native_records(), limits=ProjectionLimits(max_events=1))
    assert [e.kind for e in page.events] == ["user_message"]
    assert page.next_cursor and page.truncated
