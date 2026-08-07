#!/usr/bin/env python3
"""AI Session Viewer — a read-only, dependency-free viewer for local AI coding
agent sessions.

Three providers are supported through adapters that feed one shared
Session/Event/Turn model and one shared set of renderers:

* ``claude``  — Claude Code JSONL transcripts under ``~/.claude/projects``
* ``codex``   — OpenAI Codex CLI rollouts under ``~/.codex/sessions``
* ``hermes``  — Hermes Agent's SQLite state at ``~/.hermes/state.db``

Design rules (enforced by the test suite):

* Sources are opened read-only (SQLite via ``file:...?mode=ro``), streamed, and
  never rewritten.
* Nothing is ever written under ``~/.claude``, ``~/.codex`` or ``~/.hermes``;
  ``--out`` destinations resolving inside any of them are rejected.
* Malformed JSONL lines / rows are skipped and counted, never guessed at.
* "Real" human prompts are separated from the many synthetic user-role records
  the agents write (tool results, hooks, reminders, duplicated context). The
  rules are per-provider heuristics; they are listed in
  ``PROMPT_FILTER_RULES`` and surfaced in every rendered output.
* Model reasoning and tool output are never rendered — only counted.
* Redaction is on by default and is best-effort pattern matching only.

Standard library only. Python 3.9+ (3.8 also works).

The executable keeps its historical name ``claude_session_viewer.py`` so old
invocations and scripts keep working.
"""

import argparse
import collections
import datetime
import html as html_mod
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import textwrap
import warnings

__version__ = "2.0.0"

PROGRAM = "claude_session_viewer.py"
TITLE = "AI Session Viewer"

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
PROVIDER_HERMES = "hermes"
PROVIDER_ALL = "all"

PROVIDER_IDS = (PROVIDER_CLAUDE, PROVIDER_CODEX, PROVIDER_HERMES)
PROVIDER_CHOICES = PROVIDER_IDS + (PROVIDER_ALL,)


class Provider(object):
    """Static description of one supported agent."""

    def __init__(self, pid, label, product, default_root_parts, root_help, reason_key,
                 complete_reasons, reasoning_word, data_dir):
        self.id = pid
        self.label = label            # badge / assistant label, e.g. "Codex"
        self.product = product        # human product name for prose
        self.default_root_parts = default_root_parts
        self.root_help = root_help
        self.reason_key = reason_key  # field name shown in honest status labels
        self.complete_reasons = complete_reasons
        self.reasoning_word = reasoning_word
        self.data_dir = data_dir      # protected application data directory name


PROVIDERS = {
    PROVIDER_CLAUDE: Provider(
        PROVIDER_CLAUDE,
        "Claude",
        "Claude Code",
        (".claude", "projects"),
        "directory searched recursively for .jsonl transcripts",
        "stop_reason",
        ("end_turn", "stop_sequence"),
        "thinking",
        ".claude",
    ),
    PROVIDER_CODEX: Provider(
        PROVIDER_CODEX,
        "Codex",
        "OpenAI Codex CLI",
        (".codex", "sessions"),
        "directory searched recursively for .jsonl rollouts",
        "signal",
        ("task_complete",),
        "reasoning",
        ".codex",
    ),
    PROVIDER_HERMES: Provider(
        PROVIDER_HERMES,
        "Hermes",
        "Hermes Agent",
        (".hermes", "state.db"),
        "path to the Hermes state.db SQLite database (or a directory holding it)",
        "finish_reason",
        ("stop", "end_turn", "stop_sequence"),
        "reasoning",
        ".hermes",
    ),
}


def provider_of(pid):
    return PROVIDERS.get(pid) or PROVIDERS[PROVIDER_CLAUDE]


def provider_label(pid):
    """Badge / assistant label for a provider id."""
    return provider_of(pid).label


def assistant_label(pid):
    """What the agent's replies are labelled as in every renderer."""
    return provider_of(pid).label


def default_root_for(pid):
    return os.path.join(os.path.expanduser("~"), *provider_of(pid).default_root_parts)

# ---------------------------------------------------------------------------
# Event kinds
# ---------------------------------------------------------------------------

KIND_USER = "user"
KIND_ASSISTANT = "assistant"
KIND_COMPACTION = "compaction"
KIND_OTHER = "other"
KIND_TURN = "turn"

# ---------------------------------------------------------------------------
# Documented, deliberately imperfect heuristics
# ---------------------------------------------------------------------------

USER_PROMPT_FILTER_RULES = [
    "not_user_role: the record's top-level type is not 'user'.",
    "meta: the record carries isMeta (session resume notices, checkpoints).",
    "sidechain: the record carries isSidechain (subagent conversation).",
    "tool_result: the user-role content holds only tool_result blocks.",
    "system_reminder_only: content is nothing but <system-reminder> blocks.",
    "slash_command: content starts with <command-name>/<command-args> markers.",
    "local_command_output: content starts with <local-command-stdout/stderr>.",
    "hook_output: content starts with a <...hook...> wrapper (e.g. "
    "<user-prompt-submit-hook>).",
    "caveat_preamble: content is the 'Caveat: The messages below were generated"
    " by the user while running local commands' preamble.",
    "empty: no visible text remains after stripping the wrappers above.",
]

CODEX_PROMPT_FILTER_RULES = [
    "not_user_role: the record is not an event_msg of payload type user_message.",
    "duplicate_response_item: response_item 'message' records replay the prompt "
    "the CLI already logged as an event_msg; they are counted, not shown.",
    "developer_context: a response_item message with role developer/system "
    "(instructions, tool policy) — never typed by you.",
    "environment_context: a user_message that is only an <environment_context> "
    "or <user_instructions> block injected by the CLI.",
    "empty: no visible text remains after stripping the wrappers above.",
]

HERMES_PROMPT_FILTER_RULES = [
    "not_user_role: the messages row role is not 'user'.",
    "inactive: rows with active = 0 are rolled back and are never loaded.",
    "hidden_display_kind: display_kind marks the row hidden/internal/system "
    "(agent-injected context rather than something you typed).",
    "system_role: system/developer rows are context, not prompts.",
    "empty: the decoded content held no visible text.",
]

PROMPT_FILTER_RULES = {
    PROVIDER_CLAUDE: USER_PROMPT_FILTER_RULES,
    PROVIDER_CODEX: CODEX_PROMPT_FILTER_RULES,
    PROVIDER_HERMES: HERMES_PROMPT_FILTER_RULES,
}


def prompt_filter_rules(pid):
    return PROMPT_FILTER_RULES.get(pid, USER_PROMPT_FILTER_RULES)


PROMPT_FILTER_CAVEAT = (
    "Prompt selection uses documented heuristics over undocumented transcript "
    "fields. It can drop an unusual real prompt or keep an unusual synthetic "
    "one; it is not exact."
)

REDACTED = "[REDACTED]"

REDACTION_CAVEAT = (
    "Redaction is best-effort pattern matching. It is not a guarantee: secrets "
    "in unusual formats, prose, or code will survive it. Review output before "
    "sharing."
)

READ_ONLY_NOTE = (
    "Generated by {title} — a read-only viewer. The source session data is "
    "opened read-only and never modified."
).format(title=TITLE)

# Secret-ish patterns, applied in order. Deliberately conservative.
_SECRET_PATTERNS = [
    # PEM private key blocks -> collapse the body, keep the envelope visible.
    (
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
            r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
            re.S,
        ),
        "-----BEGIN PRIVATE KEY----- " + REDACTED + " -----END PRIVATE KEY-----",
    ),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/=]{12,}"), r"\1 " + REDACTED),
    (re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{8,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{16,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,20}\b"), REDACTED),
    (re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{30,}"), REDACTED),
    (
        re.compile(r"(?i)\b(api[-_]?key\s*[:=]\s*)[\"']?([A-Za-z0-9\-_]{16,})[\"']?"),
        r"\1" + REDACTED,
    ),
]


class TranscriptError(Exception):
    """Raised when a transcript cannot be read at all."""


class SelectorError(Exception):
    """Raised when a session selector matches zero or multiple sessions."""


class OutputPathError(Exception):
    """Raised when an --out destination is unsafe."""


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact(text, home=None):
    """Best-effort masking of home paths and common secret shapes."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    home_dir = str(home) if home else os.path.expanduser("~")
    if home_dir and home_dir != os.sep:
        home_dir = home_dir.rstrip(os.sep)
        out = re.sub(re.escape(home_dir) + r"(?![A-Za-z0-9_.\-])", "~", out)
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def maybe_redact(text, raw, home=None):
    return text if raw else redact(text, home=home)


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def format_mtime(mtime):
    """Format a POSIX mtime as a readable UTC stamp."""
    try:
        dt = datetime.datetime.utcfromtimestamp(float(mtime))
    except (TypeError, ValueError, OSError, OverflowError):
        return "(unknown)"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_ts(raw):
    """Format a transcript timestamp as UTC. Unparseable values pass through."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return str(raw)
    value = raw.strip()
    if not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def ts_or_placeholder(raw):
    return format_ts(raw) or "(no timestamp)"


def parse_epoch(raw):
    """POSIX seconds for an ISO-8601 stamp, or None when unparseable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
# Content normalisation
# ---------------------------------------------------------------------------


def blocks_of(content):
    """Normalise Claude content (str | dict | list | other) into block dicts."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, dict):
        return [content]
    if isinstance(content, list):
        out = []
        for item in content:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                out.append({"type": "text", "text": item})
            else:
                out.append({"type": "unknown", "text": ""})
        return out
    return [{"type": "text", "text": str(content)}]


def _is_text_block(block):
    if not isinstance(block, dict):
        return False
    btype = block.get("type")
    if btype in ("tool_result", "tool_use", "thinking", "redacted_thinking", "image"):
        return False
    return isinstance(block.get("text"), str)


def text_of(blocks):
    """Join the visible text of already-normalised blocks."""
    parts = [b["text"] for b in blocks if _is_text_block(b) and b["text"].strip()]
    return "\n\n".join(parts)


_REMINDER_RE = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.S)


def strip_system_reminders(text):
    """Return (text_without_reminders, had_reminder)."""
    if not isinstance(text, str) or "<system-reminder>" not in text:
        return (text or ""), False
    return _REMINDER_RE.sub("", text), True


def _message_of(rec):
    msg = rec.get("message")
    return msg if isinstance(msg, dict) else {}


def classify_user_record(rec):
    """Decide whether a record is a real human prompt.

    Returns ``(text, None)`` for real prompts and ``(None, reason)`` otherwise,
    where ``reason`` is one of the keys documented in
    :data:`USER_PROMPT_FILTER_RULES`.
    """
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return None, "not_user_role"
    if rec.get("isMeta"):
        return None, "meta"
    if rec.get("isSidechain"):
        return None, "sidechain"

    blocks = blocks_of(_message_of(rec).get("content"))
    has_tool_result = any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks
    )
    text, had_reminder = strip_system_reminders(text_of(blocks))
    text = text.strip()

    if not text:
        if had_reminder:
            return None, "system_reminder_only"
        if has_tool_result:
            return None, "tool_result"
        return None, "empty"

    if re.match(r"^<command-(name|message|args)>", text):
        return None, "slash_command"
    if re.match(r"^<local-command-(stdout|stderr)>", text):
        return None, "local_command_output"
    if re.match(r"^<[a-z0-9-]*hook[a-z0-9-]*>", text):
        return None, "hook_output"
    if text.startswith("Caveat: The messages below were generated by the user"):
        return None, "caveat_preamble"
    return text, None


def _is_compaction(rec):
    if rec.get("type") == "summary":
        return True
    if rec.get("isCompactSummary") or rec.get("isCompactBoundary"):
        return True
    if rec.get("subtype") in ("compact_boundary", "compact"):
        return True
    return False


class Event(object):
    """One normalised transcript record, in file order."""

    __slots__ = (
        "index",
        "lineno",
        "kind",
        "timestamp",
        "text",
        "tool_calls",
        "tool_results",
        "thinking_blocks",
        "stop_reason",
        "errored",
        "aborted",
        "abort_reason",
        "is_sidechain",
        "is_real_prompt",
        "skip_reason",
        "record_type",
    )

    def __init__(self, index, lineno, kind, **kw):
        self.index = index
        self.lineno = lineno
        self.kind = kind
        self.timestamp = kw.get("timestamp")
        self.text = kw.get("text", "")
        self.tool_calls = kw.get("tool_calls") or []
        self.tool_results = kw.get("tool_results", 0)
        self.thinking_blocks = kw.get("thinking_blocks", 0)
        self.stop_reason = kw.get("stop_reason")
        self.errored = kw.get("errored", False)
        self.aborted = kw.get("aborted", False)
        self.abort_reason = kw.get("abort_reason")
        self.is_sidechain = kw.get("is_sidechain", False)
        self.is_real_prompt = kw.get("is_real_prompt", False)
        self.skip_reason = kw.get("skip_reason")
        self.record_type = kw.get("record_type")

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<Event %d %s %r>" % (self.index, self.kind, self.text[:40])


def _timestamp_of(rec):
    ts = rec.get("timestamp")
    return ts if isinstance(ts, str) and ts.strip() else None


def build_event(index, lineno, rec):
    """Turn one raw record into an :class:`Event`. Never raises on odd input."""
    common = {
        "timestamp": _timestamp_of(rec),
        "is_sidechain": bool(rec.get("isSidechain")),
        "record_type": rec.get("type"),
    }

    # Compaction is checked first so a summary is never merged into neighbours.
    if _is_compaction(rec):
        summary = rec.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = text_of(blocks_of(_message_of(rec).get("content")))
        if not summary.strip():
            summary = "(compaction summary text not present in transcript)"
        return Event(index, lineno, KIND_COMPACTION, text=summary.strip(), **common)

    if rec.get("type") == "assistant":
        msg = _message_of(rec)
        blocks = blocks_of(msg.get("content"))
        tool_calls = [
            str(b.get("name") or "(unnamed tool)")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        thinking = sum(
            1
            for b in blocks
            if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
        )
        text = text_of(blocks).strip()
        stop_reason = msg.get("stop_reason")
        if not isinstance(stop_reason, str) or not stop_reason.strip():
            stop_reason = None
        errored = bool(rec.get("isApiErrorMessage")) or text.startswith("API Error")
        return Event(
            index,
            lineno,
            KIND_ASSISTANT,
            text=text,
            tool_calls=tool_calls,
            thinking_blocks=thinking,
            stop_reason=stop_reason,
            errored=errored,
            **common
        )

    if rec.get("type") == "user":
        text, reason = classify_user_record(rec)
        return Event(
            index,
            lineno,
            KIND_USER,
            text=text or "",
            is_real_prompt=reason is None,
            skip_reason=reason,
            **common
        )

    return Event(index, lineno, KIND_OTHER, **common)


# ---------------------------------------------------------------------------
# Streaming reader
# ---------------------------------------------------------------------------


def iter_raw_records(path):
    """Yield ``(lineno, record_or_None, error_or_None)`` lazily, read-only."""
    try:
        handle = io.open(path, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TranscriptError("cannot read %s: %s" % (path, exc))
    try:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except ValueError as exc:
                yield lineno, None, str(exc)
                continue
            if not isinstance(obj, dict):
                yield lineno, None, "top-level JSON value is not an object"
                continue
            yield lineno, obj, None
    finally:
        handle.close()


class Session(object):
    """One agent session (a transcript file, or one row set of a database)
    plus its normalised events. Shared by every provider adapter."""

    def __init__(self, path, events, malformed_count, mtime, session_id=None, cwd=None,
                 provider=PROVIDER_CLAUDE, title=None, model=None, source=None,
                 started_at=None, ended_at=None, end_reason=None, match_keys=None):
        self.path = os.path.abspath(path)
        self.file_stem = os.path.splitext(os.path.basename(self.path))[0]
        self.events = events
        self.malformed_count = malformed_count
        self.mtime = mtime
        # Filenames are not assumed to equal session IDs; the field wins when present.
        self.session_id = session_id or self.file_stem
        self.session_id_from_filename = not session_id
        self.cwd = cwd
        self.provider = provider
        self.title = title
        self.model = model
        self.source = source
        self.started_at = started_at
        self.ended_at = ended_at
        self.end_reason = end_reason
        # Selector keys: what a user may type to name this session.
        self._match_keys = list(match_keys) if match_keys else [
            self.session_id,
            self.file_stem,
        ]

    @property
    def provider_label(self):
        return provider_label(self.provider)

    @property
    def qualified_id(self):
        return "%s:%s" % (self.provider, self.session_id)

    def match_keys(self):
        return [k.lower() for k in self._match_keys if k]

    @property
    def sort_time(self):
        """Best-effort recency key: the session's own clock, else file mtime."""
        parsed = parse_epoch(self.ended_at or self.started_at or self.last_timestamp)
        return parsed if parsed is not None else self.mtime

    @property
    def event_count(self):
        return len(self.events)

    @property
    def real_prompt_events(self):
        return [e for e in self.events if e.kind == KIND_USER and e.is_real_prompt]

    @property
    def first_prompt(self):
        events = self.real_prompt_events
        return events[0].text if events else None

    @property
    def first_timestamp(self):
        for ev in self.events:
            if ev.timestamp:
                return ev.timestamp
        return None

    @property
    def last_timestamp(self):
        for ev in reversed(self.events):
            if ev.timestamp:
                return ev.timestamp
        return None

    def skip_reason_counts(self):
        counts = {}
        for ev in self.events:
            if ev.kind == KIND_USER and ev.skip_reason:
                counts[ev.skip_reason] = counts.get(ev.skip_reason, 0) + 1
        return counts


def load_session(path):
    """Read one transcript file into a :class:`Session` (streaming, read-only)."""
    events = []
    malformed = 0
    session_id = None
    cwd = None
    for lineno, rec, error in iter_raw_records(path):
        if error is not None:
            malformed += 1
            continue
        events.append(build_event(len(events), lineno, rec))
        if session_id is None:
            candidate = rec.get("sessionId")
            if isinstance(candidate, str) and candidate.strip():
                session_id = candidate.strip()
        if cwd is None:
            candidate = rec.get("cwd")
            if isinstance(candidate, str) and candidate.strip():
                cwd = candidate.strip()

    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0.0

    return Session(path, events, malformed, mtime, session_id=session_id, cwd=cwd)


def real_prompts(session):
    """The events judged to be real human prompts, in transcript order."""
    return session.real_prompt_events


# ---------------------------------------------------------------------------
# Codex CLI adapter (~/.codex/sessions/**/*.jsonl)
#
# Records are {timestamp, type, payload}. Only ``event_msg`` payloads of type
# ``user_message`` are treated as human input; ``response_item`` messages replay
# the same content together with system/developer context and are counted, not
# shown. Reasoning and tool output are counted and never rendered.
# ---------------------------------------------------------------------------

CODEX_TOOL_CALL_TYPES = {
    "function_call": None,
    "custom_tool_call": None,
    "local_shell_call": "local_shell",
    "web_search_call": "web_search",
    "mcp_tool_call": None,
}

CODEX_TOOL_OUTPUT_TYPES = (
    "function_call_output",
    "custom_tool_call_output",
    "local_shell_call_output",
    "mcp_tool_call_output",
)

CODEX_REASONING_TYPES = (
    "reasoning",
    "agent_reasoning",
    "agent_reasoning_delta",
    "agent_reasoning_section_break",
    "agent_reasoning_raw_content",
    "agent_reasoning_raw_content_delta",
)

CODEX_ERROR_TYPES = ("error", "stream_error")

CODEX_CONTEXT_PREFIXES = (
    "environment_context",
    "user_instructions",
    "user_shell",
    "project_doc",
    "instructions",
)

# Files Codex keeps beside rollouts that are *not* sessions.
CODEX_NON_SESSION_FILES = ("history.jsonl", "session_index.jsonl")


def codex_text(value):
    """Visible text of a Codex payload field (str | list-of-blocks | dict)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "message", "content"):
            inner = value.get(key)
            if isinstance(inner, str):
                return inner
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            piece = codex_text(item)
            if piece and piece.strip():
                parts.append(piece)
        return "\n\n".join(parts)
    return str(value)


def classify_codex_user_message(payload):
    """(text, None) for real human input, (None, reason) otherwise."""
    text = codex_text(payload.get("message"))
    if not text:
        text = codex_text(payload.get("content"))
    text = text.strip()
    if not text:
        return None, "empty"
    match = re.match(r"^<([a-z0-9_\-]+)[\s>]", text)
    if match and match.group(1).lower() in CODEX_CONTEXT_PREFIXES:
        return None, "environment_context"
    return text, None


def _codex_tool_name(payload, ptype):
    for key in ("name", "tool_name", "tool", "server"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    fallback = CODEX_TOOL_CALL_TYPES.get(ptype)
    return fallback or "(unnamed tool)"


def build_codex_event(index, lineno, rec):
    """Normalise one Codex rollout record. Never raises on odd input."""
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    rtype = rec.get("type")
    ptype = payload.get("type")
    timestamp = _timestamp_of(rec) or _timestamp_of(payload)
    common = {"timestamp": timestamp, "record_type": rtype}

    if rtype == "compacted" or ptype == "context_compacted":
        summary = codex_text(payload.get("message")) or codex_text(payload.get("summary"))
        if not summary.strip():
            summary = "(compaction summary text not present in this rollout)"
        return Event(index, lineno, KIND_COMPACTION, text=summary.strip(), **common)

    if rtype == "event_msg":
        if ptype == "user_message":
            text, reason = classify_codex_user_message(payload)
            return Event(
                index,
                lineno,
                KIND_USER,
                text=text or codex_text(payload.get("message")).strip(),
                is_real_prompt=reason is None,
                skip_reason=reason,
                **common
            )
        if ptype == "agent_message":
            return Event(
                index,
                lineno,
                KIND_ASSISTANT,
                text=codex_text(payload.get("message")).strip(),
                **common
            )
        if ptype in CODEX_REASONING_TYPES:
            return Event(index, lineno, KIND_OTHER, thinking_blocks=1, **common)
        if ptype == "task_complete":
            return Event(index, lineno, KIND_OTHER, stop_reason="task_complete", **common)
        if ptype == "turn_aborted":
            reason = payload.get("reason")
            return Event(
                index,
                lineno,
                KIND_OTHER,
                aborted=True,
                abort_reason=reason if isinstance(reason, str) else None,
                **common
            )
        if ptype in CODEX_ERROR_TYPES:
            # The error text itself is not presented as an assistant answer.
            return Event(index, lineno, KIND_ASSISTANT, text="", errored=True, **common)
        return Event(index, lineno, KIND_OTHER, **common)

    if rtype == "response_item":
        if ptype == "message":
            role = payload.get("role")
            if role in ("developer", "system"):
                return Event(
                    index,
                    lineno,
                    KIND_USER,
                    text=codex_text(payload.get("content")).strip(),
                    skip_reason="developer_context",
                    **common
                )
            if role == "user":
                return Event(
                    index,
                    lineno,
                    KIND_USER,
                    text=codex_text(payload.get("content")).strip(),
                    skip_reason="duplicate_response_item",
                    **common
                )
            # Assistant messages duplicate the agent_message event.
            return Event(index, lineno, KIND_OTHER, **common)
        if ptype in CODEX_TOOL_CALL_TYPES:
            return Event(
                index,
                lineno,
                KIND_OTHER,
                tool_calls=[_codex_tool_name(payload, ptype)],
                **common
            )
        if ptype in CODEX_TOOL_OUTPUT_TYPES:
            return Event(index, lineno, KIND_OTHER, tool_results=1, **common)
        if ptype in CODEX_REASONING_TYPES:
            return Event(index, lineno, KIND_OTHER, thinking_blocks=1, **common)
        return Event(index, lineno, KIND_OTHER, **common)

    return Event(index, lineno, KIND_OTHER, **common)


def load_codex_session(path):
    """Read one Codex rollout into a :class:`Session` (streaming, read-only)."""
    events = []
    malformed = 0
    session_id = None
    cwd = None
    model = None
    source = None
    started_at = None

    for lineno, rec, error in iter_raw_records(path):
        if error is not None:
            malformed += 1
            continue
        events.append(build_codex_event(len(events), lineno, rec))
        payload = rec.get("payload")
        if rec.get("type") == "session_meta" and isinstance(payload, dict):
            for key in ("id", "session_id", "uuid"):
                candidate = payload.get(key)
                if session_id is None and isinstance(candidate, str) and candidate.strip():
                    session_id = candidate.strip()
            for key, target in (("cwd", "cwd"), ("model_provider", "model"),
                                ("source", "source"), ("timestamp", "started_at")):
                candidate = payload.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    value = candidate.strip()
                    if target == "cwd" and cwd is None:
                        cwd = value
                    elif target == "model" and model is None:
                        model = value
                    elif target == "source" and source is None:
                        source = value
                    elif target == "started_at" and started_at is None:
                        started_at = value
            if model is None:
                candidate = payload.get("model")
                if isinstance(candidate, str) and candidate.strip():
                    model = candidate.strip()

    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0.0

    return Session(
        path,
        events,
        malformed,
        mtime,
        session_id=session_id,
        cwd=cwd,
        provider=PROVIDER_CODEX,
        model=model,
        source=source,
        started_at=started_at,
    )


def find_codex_paths(root):
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".jsonl"):
                continue
            if name in CODEX_NON_SESSION_FILES:
                # history.jsonl / session_index.jsonl are indexes, not sessions.
                continue
            paths.append(os.path.join(dirpath, name))
    return paths


# ---------------------------------------------------------------------------
# Hermes Agent adapter (~/.hermes/state.db)
#
# The database is opened through a ``file:...?mode=ro`` URI so the connection
# itself cannot write. Only active rows are read, in insertion (id) order.
# Reasoning columns and tool row content are never rendered.
# ---------------------------------------------------------------------------

HERMES_HIDDEN_DISPLAY_KINDS = (
    "hidden",
    "internal",
    "system",
    "meta",
    "context",
    "tool",
    "tool_output",
    "debug",
)

HERMES_ERROR_REASONS = ("error", "failed", "failure", "exception", "timeout")
HERMES_ABORT_REASONS = ("abort", "aborted", "cancel", "cancelled", "canceled",
                        "interrupted", "user_cancel")


def open_hermes_db(path):
    """Open the Hermes SQLite database strictly read-only."""
    absolute = os.path.abspath(str(path))
    if not os.path.exists(absolute):
        raise TranscriptError("Hermes database not found: %s" % path)
    try:
        from urllib.request import pathname2url
    except ImportError:  # pragma: no cover - Python 2 only
        raise TranscriptError("cannot build a read-only sqlite URI")
    uri = "file:%s?mode=ro" % pathname2url(absolute)
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise TranscriptError("cannot open %s read-only: %s" % (path, exc))


def _hermes_rows(conn, table, where="", params=()):
    """Return rows of ``table`` as dicts; tolerant of unknown column sets."""
    try:
        cursor = conn.execute("SELECT * FROM %s %s" % (table, where), params)
    except sqlite3.Error as exc:
        raise TranscriptError("cannot read %s from the Hermes database: %s" % (table, exc))
    columns = [d[0] for d in (cursor.description or [])]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def decode_hermes_content(value):
    """Decode a Hermes ``content`` cell. Malformed JSON degrades to raw text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            return ""
    if not isinstance(value, str):
        return str(value)
    text = value.strip()
    if not text or text[0] not in "[{\"":
        return value
    try:
        parsed = json.loads(text)
    except ValueError:
        return value  # plain text that merely looks like JSON
    return _hermes_text_of(parsed)


def _hermes_text_of(parsed):
    if parsed is None:
        return ""
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        for key in ("text", "content", "message", "value"):
            inner = parsed.get(key)
            if isinstance(inner, str):
                return inner
            if isinstance(inner, (list, dict)):
                nested = _hermes_text_of(inner)
                if nested:
                    return nested
        return ""
    if isinstance(parsed, list):
        parts = []
        for item in parsed:
            piece = _hermes_text_of(item)
            if piece and piece.strip():
                parts.append(piece)
        return "\n\n".join(parts)
    return str(parsed)


def _hermes_tool_names(raw):
    """Names of the tool calls encoded in an assistant ``tool_calls`` cell."""
    if raw is None:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ["(unparseable tool_calls entry)"]
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    names = []
    for item in parsed:
        if not isinstance(item, dict):
            names.append("(unnamed tool)")
            continue
        name = item.get("name")
        function = item.get("function")
        if not isinstance(name, str) and isinstance(function, dict):
            name = function.get("name")
        names.append(name if isinstance(name, str) and name.strip() else "(unnamed tool)")
    return names


def _hermes_truthy(value):
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "null")
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def _is_hidden_display_kind(value):
    if not isinstance(value, str) or not value.strip():
        return False
    kind = value.strip().lower()
    return any(marker in kind for marker in HERMES_HIDDEN_DISPLAY_KINDS)


def build_hermes_event(index, row):
    """Normalise one Hermes ``messages`` row."""
    role = (row.get("role") or "").strip().lower()
    timestamp = row.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp.strip():
        timestamp = None
    common = {"timestamp": timestamp, "record_type": role or "unknown"}
    lineno = row.get("id") if isinstance(row.get("id"), int) else index + 1

    if _hermes_truthy(row.get("compacted")):
        summary = decode_hermes_content(row.get("content")).strip()
        if not summary:
            summary = "(compaction summary text not present in the database)"
        return Event(index, lineno, KIND_COMPACTION, text=summary, **common)

    reasoning_blocks = 0
    for key in ("reasoning", "reasoning_content"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            reasoning_blocks += 1

    if role == "assistant":
        finish = row.get("finish_reason")
        finish = finish.strip() if isinstance(finish, str) and finish.strip() else None
        lowered = (finish or "").lower()
        errored = any(marker in lowered for marker in HERMES_ERROR_REASONS)
        aborted = any(marker == lowered for marker in HERMES_ABORT_REASONS)
        return Event(
            index,
            lineno,
            KIND_ASSISTANT,
            text=decode_hermes_content(row.get("content")).strip(),
            tool_calls=_hermes_tool_names(row.get("tool_calls")),
            thinking_blocks=reasoning_blocks,
            stop_reason=finish,
            errored=errored,
            aborted=aborted,
            abort_reason=finish if aborted else None,
            **common
        )

    if role == "tool":
        # Counted and named; the output itself is deliberately dropped here.
        return Event(index, lineno, KIND_OTHER, tool_results=1, **common)

    if role == "user":
        if _is_hidden_display_kind(row.get("display_kind")):
            return Event(
                index,
                lineno,
                KIND_USER,
                text=decode_hermes_content(row.get("content")).strip(),
                skip_reason="hidden_display_kind",
                **common
            )
        text = decode_hermes_content(row.get("content")).strip()
        if not text:
            return Event(index, lineno, KIND_USER, skip_reason="empty", **common)
        return Event(index, lineno, KIND_USER, text=text, is_real_prompt=True, **common)

    if role in ("system", "developer"):
        return Event(index, lineno, KIND_USER, skip_reason="system_role", **common)

    return Event(index, lineno, KIND_OTHER, **common)


def load_hermes_sessions(db_path):
    """Read every session in a Hermes ``state.db`` (read-only)."""
    conn = open_hermes_db(db_path)
    try:
        session_rows = _hermes_rows(conn, "sessions")
        message_rows = _hermes_rows(conn, "messages")
    finally:
        conn.close()

    try:
        mtime = os.stat(db_path).st_mtime
    except OSError:
        mtime = 0.0

    by_session = {}
    malformed = {}
    for row in message_rows:
        if "active" in row and not _hermes_truthy(row.get("active")):
            continue  # rolled back / superseded rows are never loaded
        key = row.get("session_id")
        if key is None:
            malformed["__orphan__"] = malformed.get("__orphan__", 0) + 1
            continue
        by_session.setdefault(str(key), []).append(row)

    def row_order(row):
        rid = row.get("id")
        return (0, rid) if isinstance(rid, int) else (1, str(rid))

    sessions = []
    for srow in session_rows:
        sid = srow.get("id")
        if sid is None:
            continue
        sid = str(sid)
        rows = sorted(by_session.get(sid, []), key=row_order)
        events = [build_hermes_event(i, row) for i, row in enumerate(rows)]
        title = srow.get("title")
        sessions.append(
            Session(
                db_path,
                events,
                malformed.get(sid, 0),
                mtime,
                session_id=sid,
                cwd=srow.get("cwd"),
                provider=PROVIDER_HERMES,
                title=title if isinstance(title, str) else None,
                model=srow.get("model"),
                source=srow.get("source"),
                started_at=srow.get("started_at"),
                ended_at=srow.get("ended_at"),
                end_reason=srow.get("end_reason"),
                match_keys=[sid],
            )
        )
    sessions.sort(key=lambda s: (len(s.session_id), s.session_id))
    return sessions


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


class Turn(object):
    """One human prompt with the visible assistant reply that followed it,
    or a standalone compaction boundary."""

    def __init__(self, kind=KIND_TURN, prompt=None, timestamp=None, text=None,
                 provider=PROVIDER_CLAUDE):
        self.kind = kind
        self.provider = provider
        self.prompt = prompt
        self.timestamp = timestamp
        self.text = text  # compaction summary text
        self.final_text = None
        self.final_timestamp = None
        self.assistant_count = 0
        self.tool_calls = []
        self.tool_results = 0
        self.thinking_blocks = 0
        self.subagent_events = 0
        self.stop_reason = None
        self.errored = False
        self.aborted = False
        self.abort_reason = None

    @property
    def completed(self):
        if self.errored or self.aborted or self.final_text is None:
            return False
        return self.stop_reason in provider_of(self.provider).complete_reasons

    def status_label(self):
        if self.kind == KIND_COMPACTION:
            return "context compaction boundary"
        spec = provider_of(self.provider)
        if self.errored:
            return "transcript records an error for this turn"
        if self.aborted:
            if self.abort_reason:
                return "aborted before completing (reason=%s)" % self.abort_reason
            return "aborted before completing (no reason recorded)"
        if self.assistant_count == 0 and not self.tool_calls:
            return "no assistant reply recorded in this transcript"
        if self.stop_reason in spec.complete_reasons:
            return "completed (%s)" % self.stop_reason
        if self.stop_reason == "tool_calls":
            return (
                "incomplete: last step was a tool call, no final reply recorded"
            )
        if self.stop_reason:
            return "stopped early (%s=%s)" % (spec.reason_key, self.stop_reason)
        if self.provider == PROVIDER_CLAUDE:
            return "no stop_reason recorded (possibly truncated)"
        return "no completion signal recorded (incomplete)"

    def status_ok(self):
        return self.completed

    def activity_summary(self, include_names=True):
        parts = []
        word = provider_of(self.provider).reasoning_word
        if self.tool_calls:
            count = len(self.tool_calls)
            label = "%d tool call%s" % (count, "" if count == 1 else "s")
            if include_names:
                seen = []
                for name in self.tool_calls:
                    if name not in seen:
                        seen.append(name)
                label += " (%s)" % ", ".join(seen)
            parts.append(label)
        if self.tool_results:
            parts.append(
                "%d tool result%s (omitted)"
                % (self.tool_results, "" if self.tool_results == 1 else "s")
            )
        if self.thinking_blocks:
            parts.append(
                "%d %s block%s"
                % (self.thinking_blocks, word, "" if self.thinking_blocks == 1 else "s")
            )
        if self.subagent_events:
            parts.append(
                "%d subagent event%s"
                % (self.subagent_events, "" if self.subagent_events == 1 else "s")
            )
        return "; ".join(parts)


def build_timeline(session):
    """Pair prompts with replies, preserving file order and compaction breaks."""
    entries = []
    current = [None]

    def flush():
        if current[0] is not None:
            entries.append(current[0])
            current[0] = None

    provider = getattr(session, "provider", PROVIDER_CLAUDE)

    def carries_activity(ev):
        return bool(
            ev.tool_calls
            or ev.tool_results
            or ev.thinking_blocks
            or ev.stop_reason
            or ev.errored
            or ev.aborted
        )

    for ev in session.events:
        if ev.kind == KIND_COMPACTION:
            flush()
            entries.append(
                Turn(
                    kind=KIND_COMPACTION,
                    timestamp=ev.timestamp,
                    text=ev.text,
                    provider=provider,
                )
            )
            continue

        if ev.kind == KIND_USER and ev.is_real_prompt:
            flush()
            current[0] = Turn(prompt=ev.text, timestamp=ev.timestamp, provider=provider)
            continue

        if ev.is_sidechain:
            if ev.kind in (KIND_USER, KIND_ASSISTANT):
                if current[0] is None:
                    current[0] = Turn(timestamp=ev.timestamp, provider=provider)
                current[0].subagent_events += 1
            continue

        if ev.kind != KIND_ASSISTANT and not carries_activity(ev):
            continue

        if current[0] is None:
            current[0] = Turn(timestamp=ev.timestamp, provider=provider)
        turn = current[0]
        # Hidden activity is aggregated wherever it is recorded: Claude keeps it
        # on assistant records, Codex on separate response_item records, Hermes
        # on assistant/tool rows.
        turn.tool_calls.extend(ev.tool_calls)
        turn.tool_results += ev.tool_results
        turn.thinking_blocks += ev.thinking_blocks
        if ev.stop_reason is not None:
            turn.stop_reason = ev.stop_reason
        if ev.errored:
            turn.errored = True
        if ev.aborted:
            turn.aborted = True
            if ev.abort_reason:
                turn.abort_reason = ev.abort_reason
        if ev.kind == KIND_ASSISTANT:
            turn.assistant_count += 1
            if ev.text.strip():
                turn.final_text = ev.text.strip()
                turn.final_timestamp = ev.timestamp

    flush()
    return entries


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def preview(text, width=60):
    """One-line, bounded preview of a prompt."""
    if not text:
        return "(no prompt found)"
    flat = " ".join(str(text).split())
    if len(flat) <= width:
        return flat
    if width <= 1:
        return flat[:width]
    return flat[: width - 1].rstrip() + "…"


def choose_fence(text):
    """Pick a Markdown fence longer than any fence already inside ``text``."""
    longest = 0
    for line in (text or "").splitlines():
        match = re.match(r"\s*(`{3,})", line)
        if match:
            longest = max(longest, len(match.group(1)))
    return "`" * max(3, longest + 1)


def fenced(text, info=""):
    fence = choose_fence(text)
    return "%s%s\n%s\n%s" % (fence, info, text.rstrip("\n"), fence)


_NO_WRAP_PREFIXES = ("    ", "\t", "|", "#", ">", "-", "*", "+", "=")


def wrap_prose(text, width=88):
    """Soft-wrap prose while leaving fenced/indented/table lines verbatim."""
    if not text:
        return ""
    lines = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence or not line.strip() or line.startswith(_NO_WRAP_PREFIXES):
            lines.append(line)
            continue
        wrapped = textwrap.wrap(
            line, width=width, break_long_words=False, break_on_hyphens=False
        )
        lines.extend(wrapped or [""])
    return "\n".join(lines)


def indent_block(text, prefix):
    return "\n".join((prefix + line) if line.strip() else "" for line in text.splitlines())


def terminal_width(default=80):
    try:
        cols = shutil.get_terminal_size(fallback=(default, 24)).columns
    except Exception:  # pragma: no cover - defensive
        cols = default
    return max(60, min(100, cols))


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _session_header_rows(session, raw, home):
    def clean(value):
        return maybe_redact(value, raw, home) if isinstance(value, str) else value

    events_cell = "%d" % session.event_count
    if session.malformed_count:
        events_cell += " (%d malformed line%s skipped)" % (
            session.malformed_count,
            "" if session.malformed_count == 1 else "s",
        )
    else:
        events_cell += " (0 malformed lines)"

    id_cell = session.session_id
    if session.session_id_from_filename:
        id_cell += " (from filename; no session id field found)"

    spec = provider_of(session.provider)
    rows = [
        ("Provider", "%s (%s)" % (spec.label, spec.product)),
        ("Session ID", id_cell),
    ]
    if session.title:
        rows.append(("Title", clean(session.title)))
    if session.model:
        rows.append(("Model", clean(session.model)))
    if session.source:
        rows.append(("Source", clean(session.source)))
    rows.extend(
        [
            ("Source file", clean(session.path)),
            ("Projected cwd", clean(session.cwd) or "(not recorded)"),
            ("File modified", format_mtime(session.mtime)),
        ]
    )
    if session.started_at:
        rows.append(("Session started", ts_or_placeholder(session.started_at)))
    if session.ended_at:
        rows.append(("Session ended", ts_or_placeholder(session.ended_at)))
    if session.end_reason:
        rows.append(("End reason", clean(session.end_reason)))
    rows.extend(
        [
            ("First event", ts_or_placeholder(session.first_timestamp)),
            ("Last event", ts_or_placeholder(session.last_timestamp)),
            ("Events", events_cell),
            ("Human prompts", "%d" % len(session.real_prompt_events)),
        ]
    )
    return rows


def render_markdown(session, raw=False, home=None, width=88):
    """Render a session as Markdown."""
    spec = provider_of(session.provider)
    lines = []
    lines.append("# %s Session — %s" % (spec.label, TITLE))
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    for key, value in _session_header_rows(session, raw, home):
        lines.append("| %s | %s |" % (key, str(value).replace("|", "\\|")))
    lines.append("")
    if raw:
        lines.append(
            "> **REDACTION: OFF** (`--raw`) — this file may contain secrets and "
            "absolute home paths."
        )
    else:
        lines.append("> **REDACTION: ON** — %s" % REDACTION_CAVEAT)
    lines.append(">")
    lines.append("> **Prompt filter** — %s" % PROMPT_FILTER_CAVEAT)
    lines.append("")
    lines.append("---")
    lines.append("")

    timeline = build_timeline(session)
    turn_no = 0
    for entry in timeline:
        if entry.kind == KIND_COMPACTION:
            lines.append("### ⋯ Context compaction boundary")
            lines.append("")
            lines.append("_%s_" % ts_or_placeholder(entry.timestamp))
            lines.append("")
            summary = maybe_redact(entry.text or "", raw, home)
            for line in wrap_prose(summary, width - 2).splitlines():
                lines.append("> %s" % line if line else ">")
            lines.append("")
            lines.append("---")
            lines.append("")
            continue

        turn_no += 1
        lines.append("## Turn %d · %s" % (turn_no, ts_or_placeholder(entry.timestamp)))
        lines.append("")
        if entry.prompt is None:
            lines.append("**You** — (no prompt recorded before this reply)")
        else:
            lines.append("**You**")
            lines.append("")
            lines.append(fenced(maybe_redact(entry.prompt, raw, home)))
        lines.append("")
        lines.append("**%s** — %s" % (spec.label, entry.status_label()))
        lines.append("")
        if entry.final_text is None:
            lines.append("_No visible assistant text recorded for this turn._")
        else:
            body = maybe_redact(entry.final_text, raw, home)
            lines.append(wrap_prose(body, width))
        lines.append("")
        activity = entry.activity_summary()
        if activity:
            lines.append("_Hidden by default: %s._" % activity)
            lines.append("")
        lines.append("---")
        lines.append("")

    if turn_no == 0:
        lines.append("_No human prompts or assistant replies were found._")
        lines.append("")

    lines.append("_%s_" % READ_ONLY_NOTE)
    lines.append("")
    lines.append("**Prompt filter rules (%s)**" % spec.product)
    lines.append("")
    for rule in prompt_filter_rules(session.provider):
        lines.append("- %s" % rule)
    lines.append("")
    lines.append(
        "_Reasoning text, tool inputs and tool output are counted but never "
        "included in this view._"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f7f9;
  --card: #ffffff;
  --ink: #16191d;
  --muted: #4a5159;
  --line: #d6dae0;
  --accent: #7b3fb8;
  --ok: #0f6d3f;
  --warn: #8a4b00;
  --err: #a5201c;
  --code: #f0f1f4;
  /* Provider badges: white text on these holds >= 4.5:1 contrast. */
  --claude: #6b34a8;
  --codex: #0c6152;
  --hermes: #9a3d0f;
  --badge-ink: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a;
    --card: #1d2025;
    --ink: #eceef1;
    --muted: #a8b0b9;
    --line: #343941;
    --accent: #c9a4ec;
    --ok: #6ede9f;
    --warn: #f0bd72;
    --err: #ff9d96;
    --code: #262a30;
    --claude: #d3b6f2;
    --codex: #93e2d1;
    --hermes: #f5bd9a;
    --badge-ink: #14161a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0 1rem 4rem;
  background: var(--bg);
  color: var(--ink);
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue",
        Arial, "Noto Sans", sans-serif;
}
main { max-width: 60rem; margin: 0 auto; }
header.top {
  position: sticky; top: 0; z-index: 5;
  background: var(--bg);
  padding: 1.25rem 0 0.75rem;
  border-bottom: 1px solid var(--line);
}
h1 { font-size: 1.4rem; margin: 0 0 0.25rem; letter-spacing: -0.01em; }
h1 .badge { vertical-align: 0.15em; margin-right: 0.45rem; }
p.sub { margin: 0 0 0.75rem; color: var(--muted); font-size: 0.85rem; }
.badge {
  display: inline-block; padding: 0.12rem 0.5rem; border-radius: 999px;
  font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; color: var(--badge-ink); background: var(--accent);
}
.badge-claude { background: var(--claude); }
.badge-codex { background: var(--codex); }
.badge-hermes { background: var(--hermes); }
h2.role .badge { text-transform: none; letter-spacing: 0; }
dl.meta {
  display: grid; grid-template-columns: max-content 1fr;
  gap: 0.15rem 0.9rem; margin: 0.5rem 0 0.9rem; font-size: 0.85rem;
}
dl.meta dt { color: var(--muted); }
dl.meta dd { margin: 0; word-break: break-word; }
input[type=search] {
  width: 100%; padding: 0.55rem 0.7rem; font: inherit; font-size: 0.9rem;
  color: var(--ink); background: var(--card);
  border: 1px solid var(--line); border-radius: 8px;
}
input[type=search]:focus-visible { outline: 3px solid var(--accent); outline-offset: 1px; }
.banner {
  margin: 0.75rem 0; padding: 0.55rem 0.75rem; font-size: 0.82rem;
  border-left: 4px solid var(--accent); background: var(--card);
  border-radius: 0 8px 8px 0; color: var(--muted);
}
article.turn, section.boundary {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 0.9rem 1rem; margin: 1rem 0;
}
section.boundary { border-style: dashed; text-align: left; }
.turnhead {
  display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: baseline;
  font-size: 0.8rem; color: var(--muted); margin-bottom: 0.6rem;
}
.turnhead .no { font-weight: 700; color: var(--ink); }
.status { font-weight: 600; }
.status.ok { color: var(--ok); }
.status.warn { color: var(--warn); }
.status.err { color: var(--err); }
h2.role {
  font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin: 0.8rem 0 0.3rem;
}
pre {
  margin: 0; padding: 0.7rem 0.8rem; background: var(--code);
  border-radius: 8px; overflow-x: auto;
  white-space: pre-wrap; word-wrap: break-word;
  font: 0.85rem/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
pre.prose { background: transparent; padding: 0.1rem 0; font: inherit; }
details { margin-top: 0.7rem; font-size: 0.83rem; color: var(--muted); }
summary { cursor: pointer; }
details ul { margin: 0.4rem 0 0; padding-left: 1.2rem; }
footer { margin-top: 2rem; font-size: 0.78rem; color: var(--muted); }
[hidden] { display: none !important; }
"""

_JS = """
(function () {
  var box = document.getElementById("filter");
  if (!box) { return; }
  var cards = document.querySelectorAll("article.turn, section.boundary");
  box.addEventListener("input", function () {
    var needle = box.value.toLowerCase();
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      card.hidden = needle !== "" &&
        card.textContent.toLowerCase().indexOf(needle) === -1;
    }
  });
})();
"""


def esc(value):
    return html_mod.escape("" if value is None else str(value), quote=True)


def _status_class(turn):
    if turn.errored:
        return "err"
    if turn.completed:
        return "ok"
    return "warn"


def render_html(session, raw=False, home=None):
    """Render a session as a self-contained, offline HTML document."""
    out = []
    add = out.append
    spec = provider_of(session.provider)
    heading = "%s Session — %s" % (spec.label, session.session_id)
    badge = '<span class="badge badge-%s">%s</span>' % (esc(spec.id), esc(spec.label))

    add("<!DOCTYPE html>")
    add('<html lang="en">')
    add("<head>")
    add('<meta charset="utf-8">')
    add('<meta name="viewport" content="width=device-width, initial-scale=1">')
    # CSP is a fixed constant with no HTML-special characters, so it is emitted
    # verbatim (escaping it would only obscure the policy in the source view).
    add('<meta http-equiv="Content-Security-Policy" content="%s">' % CSP)
    add('<meta name="referrer" content="no-referrer">')
    add("<title>%s — %s</title>" % (esc(TITLE), esc(heading)))
    add("<style>%s</style>" % _CSS)
    add("</head>")
    add("<body>")
    add("<main>")
    add('<header class="top">')
    add("<h1>%s%s</h1>" % (badge, esc(heading)))
    add('<p class="sub">%s</p>' % esc(READ_ONLY_NOTE))
    add('<dl class="meta">')
    for key, value in _session_header_rows(session, raw, home):
        add("<dt>%s</dt><dd>%s</dd>" % (esc(key), esc(value)))
    add("</dl>")
    add(
        '<input type="search" id="filter" aria-label="Filter turns by text"'
        ' placeholder="Filter turns…" autocomplete="off">'
    )
    add("</header>")

    if raw:
        banner = "REDACTION: OFF (--raw) — this file may contain secrets and home paths."
    else:
        banner = "REDACTION: ON — " + REDACTION_CAVEAT
    add('<p class="banner">%s</p>' % esc(banner))

    add("<details>")
    add(
        "<summary>%s</summary>"
        % esc("How this view was produced (%s prompt filter heuristics)" % spec.product)
    )
    add("<p>%s</p>" % esc(PROMPT_FILTER_CAVEAT))
    add("<ul>")
    for rule in prompt_filter_rules(session.provider):
        add("<li>%s</li>" % esc(rule))
    add("</ul>")
    add(
        "<p>%s</p>"
        % esc(
            "%s reasoning, tool inputs and tool output are counted but never "
            "rendered here." % spec.label
        )
    )
    add("</details>")

    timeline = build_timeline(session)
    turn_no = 0
    for entry in timeline:
        if entry.kind == KIND_COMPACTION:
            add('<section class="boundary">')
            add(
                '<div class="turnhead"><span class="no">⋯</span>'
                "<span>%s</span><span class=\"status warn\">%s</span></div>"
                % (esc(ts_or_placeholder(entry.timestamp)), esc("Context compaction boundary"))
            )
            add('<h2 class="role">Summary carried forward</h2>')
            add("<pre>%s</pre>" % esc(maybe_redact(entry.text or "", raw, home)))
            add("</section>")
            continue

        turn_no += 1
        add('<article class="turn">')
        add(
            '<div class="turnhead"><span class="no">Turn %d</span>'
            "<span>%s</span><span class=\"status %s\">%s</span></div>"
            % (
                turn_no,
                esc(ts_or_placeholder(entry.timestamp)),
                _status_class(entry),
                esc(entry.status_label()),
            )
        )
        add('<h2 class="role">You</h2>')
        if entry.prompt is None:
            add("<p>%s</p>" % esc("(no prompt recorded before this reply)"))
        else:
            add("<pre>%s</pre>" % esc(maybe_redact(entry.prompt, raw, home)))
        add('<h2 class="role">%s%s</h2>' % (badge, esc(" reply")))
        if entry.final_text is None:
            add("<p>%s</p>" % esc("No visible assistant text recorded for this turn."))
        else:
            add(
                '<pre class="prose">%s</pre>'
                % esc(maybe_redact(entry.final_text, raw, home))
            )
        activity = entry.activity_summary()
        if activity:
            add("<details>")
            add("<summary>%s</summary>" % esc("Hidden by default: " + activity))
            add("<ul>")
            if entry.tool_calls:
                add("<li>%s</li>" % esc("Tool calls: " + ", ".join(entry.tool_calls)))
            if entry.tool_results:
                add("<li>%s</li>" % esc("Tool results: %d" % entry.tool_results))
            if entry.thinking_blocks:
                add(
                    "<li>%s</li>"
                    % esc(
                        "%s blocks: %d"
                        % (spec.reasoning_word.capitalize(), entry.thinking_blocks)
                    )
                )
            if entry.subagent_events:
                add("<li>%s</li>" % esc("Subagent events: %d" % entry.subagent_events))
            add(
                "<li>%s</li>"
                % esc(
                    "Tool inputs, tool results, %s text and subagent "
                    "transcripts are intentionally not included in this export."
                    % spec.reasoning_word
                )
            )
            add("</ul>")
            add("</details>")
        add("</article>")

    if turn_no == 0:
        add("<p>%s</p>" % esc("No human prompts or assistant replies were found."))

    add("<footer>%s</footer>" % esc(READ_ONLY_NOTE))
    add("</main>")
    add("<script>%s</script>" % _JS)
    add("</body>")
    add("</html>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Discovery and selection
# ---------------------------------------------------------------------------


def default_root():
    """Legacy helper: the Claude default root (kept for compatibility)."""
    return default_root_for(PROVIDER_CLAUDE)


def find_transcript_paths(root):
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".jsonl"):
                paths.append(os.path.join(dirpath, name))
    return paths


def find_sessions(root):
    """Load every transcript under ``root`` (recursive), newest file first."""
    if not os.path.isdir(root):
        raise TranscriptError("Root not found or not a directory: %s" % root)
    sessions = []
    for path in find_transcript_paths(root):
        try:
            sessions.append(load_session(path))
        except TranscriptError:
            continue
    sessions.sort(key=lambda s: (-s.mtime, s.path))
    return sessions


def find_sessions_for(provider, root):
    """Load every session of ``provider`` at ``root`` (newest first)."""
    if provider == PROVIDER_HERMES:
        path = str(root)
        if os.path.isdir(path):
            path = os.path.join(path, "state.db")
        if not os.path.isfile(path):
            raise TranscriptError("Hermes database not found: %s" % root)
        sessions = load_hermes_sessions(path)
    else:
        if not os.path.isdir(root):
            raise TranscriptError("Root not found or not a directory: %s" % root)
        if provider == PROVIDER_CODEX:
            paths, loader = find_codex_paths(root), load_codex_session
        else:
            paths, loader = find_transcript_paths(root), load_session
        sessions = []
        for path in paths:
            try:
                sessions.append(loader(path))
            except TranscriptError:
                continue
    sessions.sort(key=lambda s: (-s.sort_time, s.path, s.session_id))
    return sessions


def find_sessions_multi(roots):
    """Load sessions for an ordered ``{provider: root}`` mapping.

    Returns ``(sessions, notes)``; ``notes`` records roots that could not be
    scanned so the caller can report them without failing the whole run.
    """
    sessions = []
    notes = []
    for provider, root in roots.items():
        try:
            sessions.extend(find_sessions_for(provider, root))
        except TranscriptError as exc:
            notes.append("%s: %s" % (provider_label(provider), exc))
    sessions.sort(key=lambda s: (-s.sort_time, s.provider, s.path, s.session_id))
    return sessions, notes


def _selector_candidates(sessions):
    return "\n".join(
        "  %-8s %s  (%s)"
        % (s.provider + ":", s.session_id, s.title or os.path.basename(s.path))
        for s in sessions
    )


def resolve_selector(sessions, selector):
    """Resolve ``[provider:]partial-id`` to exactly one session."""
    raw = (selector or "").strip()
    if not raw:
        raise SelectorError("Empty session selector.")

    wanted_provider = None
    if ":" in raw:
        head, tail = raw.split(":", 1)
        head = head.strip().lower()
        if head in PROVIDER_IDS:
            wanted_provider, raw = head, tail.strip()
        elif head and re.match(r"^[a-z][a-z0-9_\-]*$", head) and tail.strip():
            raise SelectorError(
                "Unknown provider qualifier %r in selector %r. Known providers: %s."
                % (head, selector, ", ".join(PROVIDER_IDS))
            )

    pool = sessions
    if wanted_provider is not None:
        pool = [s for s in sessions if s.provider == wanted_provider]
        if not pool:
            raise SelectorError(
                "No %s sessions were found to match %r."
                % (provider_label(wanted_provider), selector)
            )

    needle = raw.lower()
    if not needle:
        raise SelectorError("Empty session selector.")

    exact = [s for s in pool if needle in s.match_keys()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise SelectorError(
            "Multiple sessions match %r (%d). Qualify it with a provider "
            "(e.g. %s):\n%s"
            % (selector, len(exact), exact[0].qualified_id, _selector_candidates(exact))
        )

    matches = [s for s in pool if any(needle in key for key in s.match_keys())]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SelectorError(
            "No session matches %r. Run '%s list --provider all' to see "
            "available sessions." % (selector, PROGRAM)
        )
    raise SelectorError(
        "Multiple sessions match %r (%d). Be more specific, or qualify it with "
        "a provider (e.g. %s):\n%s"
        % (selector, len(matches), matches[0].qualified_id, _selector_candidates(matches))
    )


def _inode_identity(item):
    return (item.st_dev, item.st_ino)


def _path_inode(path):
    try:
        return _inode_identity(os.stat(path))
    except (FileNotFoundError, OSError):
        return None


def source_inode_identities(source_paths):
    return frozenset(
        identity for identity in (_path_inode(path) for path in source_paths or ())
        if identity is not None
    )


def _resolved_destination(out, home=None, source_paths=None, require_parent=False):
    home_dir = os.path.abspath(str(home) if home else os.path.expanduser("~"))
    text = str(out)
    if text == "~":
        text = home_dir
    elif text.startswith("~" + os.sep) or text.startswith("~/"):
        text = os.path.join(home_dir, text[2:])
    expanded = os.path.abspath(text)

    # Guard both the caller-supplied home (tests, unusual setups) and the real
    # one, for every provider, following symlinks on each.
    guards = []
    for base in (home_dir, os.path.abspath(os.path.expanduser("~"))):
        for pid in PROVIDER_IDS:
            guard = os.path.join(base, provider_of(pid).data_dir)
            for resolved in (guard, os.path.realpath(guard)):
                if (pid, resolved) not in guards:
                    guards.append((pid, resolved))

    real_target = os.path.realpath(expanded)
    candidates = {expanded, real_target}
    guard_inodes = set()
    for pid, guard in guards:
        identity = _path_inode(guard)
        if identity is not None:
            guard_inodes.add((pid, identity))
        for candidate in candidates:
            if candidate == guard or candidate.startswith(guard + os.sep):
                raise OutputPathError(
                    "Refusing to write inside the %s data directory (%s): %s"
                    % (provider_label(pid), guard, out)
                )

    source_inodes = source_inode_identities(source_paths)
    target_inode = _path_inode(expanded)
    if target_inode is not None and target_inode in source_inodes:
        raise OutputPathError(
            "Refusing to overwrite a source session file: %s" % expanded
        )

    parent = os.path.realpath(os.path.dirname(expanded) or ".")
    try:
        parent_stat = os.stat(parent, follow_symlinks=False)
    except FileNotFoundError:
        if require_parent:
            raise OutputPathError("output directory does not exist: %s" % parent)
        parent_identity = None
    else:
        parent_identity = _inode_identity(parent_stat)
        for pid, guard_identity in guard_inodes:
            if parent_identity == guard_identity:
                raise OutputPathError(
                    "Refusing to write inside the %s data directory: %s"
                    % (provider_label(pid), out)
                )
    return (
        os.path.join(parent, os.path.basename(expanded)),
        parent_identity,
        source_inodes,
    )


def validate_out_path(out, home=None, source_paths=None):
    """Resolve ``out`` and reject provider stores and loaded source inodes."""
    return _resolved_destination(out, home=home, source_paths=source_paths)[0]


def _stat_identity(path, dir_fd=None):
    try:
        item = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (item.st_dev, item.st_ino, item.st_mode)


def _target_inode(path, directory_fd):
    try:
        return _inode_identity(os.stat(path, dir_fd=directory_fd))
    except (FileNotFoundError, OSError):
        return None


def _refuse_source_inode(path, directory_fd, source_inodes):
    if _target_inode(path, directory_fd) in source_inodes:
        raise OutputPathError(
            "Refusing to overwrite a source session file: %s" % path
        )


def write_output_atomic(
    out_path,
    body,
    force=False,
    source_paths=None,
    source_inodes=None,
    expected_parent_identity=None,
):
    """Write a secure 0600 export without truncating the destination inode."""
    parent = os.path.dirname(out_path) or "."
    name = os.path.basename(out_path)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_fd = os.open(parent, directory_flags)
    temp_name = None
    primary_error = None
    try:
        opened_parent_identity = _inode_identity(os.fstat(directory_fd))
        if (
            expected_parent_identity is not None
            and opened_parent_identity != expected_parent_identity
        ):
            raise OutputPathError("output directory changed during export: %s" % parent)

        protected_inodes = {identity for identity in (source_inodes or ())}
        protected_inodes.update(source_inode_identities(source_paths))
        initial = _stat_identity(name, dir_fd=directory_fd)
        if initial is not None and not force:
            raise OutputPathError(
                "%s already exists; pass --force to overwrite." % out_path
            )
        _refuse_source_inode(name, directory_fd, protected_inodes)

        for _attempt in range(100):
            candidate = ".ai-session-viewer-%s.tmp" % secrets.token_hex(12)
            try:
                temp_fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                temp_name = candidate
                break
            except FileExistsError:
                continue
        else:  # pragma: no cover - improbable temporary-name collision storm
            raise OSError("cannot allocate a temporary export file")

        try:
            view = memoryview(body.encode("utf-8"))
            while view:
                view = view[os.write(temp_fd, view):]
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)

        current = _stat_identity(name, dir_fd=directory_fd)
        if current != initial:
            raise OutputPathError("output path changed during export: %s" % out_path)
        _refuse_source_inode(name, directory_fd, protected_inodes)
        os.replace(
            temp_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temp_name = None
        os.fsync(directory_fd)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error = None
        try:
            if temp_name is not None:
                try:
                    os.unlink(temp_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    cleanup_error = exc
        finally:
            try:
                os.close(directory_fd)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            if primary_error is None:
                raise cleanup_error
            warnings.warn(
                "export cleanup failed after %s: %s"
                % (type(primary_error).__name__, cleanup_error),
                RuntimeWarning,
            )


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------


class Style(object):
    def __init__(self, stream):
        self.enabled = False
        try:
            self.enabled = bool(stream.isatty()) and os.environ.get("NO_COLOR") is None
        except Exception:  # pragma: no cover - defensive
            self.enabled = False

    def _wrap(self, code, text):
        return "\033[%sm%s\033[0m" % (code, text) if self.enabled else text

    def bold(self, text):
        return self._wrap("1", text)

    def dim(self, text):
        return self._wrap("2", text)

    def accent(self, text):
        return self._wrap("35", text)


class ProviderSelectionError(Exception):
    """Raised when --provider / --root are combined in a way we cannot honour."""


def resolve_provider_roots(args):
    """Return an ordered ``{provider: root}`` mapping for this invocation.

    Backward compatibility: an explicit ``--root`` with no ``--provider`` means
    Claude, exactly as before providers existed.
    """
    explicit_root = bool(getattr(args, "root_explicit", False))
    provider = getattr(args, "provider", None)
    if provider is None:
        provider = PROVIDER_CLAUDE if explicit_root else PROVIDER_ALL

    per_provider = {
        PROVIDER_CLAUDE: getattr(args, "claude_root", None),
        PROVIDER_CODEX: getattr(args, "codex_root", None),
        PROVIDER_HERMES: getattr(args, "hermes_root", None),
    }

    roots = collections.OrderedDict()
    if provider == PROVIDER_ALL:
        if explicit_root:
            raise ProviderSelectionError(
                "--root selects one provider's location and cannot be combined "
                "with --provider all. Use --claude-root / --codex-root / "
                "--hermes-root to point each provider somewhere else."
            )
        for pid in PROVIDER_IDS:
            roots[pid] = per_provider[pid] or default_root_for(pid)
        return roots

    if explicit_root:
        roots[provider] = str(args.root)
    else:
        roots[provider] = per_provider[provider] or default_root_for(provider)
    return roots


def _roots_caption(roots, args):
    return "; ".join(
        "%s %s" % (provider_label(pid), maybe_redact(str(root), args.raw, args.home))
        for pid, root in roots.items()
    )


def _print_banner(style, caption, raw, extra=""):
    mode = "REDACTION: OFF (--raw)" if raw else "REDACTION: ON (best-effort)"
    print(style.bold("%s %s" % (TITLE, __version__)) + style.dim("  — read-only"))
    print(style.dim("Sources: %s" % caption))
    print(style.dim("%s%s" % (mode, extra)))
    print("")


def _report_notes(notes):
    for note in notes:
        sys.stderr.write("note: %s\n" % note)


def _gather(args):
    """Return ``(sessions, notes, roots, exit_code)``; sessions is None on error."""
    try:
        roots = resolve_provider_roots(args)
    except ProviderSelectionError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return None, [], {}, 2
    sessions, notes = find_sessions_multi(roots)
    if not sessions:
        # A single, explicitly chosen provider whose root cannot be read at all
        # is a hard error; when aggregating, an unreadable root is only a note.
        if notes and len(roots) == 1:
            sys.stderr.write("error: %s\n" % notes[0])
            return None, notes, roots, 2
        _report_notes(notes)
        if len(roots) == 1:
            pid, root = list(roots.items())[0]
            if pid == PROVIDER_HERMES:
                sys.stderr.write("No Hermes sessions found in %s\n" % root)
            else:
                sys.stderr.write("No .jsonl transcripts found under %s\n" % root)
        else:
            sys.stderr.write(
                "No sessions found for any provider: %s\n"
                % ", ".join(
                    "%s (%s)" % (provider_label(pid), root)
                    for pid, root in roots.items()
                )
            )
        return None, notes, roots, 1
    return sessions, notes, roots, 0


def cmd_list(args, style):
    sessions, notes, roots, code = _gather(args)
    if sessions is None:
        return code

    total_malformed = sum(s.malformed_count for s in sessions)
    _print_banner(
        style,
        _roots_caption(roots, args),
        args.raw,
        "   %d session%s, %d malformed line%s total"
        % (
            len(sessions),
            "" if len(sessions) == 1 else "s",
            total_malformed,
            "" if total_malformed == 1 else "s",
        ),
    )
    _report_notes(notes)

    width = terminal_width()
    rows = []
    for idx, session in enumerate(sessions, start=1):
        rows.append(
            (
                str(idx),
                session.provider_label,
                session.session_id,
                format_mtime(session.mtime),
                str(session.event_count),
                maybe_redact(session.cwd, args.raw, args.home) or "(no cwd)",
            )
        )

    headers = ("#", "PROVIDER", "SESSION ID", "MODIFIED (UTC)", "EVENTS", "PROJECTED CWD")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()

    print(style.bold(fmt(headers)))
    for row, session in zip(rows, sessions):
        print(fmt(row))
        root = roots.get(session.provider)
        location = maybe_redact(session.path, args.raw, args.home)
        if root and os.path.isdir(str(root)):
            try:
                rel = os.path.relpath(session.path, root)
                if rel not in (".", "") and not rel.startswith(".."):
                    location = rel
            except ValueError:  # pragma: no cover - different drives on Windows
                pass
        elif root:
            # A file-rooted provider (Hermes): name the database, not "."
            location = os.path.basename(session.path) or location
        notes_row = ["select: %s" % session.qualified_id]
        if session.session_id_from_filename:
            notes_row.append("id from filename")
        notes_row.append(
            "%d malformed" % session.malformed_count if session.malformed_count
            else "0 malformed"
        )
        print(style.dim("     source: %s  [%s]" % (location, ", ".join(notes_row))))
        meta = []
        if session.title:
            meta.append("title: %s" % maybe_redact(session.title, args.raw, args.home))
        if session.model:
            meta.append("model: %s" % session.model)
        if session.source:
            meta.append("source: %s" % session.source)
        if meta:
            print(style.dim("     " + "   ".join(meta)))
        first = maybe_redact(session.first_prompt, args.raw, args.home)
        print(
            style.dim(
                "     first prompt: %s" % preview(first, max(30, width - 22))
            )
        )
        print("")
    print(style.dim(PROMPT_FILTER_CAVEAT))
    return 0


def _load_selected(args):
    """Return (session, exit_code). ``session`` is None when the code is set."""
    sessions, notes, _roots, code = _gather(args)
    if sessions is None:
        return None, code
    args.loaded_source_paths = tuple(session.path for session in sessions)
    args.loaded_source_inodes = source_inode_identities(args.loaded_source_paths)
    _report_notes(notes)
    try:
        return resolve_selector(sessions, args.selector), 0
    except SelectorError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return None, 2


def _session_summary_lines(session, args, style):
    lines = []
    lines.append(
        style.bold(
            "%s session %s" % (session.provider_label, session.session_id)
        )
    )
    if session.title:
        lines.append(
            style.dim("title: %s" % maybe_redact(session.title, args.raw, args.home))
        )
    lines.append(
        style.dim(
            "source: %s" % maybe_redact(session.path, args.raw, args.home)
        )
    )
    detail = "cwd: %s   modified: %s" % (
        maybe_redact(session.cwd, args.raw, args.home) or "(no cwd)",
        format_mtime(session.mtime),
    )
    if session.model:
        detail += "   model: %s" % session.model
    if session.source:
        detail += "   origin: %s" % session.source
    lines.append(style.dim(detail))
    lines.append(
        style.dim(
            "events: %d   malformed lines skipped: %d   human prompts: %d"
            % (
                session.event_count,
                session.malformed_count,
                len(session.real_prompt_events),
            )
        )
    )
    return lines


def cmd_prompts(args, style):
    session, code = _load_selected(args)
    if session is None:
        return code

    width = terminal_width()
    _print_banner(
        style,
        "%s %s"
        % (session.provider_label, maybe_redact(session.path, args.raw, args.home)),
        args.raw,
    )
    for line in _session_summary_lines(session, args, style):
        print(line)
    print("")

    prompts = real_prompts(session)
    print(
        style.bold(
            "%d prompt%s judged to be real human input"
            % (len(prompts), "" if len(prompts) == 1 else "s")
        )
    )
    skipped = session.skip_reason_counts()
    if skipped:
        detail = ", ".join("%s %d" % (k, v) for k, v in sorted(skipped.items()))
        print(style.dim("filtered out: %s" % detail))
    print("")

    for number, event in enumerate(prompts, start=1):
        print(
            style.accent(
                "%s  %s" % (style.bold("[%d]" % number), ts_or_placeholder(event.timestamp))
            )
        )
        body = maybe_redact(event.text, args.raw, args.home)
        print(indent_block(wrap_prose(body, width - 4), "    "))
        print("")
    print(style.dim(PROMPT_FILTER_CAVEAT))
    return 0


def cmd_timeline(args, style):
    session, code = _load_selected(args)
    if session is None:
        return code

    width = terminal_width()
    rule = "─" * min(width, 78)
    _print_banner(
        style,
        "%s %s"
        % (session.provider_label, maybe_redact(session.path, args.raw, args.home)),
        args.raw,
    )
    for line in _session_summary_lines(session, args, style):
        print(line)
    print("")

    timeline = build_timeline(session)
    turn_no = 0
    for entry in timeline:
        if entry.kind == KIND_COMPACTION:
            print(style.dim(rule))
            print(
                style.accent(
                    "⋯ Context compaction boundary  %s"
                    % ts_or_placeholder(entry.timestamp)
                )
            )
            summary = maybe_redact(entry.text or "", args.raw, args.home)
            print(indent_block(wrap_prose(summary, width - 4), "    "))
            print(style.dim(rule))
            print("")
            continue

        turn_no += 1
        print(style.dim(rule))
        print(
            style.bold("Turn %d" % turn_no)
            + "  "
            + style.dim(ts_or_placeholder(entry.timestamp))
        )
        print("")
        print(style.accent("You:"))
        if entry.prompt is None:
            print("    (no prompt recorded before this reply)")
        else:
            print(
                indent_block(
                    wrap_prose(maybe_redact(entry.prompt, args.raw, args.home), width - 4),
                    "    ",
                )
            )
        print("")
        print(
            style.accent("%s:" % session.provider_label)
            + " "
            + style.dim("[%s]" % entry.status_label())
        )
        if entry.final_text is None:
            print("    (no visible assistant text recorded)")
        else:
            print(
                indent_block(
                    wrap_prose(
                        maybe_redact(entry.final_text, args.raw, args.home), width - 4
                    ),
                    "    ",
                )
            )
        activity = entry.activity_summary(include_names=bool(args.show_activity))
        if activity:
            print("")
            print(style.dim("    ↳ hidden: " + activity))
        print("")

    if turn_no == 0:
        print("No human prompts or assistant replies were found.")
    print(style.dim(PROMPT_FILTER_CAVEAT))
    return 0


def cmd_export(args, style):
    session, code = _load_selected(args)
    if session is None:
        return code

    try:
        out_path, parent_identity, current_source_inodes = _resolved_destination(
            args.out,
            home=args.home,
            source_paths=getattr(args, "loaded_source_paths", (session.path,)),
            require_parent=True,
        )
    except OutputPathError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    if args.format == "html":
        body = render_html(session, raw=args.raw, home=args.home)
    else:
        body = render_markdown(session, raw=args.raw, home=args.home)

    try:
        protected_source_inodes = set(current_source_inodes)
        protected_source_inodes.update(
            getattr(args, "loaded_source_inodes", ())
        )
        write_output_atomic(
            out_path,
            body,
            force=args.force,
            source_paths=getattr(args, "loaded_source_paths", (session.path,)),
            source_inodes=protected_source_inodes,
            expected_parent_identity=parent_identity,
        )
    except (OSError, OutputPathError) as exc:
        sys.stderr.write("error: cannot write %s: %s\n" % (out_path, exc))
        return 2

    print(
        "%s export written to %s (%d bytes, redaction %s)"
        % (
            args.format,
            out_path,
            len(body.encode("utf-8")),
            "OFF" if args.raw else "ON",
        )
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _RootAction(argparse.Action):
    """Record that ``--root`` was supplied, so the legacy meaning is kept:
    an explicit --root with no --provider means Claude."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, "root_explicit", True)


SELECTOR_HELP = (
    "unique partial session ID or filename, optionally provider-qualified "
    "(e.g. codex:01983b2e)"
)


def _add_common(parser):
    parser.set_defaults(root_explicit=False)
    parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=None,
        help="which agent's sessions to read: claude, codex, hermes, or all "
        "(default: all; an explicit --root alone means claude)",
    )
    parser.add_argument(
        "--root",
        action=_RootAction,
        default=default_root(),
        help="location for the selected provider — a directory of .jsonl files "
        "for claude/codex, or a state.db for hermes (default: "
        "~/.claude/projects). Cannot be combined with --provider all.",
    )
    for pid in PROVIDER_IDS:
        spec = provider_of(pid)
        parser.add_argument(
            "--%s-root" % pid,
            default=None,
            help="%s location used by --provider all / --provider %s: %s "
            "(default: ~/%s)"
            % (
                spec.label,
                pid,
                spec.root_help,
                "/".join(spec.default_root_parts),
            ),
        )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="disable redaction (output may contain secrets and home paths)",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="override the home directory used for redaction and output-path "
        "safety checks (mainly for testing)",
    )


def build_parser():
    """Build the read-only session viewer command tree."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="%s — read-only viewer for local AI coding sessions: "
        "Claude Code (~/.claude/projects), OpenAI Codex CLI (~/.codex/sessions) "
        "and Hermes Agent (~/.hermes/state.db)." % TITLE,
        epilog="Sources are only ever read. Nothing is written under ~/.claude, "
        "~/.codex or ~/.hermes.",
    )
    parser.add_argument("--version", action="version", version="%s %s" % (PROGRAM, __version__))
    subs = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_list = subs.add_parser(
        "list", help="list sessions found for one provider or all of them"
    )
    _add_common(p_list)

    p_prompts = subs.add_parser(
        "prompts", help="show the real human prompts of one session"
    )
    p_prompts.add_argument("selector", help=SELECTOR_HELP)
    _add_common(p_prompts)

    p_timeline = subs.add_parser(
        "timeline", help="show prompts paired with visible assistant replies"
    )
    p_timeline.add_argument("selector", help=SELECTOR_HELP)
    p_timeline.add_argument(
        "--show-activity",
        action="store_true",
        help="include tool names in the hidden-activity summary",
    )
    _add_common(p_timeline)

    p_export = subs.add_parser("export", help="write a Markdown or HTML session view")
    p_export.add_argument("selector", help=SELECTOR_HELP)
    p_export.add_argument(
        "--format", choices=("markdown", "html"), default="markdown", help="output format"
    )
    p_export.add_argument(
        "--out",
        required=True,
        help="destination file (required; must be outside ~/.claude, ~/.codex "
        "and ~/.hermes)",
    )
    p_export.add_argument(
        "--force", action="store_true", help="overwrite an existing destination file"
    )
    _add_common(p_export)
    return parser


def main(argv=None):
    """Dispatch a session-viewer command and return a process exit status."""
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(args, "command", None):
        parser.print_usage(sys.stderr)
        sys.stderr.write("error: a command is required (list, prompts, timeline, export)\n")
        return 2

    if not hasattr(args, "show_activity"):
        args.show_activity = False

    style = Style(sys.stdout)
    handlers = {
        "list": cmd_list,
        "prompts": cmd_prompts,
        "timeline": cmd_timeline,
        "export": cmd_export,
    }
    try:
        return handlers[args.command](args, style)
    except BrokenPipeError:  # pragma: no cover - piping into head etc.
        return 0
    except KeyboardInterrupt:  # pragma: no cover
        sys.stderr.write("interrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
