from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path

from kanban_adapter.claude_hook import handle_event
from kanban_adapter.codex_hook import main, normalize_payload
from kanban_adapter.usage import concise_summary, usage_event_id


@dataclass
class FakeAdapter:
    fail: set[str] = field(default_factory=set)
    calls: list[tuple[list[str], Path]] = field(default_factory=list)

    def __call__(self, argv: list[str], cwd: Path) -> str:
        if argv[0] == "done" and argv[3].startswith("--result-file="):
            result = Path(argv[3].split("=", 1)[1]).read_text(encoding="utf-8")
            argv = [*argv[:3], f"--result={result}", f"--summary={concise_summary(result)}"]
        self.calls.append((argv, cwd))
        if argv[0] in self.fail:
            raise RuntimeError(f"adapter {argv[0]} failed")
        return "t_abcdef12\n" if argv[0] == "start" else ""


def codex_event(event: str, payload: dict, adapter, cache: Path) -> None:
    handle_event(
        event,
        normalize_payload(payload),
        adapter=adapter,
        cache_dir=cache,
        source="codex",
    )


def start_card(adapter, cache: Path, project: Path) -> None:
    codex_event(
        "prompt",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "cx-1",
            "turn_id": "turn-1",
            "cwd": str(project),
            "prompt": "Refactor the checkout flow",
            "model": "gpt-5.6-sol",
            "permission_mode": "default",
            "transcript_path": "/secret/rollout.jsonl",
        },
        adapter,
        cache,
    )


def usage_payload(adapter, *, task_id: str = "t_abcdef12") -> dict:
    """이곳에서 외피를 검증한 단일 사용량 주석의 페이로드.

    Codex 카드는 Codex 전용 헤더, 스키마 버전, 어댑터가 중복 제거 기준으로
    전달받은 이벤트 ID를 담는다. 따라서 아래의 모든 호출자는 사용량 데이터
    자체만 계속 검증한다.
    """
    updates = [argv for argv, _cwd in adapter.calls if argv[0] == "update"]
    assert len(updates) == 1, updates
    argv = updates[0]
    header, newline, body = argv[argv.index("--message") + 1].partition("\n")
    assert header == "Codex tool usage"
    assert newline == "\n"
    payload = json.loads(body)
    expected_version = 2 if "tokens" in payload else 1
    assert payload.pop("schema_version") == expected_version
    event_id = payload.pop("event_id")
    assert event_id == usage_event_id("codex", task_id)
    assert argv[argv.index("--idempotency-key") + 1] == event_id
    return payload


def test_normalize_payload_passes_through_native_codex_fields() -> None:
    normalized = normalize_payload({
        "hook_event_name": "PostToolUse",
        "session_id": "cx-1",
        "cwd": "/work",
        "tool_name": "mcp__github__search_issues",
        "tool_input": {"query": "secret"},
        "tool_response": {"body": "secret"},
        "model": "gpt-5.6-sol",
        "agent_type": "reviewer",
        "reason": "other",
    })
    assert normalized["tool_name"] == "mcp__github__search_issues"
    assert normalized["tool_input"] == {"query": "secret"}
    assert normalized["model"] == "gpt-5.6-sol"
    assert normalized["agent_type"] == "reviewer"
    assert normalized["reason"] == "other"


def test_normalize_payload_keeps_legacy_aliases() -> None:
    normalized = normalize_payload({
        "sessionID": "cx-1", "directory": "/work", "input": "hi", "result": "bye",
    })
    assert normalized["session_id"] == "cx-1"
    assert normalized["cwd"] == "/work"
    assert normalized["prompt"] == "hi"
    assert normalized["last_assistant_message"] == "bye"
    assert normalized["tool_name"] is None


def test_codex_card_records_cumulative_token_delta(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    project = tmp_path / "project"
    project.mkdir()
    rollout = tmp_path / ".codex" / "sessions" / "2026" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)

    def token_event(input_tokens, cached, output, reasoning):
        return json.dumps({
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": input_tokens + output,
                }
            }},
        }) + "\n"

    rollout.write_text(token_event(100, 40, 20, 7), encoding="utf-8")
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    codex_event(
        "prompt",
        {
            "session_id": "cx-1",
            "cwd": str(project),
            "prompt": "measure this turn",
            "transcript_path": str(rollout),
        },
        adapter,
        cache,
    )
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(token_event(250, 90, 50, 11))

    codex_event(
        "stop",
        {"session_id": "cx-1", "last_assistant_message": "done"},
        adapter,
        cache,
    )

    assert usage_payload(adapter)["tokens"] == {
        "input": 100,
        "output": 30,
        "cache_read": 50,
        "cache_write": None,
        "reasoning": 4,
        "requests": 1,
        "total": 180,
    }


def test_post_tool_use_records_mcp_and_subagent_start_records_agents(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    start_card(adapter, cache, project)

    for tool in ("mcp__github__search_issues", "mcp__github__search_issues",
                 "exec_command", "apply_patch", "spawn_agent"):
        codex_event(
            "post-tool-use",
            {
                "hook_event_name": "PostToolUse", "session_id": "cx-1",
                "tool_name": tool, "tool_input": {"command": "secret-command"},
                "tool_response": {"output": "secret-output"},
                "model": "gpt-5.6-sol",
            },
            adapter,
            cache,
        )
    for agent_type in ("reviewer", "reviewer", "explorer"):
        codex_event(
            "subagent-start",
            {
                "hook_event_name": "SubagentStart", "session_id": "cx-1",
                "agent_id": "a-1", "agent_type": agent_type,
                "model": "gpt-5.6-sol",
            },
            adapter,
            cache,
        )

    state = json.loads(next(cache.glob("*.json")).read_text("utf-8"))
    assert state["usage"] == {
        "mcp": {"github/search_issues": 2},
        "subagents": {"reviewer": 2, "explorer": 1},
    }
    assert state["model"] == "gpt-5.6-sol"
    assert "secret" not in json.dumps(state, ensure_ascii=False)

    codex_event(
        "stop",
        {
            "hook_event_name": "Stop", "session_id": "cx-1",
            "last_assistant_message": "Checkout refactored.",
            "model": "gpt-5.6-sol", "stop_hook_active": False,
        },
        adapter,
        cache,
    )
    assert usage_payload(adapter) == {
        "source": "codex",
        "model": "gpt-5.6-sol",
        "skills": {},
        "subagents": {"reviewer": 2, "explorer": 1},
        "mcp": {"github/search_issues": 2},
        "unavailable": ["skills"],
    }
    assert adapter.calls[-1][0] == [
        "done", "--task", "t_abcdef12",
        "--result=Checkout refactored.", "--summary=Checkout refactored.",
    ]


def test_codex_card_marks_skills_unavailable_even_with_no_tool_use(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    start_card(adapter, cache, project)
    codex_event(
        "stop",
        {"session_id": "cx-1", "last_assistant_message": "done", "model": "gpt-5.6-sol"},
        adapter,
        cache,
    )
    payload = usage_payload(adapter)
    assert payload["unavailable"] == ["skills"]
    assert payload["skills"] == {}
    assert payload["source"] == "codex"


def test_codex_usage_comment_is_not_reposted_on_retry(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter(fail={"done"})
    start_card(adapter, cache, project)
    codex_event(
        "post-tool-use",
        {"session_id": "cx-1", "tool_name": "mcp__linear__get_issue", "tool_input": {}},
        adapter,
        cache,
    )
    for _ in range(3):
        try:
            codex_event(
                "stop", {"session_id": "cx-1", "last_assistant_message": "x"},
                adapter, cache,
            )
        except RuntimeError:
            pass

    assert usage_payload(adapter)["mcp"] == {"linear/get_issue": 1}


def test_codex_session_end_closes_card_with_codex_wording(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    start_card(adapter, cache, project)
    codex_event(
        "session-end",
        {"hook_event_name": "SessionEnd", "session_id": "cx-1", "reason": "other"},
        adapter,
        cache,
    )
    assert adapter.calls[-1][0][-1] == "--summary=Codex session ended: other"
    assert not list(cache.glob("*.json"))


def test_codex_summary_is_concise_and_bounded(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    start_card(adapter, cache, project)
    codex_event(
        "stop",
        {"session_id": "cx-1", "last_assistant_message": "  Summary.  \n\n" + "y" * 1_500},
        adapter,
        cache,
    )
    summary = adapter.calls[-1][0][-1].removeprefix("--summary=")
    assert len(summary) == 1_000
    assert summary.startswith("Summary.")
    assert summary.endswith("…")


def test_cli_accepts_every_wired_codex_event() -> None:
    for event in ("prompt", "post-tool-use", "subagent-start", "stop", "session-end"):
        assert main([event], stdin=io.StringIO("{}")) == 0
    assert main(["nonsense"], stdin=io.StringIO("{}")) == 2
    assert main([], stdin=io.StringIO("{}")) == 2


def test_summaries_starting_with_a_dash_are_option_safe(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    start_card(adapter, cache, project)
    codex_event(
        "stop", {"session_id": "cx-1", "last_assistant_message": "--done\nmore"},
        adapter, cache,
    )
    argv = adapter.calls[-1][0]
    assert argv[0] == "done"
    assert "--summary" not in argv
    assert argv[-1] == "--summary=--done\nmore"


def test_session_end_merges_the_enriched_model_before_reporting(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()
    # 카드는 처음에 모델 정보가 전혀 없다.
    codex_event(
        "prompt",
        {"session_id": "cx-2", "cwd": str(project), "prompt": "work"},
        adapter,
        cache,
    )
    codex_event(
        "session-end",
        {"session_id": "cx-2", "reason": "other", "model": "gpt-5.6-sol"},
        adapter,
        cache,
    )
    assert usage_payload(adapter)["model"] == "gpt-5.6-sol"
