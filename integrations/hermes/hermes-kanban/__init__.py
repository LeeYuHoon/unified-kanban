"""Forward bounded Hermes lifecycle fields to Unified Kanban observation state."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kanban_adapter.compatibility import check_hermes_compatibility  # noqa: E402
from kanban_adapter.hermes_hook import TurnTracker  # noqa: E402


logger = logging.getLogger(__name__)
_tracker = TurnTracker()


def _check_host_compatibility():
    return check_hermes_compatibility(runtime_prefix=Path(sys.prefix))


_compatibility_check = _check_host_compatibility
_runtime_enabled = False


def _refresh_runtime_compatibility() -> bool:
    global _runtime_enabled
    _runtime_enabled, reason = _compatibility_check()
    if not _runtime_enabled:
        logger.error("Hermes Kanban disabled: %s", reason)
    return _runtime_enabled


def _on_pre_llm_call(**kwargs):
    if not _refresh_runtime_compatibility():
        return None
    try:
        _tracker.start(
            session_id=kwargs.get("session_id") or "",
            turn_id=kwargs.get("turn_id") or "",
            user_message=kwargs.get("user_message") or "",
            platform=kwargs.get("platform") or "",
            model=kwargs.get("model") or "",
        )
    except Exception:
        logger.warning("Hermes Kanban turn start failed")
    return None


def _on_post_tool_call(**kwargs):
    """Count one tool call. Only ``tool_name`` and ``args`` are forwarded, and
    the tracker reads nothing from ``args`` except a skill name -- ``result``,
    ``middleware_trace`` and the rest never leave this hook."""
    if not _runtime_enabled:
        return None
    try:
        _tracker.record_tool(
            session_id=kwargs.get("session_id") or "",
            turn_id=kwargs.get("turn_id") or "",
            tool_name=kwargs.get("tool_name"),
            args=kwargs.get("args"),
        )
    except Exception:
        logger.warning("Hermes Kanban tool usage record failed")
    return None


def _on_subagent_start(**kwargs):
    if not _runtime_enabled:
        return None
    try:
        _tracker.record_subagent(
            parent_session_id=kwargs.get("parent_session_id") or "",
            parent_turn_id=kwargs.get("parent_turn_id") or "",
            child_role=kwargs.get("child_role"),
        )
    except Exception:
        logger.warning("Hermes Kanban subagent record failed")
    return None


def _on_post_llm_call(**kwargs):
    """Record the turn's model and a bounded summary. ``user_message`` and
    ``conversation_history`` are deliberately not forwarded."""
    if not _runtime_enabled:
        return None
    try:
        _tracker.record_turn_result(
            session_id=kwargs.get("session_id") or "",
            turn_id=kwargs.get("turn_id") or "",
            assistant_response=kwargs.get("assistant_response"),
            model=kwargs.get("model") or "",
        )
    except Exception:
        logger.warning("Hermes Kanban turn result record failed")
    return None


def _on_post_api_request(**kwargs):
    """Forward only normalized numeric usage and opaque request identity."""
    if not _runtime_enabled:
        return None
    try:
        _tracker.record_api_usage(
            session_id=kwargs.get("session_id") or "",
            turn_id=kwargs.get("turn_id") or "",
            api_request_id=kwargs.get("api_request_id") or "",
            usage=kwargs.get("usage"),
        )
    except Exception:
        logger.warning("Hermes Kanban token usage record failed")
    return None


def _on_session_end(**kwargs):
    if not _runtime_enabled:
        return None
    try:
        _tracker.finish(
            session_id=kwargs.get("session_id") or "",
            turn_id=kwargs.get("turn_id") or "",
            completed=bool(kwargs.get("completed")),
            interrupted=bool(kwargs.get("interrupted")),
        )
    except Exception:
        logger.warning("Hermes Kanban turn completion failed")
    return None


def register(ctx) -> None:
    """Register hooks only when the installed Hermes upstream is supported."""
    if not _refresh_runtime_compatibility():
        return
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("on_session_end", _on_session_end)
