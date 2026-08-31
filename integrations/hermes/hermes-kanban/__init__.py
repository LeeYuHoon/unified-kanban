"""범위가 제한된 Hermes 라이프사이클 필드를 Unified Kanban 관측 상태로 전달한다."""

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
    """도구 호출 한 건을 집계한다. ``tool_name``과 ``args``만 전달되며,
    트래커는 ``args``에서 스킬 이름 외에는 아무것도 읽지 않는다 -- ``result``,
    ``middleware_trace`` 및 나머지는 결코 이 훅 밖으로 나가지 않는다."""
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
    """턴의 모델과 범위가 제한된 요약을 기록한다. ``user_message``와
    ``conversation_history``는 의도적으로 전달하지 않는다."""
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
    """정규화된 숫자 사용량과 불투명한 요청 식별자만 전달한다."""
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
    """설치된 Hermes 업스트림이 지원되는 경우에만 훅을 등록한다."""
    if not _refresh_runtime_compatibility():
        return
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("on_session_end", _on_session_end)
