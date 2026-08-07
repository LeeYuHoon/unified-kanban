"""Synthetic OpenAI Codex CLI rollout fixtures.

Hand-written approximations of the ``{timestamp, type, payload}`` JSONL shapes
Codex CLI 0.145 writes under ``~/.codex/sessions``. No real Codex rollout is
ever read by the test suite.
"""

import json
import os

SESSION_ID = "01983b2e-c0de-4444-aaaa-bbbbccccdddd"
CWD = "/Users/testuser/work/codexdemo"

# Secret-looking blobs used to exercise redaction. Not real credentials.
SECRET_PROMPT = (
    "deploy with AKIAIOSFODNN7EXAMPLE and Authorization: Bearer "
    "abcdefghijklmnopqrstuvwxyz012345 from /Users/testuser/work/codexdemo"
)

HTML_PROMPT = '<img src=x onerror=alert(1)> & "quotes" — summarise this'

REASONING_TEXT = "private codex reasoning that must never be rendered"
TOOL_OUTPUT_TEXT = "sensitive codex tool output that must never be rendered"


def _rec(ts, rtype, payload):
    return {"timestamp": ts, "type": rtype, "payload": payload}


def _event(ts, ptype, **payload):
    payload["type"] = ptype
    return _rec(ts, "event_msg", payload)


def _item(ts, ptype, **payload):
    payload["type"] = ptype
    return _rec(ts, "response_item", payload)


def records():
    """The canonical synthetic rollout, in file order."""
    return [
        # 0: session_meta carries id / cwd / model_provider / source
        _rec(
            "2026-07-03T09:00:00.000Z",
            "session_meta",
            {
                "id": SESSION_ID,
                "session_id": SESSION_ID,
                "timestamp": "2026-07-03T09:00:00.000Z",
                "cwd": CWD,
                "model_provider": "openai",
                "source": "cli",
                "originator": "codex_cli_rs",
                "cli_version": "0.145.0",
            },
        ),
        # 1: developer/system context injected as a response_item message
        _item(
            "2026-07-03T09:00:00.100Z",
            "message",
            role="developer",
            content=[
                {
                    "type": "input_text",
                    "text": "<user_instructions>internal developer context"
                    "</user_instructions>",
                }
            ],
        ),
        # 2: the real human prompt
        _event(
            "2026-07-03T09:00:01.000Z",
            "user_message",
            message="Add a retry to the uploader.",
            kind="plain",
        ),
        # 3: the same prompt echoed back as a response_item -> duplicate
        _item(
            "2026-07-03T09:00:01.050Z",
            "message",
            role="user",
            content=[{"type": "input_text", "text": "Add a retry to the uploader."}],
        ),
        # 4: turn begins
        _event("2026-07-03T09:00:01.100Z", "task_started", model_context_window=272000),
        # 5/6: reasoning, in both shapes -> counted, never shown
        _event("2026-07-03T09:00:02.000Z", "agent_reasoning", text=REASONING_TEXT),
        _item(
            "2026-07-03T09:00:02.100Z",
            "reasoning",
            summary=[{"type": "summary_text", "text": REASONING_TEXT}],
        ),
        # 7/8/9: tool calls and one tool output
        _item(
            "2026-07-03T09:00:03.000Z",
            "function_call",
            name="shell",
            arguments='{"command":["ls","-la"]}',
            call_id="c1",
        ),
        _item(
            "2026-07-03T09:00:03.500Z",
            "function_call_output",
            call_id="c1",
            output=TOOL_OUTPUT_TEXT,
        ),
        _item(
            "2026-07-03T09:00:04.000Z",
            "custom_tool_call",
            name="apply_patch",
            input="*** Begin Patch",
            call_id="c2",
        ),
        # 10: the visible assistant answer
        _event(
            "2026-07-03T09:00:05.000Z",
            "agent_message",
            message="Added a retry with exponential backoff.",
            phase="final",
        ),
        # 11: the same answer echoed as a response_item -> must not double up
        _item(
            "2026-07-03T09:00:05.050Z",
            "message",
            role="assistant",
            content=[
                {"type": "output_text", "text": "Added a retry with exponential backoff."}
            ],
        ),
        # 12: turn completed
        _event(
            "2026-07-03T09:00:05.100Z",
            "task_complete",
            last_agent_message="Added a retry with exponential backoff.",
        ),
        # 13: injected environment context arriving as a user_message
        _event(
            "2026-07-03T09:00:06.000Z",
            "user_message",
            message="<environment_context>\n  <cwd>%s</cwd>\n</environment_context>" % CWD,
        ),
        # 14: second real prompt, with secrets
        _event("2026-07-03T09:01:00.000Z", "user_message", message=SECRET_PROMPT),
        _event("2026-07-03T09:01:00.100Z", "task_started"),
        _event("2026-07-03T09:01:01.000Z", "agent_message", message="Starting the deploy."),
        # 17: the user interrupted the turn
        _event("2026-07-03T09:01:02.000Z", "turn_aborted", reason="interrupted"),
        # 18: an explicit compaction record
        _rec(
            "2026-07-03T09:02:00.000Z",
            "compacted",
            {"message": "Earlier codex context condensed into a summary."},
        ),
        # 19: third real prompt, HTML-ish
        _event("2026-07-03T09:03:00.000Z", "user_message", message=HTML_PROMPT),
        # 20: a reply, but the turn never completes
        _event("2026-07-03T09:03:01.000Z", "agent_message", message="Working on it."),
        # 21: fourth real prompt, answered with a stream error
        _event("2026-07-03T09:04:00.000Z", "user_message", message="and now break it"),
        _event("2026-07-03T09:04:01.000Z", "error", message="stream disconnected"),
        # 23: compaction, event_msg flavour
        _event(
            "2026-07-03T09:05:00.000Z",
            "context_compacted",
            message="Context compacted by the CLI.",
        ),
        # 24: bookkeeping noise
        _event("2026-07-03T09:05:01.000Z", "token_count", info={"total_tokens": 1234}),
    ]


def write_rollout(root, rel=None, extra_lines=()):
    """Write the canonical rollout under ``root`` and return its path."""
    if rel is None:
        rel = os.path.join(
            "2026", "07", "03", "rollout-2026-07-03T09-00-00-%s.jsonl" % SESSION_ID
        )
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    for i, rec in enumerate(records()):
        lines.append(json.dumps(rec))
        if i == 4:
            lines.append("")  # blank: ignored
            lines.append("{not json at all")  # malformed
    lines.extend(extra_lines)
    lines.append('{"timestamp":"2026-07-03T09:06:00.000Z","type":"event_ms')  # truncated
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def write_second_rollout(root, rel=None):
    if rel is None:
        rel = os.path.join("2026", "07", "04", "rollout-2026-07-04-second.jsonl")
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    recs = [
        _rec(
            "2026-07-04T08:00:00.000Z",
            "session_meta",
            {
                "id": "01983b2e-c0de-4444-eeee-ffff00001111",
                "cwd": "/Users/testuser/work/other",
                "model_provider": "openai",
                "source": "vscode",
            },
        ),
        _event("2026-07-04T08:00:01.000Z", "user_message", message="Say hello in Korean."),
        _event("2026-07-04T08:00:02.000Z", "agent_message", message="안녕하세요"),
        _event("2026-07-04T08:00:03.000Z", "task_complete"),
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec) + "\n")
    return path


def write_index_noise(root):
    """Write the non-session bookkeeping files Codex keeps next to rollouts."""
    paths = []
    for name, payload in (
        ("history.jsonl", {"session_id": "x", "ts": 1, "text": "shell history entry"}),
        ("session_index.jsonl", {"id": "x", "path": "rollout-x.jsonl"}),
    ):
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
        paths.append(path)
    return paths
