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
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from .conversation_activation import resolve_runtime_config
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
from .backend import BoardNotMappedError, HermesCliBackend, _BOARD_RE
_TASK_RE = re.compile(r"t_[A-Za-z0-9_-]+\Z")
_SOURCE_LABEL = {"claude-code": "Claude", "codex": "Codex"}



def _create_idempotency_key(
    source: str, session_id: str, cwd: Path, prompt: str, prompt_id: object = None
) -> str:
    values = ["unified-kanban/claude-create/v2", source, session_id, str(cwd), prompt]
    # Codex는 네이티브 turn_id를 이 내부 식별자 슬롯으로 정규화한다.
    # 고정된 네이티브 스키마는 UUID가 아닌 문자열을 선언한다.
    if source == "codex" and isinstance(prompt_id, str) and prompt_id:
        values = ["unified-kanban/codex-create/v3", source, session_id, str(cwd), prompt_id]
    if source == "claude-code" and isinstance(prompt_id, str):
        from .claude_absent import _UUID
        if _UUID.fullmatch(prompt_id):
            values = ["unified-kanban/claude-create/v3", source, session_id, str(cwd), prompt_id]
    encoded = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_adapter(argv: list[str], cwd: Path) -> str:
    """현재 모듈과 같은 저장소의 호환성 래퍼를 프로젝트 디렉터리에서 호출한다."""
    module = Path(__file__).resolve()
    adapter = module.parents[2] / "bin/kanban-adapter"
    # wheel에는 래퍼가 없으며 HOME이나 다른 체크아웃으로 대체하지 않는다.
    if (
        module.parent.parent.name != "src"
        or adapter.resolve() != adapter
        or not adapter.is_file()
        or not os.access(adapter, os.X_OK)
    ):
        raise RuntimeError(
            f"repository-owned kanban-adapter unavailable: {adapter}; "
            "install from the repository with scripts/setup.sh"
        )
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
    fd_paths.extend(
        argument.rsplit("=", 1)[1]
        for argument in argv
        if argument.startswith("--conversation-receipt-fd=")
    )
    pass_fds = tuple(
        sorted(
            {
                int(value.rsplit("/", 1)[-1])
                for value in fd_paths
                if value.rsplit("/", 1)[-1].isdigit()
            }
        )
    )
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
    lifecycle = payload.get("lifecycle")
    if isinstance(lifecycle, dict):
        if (not isinstance(lifecycle.get("session"), str)
                or lifecycle.get("source") not in _SOURCE_LABEL
                or not isinstance(lifecycle.get("board"), str)
                or not _BOARD_RE.fullmatch(lifecycle["board"])
                or not isinstance(lifecycle.get("idempotency_key"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", lifecycle["idempotency_key"])):
            identity.close()
            raise RuntimeError("hook lifecycle correlation invalid")
        state["lifecycle"] = lifecycle
    for key, allowed in {
        "conversation_status": {"unavailable", "prepared"},
        "conversation_error": {"io-error", "invalid-data", "capture-failed"},
    }.items():
        if isinstance(payload.get(key), str) and payload[key] in allowed:
            state[key] = payload[key]
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
    observation_receipt = payload.get("observation_receipt")
    if isinstance(observation_receipt, dict) and len(json.dumps(observation_receipt)) <= 4096:
        state["observation_receipt"] = observation_receipt
    conversation_prepared = payload.get("conversation_prepared")
    if isinstance(conversation_prepared, dict) and len(json.dumps(conversation_prepared)) <= 4096:
        state["conversation_prepared"] = conversation_prepared
    baseline = clean_tokens(payload.get("token_baseline"))
    if baseline:
        state["token_baseline"] = baseline
    return state, identity


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Claude hook payload missing {key}")
    return value


def _preserve_conversation(state, prompt_id, *, source, cache) -> bool:
    """카드 상태를 버리기 전에 기존 준비의 인증된 수집 작업을 보존한다."""
    from .claude_hook_entry import report_diagnostic
    receipt = state.get("observation_receipt")
    prepared = state.get("conversation_prepared")
    if not isinstance(receipt, dict) or not isinstance(prepared, dict):
        if resolve_runtime_config() is not None:
            report_diagnostic("stop", "conversation-receipt-missing" if not isinstance(receipt, dict)
                              else "conversation-prepared-missing")
        return True
    from .conversation_runtime import get_conversation_service, seal_hook_binding

    # v2 표식이 있으면 mode 변조 뒤에도 legacy 봉인으로 내려가지 않는다.
    file_v2 = "captured_eof" in prepared or prepared.get("mode") == "claude-file-provenance-v2"
    if source == "codex" and (file_v2 or prepared.get("mode") == "codex-file-provenance-v1"):
        from .codex_pending_final import enqueue, launch, run_once
        identity_field, root = "turn_id", "codex-pending-final"
    elif file_v2 or prepared.get("mode") == "claude-absent-v1":
        from .claude_pending_final import enqueue, launch, run_once
        identity_field, root = "prompt_id", "pending-final"
    else:
        seal_hook_binding(board=str(receipt["board"]), task=state["task_id"],
                          prepared=prepared, task_receipt=receipt)
        return True
    if not isinstance(prompt_id, str) or prompt_id != prepared.get(identity_field):
        return False
    service = get_conversation_service()
    if service is None:
        return False
    try:
        # Stop도 새 요청도 terminal 근거는 아니다. 기존 인증/원본 검증을 그대로 거친다.
        job = enqueue(service, cache / root, board=str(receipt["board"]),
                      task=state["task_id"], prepared=prepared, task_receipt=receipt,
                      **{identity_field: prompt_id})
        status = run_once(service, job)
        if status == "pending":
            launch(job)
        elif status != "ready":
            return False
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        report_diagnostic("stop", "conversation-preserve-failed", exception=exc)
        return False
    return True


def _complete(
    state_path: Path,
    summary: str,
    *,
    adapter: Adapter,
    source: str = "claude-code",
    directory_fd: int,
    superseded: bool = False,
) -> None:
    read = _read_state(state_path, directory_fd=directory_fd)
    if read is None:
        return
    state, state_identity = read
    saved_result = state.get("result")
    lifecycle = state.get("lifecycle")
    if (not lifecycle or lifecycle["source"] != source
            or _state_path(state_path.parent, lifecycle["session"]) != state_path):
        state_identity.close()
        raise RuntimeError("hook lifecycle routing authority unavailable")
    board = lifecycle["board"]
    receipt = state.get("observation_receipt")
    if superseded and isinstance(receipt, dict):
        from .conversation_runtime import get_conversation_service

        try:
            service = get_conversation_service()
            final = service.get_hook_final(
                board=board, task=state["task_id"], task_receipt=receipt,
            ) if service is not None else None
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            final = None
        if final is not None:
            # 인증된 A final은 실패한 완료 시도에 저장된 fallback보다 우선한다.
            summary, saved_result = final, None
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
            usage_at=int(time.time()),
            usage_timing="completion",
        )
        for attempt in range(3):
            try:
                adapter(
                    [
                        "update", "--board", board, "--task", state["task_id"], "--message", message,
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
                "done", "--board", board, "--task", state["task_id"],
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
        if existing is not None and "lifecycle" not in existing[0]:
            from .legacy_hook_state import quarantine_legacy_state

            prepared = existing[0].get("conversation_prepared", {})
            if (isinstance(payload.get("prompt_id"), str)
                    and prepared.get("prompt_id") == payload["prompt_id"]):
                existing[1].close()
                return  # 알려진 구형 요청의 재전달은 독립된 새 프롬프트가 아니다.
            # 구형 카드의 board를 추측하지 않는다. 정확한 private 증거만 격리한다.
            quarantine_legacy_state(state_path, existing[1], directory_fd=directory_fd)
            existing = None
        if existing is not None:
            existing[1].close()
            lifecycle = existing[0].get("lifecycle", {})
            if lifecycle.get("idempotency_key") == _create_idempotency_key(
                source, session_id, cwd, prompt, payload.get("prompt_id")
            ):
                return
            prepared = existing[0].get("conversation_prepared", {})
            if (prepared.get("mode") == "claude-absent-v1"
                    and prepared.get("prompt_id") == payload.get("prompt_id")):
                return  # 프롬프트 재전달은 파일 부재 권한 근거를 EOF로 대체할 수 없다
            if lifecycle.get("source") != source or lifecycle.get("session") != session_id:
                raise RuntimeError("hook lifecycle routing authority unavailable")
            # 새 요청 B의 ID/경로/본문으로 A의 수집 권한을 대체하지 않는다.
            if not _preserve_conversation(existing[0], lifecycle.get("prompt_id"),
                                          source=source, cache=cache):
                return
            _complete(
                state_path,
                "Superseded by a new user prompt after a missing Stop event",
                adapter=adapter,
                source=source,
                directory_fd=directory_fd,
                superseded=True,
            )
        title = " ".join(prompt.split())[:120]
        try:
            board = HermesCliBackend().resolve_board(cwd=cwd)
        except BoardNotMappedError:
            board = os.environ.get("HERMES_KANBAN_BOARD")
        if not isinstance(board, str) or not _BOARD_RE.fullmatch(board):
            raise RuntimeError("hook lifecycle board unavailable")
        title_identity = create_anonymous_text(
            state_path.parent, title, label="title", directory_fd=directory_fd
        )
        validate_directory(cache, directory_fd)
        receipt_read_fd: int | None = None
        receipt_write_fd: int | None = None
        command = [
            "start", "--board", board,
            "--title-file",
            f"/dev/fd/{title_identity.file_fd}",
            "--source", source,
            "--idempotency-key",
            _create_idempotency_key(source, session_id, cwd, prompt, payload.get("prompt_id")),
        ]
        if resolve_runtime_config() is not None:
            receipt_read_fd, receipt_write_fd = os.pipe()
            command.append(f"--conversation-receipt-fd={receipt_write_fd}")
        try:
            output = adapter(command, cwd).strip()
        except BaseException:
            if receipt_read_fd is not None:
                os.close(receipt_read_fd)
            raise
        finally:
            title_identity.close()
            if receipt_write_fd is not None:
                os.close(receipt_write_fd)
        if not _TASK_RE.fullmatch(output):
            if receipt_read_fd is not None:
                os.close(receipt_read_fd)
            raise RuntimeError("kanban-adapter returned an invalid task id")
        new_state: dict[str, Any] = {"cwd": str(cwd), "task_id": output}
        # start는 기존 작업을 반환할 수 있다. 추적은 생성 소유권의 근거가 아니다.
        new_state["lifecycle"] = {
            "session": session_id, "source": source, "board": board,
            "idempotency_key": command[command.index("--idempotency-key") + 1],
            "prompt_id": payload.get("prompt_id") if isinstance(payload.get("prompt_id"), str) else None,
        }
        new_state["conversation_status"] = "unavailable"
        try:
            state_identity = _write_state(state_path, new_state, directory_fd=directory_fd)
        except BaseException:
            if receipt_read_fd is not None:
                os.close(receipt_read_fd)
            raise
        diagnostic_stage = "conversation-receipt-failed"
        try:
            if receipt_read_fd is not None:
                try:
                    receipt_raw = os.read(receipt_read_fd, 4097)
                finally:
                    os.close(receipt_read_fd)
                if not receipt_raw or len(receipt_raw) > 4096:
                    raise RuntimeError("Hermes observation receipt was missing or oversized")
                receipt = json.loads(receipt_raw)
                if (not isinstance(receipt, dict) or receipt.get("task") != output
                        or receipt.get("board") != board):
                    raise RuntimeError("Hermes observation receipt scope is invalid")
                new_state["observation_receipt"] = receipt
            diagnostic_stage = "conversation-prepare-failed"
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
                    # 명시적으로 enable된 대화 수집의 private source candidate만
                    # token telemetry와 독립적으로 보존한다.
                    if resolve_runtime_config() is not None:
                        new_state["transcript_path"] = transcript_path
                else:
                    new_state["transcript_path"] = transcript_path
                    new_state["token_baseline"] = baseline
            if (
                isinstance(new_state.get("observation_receipt"), dict)
                and isinstance(new_state.get("transcript_path"), str)
            ):
                from .conversation_runtime import capture_hook_start

                receipt = new_state["observation_receipt"]
                try:
                    if source == "codex" and payload.get("prompt_id") is not None:
                        # 네이티브 시작은 hook보다 빠르고 요청은 늦다. EOF로 요청을 추측하지 않는다.
                        from .codex_file_provenance import prepare
                        from .conversation_runtime import get_conversation_service
                        service = get_conversation_service()
                        prepared = None if service is None else prepare(
                            service, board=str(receipt["board"]), task=output,
                            session=session_id, source_path=Path(new_state["transcript_path"]),
                            task_receipt=receipt, turn_id=payload["prompt_id"],
                        )
                    else:
                        prepared = capture_hook_start(
                            board=str(receipt["board"]),
                            task=output,
                            provider="claude" if source == "claude-code" else source,
                            session=session_id,
                            source_path=Path(new_state["transcript_path"]),
                            task_receipt=receipt,
                            **({"prompt_id": payload["prompt_id"]}
                               if source == "claude-code" and payload.get("prompt_id") is not None else {}),
                        )
                except FileNotFoundError as exc:
                    from .claude_hook_entry import report_diagnostic
                    report_diagnostic("prompt", "conversation-prepare-failed", exception=exc)
                    prepared = None
                    if source == "claude-code":
                        from .claude_absent import capture
                        from .conversation_runtime import get_conversation_service

                        service = get_conversation_service()
                        if service is not None:
                            try:
                                prepared = capture(
                                    service, board=str(receipt["board"]), task=output,
                                    session=session_id, source_path=Path(new_state["transcript_path"]),
                                    task_receipt=receipt, prompt_id=payload.get("prompt_id"),
                                )
                            except (OSError, ValueError, RuntimeError) as exc:
                                report_diagnostic("prompt", "conversation-prepare-failed", exception=exc)
                if prepared is not None:
                    new_state["conversation_prepared"] = prepared
            if "conversation_prepared" in new_state:
                new_state["conversation_status"] = "prepared"
            elif resolve_runtime_config() is not None:
                from .claude_hook_entry import report_diagnostic
                report_diagnostic("prompt", "conversation-receipt-missing"
                                  if not isinstance(new_state.get("observation_receipt"), dict)
                                  else "conversation-transcript-missing"
                                  if not isinstance(new_state.get("transcript_path"), str)
                                  else "conversation-prepared-missing")
        except Exception as primary:
            from .claude_hook_entry import report_diagnostic
            report_diagnostic("prompt", diagnostic_stage, exception=primary)
            # 크기가 제한된 메타데이터만 보존하고 예외 본문이나 원본 영수증은 저장하지 않는다.
            new_state.pop("conversation_prepared", None)
            new_state.pop("observation_receipt", None)
            new_state["conversation_status"] = "unavailable"
            new_state["conversation_error"] = (
                "io-error" if isinstance(primary, OSError) else
                "invalid-data" if isinstance(primary, ValueError) else "capture-failed"
            )
            try:
                state_identity = _write_state(
                    state_path, new_state, expected_identity=state_identity,
                    directory_fd=directory_fd,
                )
            except Exception as exc:
                report_diagnostic("prompt", "conversation-recovery-failed", exception=exc)
            finally:
                state_identity.close()
            raise
        else:
            try:
                _write_state(state_path, new_state, expected_identity=state_identity,
                             directory_fd=directory_fd).close()
                validate_directory(cache, directory_fd)
            finally:
                state_identity.close()
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

    if event in {"stop", "session-end"}:
        correlated = _read_state(state_path, directory_fd=directory_fd)
        if correlated is not None:
            saved, identity = correlated
            identity.close()
            lifecycle = saved.get("lifecycle")
            if lifecycle is not None and (
                lifecycle["session"] != session_id or lifecycle["source"] != source
                # 네이티브 요청은 세션만으로 종료할 수 없으며 누락 ID를 복원하지 않는다.
                or (lifecycle.get("prompt_id") is not None
                    and (not isinstance(payload.get("prompt_id"), str)
                         or payload["prompt_id"] != lifecycle["prompt_id"]))
                or (isinstance(payload.get("prompt_id"), str)
                    and lifecycle.get("prompt_id") != payload["prompt_id"])
            ):
                return
        merge_model()
        read = _read_state(state_path, directory_fd=directory_fd)
        if read is not None:
            state, state_identity = read
            state_identity.close()
            if not _preserve_conversation(state, payload.get("prompt_id"),
                                          source=source, cache=cache):
                return

    if event == "stop":
        result = payload.get("last_assistant_message")
        if not isinstance(result, str) or not result.strip():
            result = f"{label} response completed"
        _complete(state_path, result, adapter=adapter, source=source, directory_fd=directory_fd)
        return

    if event == "session-end":
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
        if event == "prompt":
            # 이전 Stop 뒤 중단된 worker도 다음 네이티브 요청 시작에서 재개한다.
            from .pending_final_recovery import resume
            resume(cache, source)
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
    """예외 본문과 제공자 자유 형식 값을 진단 출력에 포함하지 않는다."""
    try:
        from .claude_hook_entry import report_diagnostic
        report_diagnostic(message.partition(":")[0], "collection-failed")
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
    except Exception:
        from .claude_hook_entry import report_failure
        report_failure(args[0], "collection-failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
