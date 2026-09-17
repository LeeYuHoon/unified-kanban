"""Claude 2.1.268에서 관측된 메타데이터 형식이며 식별자와 본문은 모두 합성 데이터다."""
import json

import pytest

from kanban_adapter import claude_absent, claude_file_provenance
from kanban_adapter.transcript_projection import ClaudeProjector
from test_claude_missing_transcript import PROMPT_ID

CONTEXT_TYPES = (
    "environment", "model", "deferred_tools_delta", "agent_listing_delta",
    "mcp_instructions_delta", "skill_listing", "auto_mode",
    "total_tokens_reminder", "instructions", "session_context", "date",
    "remote_session_change", "prompt_snapshot",
)


def observed_topology():
    rows = []
    parent = None

    def metadata(kind):
        rows.append(dict(type=kind, **({} if kind == "file-history-snapshot"
                                     else {"sessionId": "native"})))

    def node(kind, **fields):
        nonlocal parent
        uuid = f"node-{len(rows) + 1}"
        rows.append(dict(parentUuid=parent, isSidechain=False, type=kind,
                         uuid=uuid, sessionId="native", **fields))
        parent = uuid

    for kind in ("last-prompt", "mode", "permission-mode", "atis-latch"):
        metadata(kind)
    for kind in ("hook_success",) * 4 + ("hook_additional_context",):
        node("attachment", attachment={"type": kind})
    metadata("file-history-snapshot")
    node("user", promptId=PROMPT_ID,
         message={"role": "user", "content": "SYNTHETIC REQUEST"})
    for kind in CONTEXT_TYPES:
        node("attachment", attachment={"type": kind})
    for kind in ("last-prompt", "mode", "permission-mode", "atis-latch", "ai-title"):
        metadata(kind)
    node("assistant", message={"role": "assistant", "stop_reason": "end_turn",
                               "content": [{"type": "text", "text": "SYNTHETIC FINAL"}]})
    for kind in ("prompt_snapshot",) + ("hook_success",) * 4:
        node("attachment", attachment={"type": kind})
    for kind in ("last-prompt", "ai-title", "mode", "permission-mode", "atis-latch"):
        metadata(kind)
    node("system")
    node("system", isMeta=False)
    for kind in ("file-history-snapshot", "cost-state", "last-prompt", "cost-state"):
        metadata(kind)
    return rows


def wire(rows):
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


@pytest.mark.parametrize("parser", [claude_absent._range, claude_file_provenance._range])
def test_observed_full_metadata_topology_selects_public_request_final(parser):
    rows = observed_topology()
    assert len(rows) == 46
    assert [r["attachment"]["type"] for r in rows if r["type"] == "attachment"] == (
        ["hook_success"] * 4 + ["hook_additional_context"] + list(CONTEXT_TYPES)
        + ["prompt_snapshot"] + ["hook_success"] * 4)
    raw = wire(rows)
    result = parser(raw, "native", PROMPT_ID)
    start, end = result[:2]
    assert start == len(wire(rows[:10]))
    if parser is claude_file_provenance._range:
        assert result[2] == "node-11"
    selected = [json.loads(line) for line in raw[start:end].splitlines()]
    public = [r for r in selected if r.get("type") in {"user", "assistant"}
              and ClaudeProjector()._public_entry(r)]
    assert [r["uuid"] for r in public] == ["node-11", "node-30"]
    assert public[-1]["message"]["stop_reason"] == "end_turn"
    for row in rows:
        if row["type"] == "attachment":
            outcome = ClaudeProjector().project(
                row, binding=None, source_event_id="synthetic", seq=0)
            assert outcome.dropped_count == 1
            assert not outcome.events


@pytest.mark.parametrize("parser", [claude_absent._range, claude_file_provenance._range])
@pytest.mark.parametrize("kind", CONTEXT_TYPES)
@pytest.mark.parametrize("attack", ["unknown", "sidechain", "session", "scope", "duplicate",
                                     "forward", "stale", "prompt"])
def test_observed_context_types_preserve_fail_closed_checks(parser, kind, attack):
    rows = observed_topology()
    target = next(r for r in rows if r.get("attachment", {}).get("type") == kind)
    if attack == "unknown":
        target["attachment"]["type"] = kind + "_unknown"
    elif attack == "sidechain":
        del target["isSidechain"]
    elif attack == "session":
        target["sessionId"] = "other"
    elif attack == "scope":
        target["isMeta"] = True
    elif attack == "duplicate":
        target["uuid"] = "node-11"
    elif attack == "forward":
        target["parentUuid"] = "node-30"
    elif attack == "stale":
        target["parentUuid"] = "node-9"
    elif attack == "prompt":
        target["promptId"] = "other"
    with pytest.raises(PermissionError):
        parser(wire(rows), "native", PROMPT_ID)
