"""Kanban 카드용으로 정규화된, 읽기 전용의 제공자별 토큰 스냅샷."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from .usage import _TOKEN_COUNT_LIMIT, clean_tokens


class TranscriptNotReady(ValueError):
    """런타임이 JSONL을 생성하기 전에 신뢰된 경로를 먼저 알렸다."""


# 설치된 managed Stop 훅 제한은 30초다. 게시/완료 처리 여유를 남기고 실제로 관측된
# 4.412초 지연을 흡수하도록 수집기 대기를 6초로 제한한다.
CODEX_EVIDENCE_WAIT_SECONDS = 6.0
_CodexEvidenceStatus = Literal["ready", "legacy", "empty", "pending", "malformed"]


def _default_root(source: str) -> Path:
    if source == "claude-code":
        config_root = os.environ.get("CLAUDE_CONFIG_DIR")
        return (Path(config_root).expanduser() if config_root else Path.home() / ".claude") / "projects"
    if source == "codex":
        codex_home = os.environ.get("CODEX_HOME")
        return (Path(codex_home).expanduser() if codex_home else Path.home() / ".codex") / "sessions"
    raise ValueError(f"unsupported token source: {source}")


def _open_runtime_jsonl(path: Path, *, root: Path) -> TextIO:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ValueError("token transcript path must be absolute")
    # ``strict=False``는 존재하는 모든 심볼릭 링크 구성 요소를 여전히 해석하면서도,
    # 아직 생성되지 않은 첫 실행 런타임 루트를 허용한다.
    resolved_root = root.expanduser().resolve(strict=False)
    try:
        before = os.lstat(candidate)
    except FileNotFoundError as exc:
        try:
            candidate.resolve(strict=False).relative_to(resolved_root)
        except ValueError as outside:
            raise ValueError("token transcript is outside the runtime root") from outside
        raise TranscriptNotReady("token transcript does not exist") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ValueError("token transcript must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("token transcript must be a regular file")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("token transcript is outside the runtime root") from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(candidate, flags)
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        os.close(fd)
        raise RuntimeError("token transcript changed during open")
    return os.fdopen(fd, "r", encoding="utf-8")


def _count(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _claude_snapshot(handle: TextIO) -> dict[str, int | None]:
    totals = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": None,
        "requests": 0,
    }
    seen_requests: set[str] = set()
    for line in handle:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(row, Mapping) or row.get("type") != "assistant":
            continue
        message = row.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        request_id = row.get("requestId")
        if not isinstance(request_id, str) and isinstance(message, Mapping):
            request_id = message.get("id")
        if isinstance(request_id, str):
            if request_id in seen_requests:
                continue
            seen_requests.add(request_id)
        values = {
            "input": _count(usage.get("input_tokens")),
            "output": _count(usage.get("output_tokens")),
            "cache_read": _count(usage.get("cache_read_input_tokens")),
            "cache_write": _count(usage.get("cache_creation_input_tokens")),
        }
        if not any(value is not None for value in values.values()):
            continue
        for field, value in values.items():
            if value is not None:
                totals[field] += value  # type: ignore[operator]
        totals["requests"] += 1  # type: ignore[operator]
    return clean_tokens(totals)


def _codex_snapshot(
    handle: TextIO,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int | None]:
    latest: Mapping[str, Any] | None = None
    requests = 0
    bytes_read = 0
    while True:
        if deadline is not None and clock() >= deadline:
            raise TimeoutError("Codex token snapshot deadline exceeded")
        line = handle.readline(262145)
        if not line:
            break
        bytes_read += len(line.encode("utf-8"))
        if len(line.encode("utf-8")) > 262144 or bytes_read > 16 * 1024 * 1024:
            raise ValueError("Codex token snapshot exceeds read budget")
        if deadline is not None and clock() >= deadline:
            raise TimeoutError("Codex token snapshot deadline exceeded")
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(row, Mapping):
            continue
        payload = row.get("payload")
        if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        total = info.get("total_token_usage") if isinstance(info, Mapping) else None
        if not isinstance(total, Mapping):
            continue
        if total == latest:
            continue
        latest = total
        requests += 1
    if latest is None:
        return {}
    return _codex_tokens(latest, requests=requests)


def _codex_tokens(latest: Mapping[str, Any], *, requests: int) -> dict[str, int | None]:
    input_with_cache = _count(latest.get("input_tokens"))
    cache_read = _count(latest.get("cached_input_tokens"))
    uncached_input = (
        max(input_with_cache - cache_read, 0)
        if input_with_cache is not None and cache_read is not None
        else input_with_cache
    )
    reported_cache_read = (
        min(cache_read, input_with_cache)
        if cache_read is not None and input_with_cache is not None
        else cache_read
    )
    return clean_tokens({
        "input": uncached_input,
        "output": _count(latest.get("output_tokens")),
        "cache_read": reported_cache_read,
        "cache_write": _count(latest.get("cache_write_input_tokens")),
        "reasoning": _count(latest.get("reasoning_output_tokens")),
        "requests": requests,
        "total": _count(latest.get("total_tokens")),
    })


def _valid_codex_cumulative_usage(value: Any) -> bool:
    """구형 누적 행이 실제 토큰 증거로 사용할 수 있는 정식 형태인지 검사한다."""
    if not isinstance(value, Mapping):
        return False
    fields = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "reasoning_output_tokens", "total_tokens")
    if any(item is not None and (type(item) is not int or not 0 <= item <= _TOKEN_COUNT_LIMIT)
           for field in fields for item in [value.get(field)]):
        return False
    if not any(type(value.get(field)) is int
               for field in ("input_tokens", "output_tokens", "total_tokens")):
        return False
    return not (
        type(value.get("input_tokens")) is int
        and type(value.get("cached_input_tokens")) is int
        and value["cached_input_tokens"] > value["input_tokens"]
    )


def _inspect_codex_token_events(
    path: str | Path,
    *,
    session: str,
    turn: str,
    root: Path | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[_CodexEvidenceStatus, list[dict[str, Any]] | None, dict[str, int | None] | None]:
    if any(not isinstance(value, str) or not value or len(value) > 4096
           for value in (session, turn)):
        return "malformed", [], None

    def source_time(row):
        with suppress(KeyError, TypeError, ValueError, AttributeError, OverflowError):
            value = datetime.fromisoformat(row["timestamp"])
            if value.tzinfo is not None and 0 <= value.timestamp() < 253402214400:
                return value
        return None

    events = []
    active = False
    matched_session = False
    started_at = None
    completed_at = None
    target_started = False
    target_completed = False
    event_times = []
    target_native = False
    target_legacy = False
    boundaries = False
    unscoped_native = False
    unscoped_legacy = False
    cumulative_latest: Mapping[str, Any] | None = None
    cumulative_requests = 0
    seen = {}
    bytes_read = 0
    if deadline is not None and clock() >= deadline:
        return "pending", [], None
    with _open_runtime_jsonl(Path(path), root=root or _default_root("codex")) as handle:
        while True:
            if deadline is not None and clock() >= deadline:
                return "pending", [], None
            try:
                line = handle.readline(262145)
                size = len(line.encode("utf-8"))
            except UnicodeError:
                return "malformed", [], None
            if not line:
                break
            bytes_read += size
            if size > 262144 or bytes_read > 16 * 1024 * 1024:
                return "malformed", [], None
            if deadline is not None and clock() >= deadline:
                return "pending", [], None
            if not line.endswith("\n"):
                return "pending", [], None
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                return "malformed", [], None
            if not isinstance(row, dict):
                return "malformed", [], None
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if row.get("type") == "session_meta":
                if payload.get("id") != session:
                    return "malformed", [], None
                matched_session = True
            if row.get("type") == "event_msg":
                kind = payload.get("type")
                if kind == "task_started":
                    boundaries = True
                    if active:
                        return "malformed", [], None
                    active = payload.get("turn_id") == turn
                    if active:
                        target_started = True
                        started_at = source_time(row)
                elif kind == "task_complete":
                    boundaries = True
                    if payload.get("turn_id") == turn:
                        target_completed = True
                        completed_at = source_time(row)
                        break
                elif kind == "token_count":
                    info = payload.get("info")
                    total = info.get("total_token_usage") if isinstance(info, Mapping) else None
                    valid = _valid_codex_cumulative_usage(total)
                    if active:
                        if not valid:
                            return "malformed", [], None
                        target_legacy = True
                    elif not boundaries:
                        if not valid:
                            return "malformed", [], None
                        unscoped_legacy = True
                    if valid and total != cumulative_latest:
                        cumulative_latest = total
                        cumulative_requests += 1

            if row.get("type") == "token_usage_record":
                if active:
                    target_native = True
                elif not boundaries:
                    unscoped_native = True
            if row.get("type") != "token_usage_record" or not active or not matched_session:
                continue
            if (payload.get("session_id") != session or payload.get("thread_id") != session
                    or payload.get("turn_id") != turn or payload.get("root_turn_id") != turn):
                continue
            request = payload.get("response_id")
            usage = payload.get("usage")
            if (not isinstance(request, str) or not request or len(request) > 4096
                    or not isinstance(usage, dict)):
                return "malformed", [], None
            timestamp = source_time(row)
            if timestamp is None or started_at is None or timestamp < started_at:
                return "malformed", [], None
            fields = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                      "output_tokens", "reasoning_output_tokens", "total_tokens")
            if any(value is not None and (type(value) is not int or not 0 <= value <= _TOKEN_COUNT_LIMIT)
                   for field in fields for value in [usage.get(field)]):
                return "malformed", [], None
            if not any(type(usage.get(field)) is int for field in ("input_tokens", "output_tokens")):
                return "malformed", [], None
            if (type(usage.get("input_tokens")) is int and type(usage.get("cached_input_tokens")) is int
                    and usage["cached_input_tokens"] > usage["input_tokens"]):
                return "malformed", [], None
            at = int(timestamp.timestamp())
            digest = hashlib.sha256(json.dumps(
                ["codex", session, turn, request], separators=(",", ":")
            ).encode()).hexdigest()[:16]
            event = {"request_hash": digest, "usage_at": at,
                     "source_timestamp": row["timestamp"],
                     "tokens": _codex_tokens(usage, requests=1)}
            prior = seen.get(digest)
            if prior is not None:
                if prior != event:
                    return "malformed", [], None
                continue
            if len(events) >= 128:
                return "malformed", [], None
            seen[digest] = event
            events.append(event)
            event_times.append(timestamp)
    if not target_completed:
        if not unscoped_native and not boundaries and unscoped_legacy and cumulative_latest is not None:
            return "legacy", None, _codex_tokens(cumulative_latest, requests=cumulative_requests)
        return "pending", [], None
    if not target_started:
        return "malformed", [], None
    if events and (not matched_session or started_at is None or completed_at is None
                   or completed_at < started_at or any(at > completed_at for at in event_times)):
        return "malformed", [], None
    if events:
        return "ready", events, None
    if target_native:
        return "malformed", [], None
    if target_legacy and cumulative_latest is not None:
        return "legacy", None, _codex_tokens(cumulative_latest, requests=cumulative_requests)
    return "empty", [], None


def codex_token_events(
    path: str | Path, *, session: str, turn: str, root: Path | None = None,
) -> list[dict[str, Any]] | None:
    """턴이나 스레드의 누적 카운터가 아닌 요청별 사용량을 읽는다.

    None은 네이티브 경계 없이 기존 카운터의 증거만 온전히 있는 경우를 뜻한다.
    []는 증거가 불완전하거나 잘못되었거나 형식을 알 수 없거나 자원 한도를 초과했음을 뜻한다.
    이 경우 누적 스냅샷으로 대체하면 안 된다.
    """
    _status, events, _snapshot = _inspect_codex_token_events(
        path, session=session, turn=turn, root=root,
    )
    return events


def wait_for_codex_token_evidence(
    path: str | Path,
    *,
    session: str,
    turn: str,
    root: Path | None = None,
    timeout: float = CODEX_EVIDENCE_WAIT_SECONDS,
    interval: float = 0.1,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
) -> tuple[_CodexEvidenceStatus, list[dict[str, Any]] | None, dict[str, int | None] | None]:
    """불완전한 원본만 제한 시간 동안 다시 읽고, malformed 증거는 즉시 거부한다."""
    if not math.isfinite(timeout) or not math.isfinite(interval):
        return "malformed", [], None
    budget = min(max(timeout, 0.0), CODEX_EVIDENCE_WAIT_SECONDS)
    poll_interval = min(max(interval, 0.001), budget or 0.001)
    budget_deadline = clock() + budget
    deadline = min(deadline, budget_deadline) if deadline is not None else budget_deadline
    while True:
        try:
            status, events, snapshot = _inspect_codex_token_events(
                path, session=session, turn=turn, root=root,
                deadline=deadline, clock=clock,
            )
        except TranscriptNotReady:
            status, events, snapshot = "pending", [], None
        remaining = deadline - clock()
        if remaining <= 0:
            return "pending", [], None
        if status != "pending":
            return status, events, snapshot
        sleeper(min(poll_interval, remaining))


def wait_for_codex_token_events(
    path: str | Path,
    *,
    session: str,
    turn: str,
    root: Path | None = None,
    timeout: float = CODEX_EVIDENCE_WAIT_SECONDS,
    interval: float = 0.1,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
) -> list[dict[str, Any]] | None:
    """기존 호출자를 위해 증거 상태에서 요청 목록 계약만 돌려준다."""
    _status, events, _snapshot = wait_for_codex_token_evidence(
        path, session=session, turn=turn, root=root, timeout=timeout, interval=interval,
        sleeper=sleeper, clock=clock, deadline=deadline,
    )
    return events


def token_snapshot(
    source: str,
    path: str | Path,
    *,
    root: Path | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int | None]:
    """런타임 JSONL 하나에서 누적된 정식 토큰 카운터를 반환한다."""
    runtime_root = root or _default_root(source)
    with _open_runtime_jsonl(Path(path), root=runtime_root) as handle:
        if source == "claude-code":
            return _claude_snapshot(handle)
        if source == "codex":
            return _codex_snapshot(handle, deadline=deadline, clock=clock)
    raise ValueError(f"unsupported token source: {source}")


def token_delta(
    current: Any,
    baseline: Any,
) -> dict[str, int | None]:
    """세션을 이중 계산하지 않고 두 누적 스냅샷의 차를 구한다."""
    after = clean_tokens(current)
    before = clean_tokens(baseline)
    result: dict[str, int | None] = {}
    for field in ("input", "output", "cache_read", "cache_write", "reasoning", "requests"):
        value = after.get(field)
        if value is None:
            if field in after:
                result[field] = None
            continue
        prior = before.get(field)
        result[field] = max(value - (prior if isinstance(prior, int) else 0), 0)
    total = after.get("total")
    if isinstance(total, int):
        prior_total = before.get("total")
        result["total"] = max(
            total - (prior_total if isinstance(prior_total, int) else 0), 0
        )
    return clean_tokens(result)
