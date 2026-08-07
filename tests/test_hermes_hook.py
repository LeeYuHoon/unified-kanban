from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from kanban_adapter.backend import BoardNotMappedError
from kanban_adapter.hermes_hook import TurnTracker, hermes_runtime_cwd


def boards_runner(calls: list[list[str]], boards: list[dict]):
    def runner(argv: list[str]) -> str:
        calls.append(argv)
        if argv[-3:] == ["boards", "list", "--json"]:
            return json.dumps(boards)
        if "create" in argv:
            return json.dumps(
                {"id": "t_12345678", "status": "running", "observation": True}
            )
        return ""

    return runner


def test_runtime_cwd_uses_hermes_per_session_resolver(monkeypatch) -> None:
    agent = types.ModuleType("agent")
    runtime_cwd = types.ModuleType("agent.runtime_cwd")
    setattr(runtime_cwd, "resolve_agent_cwd", lambda: Path("/work/session-project"))
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.runtime_cwd", runtime_cwd)

    assert hermes_runtime_cwd() == Path("/work/session-project")


def test_turn_start_creates_running_card_and_finish_completes_it(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = boards_runner(
        calls, [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}]
    )

    tracker = TurnTracker(
        runner=runner,
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge/backend"),
    )

    tracker.start(
        session_id="session-1",
        turn_id="turn-1",
        user_message="  Implement   payment\nreconciliation  ",
        platform="cli",
    )
    tracker.finish(
        session_id="session-1",
        turn_id="turn-1",
        completed=True,
        interrupted=False,
    )

    create = next(call for call in calls if "create" in call)
    assert create[:5] == ["hermes", "kanban", "--board", "shop-bridge", "create"]
    assert "--observation" in create
    assert not any("claim" in call for call in calls)
    assert "--tenant" in create and "hermes" in create
    assert "--created-by" in create and "hermes-agent" in create
    assert "--idempotency-key" in create
    key = create[create.index("--idempotency-key") + 1]
    assert key == "hermes:session-1:turn-1"
    assert create[-1] == "Implement payment reconciliation"
    assert calls[-1][:5] == [
        "hermes", "kanban", "--board", "shop-bridge", "complete",
    ]
    assert calls[-1][-1] == "t_12345678"
    assert not list((tmp_path / "cache").glob("*.json"))


def test_turn_start_separates_option_like_prompt_title(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = TurnTracker(
        runner=boards_runner(
            calls,
            [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}],
        ),
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )

    tracker.start(
        session_id="session-1", turn_id="turn-1",
        user_message="--help", platform="cli",
    )

    create = next(call for call in calls if "create" in call)
    assert create[-2:] == ["--", "--help"]


def test_routing_reuses_unified_backend_deepest_match(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = boards_runner(
        calls,
        [
            {"slug": "outer", "default_workdir": str(tmp_path)},
            {"slug": "inner", "default_workdir": str(tmp_path / "nested")},
        ],
    )

    tracker = TurnTracker(
        runner=runner,
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: tmp_path / "nested" / "src",
    )
    tracker.start(
        session_id="session-1", turn_id="turn-1",
        user_message="deep routing", platform="cli",
    )

    create = next(call for call in calls if "create" in call)
    assert create[2:4] == ["--board", "inner"]


def test_unmapped_directory_raises_board_not_mapped(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = boards_runner(
        calls, [{"slug": "elsewhere", "default_workdir": "/somewhere/else"}]
    )
    tracker = TurnTracker(
        runner=runner,
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: tmp_path,
    )

    with pytest.raises(BoardNotMappedError):
        tracker.start(
            session_id="session-1", turn_id="turn-1",
            user_message="nowhere", platform="cli",
        )
    assert not any("create" in call for call in calls)


def test_blank_or_missing_turn_fields_do_not_create_cards(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = boards_runner(
        calls, [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}]
    )
    tracker = TurnTracker(
        runner=runner,
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )

    tracker.start(session_id="", turn_id="turn-1", user_message="x", platform="cli")
    tracker.start(session_id="s", turn_id="", user_message="x", platform="cli")
    tracker.start(session_id="s", turn_id="t", user_message="   \n ", platform="cli")

    assert calls == []


@pytest.mark.parametrize(
    "user_message",
    [
        "[ASYNC DELEGATION BATCH COMPLETE — deleg_123]\nbackground results",
        "[ASYNC DELEGATION COMPLETE — deleg_123]\nbackground result",
        "[IMPORTANT: Background process proc_123 completed normally]",
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted",
        "[CONTEXT SUMMARY]: compressed context",
        "[Your active task list was preserved across context compression]\n- item",
    ],
)
def test_automatic_hermes_notifications_do_not_create_cards(
    tmp_path: Path, user_message: str,
) -> None:
    calls: list[list[str]] = []
    tracker = TurnTracker(
        runner=boards_runner(
            calls,
            [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}],
        ),
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )

    tracker.start(
        session_id="session-1", turn_id="turn-1",
        user_message=user_message, platform="cli",
    )

    assert calls == []
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("marker", ["HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK"])
def test_auxiliary_agent_processes_do_not_create_duplicate_cards(
    monkeypatch, tmp_path: Path, marker: str,
) -> None:
    monkeypatch.setenv(marker, "1")
    calls: list[list[str]] = []
    tracker = TurnTracker(
        runner=boards_runner(
            calls,
            [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}],
        ),
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )

    tracker.start(
        session_id="session-1", turn_id="turn-1",
        user_message="internal parallel work", platform="cli",
    )

    assert calls == []
    assert not (tmp_path / "cache").exists()


def test_user_message_that_mentions_async_results_still_creates_card(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = TurnTracker(
        runner=boards_runner(
            calls,
            [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}],
        ),
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )

    tracker.start(
        session_id="session-1", turn_id="turn-1",
        user_message="Please explain the [ASYNC DELEGATION COMPLETE] message",
        platform="cli",
    )

    create = next(call for call in calls if "create" in call)
    assert create[-1] == "Please explain the [ASYNC DELEGATION COMPLETE] message"


def test_duplicate_turn_start_creates_only_one_card(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = boards_runner(
        calls, [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}]
    )
    tracker = TurnTracker(
        runner=runner,
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )
    payload = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "user_message": "one turn",
        "platform": "cli",
    }

    tracker.start(**payload)
    tracker.start(**payload)

    assert sum("create" in call for call in calls) == 1


def test_concurrent_duplicate_turn_start_creates_one_running_card(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        if argv[-3:] == ["boards", "list", "--json"]:
            return json.dumps([
                {"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}
            ])
        if "create" in argv:
            time.sleep(0.1)
            return json.dumps(
                {"id": "t_12345678", "status": "running", "observation": True}
            )
        return ""

    tracker = TurnTracker(
        runner=runner,
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )
    errors: list[BaseException] = []

    def start() -> None:
        try:
            tracker.start(
                session_id="session-1", turn_id="turn-1",
                user_message="one turn", platform="cli",
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert sum("create" in call for call in calls) == 1
    assert not any("complete" in call for call in calls)


def test_start_refuses_symlink_cache_root(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = tmp_path / "cache"
    cache.symlink_to(outside, target_is_directory=True)
    tracker = TurnTracker(
        runner=boards_runner(
            calls,
            [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}],
        ),
        cache_root=cache,
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )

    with pytest.raises(RuntimeError, match="non-symlink directory"):
        tracker.start(
            session_id="session-1", turn_id="turn-1",
            user_message="one turn", platform="cli",
        )

    assert not list(outside.iterdir())


def test_finish_without_state_is_a_safe_no_op(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = TurnTracker(
        runner=lambda argv: calls.append(argv) or "",
        cache_root=tmp_path / "cache",
    )

    tracker.finish(
        session_id="session-1", turn_id="turn-1",
        completed=True, interrupted=False,
    )

    assert calls == []


def test_state_write_failure_does_not_mutate_idempotent_card(
    tmp_path: Path, monkeypatch,
) -> None:
    calls: list[list[str]] = []
    runner = boards_runner(
        calls, [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}]
    )
    tracker = TurnTracker(
        runner=runner,
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )
    monkeypatch.setattr(
        tracker, "_write_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError):
        tracker.start(
            session_id="session-1", turn_id="turn-1",
            user_message="state failure", platform="cli",
        )

    assert not any("complete" in call for call in calls)
    assert not any("claim" in call for call in calls)
    assert not list((tmp_path / "cache").glob("*.json"))


@pytest.mark.parametrize("create_payload", [
    {"id": "t_12345678", "status": "ready", "observation": True},
    {"id": "t_12345678", "status": "running", "observation": False},
    {"id": "t_12345678", "status": "running"},
])
def test_observation_contract_mismatch_does_not_mutate_returned_task(
    tmp_path: Path, create_payload: dict,
) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        if argv[-3:] == ["boards", "list", "--json"]:
            return json.dumps([
                {"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}
            ])
        if "create" in argv:
            return json.dumps(create_payload)
        return ""

    tracker = TurnTracker(
        runner=runner,
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )

    with pytest.raises(RuntimeError, match="observation"):
        tracker.start(
            session_id="session-1", turn_id="turn-1",
            user_message="contract mismatch", platform="cli",
        )

    assert not any("complete" in call for call in calls)
    assert not any("claim" in call for call in calls)
    assert not list((tmp_path / "cache").glob("*.json"))


def test_interrupted_turn_completes_with_abnormal_summary(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = boards_runner(
        calls, [{"slug": "shop-bridge", "default_workdir": "/work/shop-bridge"}]
    )
    tracker = TurnTracker(
        runner=runner,
        cache_root=tmp_path / "cache",
        cwd_provider=lambda: Path("/work/shop-bridge"),
    )

    tracker.start(
        session_id="session-1", turn_id="turn-1",
        user_message="interrupted turn", platform="cli",
    )
    tracker.finish(
        session_id="session-1", turn_id="turn-1",
        completed=False, interrupted=True,
    )

    complete = next(call for call in calls if "complete" in call)
    summary = next(
        argument.removeprefix("--summary=")
        for argument in complete
        if argument.startswith("--summary=")
    )
    assert "without normal completion" in summary


def test_finish_refuses_symlink_state_without_following_it(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    tracker = TurnTracker(
        runner=lambda argv: calls.append(argv) or "",
        cache_root=tmp_path / "cache",
    )
    tracker.cache_root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"board": "shop-bridge", "task_id": "t_12345678"}),
        encoding="utf-8",
    )
    tracker._state_path("session-1", "turn-1").symlink_to(outside)

    with pytest.raises(ValueError, match="non-symlink regular file"):
        tracker.finish(
            session_id="session-1", turn_id="turn-1",
            completed=True, interrupted=False,
        )

    assert calls == []
    assert outside.exists()
