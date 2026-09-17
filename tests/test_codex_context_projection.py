"""합성 네이티브 컨텍스트 출처 검증이며 실제 대화록 텍스트나 식별자는 포함하지 않는다."""
import json

import pytest

from test_codex_target_turn_projection import message, setup_scope


def native_user(text, kinds):
    record = message(text=text)
    record["payload"]["internal_chat_message_metadata_passthrough"]["content_item_kinds"] = kinds
    return record


@pytest.mark.parametrize("context_text", [
    "<environment_context>SYNTHETIC_PRIVATE_CONTEXT</environment_context>",
    "SYNTHETIC_PRIVATE_CONTEXT without an XML envelope",
])
@pytest.mark.parametrize("native", [True, False])
def test_native_environment_context_is_not_a_public_user_turn(tmp_path, context_text, native):
    # 동일한 문자열에 의도적으로 서로 다른 네이티브 종류를 부여하며 텍스트로 분류하지 않는다.
    records = [
        native_user(context_text, ["environments.environment_context"]),
        native_user(context_text, ["user.text"]),
        message(role="assistant", phase="final_answer", text="synthetic final"),
    ]
    _, _, binding, locator, store, service = setup_scope(tmp_path, records, native=native)
    stored_before = store._path("demo", "task").read_bytes()
    source_before = (locator.allowed_root / locator.relative_path).read_bytes()
    events = []
    cursor = None
    for _ in range(4):
        page = service.get_parent_page(
            principal_id="owner", board="demo", task="task", cursor=cursor, limit=1,
        )
        page_events = page["events"]
        assert isinstance(page_events, list)
        events.extend(page_events)
        next_cursor = page["next_cursor"]
        assert next_cursor is None or isinstance(next_cursor, str)
        cursor = next_cursor
        if cursor is None:
            break
    assert cursor is None
    assert [(e["kind"], e["text"]) for e in events] == [
        ("user_message", context_text), ("final_assistant", "synthetic final"),
    ]
    assert [e["seq"] for e in events] == [0, 1]
    assert len({e["event_id"] for e in events}) == 2
    assert "content_item_kinds" not in json.dumps(events)
    assert store._path("demo", "task").read_bytes() == stored_before
    assert store.get("demo", "task") == (binding, locator)
    assert (locator.allowed_root / locator.relative_path).read_bytes() == source_before


@pytest.mark.parametrize("context_first", [True, False])
def test_mixed_native_context_message_drops_whole_envelope(tmp_path, context_first):
    parts = [
        ("environments.environment_context", "SYNTHETIC_PRIVATE_CONTEXT"),
        ("user.text", "embedded fragment"),
    ]
    if not context_first:
        parts.reverse()
    record = native_user("unused", [kind for kind, _ in parts])
    record["payload"]["content"] = [
        {"type": "input_text", "text": text} for _, text in parts
    ]
    _, _, _, _, _, service = setup_scope(tmp_path, [
        record, native_user("actual request", ["user.text"]),
        message(role="assistant", phase="final_answer", text="done"),
    ])
    page = service.get_parent_page(
        principal_id="owner", board="demo", task="task", cursor=None, limit=10,
    )
    events = page["events"]
    assert isinstance(events, list)
    assert [(e["kind"], e["text"]) for e in events] == [
        ("user_message", "actual request"), ("final_assistant", "done"),
    ]
    assert "SYNTHETIC_PRIVATE_CONTEXT" not in json.dumps(page)
    assert "embedded fragment" not in json.dumps(page)
