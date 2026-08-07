"""Retry-safe shared hook state machine for Claude and normalized Codex events."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from .token_usage import TranscriptNotReady, token_delta, token_snapshot
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

Adapter = Callable[[list[str], Path], str]
_TASK_RE = re.compile(r"t_[A-Za-z0-9_-]+\Z")
_SOURCE_LABEL = {"claude-code": "Claude", "codex": "Codex"}
_RESULT_ARG_LIMIT = 16_000


def run_adapter(argv: list[str], cwd: Path) -> str:
    """Invoke the installed adapter in the event's project directory."""
    adapter = Path.home() / ".local/bin/kanban-adapter"
    completed = subprocess.run(
        [str(adapter), *argv],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"kanban-adapter failed ({completed.returncode}): {detail}")
    return completed.stdout


def cache_dir_for(kind: str) -> Path:
    """Return the private per-provider hook state directory."""
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "kanban-adapter" / kind


def _state_path(cache_dir: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def _lock_path(cache_dir: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.lock"


def _open_session_lock(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        os.close(fd)
        raise RuntimeError("hook lock must be a regular file")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _ensure_cache(cache_dir: Path) -> None:
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if cache_dir.is_symlink() or not cache_dir.is_dir():
        raise RuntimeError("Claude hook cache must be a non-symlink directory")
    os.chmod(cache_dir, 0o700)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    content = (json.dumps(state, sort_keys=True) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _write_private_text(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent)
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


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError("Claude hook state must be a non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("Claude hook state changed during read")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            payload = json.load(handle)
    finally:
        os.close(fd)
    if not isinstance(payload, dict):
        raise RuntimeError("Claude hook state was not an object")
    task_id = payload.get("task_id")
    cwd = payload.get("cwd")
    if (
        not isinstance(task_id, str)
        or not _TASK_RE.fullmatch(task_id)
        or not isinstance(cwd, str)
        or not Path(cwd).is_absolute()
    ):
        raise RuntimeError("Claude hook state had invalid fields")
    state: dict[str, Any] = {"task_id": task_id, "cwd": cwd}
    usage = clean_usage(payload.get("usage"))
    if usage:
        state["usage"] = usage
    model = sanitize_model(payload.get("model"))
    if model:
        state["model"] = model
    if payload.get("usage_comment_posted") is True:
        state["usage_comment_posted"] = True
    result = payload.get("result")
    if isinstance(result, str) and result.strip():
        state["result"] = result
    summary = concise_summary(payload.get("summary"))
    if summary:
        state["summary"] = summary
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and Path(transcript_path).is_absolute():
        state["transcript_path"] = transcript_path
    baseline = clean_tokens(payload.get("token_baseline"))
    if baseline:
        state["token_baseline"] = baseline
    return state


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Claude hook payload missing {key}")
    return value


def _complete(
    state_path: Path,
    summary: str,
    *,
    adapter: Adapter,
    source: str = "claude-code",
) -> None:
    state = _read_state(state_path)
    if state is None:
        return
    saved_result = state.get("result")
    if isinstance(saved_result, str) and saved_result.strip():
        result = saved_result
        saved_summary = state.get("summary")
        concise = (
            saved_summary
            if isinstance(saved_summary, str) and saved_summary.strip()
            else concise_summary(result) or result
        )
    else:
        result = summary
        concise = concise_summary(result) or result
        state["result"] = result
        state["summary"] = concise
        _write_state(state_path, state)
    result_path = state_path.with_suffix(".result")
    if len(result.encode("utf-8")) > _RESULT_ARG_LIMIT:
        _write_private_text(result_path, result)
        result_option = f"--result-file={result_path}"
    else:
        result_option = f"--result={result}"
    cwd = Path(state["cwd"]).resolve()
    usage = state.get("usage", {})
    tokens: dict[str, int | None] = {}
    transcript_path = state.get("transcript_path")
    if isinstance(transcript_path, str):
        try:
            tokens = token_delta(
                token_snapshot(source, transcript_path),
                state.get("token_baseline", {}),
            )
        except Exception as exc:  # noqa: BLE001 - token telemetry must fail open
            # Token collection is observability only; it must never strand the
            # card or expose the transcript path in logs.
            log_error(
                f"token-snapshot: {type(exc).__name__}",
                kind="claude" if source == "claude-code" else source,
            )
    if has_reportable_usage(source, usage) and not state.get("usage_comment_posted"):
        # The event id is derived from the card, not from this process, so a
        # retry after a crash or a failed marker write recomputes the same
        # value and the adapter recognises the comment it already appended.
        event_id = usage_event_id(source, state["task_id"])
        message = usage_comment(
            source=source,
            model=state.get("model"),
            usage=usage,
            tokens=tokens,
            unavailable=unavailable_categories(source),
            event_id=event_id,
        )
        for attempt in range(3):
            try:
                adapter(
                    [
                        "update", "--task", state["task_id"], "--message", message,
                        "--idempotency-key", event_id,
                    ],
                    cwd,
                )
            except Exception as exc:
                if attempt == 2:
                    log_error(f"usage-comment: {exc}")
                continue
            state["usage_comment_posted"] = True
            try:
                _write_state(state_path, state)
            except Exception as exc:
                log_error(f"usage-comment-marker: {exc}")
            break
    # Keep the complete final response out of argv so large results are not
    # constrained by ARG_MAX. The secure state/result files remain until a
    # completion succeeds, allowing SessionEnd or a later prompt to retry.
    adapter(
        [
            "done", "--task", state["task_id"],
            result_option, f"--summary={concise}",
        ],
        cwd,
    )
    state_path.unlink()
    result_path.unlink(missing_ok=True)


def _handle_event_locked(
    event: str,
    payload: Mapping[str, Any],
    *,
    adapter: Adapter = run_adapter,
    cache_dir: Path | None = None,
    source: str = "claude-code",
) -> None:
    session_id = _required_text(payload, "session_id")
    cache = cache_dir or cache_dir_for("claude")
    _ensure_cache(cache)
    state_path = _state_path(cache, session_id)

    if event == "prompt":
        cwd_text = _required_text(payload, "cwd")
        cwd = Path(cwd_text).expanduser()
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("Claude hook cwd must be an existing absolute directory")
        cwd = cwd.resolve()
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = "Claude Code task"
        if prompt.lstrip().lower().startswith((
            "<task-notification>",
            "<agent-message",
            "<teammate-message",
        )):
            return
        if _read_state(state_path) is not None:
            _complete(
                state_path,
                "Superseded by a new user prompt after a missing Stop event",
                adapter=adapter,
                source=source,
            )
        title = " ".join(prompt.split())[:120]
        output = adapter(
            ["start", "--title", title, "--source", source], cwd
        ).strip()
        if not _TASK_RE.fullmatch(output):
            raise RuntimeError("kanban-adapter returned an invalid task id")
        new_state: dict[str, Any] = {"cwd": str(cwd), "task_id": output}
        model = sanitize_model(payload.get("model"))
        if model:
            new_state["model"] = model
        transcript_path = payload.get("transcript_path")
        if isinstance(transcript_path, str):
            try:
                baseline = token_snapshot(source, transcript_path)
            except TranscriptNotReady:
                # New sessions may not create their JSONL until after the
                # UserPromptSubmit hook. Preserve the trusted candidate with
                # an empty baseline so Stop can collect the first request.
                new_state["transcript_path"] = transcript_path
                new_state["token_baseline"] = {}
            except Exception as exc:  # noqa: BLE001 - token telemetry must fail open
                log_error(
                    f"token-baseline: {type(exc).__name__}",
                    kind="claude" if source == "claude-code" else source,
                )
            else:
                new_state["transcript_path"] = transcript_path
                new_state["token_baseline"] = baseline
        try:
            _write_state(state_path, new_state)
        except Exception:
            try:
                adapter(
                    [
                        "done", "--task", output, "--summary",
                        "Hook state persistence failed; card closed automatically",
                    ],
                    cwd,
                )
            except Exception:
                pass
            raise
        return

    if event in {"post-tool-use", "subagent-start"}:
        state = _read_state(state_path)
        if state is None:
            return
        if event == "subagent-start":
            # Codex reports each delegated child through SubagentStart; its
            # agent_type is the only name the runtime exposes. A name outside
            # the identifier whitelist is dropped rather than recorded.
            role = classify_subagent(payload.get("agent_type"))
            entry = None if role is None else ("subagents", role)
        else:
            entry = classify_tool(
                source, payload.get("tool_name"), payload.get("tool_input")
            )
        model = sanitize_model(payload.get("model"))
        if model:
            state["model"] = model
        if entry is None:
            if not model:
                return
        else:
            bump(state.setdefault("usage", {}), *entry)
        _write_state(state_path, state)
        return

    label = _SOURCE_LABEL.get(source, "Agent")

    def merge_model() -> None:
        """Fold a late-arriving model into state before the card is reported."""
        model = sanitize_model(payload.get("model"))
        if not model:
            return
        state = _read_state(state_path)
        if state is not None and state.get("model") != model:
            state["model"] = model
            _write_state(state_path, state)

    if event == "stop":
        merge_model()
        result = payload.get("last_assistant_message")
        if not isinstance(result, str) or not result.strip():
            result = f"{label} response completed"
        _complete(state_path, result, adapter=adapter, source=source)
        return

    if event == "session-end":
        merge_model()
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = "unknown"
        _complete(
            state_path,
            f"{label} session ended: {' '.join(reason.split())[:120]}",
            adapter=adapter,
            source=source,
        )
        return

    raise ValueError(f"unsupported agent hook event: {event}")


def handle_event(
    event: str,
    payload: Mapping[str, Any],
    *,
    adapter: Adapter = run_adapter,
    cache_dir: Path | None = None,
    source: str = "claude-code",
) -> None:
    """Serialize and apply one provider event under a per-session file lock."""
    session_id = _required_text(payload, "session_id")
    cache = cache_dir or cache_dir_for("claude")
    _ensure_cache(cache)
    lock_fd = _open_session_lock(_lock_path(cache, session_id))
    try:
        _handle_event_locked(
            event,
            payload,
            adapter=adapter,
            cache_dir=cache,
            source=source,
        )
    finally:
        os.close(lock_fd)


def log_error(message: str, *, kind: str = "claude") -> None:
    """Best-effort append a sanitized hook diagnostic to a private log."""
    cache = cache_dir_for(kind)
    try:
        _ensure_cache(cache)
        path = cache / "errors.log"
        flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise RuntimeError("hook error log must be a regular file")
            os.write(fd, (message.replace("\n", " ") + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def main(argv: Sequence[str] | None = None, *, stdin: TextIO | None = None) -> int:
    """Read one Claude hook payload; observation failures remain fail open."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(
            "usage: claude-kanban-hook "
            "prompt|post-tool-use|subagent-start|stop|session-end",
            file=sys.stderr,
        )
        return 2
    source = sys.stdin if stdin is None else stdin
    try:
        payload = json.load(source)
        if not isinstance(payload, dict):
            raise ValueError("Claude hook input must be a JSON object")
        handle_event(args[0], payload)
    except Exception as exc:
        log_error(f"{args[0]}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
