from __future__ import annotations

import json
from pathlib import Path

import pytest

from kanban_adapter.token_usage import token_delta, token_snapshot


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_claude_snapshot_sums_api_calls_and_marks_reasoning_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "claude" / "projects"
    transcript = root / "project" / "session.jsonl"
    _write_jsonl(transcript, [
        {"type": "assistant", "requestId": "req-1", "message": {"usage": {
            "input_tokens": 2,
            "output_tokens": 5,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 11,
        }}},
        # Claude writes multiple content-block rows for one API response. The
        # repeated request usage must be counted once.
        {"type": "assistant", "requestId": "req-1", "message": {"usage": {
            "input_tokens": 2,
            "output_tokens": 5,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 11,
        }}},
        {"type": "progress", "usage": {"input_tokens": 999}},
        {"type": "assistant", "requestId": "req-2", "message": {"usage": {
            "input_tokens": 3,
            "output_tokens": 13,
            "cache_read_input_tokens": 17,
            "cache_creation_input_tokens": 19,
        }}},
        {"type": "assistant", "message": {"usage": {"input_tokens": "secret"}}},
    ])

    assert token_snapshot("claude-code", transcript, root=root) == {
        "input": 5,
        "output": 18,
        "cache_read": 24,
        "cache_write": 30,
        "reasoning": None,
        "requests": 2,
        "total": 77,
    }


def test_codex_snapshot_uses_latest_cumulative_event_without_double_counting(tmp_path: Path) -> None:
    root = tmp_path / "codex" / "sessions"
    rollout = root / "2026" / "rollout.jsonl"
    _write_jsonl(rollout, [
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "reasoning_output_tokens": 7,
                "total_tokens": 130,
            }
        }}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {
                "input_tokens": 250,
                "cached_input_tokens": 90,
                "output_tokens": 50,
                "reasoning_output_tokens": 11,
                "total_tokens": 320,
            }
        }}},
    ])

    assert token_snapshot("codex", rollout, root=root) == {
        "input": 160,
        "output": 50,
        "cache_read": 90,
        "cache_write": None,
        "reasoning": 11,
        "requests": 2,
        "total": 320,
    }


def test_codex_snapshot_preserves_missing_provider_buckets(tmp_path: Path) -> None:
    root = tmp_path / "codex" / "sessions"
    rollout = root / "rollout.jsonl"
    _write_jsonl(rollout, [{
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"total_tokens": 9}},
        },
    }])

    assert token_snapshot("codex", rollout, root=root) == {
        "input": None,
        "output": None,
        "cache_read": None,
        "cache_write": None,
        "reasoning": None,
        "requests": 1,
        "total": 9,
    }


def test_codex_snapshot_keeps_reported_input_when_cache_bucket_is_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex" / "sessions"
    rollout = root / "rollout.jsonl"
    _write_jsonl(rollout, [{
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
            }},
        },
    }])

    snapshot = token_snapshot("codex", rollout, root=root)
    assert snapshot["input"] == 10
    assert snapshot["cache_read"] is None
    assert snapshot["cache_write"] is None
    assert snapshot["total"] == 12


def test_token_delta_preserves_unavailable_buckets_and_recomputes_total() -> None:
    assert token_delta(
        {
            "input": 12,
            "output": 15,
            "cache_read": 20,
            "cache_write": 30,
            "reasoning": None,
            "requests": 5,
        },
        {
            "input": 2,
            "output": 5,
            "cache_read": 7,
            "cache_write": 11,
            "reasoning": None,
            "requests": 2,
        },
    ) == {
        "input": 10,
        "output": 10,
        "cache_read": 13,
        "cache_write": 19,
        "reasoning": None,
        "requests": 3,
        "total": 52,
    }


def test_token_delta_prefers_provider_total_delta() -> None:
    assert token_delta(
        {"input": 20, "output": 10, "total": 45},
        {"input": 5, "output": 3, "total": 12},
    )["total"] == 33


def test_token_snapshot_rejects_paths_outside_runtime_root_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "claude" / "projects"
    root.mkdir(parents=True)
    outside = tmp_path / "private.jsonl"
    _write_jsonl(outside, [])
    link = root / "link.jsonl"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="outside"):
        token_snapshot("claude-code", outside, root=root)
    with pytest.raises(ValueError, match="symlink"):
        token_snapshot("claude-code", link, root=root)


def test_token_snapshot_ignores_malformed_and_partial_lines(tmp_path: Path) -> None:
    root = tmp_path / "claude" / "projects"
    transcript = root / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"assistant","message":{"usage":{"input_tokens":2,"output_tokens":3}}}\n'
        '{not-json}\n'
        '{"type":"assistant"',
        encoding="utf-8",
    )
    assert token_snapshot("claude-code", transcript, root=root)["total"] == 5


def test_claude_snapshot_honors_relocated_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "relocated-claude"
    transcript = config_dir / "projects" / "project" / "session.jsonl"
    _write_jsonl(transcript, [{
        "type": "assistant",
        "requestId": "req-1",
        "message": {"usage": {"input_tokens": 2, "output_tokens": 3}},
    }])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    assert token_snapshot("claude-code", transcript)["total"] == 5


def test_codex_snapshot_honors_relocated_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "relocated-codex"
    rollout = codex_home / "sessions" / "2026" / "rollout.jsonl"
    _write_jsonl(rollout, [{
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 2, "output_tokens": 3, "total_tokens": 7,
        }}},
    }])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert token_snapshot("codex", rollout)["total"] == 7
