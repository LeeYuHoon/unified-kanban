"""Provider-neutral tool-usage accounting shared by every agent source.

Only sanitized names, counts, and the model identifier are ever persisted or
posted. Tool arguments, tool responses, commands, file paths, prompt bodies,
and assistant transcripts never enter this module's output.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

USAGE_CATEGORIES: tuple[str, ...] = ("skills", "subagents", "mcp")

_MCP_TOOL_RE = re.compile(r"mcp__(.+?)__(.*)\Z", re.DOTALL)
_MODEL_RE = re.compile(r"[A-Za-z0-9._:/-]{1,64}\Z")

_NAME_LIMIT = 64
# Whitelist grammar for every recorded name: letters, digits, and the
# separators real Skill/subagent/MCP identifiers use, including the ``:`` of a
# plugin-qualified ``plugin:skill``. Anchored to alphanumerics at both ends, so
# a path, a traversal segment, a flag, or a control character cannot match.
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,62}[A-Za-z0-9])?\Z")
_RUN_RE = re.compile(r"[A-Za-z0-9]+")
_MCP_SEPARATOR = "/"
_UNKNOWN = "unknown"
_COUNT_LIMIT = 1_000_000
_TOKEN_COUNT_LIMIT = 1_000_000_000_000_000
_TOKEN_FIELDS = (
    "input", "output", "cache_read", "cache_write", "reasoning", "requests",
)

_NAMES_PER_CATEGORY = 25
_COMMENT_LIMIT = 4000
_SUMMARY_LIMIT = 1_000
_SCHEMA_VERSION = 1
_TOKEN_SCHEMA_VERSION = 2

_RESULT_SECTION_ALIASES: dict[str, str] = {
    "완료": "완료",
    "결과": "완료",
    "done": "완료",
    "completed": "완료",
    "result": "완료",
    "변경": "변경",
    "변경 사항": "변경",
    "구현": "변경",
    "changes": "변경",
    "implemented": "변경",
    "검증": "검증",
    "테스트": "검증",
    "verification": "검증",
    "tests": "검증",
    "미완료": "미완료",
    "남은 작업": "미완료",
    "주의": "미완료",
    "not done": "미완료",
    "remaining": "미완료",
    "blockers": "미완료",
}
_RESULT_SECTION_ORDER = ("완료", "변경", "검증", "미완료")
_RESULT_SECTION_PRIORITY = ("미완료", "검증", "완료", "변경")
_MARKDOWN_PREFIX_RE = re.compile(r"^(?:#{1,6}\s*|(?:[-*+•]|\d+[.)])\s+)")
_RESULT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "미완료",
        (
            "미완료", "남은", "하지 않", "못했", "실패", "보류", "차단",
            "not done", "remaining", "failed", "blocked", "pending", "unable",
        ),
    ),
    (
        "검증",
        (
            "pytest", "테스트", "검증", "통과", "빌드 성공", "test", "tests",
            "testing", "verified", "passed", "build succeeded", "lint",
        ),
    ),
    (
        "변경",
        (
            "구현", "추가", "수정", "변경", "제거", "적용", "implement", "implemented",
            "added", "adds", "fixed", "fixes", "changed", "changes", "removed",
            "removes", "updated", "updates", "refactor", "refactored",
        ),
    ),
    (
        "완료",
        ("완료", "마쳤", "해결", "성공", "done", "completed", "resolved", "succeeded"),
    ),
)
_PROSE_CHANGE_KEYWORDS = (
    "구현했", "구현함", "구현 완료", "추가했", "추가함", "수정했", "수정함",
    "변경했", "변경함", "제거했", "제거함", "적용했", "적용함", "implemented",
    "added", "fixed", "changed", "removed", "updated", "refactored",
)

# The Claude-only comment shipped first under this exact header and consumers
# match it byte for byte, so Claude Code keeps it unchanged. Only the sources
# added afterwards name themselves; unknown sources fall back to the original.
USAGE_COMMENT_HEADER = "Agent tool usage"
_SOURCE_HEADERS: dict[str, str] = {
    "codex": "Codex tool usage",
    "hermes-agent": "Hermes Agent tool usage",
}

# The one grammar for an idempotency marker, shared with the backend that
# searches a card for it: 8-128 characters, opening alphanumeric, and no
# character that would need escaping in a comment or a regex.
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")

# Per-runtime tool that loads a Skill, and the argument holding its name.
_SKILL_TOOLS: dict[str, tuple[str, str]] = {
    "claude-code": ("Skill", "skill"),
    "hermes-agent": ("skill_view", "name"),
}
# Per-runtime tool that delegates to a subagent inline. Hermes and Codex both
# emit a dedicated subagent lifecycle hook instead, which is authoritative:
# one Hermes ``delegate_task`` call can fan out to several children, so
# counting the tool call would undercount them.
_SUBAGENT_TOOLS: dict[str, tuple[str, str]] = {
    "claude-code": ("Task", "subagent_type"),
}
# Categories a runtime structurally cannot report, so the card can say so
# instead of implying "nothing was used".
_UNAVAILABLE: dict[str, tuple[str, ...]] = {
    # No Codex 0.145 hook event carries a skill name: PostToolUse never
    # reports a tool whose arguments identify the skill in use, so a skill
    # count would have to be invented. Report the gap instead.
    "codex": ("skills",),
}


_OPAQUE_RUN_LENGTH = 32
_SUSPECT_RUN_LENGTH = 16


def _case_transitions(run: str) -> tuple[int, int]:
    """``(cased characters, case changes)`` across one alphanumeric run.

    Digits are skipped rather than treated as a boundary, so ``Ab1Cd`` counts
    the same alternation ``AbCd`` does.
    """
    cased = [character for character in run if character.isalpha()]
    changes = sum(
        first.isupper() != second.isupper()
        for first, second in zip(cased, cased[1:])
    )
    return len(cased), changes


def _looks_opaque(value: str) -> bool:
    """True for high-entropy blobs -- API keys, hex digests, UUIDs.

    Real Skill, subagent and MCP names are short, separator-rich words. A long
    unbroken alphanumeric run, or a digit-dense one, is a secret or an
    identifier that carries no reporting value, so it is refused rather than
    written to a card.

    Between 16 and 31 characters a run is too short for the blob rule but still
    long enough to be a credential, so two further shapes are refused there.
    Both are chosen to leave word-shaped names alone: ``openaiDeveloperDocs``
    and ``searchIssuesInRepositoryByLabel`` change case a handful of times
    across many characters and keep their lower-case word bodies, while
    ``AbCdEfGhIjKlMnOpQrStUvWxYz123`` alternates on nearly every character and
    an AWS-style ``AKIAABCDEFGHIJKLMNOP`` has no lower-case body at all.
    """
    for run in _RUN_RE.findall(value):
        digits = sum(character.isdigit() for character in run)
        if len(run) >= _OPAQUE_RUN_LENGTH:
            return True
        if len(run) < _SUSPECT_RUN_LENGTH:
            continue
        if digits >= 4:
            return True
        cased, changes = _case_transitions(run)
        # A long run with no lower-case body at all: an opaque token, not a
        # name -- real identifiers of this length are not written in caps.
        if cased and not any(character.islower() for character in run):
            return True
        # Case flips on at least every other letter: alternation this dense is
        # generated, never written.
        if changes * 2 >= cased:
            return True
    return sum(character.isdigit() for character in value) >= 16


def sanitize_event_id(value: Any) -> str | None:
    """One idempotency marker, or None when the value is not a legal one.

    An out-of-grammar or oversized id cannot serve as a marker -- the backend
    would refuse to search a card for it -- and echoing it into a comment would
    put unvalidated text on a card, so it is dropped rather than repaired.
    """
    if not isinstance(value, str):
        return None
    return value if _EVENT_ID_RE.fullmatch(value) else None


def sanitize_identifier(value: Any) -> str | None:
    """One whitelisted identifier, or None when the value is not one.

    Rejecting is deliberate: an absolute path, a ``..`` traversal, a control
    character or a secret-looking blob is dropped outright instead of being
    stored under a placeholder, so nothing untrusted reaches state or a card.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > _NAME_LIMIT:
        return None
    if ".." in candidate or not _IDENTIFIER_RE.fullmatch(candidate):
        return None
    if _looks_opaque(candidate):
        return None
    return candidate


def _identifier_or_unknown(value: Any) -> str | None:
    """``unknown`` when the runtime reported no name at all, else the
    whitelisted identifier, else None when the reported name is illegal."""
    if value is None or not isinstance(value, str) or not value.strip():
        return _UNKNOWN
    return sanitize_identifier(value)


def sanitize_usage_name(category: str, name: Any) -> str | None:
    """The stored form of a name in ``category``, or None when it is illegal.

    ``mcp`` names are the one composite: exactly one structural ``/`` joining
    two identifiers. Every other category takes a single identifier, so a slash
    -- and with it any path -- is impossible there.
    """
    if category == "mcp":
        if not isinstance(name, str):
            return None
        server, separator, tool = name.strip().partition(_MCP_SEPARATOR)
        if separator != _MCP_SEPARATOR:
            return None
        clean_server = sanitize_identifier(server)
        clean_tool = sanitize_identifier(tool)
        if clean_server is None or clean_tool is None:
            return None
        return f"{clean_server}{_MCP_SEPARATOR}{clean_tool}"
    return sanitize_identifier(name)


def classify_subagent(role: Any) -> str | None:
    """Whitelisted subagent/agent name, or None when the event's name is not
    a legal identifier and must not be recorded."""
    return _identifier_or_unknown(role)


def classify_tool(
    runtime: str, tool_name: Any, tool_input: Any
) -> tuple[str, str] | None:
    """Map one tool call to ``(category, name)``, or None when uncounted."""
    if not isinstance(tool_name, str) or not tool_name:
        return None

    skill_tool = _SKILL_TOOLS.get(runtime)
    if skill_tool is not None and tool_name == skill_tool[0]:
        argument = (
            tool_input.get(skill_tool[1])
            if isinstance(tool_input, Mapping)
            else None
        )
        name = _identifier_or_unknown(argument)
        return None if name is None else ("skills", name)

    subagent_tool = _SUBAGENT_TOOLS.get(runtime)
    if subagent_tool is not None and tool_name == subagent_tool[0]:
        argument = (
            tool_input.get(subagent_tool[1])
            if isinstance(tool_input, Mapping)
            else None
        )
        name = _identifier_or_unknown(argument)
        return None if name is None else ("subagents", name)

    match = _MCP_TOOL_RE.fullmatch(tool_name)
    if match:
        server = _identifier_or_unknown(match.group(1))
        tool = _identifier_or_unknown(match.group(2))
        if server is None or tool is None:
            return None
        return "mcp", f"{server}{_MCP_SEPARATOR}{tool}"
    return None


def bump(usage: dict[str, Any], category: str, name: str) -> None:
    bucket = usage.setdefault(category, {})
    bucket[name] = bucket.get(name, 0) + 1


def clean_usage(usage: Any) -> dict[str, dict[str, int]]:
    """Drop anything a hostile or stale state file could smuggle in."""
    if not isinstance(usage, Mapping):
        return {}
    cleaned: dict[str, dict[str, int]] = {}
    for category, bucket in usage.items():
        if category not in USAGE_CATEGORIES or not isinstance(bucket, Mapping):
            continue
        counts: dict[str, int] = {}
        for name, count in bucket.items():
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count <= 0
            ):
                continue
            valid = sanitize_usage_name(category, name)
            if valid is None:
                continue
            # Two stale keys can normalize onto the same name; add rather than
            # overwrite so no call is lost, and cap so a forged count cannot
            # blow up the comment.
            counts[valid] = min(counts.get(valid, 0) + count, _COUNT_LIMIT)
        if counts:
            cleaned[category] = counts
    return cleaned


def clean_tokens(tokens: Any) -> dict[str, int | None]:
    """Return bounded canonical token counters with a computed total.

    ``reasoning`` is informational and is already included in provider output
    counts, so it is never added to ``total``. ``None`` is preserved to express
    that a runtime cannot report a bucket; missing and hostile fields are
    dropped rather than guessed.
    """
    if not isinstance(tokens, Mapping):
        return {}
    cleaned: dict[str, int | None] = {}
    for field in _TOKEN_FIELDS:
        value = tokens.get(field)
        if value is None:
            if field in tokens:
                cleaned[field] = None
            continue
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > _TOKEN_COUNT_LIMIT
        ):
            continue
        cleaned[field] = value
    raw_total = tokens.get("total")
    if (
        isinstance(raw_total, int)
        and not isinstance(raw_total, bool)
        and 0 <= raw_total <= _TOKEN_COUNT_LIMIT
    ):
        cleaned["total"] = raw_total
    additive = ("input", "output", "cache_read", "cache_write")
    if "total" not in cleaned and any(
        isinstance(cleaned.get(field), int) for field in additive
    ):
        total = 0
        for field in additive:
            value = cleaned.get(field)
            if isinstance(value, int):
                total += value
        cleaned["total"] = total
    return cleaned


def sanitize_model(model: Any) -> str | None:
    if not isinstance(model, str):
        return None
    stripped = model.strip()
    if not stripped or not _MODEL_RE.fullmatch(stripped):
        return None
    return stripped


def unavailable_categories(source: str) -> tuple[str, ...]:
    return _UNAVAILABLE.get(source, ())


def _section_start(line: str) -> tuple[str, str] | None:
    is_heading = re.match(r"^#{1,6}\s+", line) is not None
    candidate = re.sub(r"^#{1,6}\s*", "", line).strip()
    match = re.fullmatch(r"([^:：]{1,24})[:：]\s*(.*)", candidate)
    if match:
        label = _RESULT_SECTION_ALIASES.get(match.group(1).strip().casefold())
        if label is not None:
            return label, match.group(2).strip()
    if not is_heading:
        return None
    label = _RESULT_SECTION_ALIASES.get(candidate.rstrip(":：").strip().casefold())
    return None if label is None else (label, "")


def _render_result_sections(sections: Mapping[str, list[str]], limit: int) -> str:
    active = [name for name in _RESULT_SECTION_ORDER if sections.get(name)]
    if not active:
        return ""
    content = {name: "; ".join(sections[name]) for name in active}
    overhead = sum(len(f"{name}: ") for name in active) + len(active) - 1
    if overhead >= limit:
        raw = "\n".join(f"{name}: {content[name]}" for name in active)
        return _bound_summary(raw, limit)

    allocations = {name: 0 for name in active}
    pending = set(active)
    remaining = limit - overhead
    while pending:
        share = remaining // len(pending)
        short = [name for name in pending if len(content[name]) <= share]
        if short:
            for name in short:
                allocations[name] = len(content[name])
                remaining -= allocations[name]
                pending.remove(name)
            continue
        for name in pending:
            allocations[name] = share
        remaining -= share * len(pending)
        for name in _RESULT_SECTION_PRIORITY:
            if remaining == 0:
                break
            if name in pending:
                allocations[name] += 1
                remaining -= 1
        break

    return "\n".join(
        f"{name}: {_bound_summary(content[name], allocations[name])}"
        for name in active
    )


def _structured_result_summary(lines: list[str], limit: int) -> str:
    sections: dict[str, list[str]] = {name: [] for name in _RESULT_SECTION_ORDER}
    current: str | None = None
    list_only = False
    found_heading = False
    for line in lines:
        start = _section_start(line)
        if start is not None:
            current, inline = start
            found_heading = True
            list_only = False
            if inline:
                sections[current].append(inline)
                if re.match(r"^#{1,6}\s+", line) is None:
                    list_only = True
            continue
        if re.match(r"^#{1,6}\s+", line):
            current = None
            list_only = False
            continue
        if current is None:
            continue
        if list_only and _MARKDOWN_PREFIX_RE.match(line) is None:
            current = None
            list_only = False
            continue
        item = _MARKDOWN_PREFIX_RE.sub("", line).strip()
        if item:
            sections[current].append(item)
    if not found_heading:
        return ""
    return _render_result_sections(sections, limit)


def _contains_keyword(text: str, keyword: str) -> bool:
    if keyword.isascii():
        return re.search(
            rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text
        ) is not None
    return keyword in text


def _describes_resolved_failure(text: str) -> bool:
    if re.search(
        r"실패(?:했던|하던|한|를|가|은|는)?[^\n]*(?:수정|해결|고쳤)",
        text,
    ):
        return True
    return re.search(
        r"(?:\b(?:fixed|resolved)\b.*\b(?:failed|failure)\b"
        r"|\b(?:failed|failure)\b.*\b(?:fixed|resolved)\b)",
        text,
    ) is not None


def _describes_unresolved_failure(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "하지 않",
            "못했",
            "계속 재현",
            "하려 했으나",
            "수정 중",
            "could not",
            "not fixed",
            "not resolved",
            "still fail",
            "continues to fail",
            "unable",
            "unresolved",
        )
    )


def _fallback_result_summary(lines: list[str], limit: int) -> str:
    if len(lines) < 2:
        return ""
    sections: dict[str, list[str]] = {name: [] for name in _RESULT_SECTION_ORDER}
    for line in lines:
        is_list_item = _MARKDOWN_PREFIX_RE.match(line) is not None
        item = _MARKDOWN_PREFIX_RE.sub("", line).strip()
        if item.startswith("--"):
            continue
        lowered = item.casefold()
        if not is_list_item and lowered in _RESULT_SECTION_ALIASES:
            continue
        if _describes_unresolved_failure(lowered):
            sections["미완료"].append(item)
            continue
        if _describes_resolved_failure(lowered):
            sections["변경"].append(item)
            continue
        for category, keywords in _RESULT_KEYWORDS:
            candidates = (
                _PROSE_CHANGE_KEYWORDS
                if category == "변경" and not is_list_item
                else keywords
            )
            if any(_contains_keyword(lowered, keyword) for keyword in candidates):
                if item not in sections[category]:
                    sections[category].append(item)
                break
    return _render_result_sections(sections, limit)


def _bound_summary(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _without_fenced_code(lines: Iterable[str]) -> list[str]:
    visible: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in lines:
        stripped = line.lstrip()
        if fence_char is None:
            opening = re.match(r"^(`{3,}|~{3,})", stripped)
            if opening is not None:
                marker = opening.group(1)
                fence_char = marker[0]
                fence_length = len(marker)
                continue
        elif re.fullmatch(
            rf"{re.escape(fence_char)}{{{fence_length},}}\s*",
            stripped,
        ):
            fence_char = None
            fence_length = 0
            continue
        if fence_char is None:
            visible.append(line)
    return visible


def concise_summary(text: Any, limit: int = _SUMMARY_LIMIT) -> str:
    if not isinstance(text, str):
        return ""
    lines = (" ".join(line.split()) for line in text.splitlines())
    normalized = _without_fenced_code(line for line in lines if line)
    structured = _structured_result_summary(normalized, limit)
    fallback = _fallback_result_summary(normalized, limit) if not structured else ""
    return _bound_summary(structured or fallback or "\n".join(normalized), limit)


def has_reportable_usage(source: str, usage: Any) -> bool:
    """Every tracked card gets exactly one usage comment.

    An all-empty report is meaningful: it tells the reader the turn genuinely
    used no Skill, subagent, or MCP tool, and it still carries the source and
    model. Suppressing it would make "nothing used" indistinguishable from
    "never recorded".
    """
    return True


def usage_comment_header(source: Any) -> str:
    """The first line of a usage comment, keyed by source.

    Claude Code's header is the original one and is reproduced byte for byte so
    consumers written against it keep matching; other sources get their own.
    """
    return _SOURCE_HEADERS.get(source, USAGE_COMMENT_HEADER)


def usage_event_id(source: Any, task_id: Any) -> str:
    """Deterministic marker for one card's single usage comment.

    Derived only from the card's own identity, so every retry of the same card
    -- after a crash, a failed marker write, or a re-run hook -- recomputes the
    same value and can recognise a comment it already posted.
    """
    digest = hashlib.sha256(f"{source}\0{task_id}".encode("utf-8")).hexdigest()
    return f"usage-{digest[:32]}"


def _render(source: Any, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return f"{usage_comment_header(source)}\n{body}"


def usage_comment(
    *,
    source: str,
    model: Any,
    usage: Any,
    tokens: Any = None,
    unavailable: Iterable[str] = (),
    event_id: str | None = None,
) -> str:
    """One bounded, structured comment body naming what the turn used.

    The result is always parseable JSON. Size is enforced by dropping
    lowest-count entries and re-serializing, never by slicing the serialized
    text, so a long name can never truncate the comment mid-token. When a legal
    ``event_id`` is given it is carried in the payload as the marker that makes
    posting idempotent; one outside the marker grammar is dropped, so an
    oversized id can neither leak nor push the comment past its limit.
    """
    cleaned = clean_usage(usage)
    clean_source = sanitize_identifier(source) or _UNKNOWN
    cleaned_tokens = clean_tokens(tokens)
    header: dict[str, Any] = {
        "schema_version": _TOKEN_SCHEMA_VERSION if cleaned_tokens else _SCHEMA_VERSION,
        "source": clean_source,
    }
    clean_event_id = sanitize_event_id(event_id)
    if clean_event_id:
        header["event_id"] = clean_event_id
    resolved_model = sanitize_model(model)
    if resolved_model:
        header["model"] = resolved_model
    if cleaned_tokens:
        header["tokens"] = cleaned_tokens
    missing = sorted({c for c in unavailable if c in USAGE_CATEGORIES})
    if missing:
        header["unavailable"] = missing

    # Highest count first, then name, so shrinking always sheds the least
    # informative entries.
    ranked = {
        category: sorted(
            cleaned.get(category, {}).items(), key=lambda item: (-item[1], item[0])
        )
        for category in USAGE_CATEGORIES
    }
    kept = {
        category: entries[:_NAMES_PER_CATEGORY]
        for category, entries in ranked.items()
    }
    truncated = any(
        len(entries) > _NAMES_PER_CATEGORY for entries in ranked.values()
    )

    while True:
        payload = dict(header)
        payload.update(
            {category: dict(entries) for category, entries in kept.items()}
        )
        if truncated:
            payload["truncated"] = True
        comment = _render(clean_source, payload)
        if len(comment) <= _COMMENT_LIMIT:
            return comment
        # Too long: shed the lowest-count entry of the currently largest
        # category and try again. Terminates because the header alone fits.
        widest = max(kept, key=lambda category: (len(kept[category]), category))
        if not kept[widest]:
            # Nothing left to drop; the header itself is already bounded by
            # the name and model limits, so this is the smallest valid form.
            return comment
        kept[widest].pop()
        truncated = True
