from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import kanban_adapter.claude_hook as hook_module
from kanban_adapter.backend import HermesCliBackend
from kanban_adapter.claude_hook import handle_event, main
from kanban_adapter.cli import main as cli_main
from kanban_adapter.usage import concise_summary, usage_event_id


@dataclass
class FakeAdapter:
    calls: list[tuple[list[str], Path]] = field(default_factory=list)
    raw_calls: list[tuple[list[str], Path]] = field(default_factory=list)
    title_contents: list[str] = field(default_factory=list)

    def __call__(self, argv: list[str], cwd: Path) -> str:
        self.raw_calls.append((argv, cwd))
        if argv[0] == "start" and "--title-file" in argv:
            path = Path(argv[argv.index("--title-file") + 1])
            self.title_contents.append(path.read_text(encoding="utf-8"))
        if argv[0] == "done" and argv[3].startswith("--result-file="):
            result = Path(argv[3].split("=", 1)[1]).read_text(encoding="utf-8")
            argv = [*argv[:3], f"--result={result}", f"--summary={concise_summary(result)}"]
        self.calls.append((argv, cwd))
        if argv[0] == "start":
            return "t_12345678\n"
        return ""


def test_create_idempotency_key_is_unambiguous_with_embedded_nuls(
    tmp_path: Path,
) -> None:
    first = hook_module._create_idempotency_key(
        "claude-code", "s", tmp_path / "p1", f"{tmp_path / 'p2'}\0work"
    )
    second = hook_module._create_idempotency_key(
        "claude-code", f"s\0{tmp_path / 'p1'}", tmp_path / "p2", "work"
    )

    assert first != second


def test_prompt_creates_card_and_stop_completes_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    prompt = {
        "session_id": "session-123",
        "cwd": str(project),
        "prompt": "테스트 인사. 안녕하세요로 답변해줘",
    }

    handle_event("prompt", prompt, adapter=adapter, cache_dir=cache)
    state_files = list(cache.glob("*.json"))
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state == {"cwd": str(project.resolve()), "task_id": "t_12345678"}
    assert adapter.calls[0][0][:2] == ["start", "--title-file"]
    assert "테스트 인사. 안녕하세요로 답변해줘" not in adapter.calls[0][0]
    assert adapter.title_contents == ["테스트 인사. 안녕하세요로 답변해줘"]
    assert adapter.calls[0][1] == project.resolve()

    handle_event(
        "stop",
        {"session_id": "session-123", "last_assistant_message": "안녕하세요! 👋"},
        adapter=adapter,
        cache_dir=cache,
    )

    assert adapter.calls[-1] == (
        [
            "done", "--task", "t_12345678",
            "--result=안녕하세요! 👋", "--summary=안녕하세요! 👋",
        ],
        project.resolve(),
    )
    assert list(cache.glob("*.json")) == []


def test_stop_keeps_full_result_separate_from_bounded_summary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    handle_event(
        "prompt",
        {"session_id": "session-full", "cwd": str(project), "prompt": "work"},
        adapter=adapter,
        cache_dir=cache,
    )
    full_result = "## 완료\n- 요약\n\n## 상세\n" + "원문 상세 내용\n" * 300

    handle_event(
        "stop",
        {"session_id": "session-full", "last_assistant_message": full_result},
        adapter=adapter,
        cache_dir=cache,
    )

    done = adapter.calls[-1][0]
    assert f"--result={full_result}" in done
    summary = next(
        arg.removeprefix("--summary=")
        for arg in done
        if arg.startswith("--summary=")
    )
    assert len(summary) <= 1_000
    assert summary != full_result


def test_result_content_never_travels_in_process_argv(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adapter = FakeAdapter()
    cache = tmp_path / "cache"
    handle_event(
        "prompt", {"session_id": "secure", "cwd": str(project), "prompt": "work"},
        adapter=adapter, cache_dir=cache,
    )
    handle_event(
        "stop", {"session_id": "secure", "last_assistant_message": "private result"},
        adapter=adapter, cache_dir=cache,
    )
    argv = adapter.raw_calls[-1][0]
    assert argv[3].startswith("--result-file=")
    assert argv[4] == "--summary=Agent result recorded"
    assert "private result" not in "\0".join(argv)


def test_session_end_retries_the_saved_full_result_after_stop_failure(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    results: list[str] = []
    done_attempts = 0

    def flaky_adapter(argv: list[str], cwd: Path) -> str:
        nonlocal done_attempts
        if argv[0] == "start":
            return "t_12345678\n"
        if argv[0] == "done":
            result_arg = next(arg for arg in argv if arg.startswith("--result-file="))
            results.append(Path(result_arg.split("=", 1)[1]).read_text(encoding="utf-8"))
            done_attempts += 1
            if done_attempts == 1:
                raise RuntimeError("transient completion failure")
        return ""

    handle_event(
        "prompt",
        {"session_id": "retry-full", "cwd": str(project), "prompt": "work"},
        adapter=flaky_adapter,
        cache_dir=cache,
    )
    full_result = "완료 요약\n\n" + "원문 상세\n" * 4_000
    with pytest.raises(RuntimeError, match="transient"):
        handle_event(
            "stop",
            {"session_id": "retry-full", "last_assistant_message": full_result},
            adapter=flaky_adapter,
            cache_dir=cache,
        )

    handle_event(
        "session-end",
        {"session_id": "retry-full", "reason": "logout"},
        adapter=flaky_adapter,
        cache_dir=cache,
    )

    assert results == [full_result, full_result]
    assert not list(cache.glob("*.json"))
    assert not list(cache.glob("*.result"))


def test_session_id_is_hashed_and_cannot_escape_cache(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    session_id = "../../outside"

    handle_event(
        "prompt",
        {"session_id": session_id, "cwd": str(project), "prompt": "safe"},
        adapter=adapter,
        cache_dir=cache,
    )

    expected = hashlib.sha256(session_id.encode()).hexdigest() + ".json"
    assert [path.name for path in cache.glob("*.json")] == [expected]
    assert not (tmp_path / "outside").exists()


def test_session_end_completes_unfinished_card(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    payload = {"session_id": "session-456", "cwd": str(project), "prompt": "work"}
    handle_event("prompt", payload, adapter=adapter, cache_dir=cache)

    handle_event(
        "session-end",
        {"session_id": "session-456", "reason": "logout"},
        adapter=adapter,
        cache_dir=cache,
    )

    assert adapter.calls[-1] == (
        [
            "done", "--task", "t_12345678",
            "--result=Claude session ended: logout",
            "--summary=Claude session ended: logout",
        ],
        project.resolve(),
    )
    assert list(cache.glob("*.json")) == []


def test_synthetic_agent_prompts_do_not_create_cards(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()

    for marker in (
        "<task-notification>worker finished</task-notification>",
        "  \n<TASK-NOTIFICATION>worker finished</TASK-NOTIFICATION>",
        "<agent-message sender_id=\"x\">done</agent-message>",
        "<teammate-message teammate_id=\"x\">idle</teammate-message>",
    ):
        handle_event(
            "prompt",
            {"session_id": marker, "cwd": str(project), "prompt": marker},
            adapter=adapter,
            cache_dir=cache,
        )

    assert adapter.calls == []
    assert list(cache.glob("*.json")) == []


def test_new_prompt_completes_card_left_by_missing_stop(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    first = {"session_id": "same", "cwd": str(project), "prompt": "first"}
    second = {"session_id": "same", "cwd": str(project), "prompt": "second"}

    handle_event("prompt", first, adapter=adapter, cache_dir=cache)
    handle_event("prompt", second, adapter=adapter, cache_dir=cache)

    assert adapter.calls[2] == (
        [
            "done", "--task", "t_12345678",
            "--result=Superseded by a new user prompt after a missing Stop event",
            "--summary=Superseded by a new user prompt after a missing Stop event",
        ],
        project.resolve(),
    )
    assert adapter.calls[3][0][:2] == ["start", "--title-file"]
    assert adapter.calls[3][0][-4:-2] == ["--source", "claude-code"]
    assert adapter.calls[3][0][-2] == "--idempotency-key"
    assert re.fullmatch(r"[0-9a-f]{64}", adapter.calls[3][0][-1])
    assert adapter.title_contents == ["first", "second"]
    assert adapter.calls[3][1] == project.resolve()
    assert len(list(cache.glob("*.json"))) == 1


def _event_id(task_id: str = "t_12345678") -> str:
    return usage_event_id("claude-code", task_id)


def _usage_message(
    usage: dict[str, dict[str, int]],
    *,
    model: str | None = None,
    task_id: str = "t_12345678",
) -> str:
    payload: dict = {
        "schema_version": 1,
        "source": "claude-code",
        "event_id": _event_id(task_id),
    }
    if model:
        payload["model"] = model
    for category in ("skills", "subagents", "mcp"):
        payload[category] = usage.get(category, {})
    # Claude keeps the shared legacy header; only other sources are renamed.
    return "Agent tool usage\n" + json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    )


def _usage_argv(
    usage: dict[str, dict[str, int]],
    *,
    model: str | None = None,
    task_id: str = "t_12345678",
) -> list[str]:
    return [
        "update", "--task", task_id,
        "--message", _usage_message(usage, model=model, task_id=task_id),
        "--idempotency-key", _event_id(task_id),
    ]


def test_claude_card_records_only_the_current_prompt_token_delta(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / ".claude" / "projects" / "project" / "session.jsonl"
    transcript.parent.mkdir(parents=True)

    def assistant_usage(input_tokens, output_tokens, cache_read, cache_write):
        return json.dumps({
            "type": "assistant",
            "message": {"usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            }},
        }) + "\n"

    transcript.write_text(assistant_usage(2, 3, 5, 7), encoding="utf-8")
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    handle_event(
        "prompt",
        {
            "session_id": "s-tokens",
            "cwd": str(project),
            "prompt": "measure this turn",
            "transcript_path": str(transcript),
        },
        adapter=adapter,
        cache_dir=cache,
    )
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(assistant_usage(11, 13, 17, 19))

    handle_event(
        "stop",
        {"session_id": "s-tokens", "last_assistant_message": "done"},
        adapter=adapter,
        cache_dir=cache,
    )

    update = next(argv for argv, _cwd in adapter.calls if argv[0] == "update")
    payload = json.loads(update[update.index("--message") + 1].split("\n", 1)[1])
    assert payload["schema_version"] == 2
    assert payload["tokens"] == {
        "input": 11,
        "output": 13,
        "cache_read": 17,
        "cache_write": 19,
        "reasoning": None,
        "requests": 1,
        "total": 60,
    }


def test_claude_new_session_collects_tokens_when_transcript_appears_after_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / ".claude" / "projects" / "project" / "new.jsonl"
    cache = tmp_path / "cache"
    adapter = FakeAdapter()

    handle_event(
        "prompt",
        {
            "session_id": "s-new-transcript",
            "cwd": str(project),
            "prompt": "measure the first request",
            "transcript_path": str(transcript),
        },
        adapter=adapter,
        cache_dir=cache,
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg-first",
                "usage": {"input_tokens": 7, "output_tokens": 5},
            },
        }) + "\n",
        encoding="utf-8",
    )

    handle_event(
        "stop",
        {"session_id": "s-new-transcript", "last_assistant_message": "done"},
        adapter=adapter,
        cache_dir=cache,
    )

    update = next(argv for argv, _cwd in adapter.calls if argv[0] == "update")
    payload = json.loads(update[update.index("--message") + 1].split("\n", 1)[1])
    assert payload["schema_version"] == 2
    assert payload["tokens"]["input"] == 7
    assert payload["tokens"]["output"] == 5
    assert payload["tokens"]["requests"] == 1


def test_post_tool_use_accumulates_and_stop_records_usage_comment(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    handle_event(
        "prompt",
        {"session_id": "s-usage", "cwd": str(project), "prompt": "build"},
        adapter=adapter,
        cache_dir=cache,
    )

    events = [
        {"tool_name": "Skill", "tool_input": {"skill": "dataviz", "args": "secret-args"}},
        {"tool_name": "Skill", "tool_input": {"skill": "dataviz"}},
        {"tool_name": "Task", "tool_input": {"subagent_type": "general-purpose", "prompt": "secret-prompt"}},
        {"tool_name": "mcp__github__search_issues", "tool_input": {"query": "secret-query"}},
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
        {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}},
    ]
    for event in events:
        handle_event(
            "post-tool-use",
            {"session_id": "s-usage", **event, "tool_response": {"out": "secret-out"}},
            adapter=adapter,
            cache_dir=cache,
        )

    state = json.loads(next(cache.glob("*.json")).read_text(encoding="utf-8"))
    assert state["usage"] == {
        "mcp": {"github/search_issues": 1},
        "skills": {"dataviz": 2},
        "subagents": {"general-purpose": 1},
    }
    assert "secret" not in json.dumps(state)

    handle_event(
        "stop",
        {"session_id": "s-usage", "last_assistant_message": "done working"},
        adapter=adapter,
        cache_dir=cache,
    )

    expected_argv = _usage_argv(
        {
            "mcp": {"github/search_issues": 1},
            "skills": {"dataviz": 2},
            "subagents": {"general-purpose": 1},
        }
    )
    assert adapter.calls[-2] == (expected_argv, project.resolve())
    assert adapter.calls[-1] == (
        [
            "done", "--task", "t_12345678",
            "--result=done working", "--summary=done working",
        ],
        project.resolve(),
    )
    assert list(cache.glob("*.json")) == []


def test_post_tool_use_without_active_card_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    adapter = FakeAdapter()

    handle_event(
        "post-tool-use",
        {"session_id": "no-card", "tool_name": "Skill", "tool_input": {"skill": "x"}},
        adapter=adapter,
        cache_dir=cache,
    )

    assert adapter.calls == []
    assert list(cache.glob("*.json")) == []


def test_malformed_post_tool_use_payloads_are_ignored(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    handle_event(
        "prompt",
        {"session_id": "s-bad", "cwd": str(project), "prompt": "work"},
        adapter=adapter,
        cache_dir=cache,
    )

    for extra in (
        {},
        {"tool_name": 42},
        {"tool_name": ["Skill"]},
        {"tool_name": "mcp__broken"},
    ):
        handle_event(
            "post-tool-use",
            {"session_id": "s-bad", **extra},
            adapter=adapter,
            cache_dir=cache,
        )
    handle_event(
        "post-tool-use",
        {"session_id": "s-bad", "tool_name": "Skill", "tool_input": "not-a-dict"},
        adapter=adapter,
        cache_dir=cache,
    )

    state = json.loads(next(cache.glob("*.json")).read_text(encoding="utf-8"))
    assert state["usage"] == {"skills": {"unknown": 1}}


def test_old_state_files_without_usage_still_complete(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    cache.chmod(0o700)
    adapter = FakeAdapter()
    state_path = cache / (
        hashlib.sha256(b"legacy").hexdigest() + ".json"
    )
    state_path.write_text(
        json.dumps({"cwd": str(project), "task_id": "t_12345678"}),
        encoding="utf-8",
    )

    handle_event(
        "stop",
        {"session_id": "legacy", "last_assistant_message": "ok"},
        adapter=adapter,
        cache_dir=cache,
    )

    assert adapter.calls == [
        (
            _usage_argv({}),
            project.resolve(),
        ),
        ([
            "done", "--task", "t_12345678", "--result=ok", "--summary=ok",
        ], project.resolve()),
    ]
    assert list(cache.glob("*.json")) == []


def test_malformed_usage_in_state_is_ignored(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    cache.chmod(0o700)
    adapter = FakeAdapter()
    state_path = cache / (hashlib.sha256(b"bad-usage").hexdigest() + ".json")
    state_path.write_text(
        json.dumps({
            "cwd": str(project),
            "task_id": "t_12345678",
            "usage": {
                "skills": "garbage",
                "surprise": {"x": 1},
                "mcp": {"good/tool": 2, "bad": "nope", "zero": 0},
            },
        }),
        encoding="utf-8",
    )

    handle_event(
        "stop",
        {"session_id": "bad-usage", "last_assistant_message": "ok"},
        adapter=adapter,
        cache_dir=cache,
    )

    assert adapter.calls == [
        (
            _usage_argv({"mcp": {"good/tool": 2}}),
            project.resolve(),
        ),
        ([
            "done", "--task", "t_12345678", "--result=ok", "--summary=ok",
        ], project.resolve()),
    ]


def test_claude_stop_summary_is_concise(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    handle_event(
        "prompt",
        {"session_id": "s-long", "cwd": str(project), "prompt": "long"},
        adapter=adapter,
        cache_dir=cache,
    )

    long_message = "결과 요약  첫 줄\n\n\n" + "word " * 400
    handle_event(
        "stop",
        {"session_id": "s-long", "last_assistant_message": long_message},
        adapter=adapter,
        cache_dir=cache,
    )

    summary = adapter.calls[-1][0][-1].removeprefix("--summary=")
    assert summary.startswith("결과 요약 첫 줄\nword")
    assert len(summary) <= 1_000
    assert summary.endswith("…")


def test_codex_stop_summary_is_concise_like_every_other_source(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    handle_event(
        "prompt",
        {"session_id": "s-codex", "cwd": str(project), "prompt": "long"},
        adapter=adapter,
        cache_dir=cache,
        source="codex",
    )

    long_message = "word " * 400
    handle_event(
        "stop",
        {"session_id": "s-codex", "last_assistant_message": long_message},
        adapter=adapter,
        cache_dir=cache,
        source="codex",
    )

    summary = adapter.calls[-1][0][-1].removeprefix("--summary=")
    assert len(summary) == 1_000
    assert summary.startswith("word word")
    assert summary.endswith("…")


def test_session_end_records_usage_comment_before_completion(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    handle_event(
        "prompt",
        {"session_id": "s-end", "cwd": str(project), "prompt": "work"},
        adapter=adapter,
        cache_dir=cache,
    )
    handle_event(
        "post-tool-use",
        {"session_id": "s-end", "tool_name": "Task", "tool_input": {"subagent_type": "Explore"}},
        adapter=adapter,
        cache_dir=cache,
    )

    handle_event(
        "session-end",
        {"session_id": "s-end", "reason": "logout"},
        adapter=adapter,
        cache_dir=cache,
    )

    assert adapter.calls[-2] == (
        _usage_argv({"subagents": {"Explore": 1}}),
        project.resolve(),
    )
    assert adapter.calls[-1] == (
        [
            "done", "--task", "t_12345678",
            "--result=Claude session ended: logout",
            "--summary=Claude session ended: logout",
        ],
        project.resolve(),
    )
    assert list(cache.glob("*.json")) == []


def test_usage_comment_failure_still_completes_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    calls: list[tuple[list[str], Path]] = []

    def flaky_adapter(argv: list[str], cwd: Path) -> str:
        calls.append((argv, cwd))
        if argv[0] == "start":
            return "t_12345678\n"
        if argv[0] == "update":
            raise RuntimeError("kanban-adapter failed (1): boom")
        return ""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    handle_event(
        "prompt",
        {"session_id": "s-flaky", "cwd": str(project), "prompt": "work"},
        adapter=flaky_adapter,
        cache_dir=cache,
    )
    handle_event(
        "post-tool-use",
        {"session_id": "s-flaky", "tool_name": "Skill", "tool_input": {"skill": "run"}},
        adapter=flaky_adapter,
        cache_dir=cache,
    )

    handle_event(
        "stop",
        {"session_id": "s-flaky", "last_assistant_message": "ok"},
        adapter=flaky_adapter,
        cache_dir=cache,
    )

    assert calls[-1][0][3].startswith("--result-file=")
    assert calls[-1][0][4] == "--summary=Agent result recorded"
    assert list(cache.glob("*.json")) == []


def test_usage_comment_transient_failure_is_retried(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    calls: list[list[str]] = []
    update_attempts = 0

    def flaky_adapter(argv: list[str], cwd: Path) -> str:
        nonlocal update_attempts
        calls.append(argv)
        if argv[0] == "start":
            return "t_12345678\n"
        if argv[0] == "update":
            update_attempts += 1
            if update_attempts == 1:
                raise RuntimeError("transient")
        return ""

    handle_event(
        "prompt", {"session_id": "s-retry-comment", "cwd": str(project), "prompt": "work"},
        adapter=flaky_adapter, cache_dir=cache,
    )
    handle_event(
        "post-tool-use",
        {"session_id": "s-retry-comment", "tool_name": "Skill", "tool_input": {"skill": "run"}},
        adapter=flaky_adapter, cache_dir=cache,
    )
    handle_event(
        "stop", {"session_id": "s-retry-comment", "last_assistant_message": "ok"},
        adapter=flaky_adapter, cache_dir=cache,
    )

    assert update_attempts == 2
    assert calls[-1][0] == "done"


def test_usage_comment_is_not_reposted_when_done_fails_then_retries(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    calls: list[tuple[list[str], Path]] = []
    done_attempts = 0

    def adapter(argv: list[str], cwd: Path) -> str:
        nonlocal done_attempts
        calls.append((argv, cwd))
        if argv[0] == "start":
            return "t_12345678\n"
        if argv[0] == "done":
            done_attempts += 1
            if done_attempts == 1:
                raise RuntimeError("kanban-adapter failed (1): boom")
        return ""

    handle_event(
        "prompt",
        {"session_id": "s-retry", "cwd": str(project), "prompt": "work"},
        adapter=adapter,
        cache_dir=cache,
    )
    handle_event(
        "post-tool-use",
        {"session_id": "s-retry", "tool_name": "Skill", "tool_input": {"skill": "run"}},
        adapter=adapter,
        cache_dir=cache,
    )

    with pytest.raises(RuntimeError, match="boom"):
        handle_event(
            "stop",
            {"session_id": "s-retry", "last_assistant_message": "ok"},
            adapter=adapter,
            cache_dir=cache,
        )
    assert len(list(cache.glob("*.json"))) == 1

    handle_event(
        "stop",
        {"session_id": "s-retry", "last_assistant_message": "ok"},
        adapter=adapter,
        cache_dir=cache,
    )

    updates = [argv for argv, _ in calls if argv[0] == "update"]
    assert updates == [_usage_argv({"skills": {"run": 1}})]
    assert calls[-1][0][3].startswith("--result-file=")
    assert calls[-1][0][4] == "--summary=Agent result recorded"
    assert list(cache.glob("*.json")) == []


def test_usage_comment_is_not_duplicated_when_the_marker_write_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """The crash window: the comment was appended, the marker never recorded.

    Local state cannot answer "did I already post?", so the retry must dedupe
    on the card itself. This runs the real adapter CLI and backend against a
    Hermes stand-in that remembers its comments, so the deterministic event id
    is what has to prevent the duplicate.
    """
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    comments: list[str] = []
    comment_keys: set[str] = set()
    failing: set[str] = {"complete"}

    def hermes(argv: list[str]) -> str:
        if argv[-3:] == ["boards", "list", "--json"]:
            return json.dumps([{"slug": "board", "default_workdir": str(project)}])
        for command in failing:
            if command in argv:
                raise RuntimeError(f"hermes {command} failed")
        if "create" in argv:
            return json.dumps(
                {"id": "t_12345678", "status": "running", "observation": True}
            )
        if "show" in argv:
            return json.dumps({"comments": [{"body": body} for body in comments]})
        if "comment" in argv:
            key = next(
                (
                    item.split("=", 1)[1]
                    for item in argv
                    if item.startswith("--idempotency-key=")
                ),
                None,
            )
            if key is None or key not in comment_keys:
                comments.append(argv[-1])
            if key is not None:
                comment_keys.add(key)
        return ""

    def adapter(argv: list[str], cwd: Path) -> str:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = cli_main(argv, backend=HermesCliBackend(runner=hermes))
        if code != 0:
            raise RuntimeError(f"kanban-adapter failed ({code})")
        return captured.getvalue()

    monkeypatch.chdir(project)
    handle_event(
        "prompt",
        {"session_id": "s-crash", "cwd": str(project), "prompt": "work"},
        adapter=adapter,
        cache_dir=cache,
    )
    handle_event(
        "post-tool-use",
        {"session_id": "s-crash", "tool_name": "Skill", "tool_input": {"skill": "run"}},
        adapter=adapter,
        cache_dir=cache,
    )

    real_write_state = hook_module._write_state

    def write_state_losing_the_marker(
        path: Path, state: dict, *, expected_identity=None, directory_fd=None
    ):
        if state.get("usage_comment_posted"):
            raise OSError("marker write failed")
        return real_write_state(
            path, state, expected_identity=expected_identity, directory_fd=directory_fd
        )

    monkeypatch.setattr(hook_module, "_write_state", write_state_losing_the_marker)
    with pytest.raises(RuntimeError, match="kanban-adapter failed"):
        handle_event(
            "stop",
            {"session_id": "s-crash", "last_assistant_message": "ok"},
            adapter=adapter,
            cache_dir=cache,
        )

    # The comment went out, but nothing on disk remembers that it did.
    assert len(comments) == 1
    state = json.loads(next(cache.glob("*.json")).read_text("utf-8"))
    assert "usage_comment_posted" not in state

    monkeypatch.setattr(hook_module, "_write_state", real_write_state)
    failing.clear()
    handle_event(
        "stop",
        {"session_id": "s-crash", "last_assistant_message": "ok"},
        adapter=adapter,
        cache_dir=cache,
    )

    assert len(comments) == 1
    assert comments[0].startswith("Agent tool usage\n")
    assert usage_event_id("claude-code", "t_12345678") in comments[0]
    assert list(cache.glob("*.json")) == []


def test_main_returns_zero_when_cache_is_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    parent = tmp_path / "kanban-adapter"
    parent.mkdir()
    (parent / "claude").symlink_to(target)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    result = main(["prompt"], stdin=io.StringIO("{}"))

    assert result == 0


def test_state_write_failure_closes_created_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adapter = FakeAdapter()

    def fail_write(path: Path, state: dict[str, str], **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(hook_module, "_write_state", fail_write)
    with pytest.raises(OSError, match="disk full"):
        handle_event(
            "prompt",
            {"session_id": "write-fail", "cwd": str(project), "prompt": "work"},
            adapter=adapter,
            cache_dir=tmp_path / "cache",
        )

    assert adapter.calls[-1] == (
        [
            "done", "--task", "t_12345678", "--summary",
            "Hook state persistence failed; card closed automatically",
        ],
        project.resolve(),
    )


def test_predictable_title_and_result_paths_are_preserved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    cache.chmod(0o700)
    session_id = "capability-paths"
    digest = hashlib.sha256(session_id.encode()).hexdigest()
    predictable_title = cache / f"{digest}.title"
    predictable_result = cache / f"{digest}.result"
    predictable_title.write_text("foreign title", encoding="utf-8")
    predictable_result.write_text("foreign result", encoding="utf-8")
    capability_paths: list[Path] = []

    def adapter(argv: list[str], cwd: Path) -> str:
        if argv[0] == "start":
            path = Path(argv[argv.index("--title-file") + 1])
            capability_paths.append(path)
            assert path.read_text(encoding="utf-8") == "work"
            return "t_12345678\n"
        if argv[0] == "done":
            option = next(arg for arg in argv if arg.startswith("--result-file="))
            path = Path(option.split("=", 1)[1])
            capability_paths.append(path)
            assert path.read_text(encoding="utf-8") == "done"
        return ""

    handle_event(
        "prompt", {"session_id": session_id, "cwd": str(project), "prompt": "work"},
        adapter=adapter, cache_dir=cache,
    )
    handle_event(
        "stop", {"session_id": session_id, "last_assistant_message": "done"},
        adapter=adapter, cache_dir=cache,
    )

    assert all(path not in {predictable_title, predictable_result} for path in capability_paths)
    assert predictable_title.read_text(encoding="utf-8") == "foreign title"
    assert predictable_result.read_text(encoding="utf-8") == "foreign result"


def test_state_mutation_replaces_the_identity_that_was_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    real_publish = hook_module.atomic_publish
    expected_receipts: list[tuple[int, int] | None] = []

    def capture_publish(path: Path, content: bytes, *, expected_identity=None, directory_fd=None):
        expected_receipts.append(expected_identity)
        return real_publish(
            path, content, expected_identity=expected_identity, directory_fd=directory_fd
        )

    monkeypatch.setattr(hook_module, "atomic_publish", capture_publish)
    handle_event(
        "prompt", {"session_id": "identity", "cwd": str(project), "prompt": "work"},
        adapter=adapter, cache_dir=cache,
    )
    handle_event(
        "post-tool-use",
        {"session_id": "identity", "tool_name": "Skill", "tool_input": {"skill": "run"}},
        adapter=adapter, cache_dir=cache,
    )

    assert expected_receipts[0] is None
    assert expected_receipts[1] is not None


def test_completion_cleanup_preserves_foreign_state_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    handle_event(
        "prompt", {"session_id": "cleanup-race", "cwd": str(project), "prompt": "work"},
        adapter=adapter, cache_dir=cache,
    )
    state_path = next(cache.glob("*.json"))
    foreign = cache / "foreign"
    foreign.write_text("foreign successor", encoding="utf-8")

    def substitute_then_fail(_receipt) -> None:
        os.replace(foreign, state_path)
        raise OSError("injected detached cleanup failure")

    monkeypatch.setattr(hook_module, "discard_detached", substitute_then_fail)
    handle_event(
        "stop", {"session_id": "cleanup-race", "last_assistant_message": "done"},
        adapter=adapter, cache_dir=cache,
    )

    assert state_path.read_text(encoding="utf-8") == "foreign successor"
    assert len([call for call in adapter.calls if call[0][0] == "done"]) == 1


def test_lock_flock_failure_does_not_leak_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    directory_fd = hook_module._ensure_cache(cache)
    before = len(os.listdir("/dev/fd"))
    monkeypatch.setattr(hook_module.fcntl, "flock", lambda *_a: (_ for _ in ()).throw(OSError("flock")))
    try:
        with pytest.raises(OSError, match="flock"):
            hook_module._open_session_lock(cache / "event.lock", directory_fd)
        assert len(os.listdir("/dev/fd")) == before
    finally:
        os.close(directory_fd)


def test_start_fails_closed_when_cache_is_replaced_after_card_creation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    retained = tmp_path / "retained-cache"
    calls: list[list[str]] = []

    def adapter(argv: list[str], _cwd: Path) -> str:
        calls.append(argv)
        if argv[0] == "start":
            cache.rename(retained)
            cache.mkdir(mode=0o700)
            return "t_12345678\n"
        return ""

    with pytest.raises(hook_module.NamespaceAuthorityError, match="canonical pathname"):
        handle_event(
            "prompt",
            {"session_id": "parent-race", "cwd": str(project), "prompt": "work"},
            adapter=adapter,
            cache_dir=cache,
        )

    assert not list(cache.iterdir())
    assert len(list(retained.glob("*.json"))) == 1
    assert [call[0] for call in calls] == ["start", "done"]


def test_error_logging_never_appends_to_foreign_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "kanban-adapter" / "claude"
    cache.mkdir(parents=True, mode=0o700)
    foreign = tmp_path / "foreign"
    foreign.write_text("foreign\n", encoding="utf-8")
    os.link(foreign, cache / "errors.log")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    hook_module.log_error("diagnostic")

    assert foreign.read_text(encoding="utf-8") == "foreign\n"


def test_repeated_failed_completion_does_not_leak_receipt_fds(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    base = FakeAdapter()
    handle_event(
        "prompt",
        {"session_id": "fd-retry", "cwd": str(project), "prompt": "work"},
        adapter=base,
        cache_dir=cache,
    )

    def failing_adapter(argv: list[str], cwd: Path) -> str:
        if argv[0] == "done":
            raise RuntimeError("completion failed")
        return base(argv, cwd)

    before = len(os.listdir("/dev/fd"))
    for _ in range(3):
        with pytest.raises(RuntimeError, match="completion failed"):
            handle_event(
                "stop",
                {"session_id": "fd-retry", "last_assistant_message": "done"},
                adapter=failing_adapter,
                cache_dir=cache,
            )
        assert len(list(cache.glob("*.json"))) == 1
    assert len(os.listdir("/dev/fd")) == before


def test_successful_completion_cleanup_failure_cannot_retry_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    handle_event(
        "prompt",
        {"session_id": "no-retry", "cwd": str(project), "prompt": "work"},
        adapter=adapter,
        cache_dir=cache,
    )

    def close_then_fail(receipt) -> None:
        receipt.close()
        raise OSError("discard failed")

    monkeypatch.setattr(hook_module, "discard_detached", close_then_fail)
    payload = {"session_id": "no-retry", "last_assistant_message": "done"}
    handle_event("stop", payload, adapter=adapter, cache_dir=cache)
    handle_event("stop", payload, adapter=adapter, cache_dir=cache)

    assert not list(cache.glob("*.json"))
    assert len([call for call in adapter.calls if call[0][0] == "done"]) == 1


def test_parent_race_and_failed_compensation_retry_reuses_create_key(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    retained = tmp_path / "retained-cache"
    starts: list[str] = []
    created: dict[str, str] = {}
    first = True

    def adapter(argv: list[str], _cwd: Path) -> str:
        nonlocal first
        if argv[0] == "start":
            key = argv[argv.index("--idempotency-key") + 1]
            starts.append(key)
            task = created.setdefault(key, "t_12345678")
            if first:
                first = False
                cache.rename(retained)
                cache.mkdir(mode=0o700)
            return task + "\n"
        if argv[0] == "done" and len(starts) == 1:
            raise RuntimeError("compensation failed")
        return ""

    payload = {"session_id": "retry", "cwd": str(project), "prompt": "work"}
    with pytest.raises(hook_module.NamespaceAuthorityError):
        handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    handle_event("prompt", payload, adapter=adapter, cache_dir=cache)

    assert starts[0] == starts[1]
    assert len(created) == 1
    assert len(list(cache.glob("*.json"))) == 1


def test_result_fd_survives_cache_parent_rename_before_external_read(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    retained = tmp_path / "retained-cache"
    base = FakeAdapter()
    handle_event(
        "prompt",
        {"session_id": "fd-parent", "cwd": str(project), "prompt": "work"},
        adapter=base,
        cache_dir=cache,
    )

    def adapter(argv: list[str], cwd: Path) -> str:
        if argv[0] == "done":
            cache.rename(retained)
            cache.mkdir(mode=0o700)
        return base(argv, cwd)

    handle_event(
        "stop",
        {"session_id": "fd-parent", "last_assistant_message": "done"},
        adapter=adapter,
        cache_dir=cache,
    )

    assert base.calls[-1][0][3] == "--result=done"
    assert not list(cache.glob("*.json"))
