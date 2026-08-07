"""Hermes user-turn observation lifecycle and auxiliary-work exclusion policy."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .backend import (
    HermesCliBackend,
    Runner,
    comment_marker_present,
    run_command,
)
from .usage import (
    bump,
    classify_subagent,
    classify_tool,
    clean_tokens,
    clean_usage,
    concise_summary,
    has_reportable_usage,
    sanitize_model,
    unavailable_categories,
    usage_comment,
    usage_event_id,
)

_BOARD_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_TASK_RE = re.compile(r"t_[A-Za-z0-9]+\Z")
_SOURCE = "hermes-agent"
_RESULT_ARG_LIMIT = 16_000
_AUXILIARY_PROCESS_MARKERS = (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_TASK",
)
_AUTOMATIC_MESSAGE_PREFIXES = (
    "[ASYNC DELEGATION ",
    "[IMPORTANT: Background process ",
    "[CONTEXT COMPACTION",
    "[CONTEXT SUMMARY]:",
    "[Your active task list was preserved across context compression]",
)

logger = logging.getLogger(__name__)


def is_auxiliary_hermes_turn(user_message: str) -> bool:
    """Identify internal Hermes work that must not create duplicate Kanban cards."""
    if any(os.environ.get(name) for name in _AUXILIARY_PROCESS_MARKERS):
        return True
    message = user_message.lstrip()
    return message.startswith(_AUTOMATIC_MESSAGE_PREFIXES)


def hermes_runtime_cwd() -> Path:
    """Resolve Hermes' per-session cwd, falling back outside Hermes runtime."""
    try:
        from agent.runtime_cwd import resolve_agent_cwd
    except ImportError:
        return Path.cwd()
    return resolve_agent_cwd()


def default_cache_root() -> Path:
    """Return the private cache root used to bridge Hermes lifecycle hooks."""
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "unified-kanban" / "hermes-turns"


class TurnTracker:
    """Per-turn Hermes Agent card lifecycle: pre_llm_call starts a running card,
    on_session_end completes the same card. Board routing reuses the unified
    dashboard project-directory resolution from HermesCliBackend."""

    def __init__(
        self,
        *,
        runner: Runner = run_command,
        cache_root: Path | None = None,
        cwd_provider: Callable[[], Path] = hermes_runtime_cwd,
    ) -> None:
        self.runner = runner
        self.backend = HermesCliBackend(runner=runner)
        self.cache_root = cache_root or default_cache_root()
        self.cwd_provider = cwd_provider

    def _state_path(self, session_id: str, turn_id: str) -> Path:
        digest = hashlib.sha256(f"{session_id}\0{turn_id}".encode()).hexdigest()
        return self.cache_root / f"{digest}.json"

    def _lock_path(self, session_id: str, turn_id: str) -> Path:
        digest = hashlib.sha256(f"{session_id}\0{turn_id}".encode()).hexdigest()
        return self.cache_root / f"{digest}.lock"

    def _ensure_cache(self) -> None:
        self.cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.cache_root.is_symlink() or not self.cache_root.is_dir():
            raise RuntimeError("Hermes turn cache must be a non-symlink directory")
        os.chmod(self.cache_root, 0o700)

    def _open_turn_lock(self, session_id: str, turn_id: str) -> int:
        self._ensure_cache()
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._lock_path(session_id, turn_id), flags, 0o600)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            os.close(fd)
            raise RuntimeError("Hermes turn lock must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _write_state(
        self, path: Path, payload: dict[str, Any], *, overwrite: bool = False
    ) -> None:
        self.cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.cache_root)
        temp = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            if not overwrite and os.path.lexists(path):
                raise RuntimeError("turn state already exists")
            os.replace(temp, path)
            temp = None
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def _read_state(self, path: Path) -> tuple[dict[str, str], tuple[int, int]]:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ValueError("turn state must be a non-symlink regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            identity = (opened.st_dev, opened.st_ino)
            if identity != (before.st_dev, before.st_ino):
                raise RuntimeError("turn state changed during read")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
                payload = json.load(handle)
        finally:
            os.close(fd)
        if not isinstance(payload, dict):
            raise ValueError("turn state must be a JSON object")
        return payload, identity

    def _write_private_text(self, path: Path, content: str) -> None:
        fd, name = tempfile.mkstemp(dir=self.cache_root)
        temp = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            temp = None
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def start(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_message: str,
        platform: str,
        model: Any = None,
    ) -> None:
        """Create state for a real user turn, ignoring internal auxiliary turns."""
        if not session_id or not turn_id or not isinstance(user_message, str):
            return
        if is_auxiliary_hermes_turn(user_message):
            return
        title = " ".join(user_message.strip().split())[:120]
        if not title:
            return
        lock_fd = self._open_turn_lock(session_id, turn_id)
        try:
            state_path = self._state_path(session_id, turn_id)
            if os.path.lexists(state_path):
                self._read_state(state_path)
                return
            board = self.backend.resolve_board(cwd=self.cwd_provider())
            raw = self.runner([
                "hermes", "kanban", "--board", board, "create",
                "--observation",
                "--tenant", "hermes",
                "--created-by", "hermes-agent",
                "--idempotency-key", f"hermes:{session_id}:{turn_id}",
                "--json",
                "--",
                title,
            ])
            result = json.loads(raw)
            task_id = result.get("id") if isinstance(result, dict) else None
            if not isinstance(task_id, str) or not _TASK_RE.fullmatch(task_id):
                raise RuntimeError("Hermes create returned an invalid task id")
            if (
                result.get("status") != "running"
                or result.get("observation") is not True
            ):
                # Idempotent create may return a pre-existing row. Without a
                # creation token we cannot prove this call owns the returned
                # task, so never compensate a contract mismatch by completing
                # it.
                raise RuntimeError(
                    "Hermes create did not return a running observation card"
                )
            # Idempotent create may have returned an observation owned by an
            # earlier duplicate invocation. If local state persistence fails,
            # we cannot prove this call created the card, so completing it as
            # compensation could terminate another live turn. Leave it running;
            # the normal owner will complete it, or orphan expiry will close it.
            state: dict[str, Any] = {"board": board, "task_id": task_id}
            resolved_model = sanitize_model(model)
            if resolved_model:
                state["model"] = resolved_model
            self._write_state(state_path, state)
        finally:
            os.close(lock_fd)

    def _mutate(self, session_id: str, turn_id: str, apply):
        """Run ``apply(state)`` under the turn lock, persisting any change.

        No-ops when the turn has no live card, so observer hooks that fire
        outside a tracked turn (or after completion) stay harmless.
        """
        if not session_id or not turn_id:
            return
        lock_fd = self._open_turn_lock(session_id, turn_id)
        try:
            path = self._state_path(session_id, turn_id)
            try:
                state, _identity = self._read_state(path)
            except FileNotFoundError:
                return
            if apply(state) is False:
                return
            self._write_state(path, state, overwrite=True)
        finally:
            os.close(lock_fd)

    def record_tool(
        self,
        *,
        session_id: str,
        turn_id: str,
        tool_name: Any,
        args: Any = None,
        result: Any = None,
        status: Any = None,
        **_ignored: Any,
    ) -> None:
        """Count one ``post_tool_call``. Only the sanitized name is kept —
        ``args``/``result`` are accepted so the hook signature matches Hermes
        and are deliberately never read beyond the skill-name argument."""
        entry = classify_tool(_SOURCE, tool_name, args)
        if entry is None:
            return
        category, name = entry

        def apply(state: dict[str, Any]) -> None:
            bump(state.setdefault("usage", {}), category, name)

        self._mutate(session_id, turn_id, apply)

    def record_subagent(
        self,
        *,
        parent_session_id: str,
        parent_turn_id: str,
        child_role: Any = None,
        **_ignored: Any,
    ) -> None:
        """Count one delegated child from ``subagent_start``. This is the
        authoritative subagent signal: a single ``delegate_task`` call can fan
        out to several children, so counting the tool call would undercount."""
        name = classify_subagent(child_role)
        if name is None:
            # Outside the identifier whitelist: recorded nowhere, not as a
            # placeholder either.
            return

        def apply(state: dict[str, Any]) -> None:
            bump(state.setdefault("usage", {}), "subagents", name)

        self._mutate(parent_session_id, parent_turn_id, apply)

    def record_turn_result(
        self,
        *,
        session_id: str,
        turn_id: str,
        assistant_response: Any = None,
        model: Any = None,
        **_ignored: Any,
    ) -> None:
        """Store the complete final response plus its bounded card summary."""
        summary = concise_summary(assistant_response)
        result = (
            assistant_response
            if isinstance(assistant_response, str) and assistant_response.strip()
            else None
        )
        resolved_model = sanitize_model(model)

        def apply(state: dict[str, Any]) -> bool:
            changed = False
            if resolved_model and state.get("model") != resolved_model:
                state["model"] = resolved_model
                changed = True
            if summary and state.get("summary") != summary:
                state["summary"] = summary
                changed = True
            if result and state.get("result") != result:
                state["result"] = result
                changed = True
            return changed

        self._mutate(session_id, turn_id, apply)

    def record_api_usage(
        self,
        *,
        session_id: str,
        turn_id: str,
        api_request_id: Any,
        usage: Any,
        **_ignored: Any,
    ) -> None:
        """Accumulate one canonical ``post_api_request`` usage payload.

        Request identifiers are hashed before local persistence and never reach
        the card. The hash makes replayed hooks idempotent without retaining a
        provider request id.
        """
        if not isinstance(api_request_id, str) or not api_request_id:
            return
        if not isinstance(usage, Mapping):
            return
        request_hash = hashlib.sha256(api_request_id.encode("utf-8")).hexdigest()[:16]
        incoming = clean_tokens({
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "cache_read": usage.get("cache_read_tokens"),
            "cache_write": usage.get("cache_write_tokens"),
            "reasoning": usage.get("reasoning_tokens"),
            "requests": 1,
            "total": usage.get("total_tokens"),
        })
        if not incoming:
            return

        def apply(state: dict[str, Any]) -> bool:
            raw_ids = state.get("token_request_ids")
            request_ids = (
                [value for value in raw_ids if isinstance(value, str)]
                if isinstance(raw_ids, list)
                else []
            )
            if request_hash in request_ids:
                return False
            if len(request_ids) >= 512:
                # Refuse to drop old ids: doing so could make a later replay
                # count twice. A normal Hermes turn stays far below this bound;
                # warn rather than silently under-reporting an abnormal turn.
                logger.warning(
                    "Hermes Kanban token request cap reached for turn %s", turn_id
                )
                return False
            request_ids.append(request_hash)
            current = clean_tokens(state.get("tokens"))
            merged: dict[str, int | None] = {}
            for field in (
                "input", "output", "cache_read", "cache_write", "reasoning", "requests", "total"
            ):
                value = incoming.get(field)
                existing = current.get(field)
                if value is None:
                    if field in incoming and field not in current:
                        merged[field] = None
                    elif field in current:
                        merged[field] = existing
                    continue
                merged[field] = value + (existing if isinstance(existing, int) else 0)
            state["tokens"] = clean_tokens(merged)
            state["token_request_ids"] = request_ids
            return True

        self._mutate(session_id, turn_id, apply)

    def _post_usage_comment(
        self, path: Path, state: dict[str, Any], board: str, task_id: str
    ) -> None:
        usage = clean_usage(state.get("usage"))
        if state.get("usage_comment_posted") is True:
            return
        if not has_reportable_usage(_SOURCE, usage):
            return
        # Same contract as the adapter's `--idempotency-key`: the marker is
        # derived from the card, carried inside the comment, and checked
        # against the card before appending, so a retry after a crash or a
        # failed marker write cannot post a second comment.
        event_id = usage_event_id(_SOURCE, task_id)
        message = usage_comment(
            source=_SOURCE,
            model=state.get("model"),
            usage=usage,
            tokens=clean_tokens(state.get("tokens")),
            unavailable=unavailable_categories(_SOURCE),
            event_id=event_id,
        )
        for attempt in range(3):
            try:
                if not comment_marker_present(
                    self.runner, board=board, task_id=task_id, marker=event_id
                ):
                    self.runner([
                        "hermes", "kanban", "--board", board, "comment",
                        "--author", "hermes-agent",
                        "--", task_id, message,
                    ])
            except Exception as exc:
                if attempt == 2:
                    # Telemetry must not strand the card after bounded retries.
                    logger.warning("Hermes Kanban usage comment failed: %s", exc)
                continue
            break
        else:
            return
        state["usage_comment_posted"] = True
        try:
            self._write_state(path, state, overwrite=True)
        except Exception as exc:
            logger.warning("Hermes Kanban usage marker failed: %s", exc)

    def finish(
        self,
        *,
        session_id: str,
        turn_id: str,
        completed: bool,
        interrupted: bool,
    ) -> None:
        """Complete an existing turn card and remove state only after success."""
        lock_fd = self._open_turn_lock(session_id, turn_id)
        try:
            path = self._state_path(session_id, turn_id)
            try:
                payload, identity = self._read_state(path)
            except FileNotFoundError:
                return
            board = payload.get("board")
            task_id = payload.get("task_id")
            if not isinstance(board, str) or not _BOARD_RE.fullmatch(board):
                raise RuntimeError("turn state board is invalid")
            if not isinstance(task_id, str) or not _TASK_RE.fullmatch(task_id):
                raise RuntimeError("turn state task id is invalid")
            if interrupted or not completed:
                # Abnormal completion always says so plainly; the turn's own
                # words could otherwise imply the work finished.
                summary = "Hermes turn ended without normal completion"
                result = summary
            else:
                summary = concise_summary(payload.get("summary")) or (
                    "Hermes turn completed"
                )
                raw_result = payload.get("result")
                result = (
                    raw_result
                    if isinstance(raw_result, str) and raw_result.strip()
                    else summary
                )

            self._post_usage_comment(path, payload, board, task_id)
            # The marker write above replaces the file, so re-anchor identity.
            payload, identity = self._read_state(path)

            result_path = path.with_suffix(".result")
            if len(result.encode("utf-8")) > _RESULT_ARG_LIMIT:
                self._write_private_text(result_path, result)
                result_option = f"--result-file={result_path}"
            else:
                result_option = f"--result={result}"

            # Large results travel through a 0600 file rather than argv so
            # platform command-line limits cannot truncate or reject them.
            self.runner([
                "hermes", "kanban", "--board", board, "complete",
                result_option,
                f"--summary={summary}",
                "--", task_id,
            ])
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) != identity:
                raise RuntimeError("turn state changed before cleanup")
            path.unlink()
            result_path.unlink(missing_ok=True)
        finally:
            os.close(lock_fd)
