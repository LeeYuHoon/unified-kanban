"""Claude와 정규화된 Codex 이벤트를 위한, 재시도에 안전한 공유 훅 상태 기계."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from .token_usage import TranscriptNotReady, token_delta, token_snapshot
from .private_files import (
    CommittedPublicationError,
    Identity,
    NamespaceAuthorityError,
    Receipt,
    atomic_publish,
    create_anonymous_text,
    detach_expected,
    discard_detached,
    open_directory,
    read_bytes,
    restore_detached,
    validate_directory,
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

Adapter = Callable[[list[str], Path], str]
_TASK_RE = re.compile(r"t_[A-Za-z0-9_-]+\Z")
_SOURCE_LABEL = {"claude-code": "Claude", "codex": "Codex"}



def _create_idempotency_key(
    source: str, session_id: str, cwd: Path, prompt: str
) -> str:
    encoded = json.dumps(
        ["unified-kanban/claude-create/v2", source, session_id, str(cwd), prompt],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_adapter(argv: list[str], cwd: Path) -> str:
    """설치된 어댑터를 이벤트의 프로젝트 디렉터리에서 호출한다."""
    adapter = Path.home() / ".local/bin/kanban-adapter"
    fd_paths = [
        argument.split("=", 1)[-1]
        for argument in argv
        if argument.startswith(("--title-file=/dev/fd/", "--result-file=/dev/fd/"))
    ]
    fd_paths.extend(
        argv[index + 1]
        for index, argument in enumerate(argv[:-1])
        if argument in {"--title-file", "--result-file"}
        and argv[index + 1].startswith("/dev/fd/")
    )
    pass_fds = tuple(sorted({int(path.rsplit("/", 1)[1]) for path in fd_paths}))
    completed = subprocess.run(
        [str(adapter), *argv],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        pass_fds=pass_fds,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"kanban-adapter failed ({completed.returncode}): {detail}")
    return completed.stdout


def cache_dir_for(kind: str) -> Path:
    """제공자별 전용(private) 훅 상태 디렉터리를 반환한다."""
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "kanban-adapter" / kind


def _state_path(cache_dir: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def _lock_path(cache_dir: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.lock"


def _open_session_lock(path: Path, directory_fd: int) -> int:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
    except FileExistsError:
        fd = os.open(path.name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise RuntimeError("hook lock must be a singly-linked regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _ensure_cache(cache_dir: Path) -> int:
    try:
        fd = open_directory(cache_dir, create=True)
    except OSError as exc:
        raise RuntimeError("Claude hook cache must be a non-symlink directory") from exc
    opened = os.fstat(fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) & 0o077
    ):
        os.close(fd)
        raise RuntimeError("Claude hook cache must be an owner-only directory")
    return fd


def _write_state(
    path: Path,
    state: dict[str, Any],
    *,
    expected_identity: Receipt | Identity | None = None,
    directory_fd: int | None = None,
) -> Receipt:
    content = (json.dumps(state, sort_keys=True) + "\n").encode("utf-8")
    try:
        return atomic_publish(
            path,
            content,
            expected_identity=expected_identity,
            directory_fd=directory_fd,
        )
    except CommittedPublicationError as exc:
        # 정식(canonical) 엔트리가 설치된 inode로 검증되었다.  그 살아 있는
        # receipt를 누출하거나 낡은 상태를 유지하는 대신 그대로 이어 전달한다.
        log_error(f"state-publication-durability: {exc}")
        return exc.receipt


def _read_state(path: Path, *, directory_fd: int | None = None) -> tuple[dict[str, Any], Receipt] | None:
    try:
        content, identity = read_bytes(path, directory_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        identity.close()
        raise RuntimeError("Claude hook state was invalid JSON") from exc
    if not isinstance(payload, dict):
        identity.close()
        raise RuntimeError("Claude hook state was not an object")
    task_id = payload.get("task_id")
    cwd = payload.get("cwd")
    if (
        not isinstance(task_id, str)
        or not _TASK_RE.fullmatch(task_id)
        or not isinstance(cwd, str)
        or not Path(cwd).is_absolute()
    ):
        identity.close()
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
    return state, identity


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
    directory_fd: int,
) -> None:
    read = _read_state(state_path, directory_fd=directory_fd)
    if read is None:
        return
    state, state_identity = read
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
        try:
            state_identity = _write_state(
                state_path, state, expected_identity=state_identity, directory_fd=directory_fd
            )
        except BaseException:
            state_identity.close()
            raise
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
        except Exception as exc:  # noqa: BLE001 - token telemetry는 fail open이어야 한다
            # token 수집은 관측 용도일 뿐이므로 card를 고립시키거나 log에 transcript
            # 경로를 노출해서는 안 된다.
            log_error(
                f"token-snapshot: {type(exc).__name__}",
                kind="claude" if source == "claude-code" else source,
            )
    if has_reportable_usage(source, usage) and not state.get("usage_comment_posted"):
        # event id는 이 프로세스가 아니라 card에서 유도한다. 따라서 충돌이나 marker
        # 쓰기 실패 후 재시도해도 같은 값을 다시 계산하며, adapter는 이미 추가한
        # comment를 인식한다.
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
                state_identity = _write_state(
                    state_path, state, expected_identity=state_identity, directory_fd=directory_fd
                )
            except NamespaceAuthorityError:
                raise
            except Exception as exc:
                log_error(f"usage-comment-marker: {exc}")
            break
    try:
        result_identity = create_anonymous_text(
            state_path.parent, result, label="result", directory_fd=directory_fd
        )
    except BaseException:
        state_identity.close()
        raise
    result_option = f"--result-file=/dev/fd/{result_identity.file_fd}"
    # 외부 side effect 전에 정규 state를 분리한다. 그러면 성공적으로 완료된 뒤에는
    # 예측 가능한 재시도 trigger가 절대 남지 않는다.
    try:
        validate_directory(state_path.parent, directory_fd)
        detached = detach_expected(
            state_path, state_identity, directory_fd=directory_fd
        )
    except BaseException:
        state_identity.close()
        result_identity.close()
        raise
    try:
        adapter(
            [
                "done", "--task", state["task_id"],
                result_option, "--summary=Agent result recorded",
            ],
            cwd,
        )
    except BaseException:
        restore_detached(state_path, detached).close()
        raise
    else:
        try:
            discard_detached(detached)
        except Exception as exc:
            log_error(f"state-cleanup: {exc}")
    finally:
        result_identity.close()


def _handle_event_locked(
    event: str,
    payload: Mapping[str, Any],
    *,
    adapter: Adapter = run_adapter,
    cache_dir: Path | None = None,
    source: str = "claude-code",
    directory_fd: int,
) -> None:
    session_id = _required_text(payload, "session_id")
    cache = cache_dir or cache_dir_for("claude")
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
        existing = _read_state(state_path, directory_fd=directory_fd)
        if existing is not None:
            existing[1].close()
            _complete(
                state_path,
                "Superseded by a new user prompt after a missing Stop event",
                adapter=adapter,
                source=source,
                directory_fd=directory_fd,
            )
        title = " ".join(prompt.split())[:120]
        title_identity = create_anonymous_text(
            state_path.parent, title, label="title", directory_fd=directory_fd
        )
        validate_directory(cache, directory_fd)
        try:
            output = adapter(
                [
                    "start",
                    "--title-file",
                    f"/dev/fd/{title_identity.file_fd}",
                    "--source", source,
                    "--idempotency-key",
                    _create_idempotency_key(source, session_id, cwd, prompt),
                ],
                cwd,
            ).strip()
        finally:
            title_identity.close()
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
                # 새 session은 UserPromptSubmit hook 이후에야 JSONL을 만들 수 있다.
                # Stop이 첫 요청을 수집할 수 있도록 신뢰된 candidate를 빈 baseline과
                # 함께 보존한다.
                new_state["transcript_path"] = transcript_path
                new_state["token_baseline"] = {}
            except Exception as exc:  # noqa: BLE001 - token telemetry는 fail open이어야 한다
                log_error(
                    f"token-baseline: {type(exc).__name__}",
                    kind="claude" if source == "claude-code" else source,
                )
            else:
                new_state["transcript_path"] = transcript_path
                new_state["token_baseline"] = baseline
        try:
            _write_state(state_path, new_state, directory_fd=directory_fd).close()
            validate_directory(cache, directory_fd)
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
        read = _read_state(state_path, directory_fd=directory_fd)
        if read is None:
            return
        state, state_identity = read
        if event == "subagent-start":
            # Codex는 위임된 각 child를 SubagentStart를 통해 보고하며, agent_type은
            # runtime이 노출하는 유일한 이름이다. identifier 허용 목록 밖의 이름은
            # 기록하지 않고 버린다.
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
                state_identity.close()
                return
        else:
            bump(state.setdefault("usage", {}), *entry)
        try:
            _write_state(
                state_path, state, expected_identity=state_identity, directory_fd=directory_fd
            ).close()
            validate_directory(cache, directory_fd)
        except BaseException:
            state_identity.close()
            raise
        return

    label = _SOURCE_LABEL.get(source, "Agent")

    def merge_model() -> None:
        """card를 보고하기 전에 늦게 도착한 model을 state에 병합한다."""
        model = sanitize_model(payload.get("model"))
        if not model:
            return
        read = _read_state(state_path, directory_fd=directory_fd)
        if read is None:
            return
        state, state_identity = read
        if state.get("model") != model:
            state["model"] = model
            try:
                _write_state(
                    state_path, state, expected_identity=state_identity, directory_fd=directory_fd
                ).close()
                validate_directory(cache, directory_fd)
            except BaseException:
                state_identity.close()
                raise
        else:
            state_identity.close()

    if event == "stop":
        merge_model()
        result = payload.get("last_assistant_message")
        if not isinstance(result, str) or not result.strip():
            result = f"{label} response completed"
        _complete(state_path, result, adapter=adapter, source=source, directory_fd=directory_fd)
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
            directory_fd=directory_fd,
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
    """session별 file lock 아래에서 provider event 하나를 직렬화하여 적용한다."""
    session_id = _required_text(payload, "session_id")
    cache = cache_dir or cache_dir_for("claude")
    directory_fd = _ensure_cache(cache)
    try:
        lock_fd = _open_session_lock(_lock_path(cache, session_id), directory_fd)
    except BaseException:
        os.close(directory_fd)
        raise
    try:
        _handle_event_locked(
            event,
            payload,
            adapter=adapter,
            cache_dir=cache,
            source=source,
            directory_fd=directory_fd,
        )
    finally:
        try:
            os.close(lock_fd)
        finally:
            os.close(directory_fd)


def log_error(message: str, *, kind: str = "claude") -> None:
    """공개 cache 경로명을 변경하지 않고 최선형 진단을 수행한다."""
    try:
        sanitized = message.replace("\n", " ")
        print(f"kanban-adapter[{kind}]: {sanitized}", file=sys.stderr)
    except Exception:
        pass


def main(argv: Sequence[str] | None = None, *, stdin: TextIO | None = None) -> int:
    """Claude hook payload 하나를 읽으며, 관측 실패는 계속 fail open으로 처리한다."""
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
