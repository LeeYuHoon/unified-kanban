"""Read-only, provider-specific token snapshots normalized for Kanban cards."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from .usage import clean_tokens


class TranscriptNotReady(ValueError):
    """The runtime announced a trusted path before creating its JSONL."""


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
    # ``strict=False`` still resolves every existing symlink component while
    # allowing a first-run runtime root that has not been created yet.
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


def _codex_snapshot(handle: TextIO) -> dict[str, int | None]:
    latest: Mapping[str, Any] | None = None
    requests = 0
    for line in handle:
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
        latest = total
        requests += 1
    if latest is None:
        return {}
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
        # Codex currently reports cached input reads but no cache-write bucket.
        "cache_write": None,
        "reasoning": _count(latest.get("reasoning_output_tokens")),
        "requests": requests,
        "total": _count(latest.get("total_tokens")),
    })


def token_snapshot(
    source: str,
    path: str | Path,
    *,
    root: Path | None = None,
) -> dict[str, int | None]:
    """Return cumulative canonical token counters from one runtime JSONL."""
    runtime_root = root or _default_root(source)
    with _open_runtime_jsonl(Path(path), root=runtime_root) as handle:
        if source == "claude-code":
            return _claude_snapshot(handle)
        if source == "codex":
            return _codex_snapshot(handle)
    raise ValueError(f"unsupported token source: {source}")


def token_delta(
    current: Any,
    baseline: Any,
) -> dict[str, int | None]:
    """Subtract two cumulative snapshots without double-counting a session."""
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
