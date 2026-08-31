"""모든 에이전트 소스가 공유하는, 제공자 중립적인 도구 사용량 집계.

정제(sanitize)된 이름, 횟수, 모델 식별자만이 영구 저장되거나 게시된다.
도구 인자, 도구 응답, 명령, 파일 경로, 프롬프트 본문, 어시스턴트
트랜스크립트는 절대 이 모듈의 출력에 포함되지 않는다.
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
# 기록되는 모든 이름에 대한 화이트리스트 문법: 문자, 숫자, 그리고 실제
# Skill/subagent/MCP 식별자가 사용하는 구분자들로, 플러그인 한정
# ``plugin:skill``의 ``:``도 포함한다. 양 끝을 영숫자로 고정(anchor)하므로
# 경로, 상위 디렉터리 탐색 조각, 플래그, 제어 문자는 매칭될 수 없다.
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
_SUMMARY_INPUT_LIMIT = 64 * 1024
_SUMMARY_LINE_LIMIT = 4 * 1024
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

# Claude 전용 코멘트가 정확히 이 헤더로 처음 출시되었고 소비자들이 이를
# 바이트 단위로 그대로 매칭하므로, Claude Code는 헤더를 변경하지 않는다.
# 이후에 추가된 소스만 자신의 이름을 붙이며, 알 수 없는 소스는 원래
# 헤더로 되돌아간다.
USAGE_COMMENT_HEADER = "Agent tool usage"
_SOURCE_HEADERS: dict[str, str] = {
    "codex": "Codex tool usage",
    "hermes-agent": "Hermes Agent tool usage",
}

# 멱등성 마커에 대한 단일 문법으로, 카드에서 이를 검색하는 백엔드와
# 공유된다: 8-128자, 첫 글자는 영숫자, 그리고 코멘트나 정규식 안에서
# 이스케이프가 필요한 문자는 포함하지 않는다.
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")

# 런타임별로 Skill을 로드하는 도구와, 그 이름을 담는 인자.
_SKILL_TOOLS: dict[str, tuple[str, str]] = {
    "claude-code": ("Skill", "skill"),
    "hermes-agent": ("skill_view", "name"),
}
# 런타임별로 인라인으로 subagent에 위임하는 도구. Hermes와 Codex는 둘 다
# 대신 전용 subagent 생명주기 훅을 발행하며, 그쪽이 권위 있는(authoritative)
# 신호다: Hermes의 ``delegate_task`` 호출 한 번이 여러 자식으로 확산(fan out)
# 될 수 있으므로, 도구 호출을 세면 실제보다 적게 집계된다.
_SUBAGENT_TOOLS: dict[str, tuple[str, str]] = {
    "claude-code": ("Task", "subagent_type"),
}
# 런타임이 구조적으로 보고할 수 없는 카테고리. 카드가 "아무것도 사용되지
# 않았다"고 암시하는 대신 그 사실을 명시할 수 있게 한다.
_UNAVAILABLE: dict[str, tuple[str, ...]] = {
    # Codex 0.145의 어떤 훅 이벤트도 스킬 이름을 담지 않는다: PostToolUse는
    # 사용 중인 스킬을 인자로 식별할 수 있는 도구를 결코 보고하지 않으므로,
    # 스킬 횟수는 지어내야만 얻을 수 있다. 대신 그 공백을 보고한다.
    "codex": ("skills",),
}


_OPAQUE_RUN_LENGTH = 32
_SUSPECT_RUN_LENGTH = 16


def _case_transitions(run: str) -> tuple[int, int]:
    """하나의 영숫자 연속 구간(run)에 대한 ``(대소문자 있는 글자 수, 대소문자 전환 수)``.

    숫자는 경계로 취급하지 않고 건너뛰므로, ``Ab1Cd``는 ``AbCd``와 같은
    교대(alternation) 횟수로 센다.
    """
    cased = [character for character in run if character.isalpha()]
    changes = sum(
        first.isupper() != second.isupper()
        for first, second in zip(cased, cased[1:])
    )
    return len(cased), changes


def _looks_opaque(value: str) -> bool:
    """높은 엔트로피의 덩어리(blob) -- API 키, 16진수 다이제스트, UUID -- 이면 True.

    실제 Skill, subagent, MCP 이름은 짧고 구분자가 풍부한 단어들이다. 길게
    끊기지 않은 영숫자 연속 구간이나 숫자가 빽빽한 구간은 비밀값이거나
    보고 가치가 없는 식별자이므로, 카드에 기록하는 대신 거부한다.

    16자 이상 31자 이하의 구간은 blob 규칙에 걸리기엔 너무 짧지만 자격
    증명(credential)이 되기엔 여전히 충분히 길므로, 그 구간에서는 두 가지
    형태를 추가로 거부한다. 둘 다 단어 모양의 이름은 건드리지 않도록
    선택되었다: ``openaiDeveloperDocs``와 ``searchIssuesInRepositoryByLabel``은
    많은 글자에 걸쳐 대소문자가 몇 번만 바뀌고 소문자 단어 몸통을 유지하는
    반면, ``AbCdEfGhIjKlMnOpQrStUvWxYz123``은 거의 모든 글자마다 교대하고,
    전부 대문자인 자격 증명 모양의 토큰은 소문자 몸통이 아예 없다.
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
        # 소문자 몸통이 전혀 없는 긴 연속 구간: 이름이 아니라 불투명한
        # 토큰이다 -- 이 길이의 실제 식별자는 전부 대문자로 쓰이지 않는다.
        if cased and not any(character.islower() for character in run):
            return True
        # 최소 두 글자마다 대소문자가 뒤집힘: 이렇게 빽빽한 교대는 생성된
        # 것이지, 사람이 쓴 것이 아니다.
        if changes * 2 >= cased:
            return True
    return sum(character.isdigit() for character in value) >= 16


def sanitize_event_id(value: Any) -> str | None:
    """멱등성 마커 하나를 반환하고, 값이 적법하지 않으면 None을 반환한다.

    문법을 벗어나거나 과도하게 긴 id는 마커 역할을 할 수 없고 -- 백엔드가
    그것으로 카드를 검색하기를 거부할 것이다 -- 그것을 코멘트에 그대로
    되돌려 넣으면 검증되지 않은 텍스트가 카드에 실리게 되므로, 고치지
    않고 버린다.
    """
    if not isinstance(value, str):
        return None
    return value if _EVENT_ID_RE.fullmatch(value) else None


def sanitize_identifier(value: Any) -> str | None:
    """화이트리스트에 부합하는 식별자 하나를 반환하고, 아니면 None을 반환한다.

    거부는 의도적이다: 절대 경로, ``..`` 상위 디렉터리 탐색, 제어 문자,
    비밀값처럼 보이는 덩어리는 자리표시자(placeholder) 아래 저장되는 대신
    그 자리에서 버려지므로, 신뢰할 수 없는 어떤 것도 상태나 카드에
    도달하지 않는다.
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
    """런타임이 이름을 전혀 보고하지 않았으면 ``unknown``, 그렇지 않으면
    화이트리스트를 통과한 식별자, 보고된 이름이 불법이면 None."""
    if value is None or not isinstance(value, str) or not value.strip():
        return _UNKNOWN
    return sanitize_identifier(value)


def sanitize_usage_name(category: str, name: Any) -> str | None:
    """``category``에 속한 이름의 저장 형태를 반환하고, 불법이면 None을 반환한다.

    ``mcp`` 이름만이 유일한 복합형이다: 두 식별자를 잇는 구조적 ``/`` 정확히
    하나. 다른 모든 카테고리는 단일 식별자를 받으므로, 슬래시는 -- 따라서
    어떤 경로도 -- 거기서는 불가능하다.
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
    """화이트리스트를 통과한 subagent/agent 이름을 반환하고, 이벤트의 이름이
    적법한 식별자가 아니어서 기록하면 안 될 때는 None을 반환한다."""
    return _identifier_or_unknown(role)


def classify_tool(
    runtime: str, tool_name: Any, tool_input: Any
) -> tuple[str, str] | None:
    """도구 호출 하나를 ``(category, name)``으로 매핑하고, 집계 대상이 아니면 None을 반환한다."""
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
    """적대적이거나 오래된 상태 파일이 몰래 끼워 넣을 수 있는 것은 전부 버린다."""
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
            # 오래된 키 두 개가 같은 이름으로 정규화될 수 있다. 덮어쓰지
            # 않고 더해서 어떤 호출도 잃지 않게 하고, 위조된 횟수가 코멘트를
            # 부풀리지 못하도록 상한을 둔다.
            counts[valid] = min(counts.get(valid, 0) + count, _COUNT_LIMIT)
        if counts:
            cleaned[category] = counts
    return cleaned


def clean_tokens(tokens: Any) -> dict[str, int | None]:
    """상한이 적용된 정규 토큰 카운터를, 계산된 total과 함께 반환한다.

    ``reasoning``은 참고용이며 제공자의 output 횟수에 이미 포함되어 있으므로
    ``total``에는 결코 더하지 않는다. ``None``은 런타임이 해당 버킷을 보고할
    수 없음을 표현하기 위해 보존한다. 누락되었거나 적대적인 필드는
    추측하는 대신 버린다.
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
        r"실패(?:했던|하던|한|를|가|은|는)?[^\n]{0,256}(?:수정|해결|고쳤)",
        text,
    ):
        return True
    return re.search(
        r"(?:\b(?:fixed|resolved)\b.{0,256}?\b(?:failed|failure)\b"
        r"|\b(?:failed|failure)\b.{0,256}?\b(?:fixed|resolved)\b)",
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
    bounded = text[:_SUMMARY_INPUT_LIMIT]
    lines = (
        " ".join(line[:_SUMMARY_LINE_LIMIT].split())
        for line in bounded.splitlines()
    )
    normalized = _without_fenced_code(line for line in lines if line)
    structured = _structured_result_summary(normalized, limit)
    fallback = _fallback_result_summary(normalized, limit) if not structured else ""
    return _bound_summary(structured or fallback or "\n".join(normalized), limit)


def has_reportable_usage(source: str, usage: Any) -> bool:
    """추적되는 모든 카드는 정확히 하나의 usage 코멘트를 받는다.

    전부 비어 있는 보고도 의미가 있다: 그 턴이 정말로 Skill, subagent, MCP
    도구를 전혀 사용하지 않았음을 읽는 이에게 알려 주고, 여전히 source와
    model 정보를 담는다. 이를 억누르면 "아무것도 사용하지 않음"이 "기록된
    적 없음"과 구별할 수 없게 된다.
    """
    return True


def usage_comment_header(source: Any) -> str:
    """source를 키로 하는, usage 코멘트의 첫 줄.

    Claude Code의 헤더가 원본이며, 그것에 맞춰 작성된 소비자들이 계속
    매칭할 수 있도록 바이트 단위로 그대로 재현된다. 다른 소스는 각자의
    헤더를 가진다.
    """
    return _SOURCE_HEADERS.get(source, USAGE_COMMENT_HEADER)


def usage_event_id(source: Any, task_id: Any) -> str:
    """카드 하나의 단일 usage 코멘트를 위한 결정론적 마커.

    카드 자신의 정체성에서만 파생되므로, 같은 카드에 대한 모든 재시도는
    -- 크래시 이후든, 마커 기록 실패 이후든, 재실행된 훅이든 -- 같은 값을
    다시 계산하고 자신이 이미 게시한 코멘트를 알아볼 수 있다.
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
    """그 턴이 무엇을 사용했는지 밝히는, 크기가 제한된 구조화 코멘트 본문 하나.

    결과는 항상 파싱 가능한 JSON이다. 크기 제한은 횟수가 가장 낮은 항목을
    버리고 다시 직렬화하는 방식으로 강제하며, 직렬화된 텍스트를 자르는
    일은 결코 없으므로 긴 이름이 코멘트를 토큰 중간에서 잘리게 만들 수
    없다. 적법한 ``event_id``가 주어지면 게시를 멱등하게 만드는 마커로서
    페이로드에 실린다. 마커 문법을 벗어난 id는 버려지므로, 과도하게 긴
    id는 유출될 수도, 코멘트를 제한 너머로 밀어낼 수도 없다.
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

    # 횟수 높은 순, 그다음 이름 순으로 정렬해, 축소할 때 항상 정보 가치가
    # 가장 낮은 항목부터 떨어져 나가게 한다.
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
        # 너무 길다: 현재 가장 큰 카테고리에서 횟수가 가장 낮은 항목을
        # 떨어내고 다시 시도한다. 헤더만으로는 제한 안에 들어가므로 반드시
        # 종료된다.
        widest = max(kept, key=lambda category: (len(kept[category]), category))
        if not kept[widest]:
            # 더 버릴 것이 없다. 헤더 자체는 이미 이름과 모델 길이 제한에
            # 묶여 있으므로, 이것이 가장 작은 유효한 형태다.
            return comment
        kept[widest].pop()
        truncated = True
