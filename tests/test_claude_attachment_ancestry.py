"""합성 연결 구조만 사용하며 실제 대화 기록이나 첨부 본문은 읽지 않는다."""
import json

import pytest

from kanban_adapter import claude_absent as absent, claude_file_provenance as v2
from test_claude_absent_final_preparation import setup_source
from test_claude_file_provenance_v2 import _row, _page
from test_claude_missing_transcript import PROMPT_ID

CANARY = "SYNTHETIC_ATTACHMENT_PRIVATE_BODY"


def attachment(uuid, parent, kind="hook_success", **fields):
    return dict(type="attachment", sessionId="native", uuid=uuid, parentUuid=parent,
                isSidechain=False, attachment={"type": kind, "content": CANARY}, **fields)


def topology():
    return [attachment("root", None), attachment("context", "root", "hook_additional_context"),
            json.loads(_row("user", "request", "context", "synthetic request", PROMPT_ID)),
            attachment("bridge", "request", "hook_additional_context"),
            json.loads(_row("assistant", "final", "bridge", "synthetic final"))]


def wire(rows):
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


@pytest.mark.parametrize("mode", ["absent", "file"])
def test_rooted_attachments_seal_exact_request_and_project_public_final(tmp_path, mode):
    source, service, kwargs, proof, _ = setup_source(tmp_path)
    rows = topology()
    source.write_bytes(wire(rows[:-1]))
    if mode == "absent":
        pin = absent.prepare_final(service, prepared=proof, **kwargs)
        seal = absent.seal_final
    else:
        pin = v2.capture(service, session="native", source_path=source, **kwargs)
        seal = lambda *a, **kw: v2.seal(*a, require_final=True, **kw)
    assert seal(service, prepared=pin, **kwargs)["status"] == "pending"
    with source.open("ab") as stream:
        stream.write(wire(rows[-1:]))
        # 이후 턴은 확정된 요청 범위를 넓힐 수 없다.
        stream.write(_row("user", "next-request", "final", "excluded request",
                          "750e8400-e29b-41d4-a716-446655440000"))
        stream.write(_row("assistant", "next-final", "next-request", "excluded final"))
    result = seal(service, prepared=pin, **kwargs)
    assert result["status"] == "ready"
    binding = result["binding"]
    assert binding.turn_start.byte_offset == len(wire(rows[:2]))
    assert binding.turn_end.byte_offset == len(wire(rows))
    page = _page(service)
    assert [(e["kind"], e["text"]) for e in page["events"]] == [
        ("user_message", "synthetic request"), ("final_assistant", "synthetic final")]
    assert CANARY not in json.dumps(page)


@pytest.mark.parametrize("parser", [absent._range, v2._range])
@pytest.mark.parametrize("attack", [
    "orphan", "cycle", "duplicate", "session", "missing-session", "sidechain",
    "missing-sidechain", "malformed-sidechain", "unknown-type", "missing-parent",
    "cross-prompt", "pre-request-parent", "duplicate-message", "unknown-bridge",
    "unknown-duplicate", "wrong-native", "root-reset", "forward-parent",
])
def test_attachment_graph_rejects_ambiguous_nodes(parser, attack):
    rows = topology()
    bridge = rows[3]
    if attack == "orphan":
        rows[0]["parentUuid"] = "missing"
    elif attack == "cycle":
        rows[0]["parentUuid"] = "context"
    elif attack == "duplicate":
        bridge["uuid"] = "root"
    elif attack == "session":
        bridge["sessionId"] = "other"
    elif attack == "missing-session":
        del bridge["sessionId"]
    elif attack == "sidechain":
        bridge["isSidechain"] = True
    elif attack == "missing-sidechain":
        del bridge["isSidechain"]
    elif attack == "malformed-sidechain":
        bridge["isSidechain"] = 0
    elif attack == "unknown-type":
        bridge["attachment"]["type"] = "arbitrary"
    elif attack == "missing-parent":
        del bridge["parentUuid"]
    elif attack == "cross-prompt":
        bridge["promptId"] = "650e8400-e29b-41d4-a716-446655440000"
    elif attack == "pre-request-parent":
        bridge["parentUuid"] = "context"
    elif attack == "duplicate-message":
        rows[4]["uuid"] = "bridge"
    elif attack == "unknown-bridge":
        bridge["type"] = "unknown"
    elif attack == "unknown-duplicate":
        rows.insert(3, dict(type="unknown", uuid="bridge"))
    elif attack == "wrong-native":
        rows[2]["promptId"] = "650e8400-e29b-41d4-a716-446655440000"
        bridge["promptId"] = PROMPT_ID
    elif attack == "root-reset":
        bridge["parentUuid"] = None
    elif attack == "forward-parent":
        bridge["parentUuid"] = "final"
    with pytest.raises(PermissionError):
        parser(wire(rows), "native", PROMPT_ID)


@pytest.mark.parametrize("parser", [absent._range, v2._range])
def test_unknown_node_cannot_shadow_valid_ancestry(parser):
    rows = topology()
    rows.insert(4, dict(type="unknown", uuid="request", parentUuid="bridge"))
    with pytest.raises(PermissionError):
        parser(wire(rows), "native", PROMPT_ID)


@pytest.mark.parametrize("stale_parent", [False, True])
def test_file_attachment_cannot_bridge_from_prior_turn(stale_parent):
    old_id = "650e8400-e29b-41d4-a716-446655440000"
    rows = topology()
    rows[2]["promptId"] = old_id
    rows[3]["promptId"] = old_id
    old = wire(rows)
    current = [json.loads(_row("user", "current", "final", "synthetic request", PROMPT_ID)),
               attachment("current-context", "bridge" if stale_parent else "current"),
               json.loads(_row("assistant", "current-final", "current-context", "current answer"))]
    if stale_parent:
        with pytest.raises(PermissionError):
            v2._range(old + wire(current), "native", PROMPT_ID)
    else:
        selected = v2._range(old + wire(current), "native", PROMPT_ID)
        assert selected == (len(old), len(old + wire(current)), "current")
    with pytest.raises(PermissionError):
        absent._range(old + wire(current), "native", PROMPT_ID)


@pytest.mark.parametrize("parser", [absent._range, v2._range])
def test_attachment_preamble_does_not_supply_native_request_identity(parser):
    rows = topology()
    rows[0]["promptId"] = PROMPT_ID
    del rows[2]["promptId"]
    with pytest.raises(PermissionError):
        parser(wire(rows), "native", PROMPT_ID)
