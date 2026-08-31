"""Codex hook 페이로드를 공유 이벤트 상태 머신 형식으로 정규화한다."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from .claude_hook import cache_dir_for, handle_event, log_error
from .codex_model import default_state_db, resolve_model, resolve_rollout_path


def _first_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


_EVENTS = frozenset(
    {"prompt", "post-tool-use", "subagent-start", "stop", "session-end"}
)


def normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Codex hook 페이로드를 공유 hook 어휘로 매핑한다.

    Codex 0.145는 이미 일치하는 snake_case 이름을 내보낸다; camelCase와
    레거시 별칭은 더 오래된 Codex 빌드가 계속 동작하도록 유지된다.
    ``tool_input``은 분류기가 도구 이름을 정하는 데 필요해서 전달되지만,
    다운스트림 어디에서도 영속화되지 않는다; ``tool_response``는 아예
    전달되지 않는다.
    """
    return {
        "session_id": _first_text(payload, "session_id", "sessionID", "sessionId", "id"),
        "cwd": _first_text(payload, "cwd", "directory", "workspace", "projectDir"),
        "prompt": _first_text(payload, "prompt", "input", "message", "text"),
        "last_assistant_message": _first_text(
            payload, "last_assistant_message", "lastAssistantMessage", "result", "message", "text"
        ),
        "tool_name": _first_text(payload, "tool_name", "toolName"),
        "tool_input": payload.get("tool_input", payload.get("toolInput")),
        "agent_type": _first_text(payload, "agent_type", "agentType"),
        "model": _first_text(payload, "model"),
        "reason": _first_text(payload, "reason"),
        "transcript_path": _first_text(
            payload, "transcript_path", "transcriptPath", "rollout_path", "rolloutPath"
        ),
    }


_UNSET = object()


def enrich_payload(
    payload: Mapping[str, Any],
    *,
    state_db: Path | str | None | object = _UNSET,
) -> dict[str, Any]:
    """Codex 페이로드를 정규화하고 이벤트에 ``model``이 없으면 채워 넣는다.

    hook 페이로드에 명시된 모델이 항상 우선한다. 유일한 폴백은 바로 이
    세션이 Codex의 state 데이터베이스에 기록한 모델이다; 머신 전역
    기본값은 없으므로, 세션을 읽을 수 없으면 모델은 설정되지 않은 채로 남는다.
    """
    normalized = normalize_payload(payload)
    session_id = normalized.get("session_id")
    if not session_id:
        return normalized
    if state_db is _UNSET:
        db = default_state_db()
    elif isinstance(state_db, (str, Path)):
        db = Path(state_db)
    else:
        db = None
    if not normalized.get("model"):
        try:
            normalized["model"] = resolve_model(session_id, state_db=db)
        except Exception as exc:  # noqa: BLE001 - optional state enrichment fails open
            log_error(f"model-resolve: {exc}", kind="codex")
    if not normalized.get("transcript_path"):
        try:
            normalized["transcript_path"] = resolve_rollout_path(session_id, state_db=db)
        except Exception as exc:  # noqa: BLE001 - optional state enrichment fails open
            log_error(f"rollout-resolve: {exc}", kind="codex")
    return normalized


def main(argv: Sequence[str] | None = None, *, stdin: TextIO | None = None) -> int:
    """Codex hook 페이로드 하나를 읽는다; observation 실패는 계속 fail open으로 남는다."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in _EVENTS:
        print(
            "usage: codex-kanban-hook "
            "prompt|post-tool-use|subagent-start|stop|session-end",
            file=sys.stderr,
        )
        return 2
    source = sys.stdin if stdin is None else stdin
    try:
        payload = json.load(source)
        if not isinstance(payload, dict):
            raise ValueError("Codex hook input must be a JSON object")
        handle_event(
            args[0],
            enrich_payload(payload),
            cache_dir=cache_dir_for("codex"),
            source="codex",
        )
    except Exception as exc:
        log_error(f"{args[0]}: {exc}", kind="codex")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
