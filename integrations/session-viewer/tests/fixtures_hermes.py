"""Synthetic Hermes Agent state.db fixtures.

Builds a throwaway SQLite database with the column shapes Hermes Agent 0.19
uses in ``~/.hermes/state.db``. The real database is never opened by the test
suite; every row here is hand-written.
"""

import os
import sqlite3

SESSION_ID = "101"
SECOND_SESSION_ID = "102"
CWD = "/Users/testuser/work/hermesdemo"

SECRET_PROMPT = (
    "deploy AKIAIOSFODNN7EXAMPLE from /Users/testuser/work/hermesdemo "
    "using sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"
)

HTML_PROMPT = '<img src=x onerror=alert(1)> & "quotes" — escape me'

REASONING_TEXT = "private hermes reasoning that must never be rendered"
REASONING_CONTENT_TEXT = "more private hermes reasoning that must never be rendered"
TOOL_OUTPUT_TEXT = "sensitive hermes tool output that must never be rendered"
INACTIVE_TEXT = "this message was rolled back and is inactive"
INTERNAL_TEXT = "internal planner note injected by the agent"

SESSIONS_DDL = """
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    source TEXT,
    model TEXT,
    title TEXT,
    cwd TEXT,
    started_at TEXT,
    ended_at TEXT,
    end_reason TEXT,
    message_count INTEGER,
    tool_call_count INTEGER
)
"""

MESSAGES_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    session_id INTEGER,
    role TEXT,
    content TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp TEXT,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    active INTEGER,
    compacted INTEGER,
    display_kind TEXT
)
"""

_INSERT_MESSAGES = (
    "INSERT INTO messages "
    "(id, session_id, role, content, tool_calls, tool_name, timestamp, "
    "finish_reason, reasoning, reasoning_content, active, compacted, display_kind) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _msg(
    mid,
    session_id,
    role,
    content=None,
    tool_calls=None,
    tool_name=None,
    timestamp=None,
    finish_reason=None,
    reasoning=None,
    reasoning_content=None,
    active=1,
    compacted=0,
    display_kind=None,
):
    return (
        mid,
        session_id,
        role,
        content,
        tool_calls,
        tool_name,
        timestamp or "2026-07-05T12:00:%02d.000Z" % (mid % 60),
        finish_reason,
        reasoning,
        reasoning_content,
        active,
        compacted,
        display_kind,
    )


def session_rows():
    return [
        (
            101,
            "cli",
            "hermes-large",
            "Refactor the uploader",
            CWD,
            "2026-07-05T12:00:00.000Z",
            "2026-07-05T12:30:00.000Z",
            "completed",
            14,
            2,
        ),
        (
            102,
            "api",
            "hermes-mini",
            "Greeting",
            "/Users/testuser/work/other",
            "2026-07-04T09:00:00.000Z",
            None,
            "aborted",
            3,
            1,
        ),
    ]


def message_rows():
    return [
        # --- session 101 ---
        _msg(1, 101, "system", "You are Hermes, an agent."),
        _msg(2, 101, "user", "Explain the retry policy."),
        _msg(3, 101, "user", INTERNAL_TEXT, display_kind="internal"),
        _msg(
            4,
            101,
            "assistant",
            None,
            tool_calls='[{"id":"t1","function":{"name":"read_file"}},'
            '{"id":"t2","function":{"name":"grep"}}]',
            finish_reason="tool_calls",
            reasoning=REASONING_TEXT,
        ),
        _msg(5, 101, "tool", TOOL_OUTPUT_TEXT, tool_name="read_file"),
        _msg(
            6,
            101,
            "assistant",
            '[{"type":"text","text":"Retries use exponential backoff."}]',
            finish_reason="stop",
            reasoning_content=REASONING_CONTENT_TEXT,
        ),
        # rolled back -> never loaded
        _msg(7, 101, "user", INACTIVE_TEXT, active=0),
        # explicit compaction boundary
        _msg(
            8,
            101,
            "system",
            "Earlier hermes context condensed into a summary.",
            compacted=1,
        ),
        _msg(9, 101, "user", SECRET_PROMPT),
        _msg(10, 101, "assistant", '"a JSON string reply"', finish_reason="error"),
        _msg(11, 101, "user", '{"text":"dict shaped prompt content"}'),
        _msg(12, 101, "assistant", "{not json at all", finish_reason="length"),
        _msg(13, 101, "user", HTML_PROMPT),
        _msg(14, 101, "assistant", "All done.", finish_reason="end_turn"),
        # --- session 102 ---
        _msg(20, 102, "user", "Say hello in Korean.", timestamp="2026-07-04T09:00:01.000Z"),
        _msg(21, 102, "user", "hidden ui note", display_kind="hidden"),
        _msg(
            22,
            102,
            "assistant",
            None,
            tool_calls='[{"function":{"name":"speak"}}]',
            finish_reason="tool_calls",
            timestamp="2026-07-04T09:00:02.000Z",
        ),
    ]


def write_db(path):
    """Create the synthetic state.db at ``path`` and return it."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(SESSIONS_DDL)
        conn.execute(MESSAGES_DDL)
        conn.executemany(
            "INSERT INTO sessions (id, source, model, title, cwd, started_at, "
            "ended_at, end_reason, message_count, tool_call_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            session_rows(),
        )
        conn.executemany(_INSERT_MESSAGES, message_rows())
        conn.commit()
    finally:
        conn.close()
    return path
