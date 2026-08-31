from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import kanban_adapter.backend as backend_module
from kanban_adapter.backend import HermesCliBackend


@dataclass
class FakeRunner:
    stdout: str = ""
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        return self.stdout


def test_start_creates_running_observation_card_and_returns_id() -> None:
    runner = FakeRunner(stdout=json.dumps(
        {"id": "t_abc123", "status": "running", "observation": True}
    ))
    backend = HermesCliBackend(runner=runner)

    task_id = backend.start(board="project-a", title="Fix checkout", source="claude-code")

    assert task_id == "t_abc123"
    assert runner.calls[0][:-1] == [
        "hermes", "kanban", "--board", "project-a", "create",
        "--assignee", "claude-code-external", "--tenant", "claude",
        "--created-by", "kanban-adapter", "--observation", "--json",
    ]
    assert runner.calls[0][-1].startswith("--title-file=")
    assert "Fix checkout" not in runner.calls[0]
    assert not any("claim" in call for call in runner.calls)


def test_start_threads_valid_idempotency_key_to_create() -> None:
    runner = FakeRunner(stdout=json.dumps(
        {"id": "t_abc123", "status": "running", "observation": True}
    ))
    backend = HermesCliBackend(runner=runner)
    key = "a" * 64

    assert backend.start(
        board="project-a", title="Fix checkout", source="claude-code",
        idempotency_key=key,
    ) == "t_abc123"

    assert f"--idempotency-key={key}" in runner.calls[0]


def test_run_command_inherits_only_declared_file_fds(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict = {}

    def fake_run(argv, **kwargs):
        observed.update(kwargs)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(backend_module.subprocess, "run", fake_run)
    backend_module.run_command([
        "hermes", "kanban", "complete", "--result-file=/dev/fd/17"
    ])

    assert observed["pass_fds"] == (17,)


@pytest.mark.parametrize("payload", [
    {"id": "t_abc123", "status": "ready", "observation": True},
    {"id": "t_abc123", "status": "running", "observation": False},
    {"id": "t_abc123", "status": "running"},
])
def test_start_contract_mismatch_does_not_mutate_returned_task(payload: dict) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        if "create" in argv:
            return json.dumps(payload)
        return ""

    backend = HermesCliBackend(runner=runner)

    with pytest.raises(RuntimeError, match="observation"):
        backend.start(board="project-a", title="x", source="codex")

    assert not any("complete" in call for call in calls)
    assert not any("claim" in call for call in calls)


@pytest.mark.parametrize("task_id", ["", "../task", "not-a-task", "t_bad/value"])
def test_start_rejects_invalid_task_id(task_id: str) -> None:
    runner = FakeRunner(stdout=json.dumps(
        {"id": task_id, "status": "running", "observation": True}
    ))
    backend = HermesCliBackend(runner=runner)

    with pytest.raises(RuntimeError, match="valid task id"):
        backend.start(board="project-a", title="x", source="manual")


def test_start_rejects_unknown_source_before_running_command() -> None:
    runner = FakeRunner()
    backend = HermesCliBackend(runner=runner)

    with pytest.raises(ValueError, match="unsupported source"):
        backend.start(board="project-a", title="x", source="unknown")

    assert runner.calls == []


def test_start_reports_non_json_hermes_output() -> None:
    runner = FakeRunner(stdout="kanban: board does not exist")
    backend = HermesCliBackend(runner=runner)

    with pytest.raises(RuntimeError, match="non-JSON"):
        backend.start(board="missing", title="x", source="manual")


@pytest.mark.parametrize("payload", [[], [1], "task", 1, None])
def test_start_rejects_non_object_json_without_attribute_error(payload: object) -> None:
    runner = FakeRunner(stdout=json.dumps(payload))
    backend = HermesCliBackend(runner=runner)

    with pytest.raises(RuntimeError, match="JSON object"):
        backend.start(board="project-a", title="x", source="manual")


def test_update_appends_adapter_comment() -> None:
    runner = FakeRunner()
    backend = HermesCliBackend(runner=runner)

    backend.update(board="project-a", task_id="t_abc123", message="tests passing")

    assert runner.calls == [[
        "hermes", "kanban", "--board", "project-a", "comment",
        "--author", "kanban-adapter", "--", "t_abc123", "tests passing",
    ]]


MARKER = "usage-" + "0" * 32


@dataclass
class ShowRunner:
    """``show --json`` 호출 시 주어진 주석이 담긴 카드를 반환하는 실행기."""

    comments: list[str] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    show_stdout: str | None = None

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        if "show" in argv:
            if self.show_stdout is not None:
                return self.show_stdout
            return json.dumps({
                "id": "t_abc123",
                "title": "Fix checkout",
                "comments": [
                    {"author": "kanban-adapter", "body": body}
                    for body in self.comments
                ],
            })
        return ""


def test_update_delegates_idempotency_atomically_to_hermes() -> None:
    runner = ShowRunner(comments=["unrelated note"])
    backend = HermesCliBackend(runner=runner)

    backend.update(
        board="project-a",
        task_id="t_abc123",
        message=f'Claude Code tool usage\n{{"event_id": "{MARKER}"}}',
        idempotency_key=MARKER,
    )

    assert runner.calls[0][:6] == [
        "hermes", "kanban", "--board", "project-a", "comment", "--author",
    ]
    assert f"--idempotency-key={MARKER}" in runner.calls[0]
    assert len(runner.calls) == 1


def test_update_without_an_idempotency_key_never_inspects_the_card() -> None:
    runner = ShowRunner()
    backend = HermesCliBackend(runner=runner)

    backend.update(board="project-a", task_id="t_abc123", message="plain")

    assert [call for call in runner.calls if "show" in call] == []


@pytest.mark.parametrize(
    "key", ["", "  ", "-flag", "../etc", "key with spaces", "x" * 200, "a/b"]
)
def test_update_rejects_a_malformed_idempotency_key(key: str) -> None:
    runner = ShowRunner()
    backend = HermesCliBackend(runner=runner)

    with pytest.raises(ValueError):
        backend.update(
            board="project-a", task_id="t_abc123", message="x", idempotency_key=key
        )

    assert runner.calls == []


def test_done_completes_with_full_result_and_distinct_summary() -> None:
    runner = FakeRunner()
    backend = HermesCliBackend(runner=runner)

    backend.done(
        board="project-a",
        task_id="t_abc123",
        result="Full implementation details.",
        summary="implemented",
    )

    # 전체 결과는 카드에 영구 보존되는 내용이고, 요약은 기본 미리보기에 쓰이는
    # 크기가 제한된 실행 인계 내용으로 남는다.
    assert runner.calls == [[
        "hermes", "kanban", "--board", "project-a", "complete",
        "--result=Full implementation details.",
        "--summary=implemented", "--", "t_abc123",
    ]]


def test_done_hyphen_prefixed_summary_is_bound_as_option_value() -> None:
    runner = FakeRunner()
    backend = HermesCliBackend(runner=runner)

    backend.done(
        board="project-a", task_id="t_abc123", result="--raw", summary="--help"
    )

    assert runner.calls == [[
        "hermes", "kanban", "--board", "project-a", "complete",
        "--result=--raw", "--summary=--help", "--", "t_abc123",
    ]]


def test_done_forwards_full_result_file_without_reading_it(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = HermesCliBackend(runner=runner)
    result_file = tmp_path / "result.txt"

    backend.done(
        board="project-a", task_id="t_abc123",
        result_file=result_file, summary="implemented",
    )

    assert runner.calls == [[
        "hermes", "kanban", "--board", "project-a", "complete",
        f"--result-file={result_file}",
        "--summary=implemented", "--", "t_abc123",
    ]]


def test_block_records_reason() -> None:
    runner = FakeRunner()
    backend = HermesCliBackend(runner=runner)

    backend.block(board="project-a", task_id="t_abc123", reason="review needed")

    assert runner.calls == [[
        "hermes", "kanban", "--board", "project-a", "block",
        "--kind", "needs_input", "--", "t_abc123", "review needed",
    ]]


def test_hyphen_prefixed_user_data_is_after_option_separator() -> None:
    runner = FakeRunner(stdout=json.dumps(
        {"id": "t_abc123", "status": "running", "observation": True}
    ))
    backend = HermesCliBackend(runner=runner)

    backend.start(board="project-a", title="--help", source="manual")
    backend.update(board="project-a", task_id="t_abc123", message="--author")
    backend.block(board="project-a", task_id="t_abc123", reason="--kind")

    assert runner.calls[0][-1].startswith("--title-file=")
    assert "--help" not in runner.calls[0]
    assert runner.calls[1][-3:] == ["--", "t_abc123", "--author"]
    assert runner.calls[2][-3:] == ["--", "t_abc123", "--kind"]


def test_resolve_board_uses_dashboard_project_directory_for_nested_cwd(tmp_path: Path) -> None:
    project = tmp_path / "shop-bridge"
    nested = project / "frontend" / "src"
    nested.mkdir(parents=True)
    runner = FakeRunner(stdout=json.dumps([
        {"slug": "default", "default_workdir": None},
        {"slug": "shop-bridge", "default_workdir": str(project)},
    ]))
    backend = HermesCliBackend(runner=runner)

    board = backend.resolve_board(cwd=nested)

    assert board == "shop-bridge"
    assert runner.calls == [["hermes", "kanban", "boards", "list", "--json"]]


def test_resolve_board_rejects_duplicate_project_directory(tmp_path: Path) -> None:
    project = tmp_path / "shared"
    project.mkdir()
    runner = FakeRunner(stdout=json.dumps([
        {"slug": "project-a", "default_workdir": str(project)},
        {"slug": "project-b", "default_workdir": str(project)},
    ]))
    backend = HermesCliBackend(runner=runner)

    with pytest.raises(RuntimeError, match="multiple Kanban boards"):
        backend.resolve_board(cwd=project)


def test_resolve_board_rejects_relative_project_directory(tmp_path: Path) -> None:
    runner = FakeRunner(stdout=json.dumps([
        {"slug": "project-a", "default_workdir": "relative/project"},
    ]))
    backend = HermesCliBackend(runner=runner)

    with pytest.raises(RuntimeError, match="absolute"):
        backend.resolve_board(cwd=tmp_path)


def test_resolve_board_prefers_most_specific_nested_project(tmp_path: Path) -> None:
    monorepo = tmp_path / "shop-bridge"
    frontend = monorepo / "frontend"
    cwd = frontend / "src"
    cwd.mkdir(parents=True)
    runner = FakeRunner(stdout=json.dumps([
        {"slug": "shop-bridge", "default_workdir": str(monorepo)},
        {"slug": "shop-bridge-frontend", "default_workdir": str(frontend)},
    ]))

    assert HermesCliBackend(runner=runner).resolve_board(cwd=cwd) == "shop-bridge-frontend"


def test_resolve_board_reports_unmapped_directory(tmp_path: Path) -> None:
    mapped = tmp_path / "mapped"
    elsewhere = tmp_path / "elsewhere"
    mapped.mkdir()
    elsewhere.mkdir()
    runner = FakeRunner(stdout=json.dumps([
        {"slug": "project-a", "default_workdir": str(mapped)},
    ]))

    with pytest.raises(RuntimeError, match="Project directory"):
        HermesCliBackend(runner=runner).resolve_board(cwd=elsewhere)


def test_resolve_board_rejects_non_json_board_list(tmp_path: Path) -> None:
    runner = FakeRunner(stdout="not-json")

    with pytest.raises(RuntimeError, match="non-JSON"):
        HermesCliBackend(runner=runner).resolve_board(cwd=tmp_path)


def test_resolve_board_rejects_missing_default_workdir_key(tmp_path: Path) -> None:
    runner = FakeRunner(stdout=json.dumps([{"slug": "project-a"}]))

    with pytest.raises(RuntimeError, match="default_workdir"):
        HermesCliBackend(runner=runner).resolve_board(cwd=tmp_path)


@pytest.mark.parametrize("slug", ["BadBoard", "bad.board"])
def test_resolve_board_rejects_slug_outside_hermes_contract(
    tmp_path: Path, slug: str,
) -> None:
    runner = FakeRunner(stdout=json.dumps([
        {"slug": slug, "default_workdir": str(tmp_path)},
    ]))

    with pytest.raises(RuntimeError, match="invalid slug"):
        HermesCliBackend(runner=runner).resolve_board(cwd=tmp_path)
