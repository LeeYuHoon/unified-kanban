"""Normalize Codex hook payloads into the shared event state machine."""

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
    """Map a Codex hook payload onto the shared hook vocabulary.

    Codex 0.145 emits snake_case names that already match; the camelCase and
    legacy aliases are kept so older Codex builds keep working. ``tool_input``
    is forwarded because the classifier needs it to name a tool, but nothing
    downstream persists it; ``tool_response`` is never forwarded at all.
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
    """Normalize a Codex payload and fill in ``model`` when the event omits it.

    An explicit model in the hook payload always wins. The only fallback is the
    model this exact session recorded in Codex's state database; there is no
    machine-wide default, so an unreadable session leaves the model unset.
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
    """Read one Codex hook payload; observation failures remain fail open."""
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
