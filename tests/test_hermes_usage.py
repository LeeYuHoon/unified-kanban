from __future__ import annotations

import json
import threading
from pathlib import Path

from kanban_adapter.hermes_hook import TurnTracker
from kanban_adapter.usage import usage_event_id

BOARDS = [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}]


def make_runner(
    calls: list[list[str]],
    *,
    fail: set[str] | frozenset[str] = frozenset(),
    result_contents: list[str] | None = None,
):
    """Stand-in for the Hermes CLI that remembers the comments it was given,
    so ``show --json`` answers truthfully and idempotency can be observed."""
    posted: list[str] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        if argv[-3:] == ["boards", "list", "--json"]:
            return json.dumps(BOARDS)
        for marker in fail:
            if marker in argv:
                raise RuntimeError(f"hermes {marker} failed")
        if "create" in argv:
            return json.dumps(
                {"id": "t_12345678", "status": "running", "observation": True}
            )
        if "show" in argv:
            return json.dumps({
                "id": "t_12345678",
                "comments": [{"author": "hermes-agent", "body": body}
                             for body in posted],
            })
        if "comment" in argv:
            posted.append(argv[-1])
        if "complete" in argv and result_contents is not None:
            option = next(arg for arg in argv if arg.startswith("--result-file="))
            result_contents.append(
                Path(option.split("=", 1)[1]).read_text(encoding="utf-8")
            )
        return ""

    return runner


def make_tracker(tmp_path: Path, calls: list[list[str]], **kwargs) -> TurnTracker:
    return TurnTracker(
        runner=make_runner(calls, **kwargs),
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge/backend"),
    )


def start_turn(tracker: TurnTracker, **kwargs) -> None:
    tracker.start(
        session_id="s1",
        turn_id="t1",
        user_message="Implement payment reconciliation",
        platform="cli",
        **kwargs,
    )


def state_of(tmp_path: Path) -> dict:
    return json.loads(next((tmp_path / "cache").glob("*.json")).read_text("utf-8"))


def comment_of(calls: list[list[str]]) -> str:
    comments = [call for call in calls if "comment" in call]
    assert len(comments) == 1, comments
    return comments[0][-1]


def usage_payload(calls: list[list[str]], *, task_id: str = "t_12345678") -> dict:
    """The one usage comment's payload, with its envelope asserted here.

    Hermes cards carry the Hermes-specific header, the schema version, and the
    deterministic event id used to deduplicate the comment -- so each caller
    below keeps asserting the usage data itself, in full.
    """
    header, newline, body = comment_of(calls).partition("\n")
    assert header == "Hermes Agent tool usage"
    assert newline == "\n"
    payload = json.loads(body)
    expected_version = 2 if "tokens" in payload else 1
    assert payload.pop("schema_version") == expected_version
    assert payload.pop("event_id") == usage_event_id("hermes-agent", task_id)
    return payload


def test_start_bounds_prompt_derived_card_title_to_120_characters(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)

    tracker.start(
        session_id="s1",
        turn_id="t1",
        user_message="x" * 250,
        platform="cli",
    )

    create_call = next(call for call in calls if "create" in call)
    assert create_call[-2] == "--"
    assert create_call[-1] == "x" * 120


def test_post_tool_call_records_skills_and_mcp_names_only(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    start_turn(tracker)

    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="skill_view",
        args={"name": "axolotl", "file_path": "/secret/path.md"},
        result="secret-result", status="ok",
    )
    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="skill_view",
        args={"name": "axolotl"}, result="secret-result", status="ok",
    )
    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="mcp__linear__get_issue",
        args={"query": "secret-query"}, result="secret-result", status="ok",
    )
    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="terminal",
        args={"command": "cat /etc/secret"}, result="secret-result", status="ok",
    )

    state = state_of(tmp_path)
    assert state["usage"] == {
        "skills": {"axolotl": 2},
        "mcp": {"linear/get_issue": 1},
    }
    assert "secret" not in json.dumps(state, ensure_ascii=False)


def test_subagent_start_counts_delegated_children_by_role(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    start_turn(tracker)

    # One delegate_task fanning out to three children must count as three.
    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="delegate_task",
        args={"tasks": [{"goal": "secret"}] * 3}, result="", status="ok",
    )
    for role in ("leaf", "leaf", "orchestrator"):
        tracker.record_subagent(
            parent_session_id="s1", parent_turn_id="t1", child_role=role,
            child_goal="secret goal",
        )

    assert state_of(tmp_path)["usage"] == {
        "subagents": {"leaf": 2, "orchestrator": 1}
    }


def test_turn_result_stores_model_and_bounded_summary(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    start_turn(tracker, model="claude-fable-5")
    assert state_of(tmp_path)["model"] == "claude-fable-5"

    full_result = "  Summary.  \n\n" + "x" * 1_500
    tracker.record_turn_result(
        session_id="s1", turn_id="t1", model="gpt-5.6-sol",
        assistant_response=full_result,
    )
    state = state_of(tmp_path)
    assert state["model"] == "gpt-5.6-sol"
    assert len(state["summary"]) == 1_000
    assert state["summary"].startswith("Summary.")
    assert state["summary"].endswith("…")
    assert state["result"] == full_result


def test_large_turn_result_uses_private_file_without_truncation(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    result_contents: list[str] = []
    tracker = make_tracker(tmp_path, calls, result_contents=result_contents)
    start_turn(tracker)
    full_result = "완료\n" + "원문 상세\n" * 4_000
    tracker.record_turn_result(
        session_id="s1", turn_id="t1", assistant_response=full_result,
    )

    tracker.finish(session_id="s1", turn_id="t1", completed=True, interrupted=False)

    assert result_contents == [full_result]
    complete = calls[-1]
    assert any(arg.startswith("--result-file=") for arg in complete)
    assert not list((tmp_path / "cache").glob("*.result"))


def test_post_api_request_accumulates_tokens_once_per_request(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    start_turn(tracker, model="gpt-5.6-sol")

    first = {
        "input_tokens": 2,
        "output_tokens": 3,
        "cache_read_tokens": 5,
        "cache_write_tokens": 7,
        "reasoning_tokens": 11,
        "total_tokens": 19,
        "request_count": 1,
    }
    tracker.record_api_usage(
        session_id="s1", turn_id="t1", api_request_id="request-1", usage=first,
    )
    tracker.record_api_usage(
        session_id="s1", turn_id="t1", api_request_id="request-1", usage=first,
    )
    tracker.record_api_usage(
        session_id="s1", turn_id="t1", api_request_id="request-2",
        usage={
            "input_tokens": 13,
            "output_tokens": 17,
            "cache_read_tokens": 19,
            "cache_write_tokens": 23,
            "reasoning_tokens": 29,
            "total_tokens": 80,
            "request_count": 1,
        },
    )
    request_ids = state_of(tmp_path)["token_request_ids"]
    assert len(request_ids) == 2
    assert all(len(value) == 16 for value in request_ids)
    tracker.finish(session_id="s1", turn_id="t1", completed=True, interrupted=False)

    assert usage_payload(calls)["tokens"] == {
        "input": 15,
        "output": 20,
        "cache_read": 24,
        "cache_write": 30,
        "reasoning": 40,
        "requests": 2,
        "total": 99,
    }


def test_finish_posts_usage_comment_then_completes_with_summary(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    start_turn(tracker, model="claude-fable-5")
    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="skill_view",
        args={"name": "axolotl"}, result="", status="ok",
    )
    tracker.record_subagent(
        parent_session_id="s1", parent_turn_id="t1", child_role="leaf",
        child_goal="g",
    )
    tracker.record_turn_result(
        session_id="s1", turn_id="t1", model="claude-fable-5",
        assistant_response="Reconciliation implemented and tested.",
    )
    tracker.finish(
        session_id="s1", turn_id="t1", completed=True, interrupted=False
    )

    assert usage_payload(calls) == {
        "source": "hermes-agent",
        "model": "claude-fable-5",
        "skills": {"axolotl": 1},
        "subagents": {"leaf": 1},
        "mcp": {},
    }
    comment_call = next(call for call in calls if "comment" in call)
    assert comment_call[:6] == [
        "hermes", "kanban", "--board", "shop-bridge", "comment", "--author",
    ]
    assert comment_call[-2] == "t_12345678"

    complete = calls[-1]
    assert complete[:5] == ["hermes", "kanban", "--board", "shop-bridge", "complete"]
    assert "--result=Reconciliation implemented and tested." in complete
    assert "--summary=Reconciliation implemented and tested." in complete
    assert not list((tmp_path / "cache").glob("*.json"))


def test_abnormal_finish_states_abnormal_status_and_still_reports_usage(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    start_turn(tracker, model="gpt-5.6-sol")
    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="mcp__linear__get_issue",
        args={}, result="", status="ok",
    )
    tracker.record_turn_result(
        session_id="s1", turn_id="t1", model="gpt-5.6-sol",
        assistant_response="partial work",
    )
    tracker.finish(
        session_id="s1", turn_id="t1", completed=False, interrupted=True
    )

    assert json.loads(comment_of(calls).split("\n", 1)[1])["mcp"] == {
        "linear/get_issue": 1
    }
    complete = calls[-1]
    assert "--result=Hermes turn ended without normal completion" in complete
    assert "--summary=Hermes turn ended without normal completion" in complete


def test_usage_comment_is_not_reposted_when_complete_fails_then_retries(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls, fail={"complete"})
    start_turn(tracker)
    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="skill_view",
        args={"name": "axolotl"}, result="", status="ok",
    )

    for _ in range(3):
        try:
            tracker.finish(
                session_id="s1", turn_id="t1", completed=True, interrupted=False
            )
        except RuntimeError:
            pass

    assert len([call for call in calls if "comment" in call]) == 1
    assert state_of(tmp_path)["usage_comment_posted"] is True


def test_usage_comment_transient_failure_is_retried(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    base_runner = make_runner(calls)
    comment_attempts = 0

    def flaky_runner(argv: list[str]) -> str:
        nonlocal comment_attempts
        if "comment" in argv:
            comment_attempts += 1
            if comment_attempts == 1:
                calls.append(argv)
                raise RuntimeError("transient")
        return base_runner(argv)

    tracker = TurnTracker(
        runner=flaky_runner,
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge/backend"),
    )
    start_turn(tracker)
    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="skill_view",
        args={"name": "axolotl"}, result="", status="ok",
    )
    tracker.finish(session_id="s1", turn_id="t1", completed=True, interrupted=False)

    assert comment_attempts == 2
    assert "complete" in calls[-1]


def test_usage_comment_is_not_duplicated_when_the_marker_write_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """The crash window: the comment was appended, the marker never recorded.

    With no local record that the comment went out, the only thing standing
    between a retry and a duplicate is the deterministic event id, which is
    looked up on the card itself before appending.
    """
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls, fail={"complete"})
    start_turn(tracker)
    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="skill_view",
        args={"name": "axolotl"}, result="", status="ok",
    )

    real_write_state = tracker._write_state

    def write_state_losing_the_marker(path, state, **kwargs):
        if state.get("usage_comment_posted"):
            raise OSError("marker write failed")
        return real_write_state(path, state, **kwargs)

    monkeypatch.setattr(tracker, "_write_state", write_state_losing_the_marker)
    for _ in range(3):
        try:
            tracker.finish(
                session_id="s1", turn_id="t1", completed=True, interrupted=False
            )
        except RuntimeError:
            pass

    assert "usage_comment_posted" not in state_of(tmp_path)
    assert usage_payload(calls) == {
        "source": "hermes-agent",
        "skills": {"axolotl": 1},
        "subagents": {},
        "mcp": {},
    }


def test_recording_without_an_active_turn_is_ignored(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)

    tracker.record_tool(
        session_id="ghost", turn_id="t1", tool_name="skill_view",
        args={"name": "x"}, result="", status="ok",
    )
    tracker.record_subagent(
        parent_session_id="ghost", parent_turn_id="t1", child_role="leaf",
        child_goal="g",
    )
    tracker.record_turn_result(
        session_id="ghost", turn_id="t1", model="m", assistant_response="hi"
    )
    assert calls == []
    assert not list((tmp_path / "cache").glob("*.json"))


def test_records_are_isolated_per_turn(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    tracker.start(
        session_id="s1", turn_id="t1", user_message="first", platform="cli"
    )
    tracker.start(
        session_id="s1", turn_id="t2", user_message="second", platform="cli"
    )
    tracker.record_tool(
        session_id="s1", turn_id="t1", tool_name="skill_view",
        args={"name": "only-first"}, result="", status="ok",
    )

    states = [
        json.loads(path.read_text("utf-8"))
        for path in (tmp_path / "cache").glob("*.json")
    ]
    with_usage = [s for s in states if s.get("usage")]
    assert len(states) == 2
    assert len(with_usage) == 1
    assert with_usage[0]["usage"] == {"skills": {"only-first": 1}}


def test_concurrent_records_do_not_lose_counts(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    start_turn(tracker)

    def worker() -> None:
        for _ in range(10):
            tracker.record_tool(
                session_id="s1", turn_id="t1", tool_name="skill_view",
                args={"name": "axolotl"}, result="", status="ok",
            )

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state_of(tmp_path)["usage"] == {"skills": {"axolotl": 40}}


def test_malformed_usage_in_turn_state_is_ignored(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    start_turn(tracker)

    path = next((tmp_path / "cache").glob("*.json"))
    payload = json.loads(path.read_text("utf-8"))
    payload["usage"] = {
        "skills": {"good": 2, "bad": "3"},
        "evil": {"x": 1},
    }
    payload["model"] = "not a model name"
    path.write_text(json.dumps(payload), encoding="utf-8")

    tracker.finish(
        session_id="s1", turn_id="t1", completed=True, interrupted=False
    )
    assert usage_payload(calls) == {
        "source": "hermes-agent",
        "skills": {"good": 2},
        "subagents": {},
        "mcp": {},
    }


def test_complete_summary_is_option_safe(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    start_turn(tracker)
    tracker.record_turn_result(
        session_id="s1", turn_id="t1", model="gpt-5.6-sol",
        assistant_response="--done\n--force removed",
    )
    tracker.finish(
        session_id="s1", turn_id="t1", completed=True, interrupted=False
    )
    complete = calls[-1]
    assert "--summary" not in complete
    assert "--result" not in complete
    assert "--result=--done\n--force removed" in complete
    assert "--summary=--done\n--force removed" in complete
    assert complete[-1] == "t_12345678"
    assert complete[-2] == "--"


def test_empty_usage_still_gets_one_zero_report(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = make_tracker(tmp_path, calls)
    start_turn(tracker, model="claude-fable-5")
    tracker.finish(
        session_id="s1", turn_id="t1", completed=True, interrupted=False
    )
    assert usage_payload(calls) == {
        "source": "hermes-agent",
        "model": "claude-fable-5",
        "skills": {},
        "subagents": {},
        "mcp": {},
    }
