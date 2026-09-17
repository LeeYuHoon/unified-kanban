"""네이티브 턴 경계와 발행 차단 계약을 검증한다."""
import importlib
import json

import pytest


def row(kind, payload, ordinal):
    return json.dumps(dict(type=kind, payload=payload, ordinal=ordinal)).encode() + b"\n"


def marker(kind, turn="target", ordinal=1):
    return row("event_msg", dict(type=kind, turn_id=turn), ordinal)


def message(role, ordinal, turn="target"):
    payload = dict(type="message", role=role, content=[dict(
        type="input_text" if role == "user" else "output_text", text="public")],
        internal_chat_message_metadata_passthrough=dict(turn_id=turn))
    if role == "assistant":
        payload["phase"] = "final_answer"
    return row("response_item", payload, ordinal)


def module():
    return importlib.import_module("kanban_adapter.codex_file_provenance")


def test_exact_terminal_range_keeps_global_ordinals():
    m = module()
    history = marker("task_started", "old", 40) + marker("task_complete", "old", 41)
    target = marker("task_started", ordinal=42) + message("user", 43) + message("assistant", 44) + marker("task_complete", ordinal=45)
    result = m.parse_turn(history + target + marker("task_started", "next", 46), "target")
    assert result.ready
    assert (result.start_offset, result.end_offset) == (len(history), len(history + target))
    assert (result.start_ordinal, result.end_ordinal) == (42, 46)
    assert result.request_count == result.final_count == 1


@pytest.mark.parametrize("suffix", [b"", message("user", 2), message("user", 2) + message("assistant", 3), message("user", 2) + message("assistant", 3, "wrong") + marker("task_complete", ordinal=4)])
def test_pending_requires_request_final_and_terminal(suffix):
    assert not module().parse_turn(marker("task_started") + suffix, "target").ready


@pytest.mark.parametrize("suffix", [marker("task_started", ordinal=2), marker("task_started", "next", 2), marker("task_complete", "wrong", 2), message("user", 3), message("user", True)])
def test_ambiguous_boundary_and_ordinal_rejected(suffix):
    with pytest.raises(PermissionError):
        module().parse_turn(marker("task_started") + suffix, "target")


def test_private_blocks_do_not_make_final_ready():
    private = row("response_item", dict(type="message", role="assistant", phase="final_answer", content=[dict(type="reasoning", text="secret")], internal_chat_message_metadata_passthrough=dict(turn_id="target")), 3)
    assert not module().parse_turn(marker("task_started") + message("user", 2) + private + marker("task_complete", ordinal=4), "target").ready


def test_multiple_requests_and_finals_are_one_turn():
    raw = marker("task_started") + message("user", 2) + message("assistant", 3) + message("user", 4) + message("assistant", 5) + marker("task_complete", ordinal=6)
    result = module().parse_turn(raw, "target")
    assert result.ready and result.request_count == result.final_count == 2


@pytest.mark.parametrize("raw", [b'[]\n', b'{bad}\n', row("event_msg", [], 1)])
def test_malformed_records_fail_closed(raw):
    with pytest.raises(PermissionError):
        module().parse_turn(raw, "target")


def test_duplicate_identity_is_ambiguous():
    raw = b'{"type":"event_msg","ordinal":1,"payload":{"type":"task_started","turn_id":"wrong","turn_id":"target"}}\n'
    with pytest.raises(PermissionError):
        module().parse_turn(raw, "target")


@pytest.mark.parametrize("budget", ["JSONL_SCAN_BYTES", "JSONL_LINE_BYTES", "JSONL_RECORD_LIMIT"])
def test_scan_budgets_fail_closed(monkeypatch, budget):
    m = module()
    monkeypatch.setattr(m, budget, 0, raising=False)
    with pytest.raises(PermissionError):
        m.parse_turn(marker("task_started"), "target")


def test_wrong_request_metadata_never_satisfies_readiness():
    raw = marker("task_started") + message("user", 2, "wrong") + message("assistant", 3) + marker("task_complete", ordinal=4)
    result = module().parse_turn(raw, "target")
    assert not result.ready and result.request_count == 0


def test_partial_terminal_is_pending():
    raw = marker("task_started") + message("user", 2) + message("assistant", 3) + marker("task_complete", ordinal=4)[:-1]
    assert not module().parse_turn(raw, "target").ready
