#!/usr/bin/env python3
"""AI Session Viewer — 로컬 AI 코딩 에이전트 세션을 위한 읽기 전용, 의존성 없는
뷰어.

세 가지 공급자를 어댑터를 통해 지원하며, 어댑터들은 하나의 공유
Session/Event/Turn 모델과 하나의 공유 렌더러 집합에 데이터를 공급한다:

* ``claude``  — ``~/.claude/projects`` 아래의 Claude Code JSONL 트랜스크립트
* ``codex``   — ``~/.codex/sessions`` 아래의 OpenAI Codex CLI 롤아웃
* ``hermes``  — ``~/.hermes/state.db`` 에 있는 Hermes Agent의 SQLite 상태

설계 규칙(테스트 스위트로 강제됨):

* 소스는 읽기 전용으로 열리고(SQLite는 ``file:...?mode=ro`` 사용), 스트리밍으로
  읽히며, 절대 다시 쓰이지 않는다.
* ``~/.claude``, ``~/.codex``, ``~/.hermes`` 아래에는 어떤 것도 절대 쓰지 않는다;
  이들 중 어느 곳 안으로 해석되는 ``--out`` 대상은 거부된다.
* 형식이 잘못된 JSONL 줄/행은 건너뛰고 개수만 세며, 절대 내용을 추측하지 않는다.
* "진짜" 사람 프롬프트는 에이전트가 기록하는 수많은 합성 user 역할 레코드
  (도구 결과, 훅, 리마인더, 중복된 컨텍스트)와 분리된다. 이 규칙들은
  공급자별 휴리스틱이며, ``PROMPT_FILTER_RULES`` 에 나열되고 렌더링되는
  모든 출력에 표시된다.
* 모델의 추론(reasoning)과 도구 출력은 절대 렌더링되지 않고 개수만 센다.
* 마스킹(redaction)은 기본으로 켜져 있으며 최선 노력(best-effort) 패턴 매칭일
  뿐이다.

표준 라이브러리만 사용한다. Python 3.9+ (3.8에서도 동작).

실행 파일은 기존 호출과 스크립트가 계속 동작하도록 역사적인 이름
``claude_session_viewer.py`` 를 유지한다.
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
# 공급자
# ---------------------------------------------------------------------------

PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
PROVIDER_HERMES = "hermes"
PROVIDER_ALL = "all"

PROVIDER_IDS = (PROVIDER_CLAUDE, PROVIDER_CODEX, PROVIDER_HERMES)
PROVIDER_CHOICES = PROVIDER_IDS + (PROVIDER_ALL,)


class Provider(object):
    """지원되는 에이전트 하나에 대한 정적 설명."""

    def __init__(self, pid, label, product, default_root_parts, root_help, reason_key,
                 complete_reasons, reasoning_word, data_dir):
        self.id = pid
        self.label = label            # 배지/어시스턴트 라벨, 예: "Codex"
        self.product = product        # 산문에 쓰는 사람이 읽는 제품 이름
        self.default_root_parts = default_root_parts
        self.root_help = root_help
        self.reason_key = reason_key  # 정직한 상태 라벨에 표시되는 필드 이름
        self.complete_reasons = complete_reasons
        self.reasoning_word = reasoning_word
        self.data_dir = data_dir      # 보호되는 애플리케이션 데이터 디렉터리 이름


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
    """공급자 id에 대한 배지/어시스턴트 라벨."""
    return provider_of(pid).label


def assistant_label(pid):
    """모든 렌더러에서 에이전트의 답변에 붙이는 라벨."""
    return provider_of(pid).label


def default_root_for(pid):
    return os.path.join(os.path.expanduser("~"), *provider_of(pid).default_root_parts)

# ---------------------------------------------------------------------------
# 이벤트 종류
# ---------------------------------------------------------------------------

KIND_USER = "user"
KIND_ASSISTANT = "assistant"
KIND_COMPACTION = "compaction"
KIND_OTHER = "other"
KIND_TURN = "turn"

# ---------------------------------------------------------------------------
# 문서화된, 의도적으로 불완전한 휴리스틱
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

# 비밀값처럼 보이는 패턴들. 순서대로 적용되며, 의도적으로 보수적이다.
_SECRET_PATTERNS = [
    # PEM 개인 키 블록 -> 본문은 접어 감추고 envelope는 보이게 유지한다.
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
    """트랜스크립트를 전혀 읽을 수 없을 때 발생한다."""


class SelectorError(Exception):
    """세션 셀렉터가 0개 또는 여러 개의 세션과 일치할 때 발생한다."""


class OutputPathError(Exception):
    """--out 대상이 안전하지 않을 때 발생한다."""


# ---------------------------------------------------------------------------
# 마스킹(redaction)
# ---------------------------------------------------------------------------


def redact(text, home=None):
    """홈 경로와 흔한 비밀값 형태에 대한 최선 노력(best-effort) 마스킹."""
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
# 타임스탬프 헬퍼
# ---------------------------------------------------------------------------


def format_mtime(mtime):
    """POSIX mtime을 읽기 좋은 UTC 시각 문자열로 포맷한다."""
    try:
        dt = datetime.datetime.fromtimestamp(float(mtime), datetime.timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return "(unknown)"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_ts(raw):
    """트랜스크립트 타임스탬프를 UTC로 포맷한다. 파싱 불가능한 값은 그대로 통과시킨다."""
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
    """ISO-8601 시각 문자열의 POSIX 초를 반환하고, 파싱 불가능하면 None을 반환한다."""
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
    except (OverflowError, OSError, ValueError):  # pragma: no cover - 방어적 예외 처리
        return None


# ---------------------------------------------------------------------------
# 콘텐츠 정규화
# ---------------------------------------------------------------------------


def blocks_of(content):
    """Claude 콘텐츠(str | dict | list | 기타)를 블록 dict들로 정규화한다."""
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
    """이미 정규화된 블록들의 보이는 텍스트를 이어 붙인다."""
    parts = [b["text"] for b in blocks if _is_text_block(b) and b["text"].strip()]
    return "\n\n".join(parts)


_REMINDER_RE = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.S)


def strip_system_reminders(text):
    """(리마인더를 제거한 텍스트, 리마인더 존재 여부)를 반환한다."""
    if not isinstance(text, str) or "<system-reminder>" not in text:
        return (text or ""), False
    return _REMINDER_RE.sub("", text), True


def _message_of(rec):
    msg = rec.get("message")
    return msg if isinstance(msg, dict) else {}


def classify_user_record(rec):
    """레코드가 진짜 사람 프롬프트인지 판정한다.

    진짜 프롬프트면 ``(text, None)`` 을, 아니면 ``(None, reason)`` 을 반환하며,
    ``reason`` 은 :data:`USER_PROMPT_FILTER_RULES` 에 문서화된 키 중 하나다.
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
    """정규화된 트랜스크립트 레코드 하나로, 파일 내 순서를 따른다."""

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

    def __repr__(self):  # pragma: no cover - 디버깅 지원
        return "<Event %d %s %r>" % (self.index, self.kind, self.text[:40])


def _timestamp_of(rec):
    ts = rec.get("timestamp")
    return ts if isinstance(ts, str) and ts.strip() else None


def build_event(index, lineno, rec):
    """원시 레코드 하나를 :class:`Event` 로 변환한다. 이상한 입력에도 절대 예외를 던지지 않는다."""
    common = {
        "timestamp": _timestamp_of(rec),
        "is_sidechain": bool(rec.get("isSidechain")),
        "record_type": rec.get("type"),
    }

    # 요약이 이웃 레코드에 병합되는 일이 없도록 압축(compaction) 여부를 가장 먼저 검사한다.
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
# 스트리밍 리더
# ---------------------------------------------------------------------------


def iter_raw_records(path):
    """``(lineno, record_or_None, error_or_None)`` 을 지연 방식으로, 읽기 전용으로 산출한다."""
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
    """에이전트 세션 하나(트랜스크립트 파일 하나, 또는 데이터베이스의 행 집합 하나)와
    그 정규화된 이벤트들. 모든 공급자 어댑터가 공유한다."""

    def __init__(self, path, events, malformed_count, mtime, session_id=None, cwd=None,
                 provider=PROVIDER_CLAUDE, title=None, model=None, source=None,
                 started_at=None, ended_at=None, end_reason=None, match_keys=None):
        self.path = os.path.abspath(path)
        self.file_stem = os.path.splitext(os.path.basename(self.path))[0]
        self.events = events
        self.malformed_count = malformed_count
        self.mtime = mtime
        # 파일 이름이 세션 ID와 같다고 가정하지 않는다; 필드가 있으면 필드가 우선한다.
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
        # 셀렉터 키: 사용자가 이 세션을 지목하기 위해 입력할 수 있는 값들.
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
        """최선 노력의 최근성 키: 세션 자체의 시각을 쓰고, 없으면 파일 mtime을 쓴다."""
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
    """트랜스크립트 파일 하나를 :class:`Session` 으로 읽어 들인다(스트리밍, 읽기 전용)."""
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
    """진짜 사람 프롬프트로 판정된 이벤트들, 트랜스크립트 순서대로."""
    return session.real_prompt_events


# ---------------------------------------------------------------------------
# Codex CLI 어댑터 (~/.codex/sessions/**/*.jsonl)
#
# 레코드는 {timestamp, type, payload} 형태다. ``user_message`` 타입의
# ``event_msg`` payload만 사람 입력으로 취급한다; ``response_item`` 메시지는
# 같은 내용을 system/developer 컨텍스트와 함께 재생(replay)하므로 개수만 세고
# 표시하지 않는다. 추론(reasoning)과 도구 출력은 개수만 세고 절대 렌더링하지 않는다.
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

# Codex가 롤아웃 옆에 두는, 세션이 *아닌* 파일들.
CODEX_NON_SESSION_FILES = ("history.jsonl", "session_index.jsonl")


def codex_text(value):
    """Codex payload 필드(str | 블록 리스트 | dict)의 보이는 텍스트."""
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
    """진짜 사람 입력이면 (text, None), 아니면 (None, reason)을 반환한다."""
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
    """Codex 롤아웃 레코드 하나를 정규화한다. 이상한 입력에도 절대 예외를 던지지 않는다."""
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
            # 오류 텍스트 자체는 어시스턴트 답변으로 제시하지 않는다.
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
            # 어시스턴트 메시지는 agent_message 이벤트와 중복된다.
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
    """Codex 롤아웃 하나를 :class:`Session` 으로 읽어 들인다(스트리밍, 읽기 전용)."""
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
                # history.jsonl / session_index.jsonl 은 인덱스이지 세션이 아니다.
                continue
            paths.append(os.path.join(dirpath, name))
    return paths


# ---------------------------------------------------------------------------
# Hermes Agent 어댑터 (~/.hermes/state.db)
#
# 데이터베이스는 ``file:...?mode=ro`` URI를 통해 열리므로 연결 자체가 쓰기를
# 할 수 없다. active 행만 삽입(id) 순서대로 읽는다.
# 추론(reasoning) 컬럼과 tool 행의 내용은 절대 렌더링하지 않는다.
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
    """Hermes SQLite 데이터베이스를 엄격하게 읽기 전용으로 연다."""
    absolute = os.path.abspath(str(path))
    if not os.path.exists(absolute):
        raise TranscriptError("Hermes database not found: %s" % path)
    try:
        from urllib.request import pathname2url
    except ImportError:  # pragma: no cover - Python 2 전용
        raise TranscriptError("cannot build a read-only sqlite URI")
    uri = "file:%s?mode=ro" % pathname2url(absolute)
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise TranscriptError("cannot open %s read-only: %s" % (path, exc))


def _hermes_rows(conn, table):
    """``table`` 의 행들을 dict로 반환한다; 알 수 없는 컬럼 구성도 허용한다."""
    try:
        if table == "sessions":
            cursor = conn.execute("SELECT * FROM sessions")
        elif table == "messages":
            cursor = conn.execute("SELECT * FROM messages")
        else:
            raise TranscriptError("unsupported Hermes database table: %s" % table)
    except sqlite3.Error as exc:
        raise TranscriptError("cannot read %s from the Hermes database: %s" % (table, exc))
    columns = [d[0] for d in (cursor.description or [])]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def decode_hermes_content(value):
    """Hermes ``content`` 셀을 디코드한다. 형식이 잘못된 JSON은 원시 텍스트로 강등된다."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - 방어적 예외 처리
            return ""
    if not isinstance(value, str):
        return str(value)
    text = value.strip()
    if not text or text[0] not in "[{\"":
        return value
    try:
        parsed = json.loads(text)
    except ValueError:
        return value  # JSON처럼 보일 뿐인 일반 텍스트
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
    """어시스턴트 ``tool_calls`` 셀에 인코딩된 도구 호출들의 이름."""
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
    """Hermes ``messages`` 행 하나를 정규화한다."""
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
        # 개수를 세고 이름만 남긴다; 출력 자체는 여기서 의도적으로 버린다.
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
    """Hermes ``state.db`` 안의 모든 세션을 읽는다(읽기 전용)."""
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
            continue  # 롤백되었거나 대체된 행은 절대 로드하지 않는다
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
# 타임라인
# ---------------------------------------------------------------------------


class Turn(object):
    """사람 프롬프트 하나와 그 뒤에 이어진 보이는 어시스턴트 답변,
    또는 독립적인 압축(compaction) 경계."""

    def __init__(self, kind=KIND_TURN, prompt=None, timestamp=None, text=None,
                 provider=PROVIDER_CLAUDE):
        self.kind = kind
        self.provider = provider
        self.prompt = prompt
        self.timestamp = timestamp
        self.text = text  # 압축(compaction) 요약 텍스트
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
    """프롬프트와 답변을 짝지으며, 파일 순서와 압축(compaction) 구분을 보존한다."""
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
        # 숨겨진 활동은 기록된 위치가 어디든 집계한다: Claude는 assistant 레코드에,
        # Codex는 별도의 response_item 레코드에, Hermes는 assistant/tool 행에
        # 기록한다.
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
# 텍스트 헬퍼
# ---------------------------------------------------------------------------


def preview(text, width=60):
    """프롬프트의 길이가 제한된 한 줄 미리보기."""
    if not text:
        return "(no prompt found)"
    flat = " ".join(str(text).split())
    if len(flat) <= width:
        return flat
    if width <= 1:
        return flat[:width]
    return flat[: width - 1].rstrip() + "…"


def choose_fence(text):
    """``text`` 안에 이미 있는 어떤 펜스보다 긴 Markdown 펜스를 고른다."""
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
    """산문을 소프트 랩하되 펜스/들여쓰기/표 줄은 원문 그대로 둔다."""
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
    except Exception:  # pragma: no cover - 방어적 예외 처리
        cols = default
    return max(60, min(100, cols))


# ---------------------------------------------------------------------------
# Markdown 렌더링
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
    """세션을 Markdown으로 렌더링한다."""
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
# HTML 렌더링
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
    """세션을 자기 완결적인 오프라인 HTML 문서로 렌더링한다."""
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
    # CSP는 HTML 특수 문자가 없는 고정 상수이므로 그대로 출력한다
    # (이스케이프하면 소스 보기에서 정책을 알아보기 어렵게 만들 뿐이다).
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
# 검색 및 선택
# ---------------------------------------------------------------------------


def default_root():
    """호환성을 위해 유지하는 기존 helper로, Claude 기본 root를 반환한다."""
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
    """``root`` 아래의 모든 transcript를 재귀적으로 최신 파일부터 로드한다."""
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
    """``root``에 있는 ``provider``의 모든 session을 최신순으로 로드한다."""
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
    """정렬된 ``{provider: root}`` mapping의 session을 로드한다.

    ``(sessions, notes)``를 반환한다. ``notes``에는 scan할 수 없었던 root를
    기록하여 호출자가 전체 실행을 실패시키지 않고 이를 보고할 수 있게 한다.
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
    """``[provider:]partial-id``를 정확히 하나의 session으로 해석한다."""
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

    # 각 provider에서 symlink를 따라가며 호출자가 제공한 home(테스트나 특수 설정)과
    # 실제 home을 모두 보호한다.
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
    """``out``을 해석하고 provider 저장소 및 로드된 source inode를 거부한다."""
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
    """대상 inode를 truncate하지 않고 안전한 0600 export를 쓴다."""
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
        else:  # pragma: no cover - 발생 가능성이 희박한 임시 이름 충돌 반복
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
        if initial is None:
            try:
                os.link(
                    temp_name,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise OutputPathError(
                    "output path changed during export: %s" % out_path
                ) from exc
            published = _stat_identity(name, dir_fd=directory_fd)
            staged = _stat_identity(temp_name, dir_fd=directory_fd)
            if published != staged:
                raise OutputPathError("published export identity changed: %s" % out_path)
            os.unlink(temp_name, dir_fd=directory_fd)
            temp_name = None
        else:
            retired = ".ai-session-viewer-retired-%s" % secrets.token_hex(16)
            os.rename(
                name,
                retired,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            moved = _stat_identity(retired, dir_fd=directory_fd)
            if moved != initial:
                try:
                    os.rename(
                        retired,
                        name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                except OSError as restore_error:
                    raise OutputPathError(
                        "foreign output successor retained after race: %s" % out_path
                    ) from restore_error
                raise OutputPathError("output path changed during export: %s" % out_path)
            try:
                os.link(
                    temp_name,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                published = _stat_identity(name, dir_fd=directory_fd)
                staged = _stat_identity(temp_name, dir_fd=directory_fd)
                if published != staged:
                    raise OutputPathError(
                        "published export identity changed: %s" % out_path
                    )
            except BaseException:
                if _stat_identity(name, dir_fd=directory_fd) is None:
                    os.rename(
                        retired,
                        name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                raise
            os.unlink(temp_name, dir_fd=directory_fd)
            temp_name = None
            if _stat_identity(retired, dir_fd=directory_fd) != initial:
                raise OutputPathError(
                    "retired output identity changed; foreign inode retained: %s" % out_path
                )
            os.unlink(retired, dir_fd=directory_fd)
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
# 터미널 출력
# ---------------------------------------------------------------------------


class Style(object):
    def __init__(self, stream):
        self.enabled = False
        try:
            self.enabled = bool(stream.isatty()) and os.environ.get("NO_COLOR") is None
        except Exception:  # pragma: no cover - 방어적 예외 처리
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
    """지원할 수 없는 방식으로 --provider와 --root를 조합했을 때 발생한다."""


def resolve_provider_roots(args):
    """이번 호출에 사용할 정렬된 ``{provider: root}`` mapping을 반환한다.

    하위 호환성을 위해 ``--provider`` 없이 명시한 ``--root``는 provider 도입
    이전과 정확히 동일하게 Claude를 뜻한다.
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
    """``(sessions, notes, roots, exit_code)``를 반환하며, 오류 시 sessions는 None이다."""
    try:
        roots = resolve_provider_roots(args)
    except ProviderSelectionError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return None, [], {}, 2
    sessions, notes = find_sessions_multi(roots)
    if not sessions:
        # 명시적으로 선택한 단일 provider의 root를 전혀 읽을 수 없으면 치명적
        # 오류이며, 여러 provider를 집계할 때 읽을 수 없는 root는 알림에 그친다.
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
            except ValueError:  # pragma: no cover - Windows의 서로 다른 드라이브
                pass
        elif root:
            # 파일을 root로 삼는 provider(Hermes)는 "."이 아니라 database 이름을 표시한다.
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
    """(session, exit_code)를 반환하며, code가 설정된 경우 ``session``은 None이다."""
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
# 명령줄 인터페이스
# ---------------------------------------------------------------------------


class _RootAction(argparse.Action):
    """기존 의미를 유지하도록 ``--root``가 제공되었음을 기록한다.
    --provider 없이 명시한 --root는 Claude를 뜻한다."""

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
    """읽기 전용 session viewer 명령 tree를 구성한다."""
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
    """session-viewer 명령을 전달하고 프로세스 종료 상태를 반환한다."""
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
    except BrokenPipeError:  # pragma: no cover - head 등으로 파이프 연결할 때
        return 0
    except KeyboardInterrupt:  # pragma: no cover
        sys.stderr.write("interrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
