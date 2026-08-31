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
    title_contents: list[str] | None = None,
):
    """전달받은 주석을 기억하여 ``show --json``이 사실대로 응답하고 멱등성을
    관찰할 수 있게 하는 Hermes CLI 대역."""
    posted: list[str] = []
    posted_keys: set[str] = set()

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        if argv[-3:] == ["boards", "list", "--json"]:
            return json.dumps(BOARDS)
        for marker in fail:
            if marker in argv:
                raise RuntimeError(f"hermes {marker} failed")
        if "create" in argv:
            if title_contents is not None:
                option = next(arg for arg in argv if arg.startswith("--title-file="))
                title_contents.append(
                    Path(option.split("=", 1)[1]).read_text(encoding="utf-8")
                )
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
            key = next(
                (
                    arg.split("=", 1)[1]
                    for arg in argv
                    if arg.startswith("--idempotency-key=")
                ),
                None,
            )
            if key is None or key not in posted_keys:
                posted.append(argv[-1])
            if key is not None:
                posted_keys.add(key)
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
    assert comments
    assert len({call[-1] for call in comments}) == 1, comments
    keys = {
        arg
        for call in comments
        for arg in call
        if arg.startswith("--idempotency-key=")
    }
    assert len(keys) == 1, comments
    return comments[0][-1]


def usage_payload(calls: list[list[str]], *, task_id: str = "t_12345678") -> dict:
    """이곳에서 외피를 검증한 단일 사용량 주석의 페이로드.

    Hermes 카드는 Hermes 전용 헤더, 스키마 버전, 주석 중복 제거에 쓰이는 결정적
    이벤트 ID를 담는다. 따라서 아래의 각 호출자는 사용량 데이터 자체를 완전히
    검증한다.
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
    titles: list[str] = []
    tracker = make_tracker(tmp_path, calls, title_contents=titles)

    tracker.start(
        session_id="s1",
        turn_id="t1",
        user_message="x" * 250,
        platform="cli",
    )

    create_call = next(call for call in calls if "create" in call)
    assert create_call[-1].startswith("--title-file=")
    assert "x" * 120 not in create_call
    assert titles == ["x" * 120]


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

    # 세 자식으로 분기하는 delegate_task 하나는 3회로 계산해야 한다.
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
    result_contents: list[str] = []
    tracker = make_tracker(tmp_path, calls, result_contents=result_contents)
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
    assert any(arg.startswith("--result-file=") for arg in complete)
    assert "--summary=Hermes turn result recorded" in complete
    assert result_contents == ["Reconciliation implemented and tested."]
    assert not list((tmp_path / "cache").glob("*.json"))


def test_abnormal_finish_states_abnormal_status_and_still_reports_usage(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    result_contents: list[str] = []
    tracker = make_tracker(tmp_path, calls, result_contents=result_contents)
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
    assert result_contents == ["Hermes turn ended without normal completion"]
    assert "--summary=Hermes turn result recorded" in complete


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
    """충돌 발생 구간: 주석은 추가됐지만 마커는 기록되지 않았다.

    주석이 전송됐다는 로컬 기록이 없을 때 재시도와 중복 사이를 막는 유일한 것은
    결정적 이벤트 ID이며, 추가하기 전에 카드 자체에서 이 ID를 조회한다.
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
    result_contents: list[str] = []
    tracker = make_tracker(tmp_path, calls, result_contents=result_contents)
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
    assert result_contents == ["--done\n--force removed"]
    assert "--summary=Hermes turn result recorded" in complete
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
