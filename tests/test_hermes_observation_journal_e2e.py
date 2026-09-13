from __future__ import annotations

import json
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path


import pytest

from kanban_adapter.hermes_hook import TurnTracker

pytest.importorskip("hermes_cli")
kb = importlib.import_module("hermes_cli.kanban_db")
kbc = importlib.import_module("hermes_cli.kanban_db_connect")
configure_collection = importlib.import_module(
    "hermes_cli.conversation_journal"
).configure_collection
ConversationJournal = importlib.import_module(
    "hermes_cli.conversation_journal"
).ConversationJournal
NativeConversationService = importlib.import_module(
    "hermes_cli.conversation_journal"
).NativeConversationService


_FD_RE = re.compile(r"--(?:title|result)-file=/dev/fd/(\d+)\Z|--conversation-receipt-fd=(\d+)\Z")


def _native_hermes_runner(commands: list[list[str]]):
    def run(argv: list[str]) -> str:
        commands.append(argv)
        pass_fds = tuple(
            sorted(
                {
                    int(next(group for group in match.groups() if group is not None))
                    for argument in argv
                    if (match := _FD_RE.fullmatch(argument)) is not None
                }
            )
        )
        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", *argv[1:]],
            text=True,
            capture_output=True,
            check=False,
            pass_fds=pass_fds,
            env=os.environ.copy(),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout

    return run


def test_actual_hermes_observation_hook_collects_without_claim(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir(mode=0o700)
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.delenv("HERMES_KANBAN_CONVERSATION_KERNEL_SECRET_FILE", raising=False)
    commands: list[list[str]] = []
    policy = json.loads(_native_hermes_runner(commands)([
        "hermes", "kanban", "--board", "default", "conversation-config",
        "--enable", "--principal", "test:owner", "--json",
    ]))
    assert policy["board_principal_grants"] == {"default": ["test:owner"]}
    secret = home / "conversation-journal" / "kernel-receipt.key"
    assert secret.exists()
    assert secret.stat().st_mode & 0o777 == 0o600
    for directory in (
        secret.parent,
        secret.parent / "open",
        secret.parent / "records",
        secret.parent / "retired",
    ):
        assert directory.stat().st_mode & 0o777 == 0o700
    assert secret.read_bytes().hex() not in json.dumps(policy)
    kb.init_db(db)

    tracker = TurnTracker(
        runner=_native_hermes_runner(commands),
        cache_root=cache,
        cwd_provider=lambda: tmp_path,
    )
    tracker.backend.resolve_board = lambda *, cwd: "default"

    tracker.start(
        session_id="public-session",
        turn_id="public-turn",
        user_message="contact alice@example.com token=abcdefghijklmnopqrstuv",
        platform="cli",
        model="fixture/model",
    )

    conn = kbc.connect(db)
    try:
        task = conn.execute("SELECT * FROM tasks").fetchone()
        assert task is not None
        assert task["observation"] == 1
        assert task["status"] == "running"
        assert task["current_run_id"] is None
        assert task["claim_lock"] is None
        task_id = task["id"]
    finally:
        conn.close()

    # 프로세스 내부의 수집기 상태는 필요하지 않다. 비공개 턴 추적기 영수증만으로
    # 재시작 후에도 충분하며, 이 영수증에는 대시보드 세션 자격 증명이 포함되지 않는다.
    tracker = TurnTracker(
        runner=_native_hermes_runner(commands), cache_root=cache, cwd_provider=lambda: tmp_path,
    )
    tracker.backend.resolve_board = lambda *, cwd: "default"
    tracker.record_turn_result(
        session_id="public-session",
        turn_id="public-turn",
        assistant_response="PRIVATE_CHAIN_SHOULD_NOT_SEAL",
        response_channel="analysis",
        final_authority="agent.turn_finalizer",
    )
    open_record = [payload for _path, payload in ConversationJournal()._matching_records(
        "default", task_id,
    )]
    assert len(open_record) == 1
    assert len(open_record[0]["events"]) == 1
    assert "PRIVATE_CHAIN_SHOULD_NOT_SEAL" not in json.dumps(open_record)
    tracker.record_turn_result(
        session_id="public-session",
        turn_id="public-turn",
        assistant_response="final /Users/alice/private password=hunter2",
        model="fixture/model",
        response_channel="final",
        final_authority="agent.turn_finalizer",
    )
    tracker.finish(
        session_id="public-session", turn_id="public-turn", completed=True, interrupted=False,
    )

    record_path = next((home / "conversation-journal" / "records").glob("*.json"))
    record = json.loads(record_path.read_text())
    wire = record_path.read_text()
    assert record["task_id"] == task_id
    assert record["source_type"] == "hermes_observation_creation_receipt"
    assert [event["kind"] for event in record["events"]] == ["user_message", "final_assistant"]
    assert "alice@example.com" not in wire
    assert "abcdefghijklmnopqrstuv" not in wire
    assert "/Users/alice/private" not in wire
    assert "hunter2" not in wire
    assert "analysis" not in wire
    assert "tool" not in wire
    assert not any("claim" in command for argv in commands for command in argv)

    service = NativeConversationService(
        board_authorizer=lambda principal, board: principal == "test:owner" and board == "default",
        task_membership=lambda board, task: board == "default" and task == task_id,
    )
    page = service.get_parent_page(
        principal_id="test:owner", board="default", task=task_id, cursor=None, limit=100,
    )
    assert [event["kind"] for event in page["events"]] == ["user_message", "final_assistant"]
    assert all("alice@example.com" not in event.get("text", "") for event in page["events"])
    deleted = service.delete_task_records(
        principal_id="test:owner", board="default", task=task_id,
    )
    assert deleted == {"deleted_records": 1, "skipped_foreign": 0, "verified": True}
    assert service.get_parent_page(
        principal_id="test:owner", board="default", task=task_id, cursor=None, limit=100,
    )["source_availability"] == "missing"


def test_nonfinal_hook_payload_cannot_seal_or_persist_assistant_text(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.delenv("HERMES_KANBAN_CONVERSATION_KERNEL_SECRET_FILE", raising=False)
    configure_collection(board="default", enabled=True, principal_id="test:owner")
    kb.init_db(tmp_path / "kanban.db")
    tracker = TurnTracker(
        runner=_native_hermes_runner([]), cache_root=cache, cwd_provider=lambda: tmp_path,
    )
    tracker.backend.resolve_board = lambda *, cwd: "default"
    tracker.start(
        session_id="s", turn_id="t", user_message="public", platform="cli", model="m",
    )

    tracker.record_turn_result(
        session_id="s", turn_id="t", assistant_response="private reasoning",
        response_channel="analysis", final_authority="agent.turn_finalizer",
    )

    assert not list((home / "conversation-journal" / "records").glob("*.json"))
    open_wire = next((home / "conversation-journal" / "open").glob("*.json")).read_text()
    assert "private reasoning" not in open_wire
