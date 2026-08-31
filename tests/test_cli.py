from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import tomllib

from kanban_adapter.backend import BoardNotMappedError
from kanban_adapter.cli import main


REPO = Path(__file__).resolve().parents[1]


def test_distribution_does_not_publish_an_unguarded_console_script() -> None:
    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

    assert "scripts" not in metadata["project"]
    assert "THIRD_PARTY_NOTICES.md" in metadata["project"]["license-files"]


@dataclass
class FakeBackend:
    calls: list[tuple[str, dict]] = field(default_factory=list)
    resolved_board: str | None = None

    def resolve_board(self, *, cwd: Path) -> str:
        self.calls.append(("resolve_board", {"cwd": cwd}))
        if self.resolved_board is None:
            raise BoardNotMappedError("no mapped board")
        return self.resolved_board

    def start(self, **kwargs: str) -> str:
        self.calls.append(("start", kwargs))
        return "t_12345678"

    def update(self, **kwargs: str) -> None:
        self.calls.append(("update", kwargs))

    def done(self, **kwargs: str) -> None:
        self.calls.append(("done", kwargs))

    def block(self, **kwargs: str) -> None:
        self.calls.append(("block", kwargs))


def test_start_prints_only_task_id(capsys) -> None:
    backend = FakeBackend()

    result = main([
        "start", "--board", "project-a", "--title", "Fix checkout",
        "--source", "claude-code",
    ], backend=backend)

    assert result == 0
    assert capsys.readouterr().out == "t_12345678\n"
    assert backend.calls == [("start", {
        "board": "project-a", "title": "Fix checkout", "source": "claude-code",
    })]


def test_cli_without_injected_backend_fails_closed_when_hermes_is_incompatible(
    capsys,
) -> None:
    result = main(
        [
            "start", "--board", "project-a", "--title", "unsafe",
            "--source", "manual",
        ],
        compatibility_check=lambda: (False, "active Hermes checkout is unsupported"),
    )

    assert result == 1
    assert "active Hermes checkout is unsupported" in capsys.readouterr().err


def test_update_uses_board_from_environment_when_directory_is_unmapped(monkeypatch) -> None:
    backend = FakeBackend()
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "project-env")

    result = main([
        "update", "--task", "t_12345678", "--message", "tests passing",
    ], backend=backend)

    assert result == 0
    assert backend.calls == [
        ("resolve_board", {"cwd": Path.cwd().resolve()}),
        ("update", {
            "board": "project-env", "task_id": "t_12345678", "message": "tests passing",
        }),
    ]


def test_dashboard_project_directory_overrides_inherited_default_board(monkeypatch) -> None:
    backend = FakeBackend(resolved_board="shop-bridge")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")

    result = main([
        "update", "--task", "t_12345678", "--message", "tests passing",
    ], backend=backend)

    assert result == 0
    assert backend.calls[-1] == ("update", {
        "board": "shop-bridge", "task_id": "t_12345678", "message": "tests passing",
    })


def test_update_auto_resolves_board_from_current_directory(monkeypatch, tmp_path: Path) -> None:
    backend = FakeBackend(resolved_board="shop-bridge")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.chdir(tmp_path)

    result = main([
        "update", "--task", "t_12345678", "--message", "tests passing",
    ], backend=backend)

    assert result == 0
    assert backend.calls == [
        ("resolve_board", {"cwd": tmp_path.resolve()}),
        ("update", {
            "board": "shop-bridge", "task_id": "t_12345678", "message": "tests passing",
        }),
    ]


def test_update_forwards_an_idempotency_key_when_given_one() -> None:
    backend = FakeBackend(resolved_board="shop-bridge")

    assert main([
        "update", "--board", "project-a", "--task", "t_12345678",
        "--message", "Claude Code tool usage\n{}",
        "--idempotency-key", "usage-" + "0" * 32,
    ], backend=backend) == 0

    assert backend.calls[-1] == ("update", {
        "board": "project-a",
        "task_id": "t_12345678",
        "message": "Claude Code tool usage\n{}",
        "idempotency_key": "usage-" + "0" * 32,
    })


def test_update_stays_key_free_when_none_is_given() -> None:
    """이전 호출자는 기존 호출 형태를 정확히 유지한다."""
    backend = FakeBackend(resolved_board="shop-bridge")

    assert main([
        "update", "--board", "project-a", "--task", "t_12345678",
        "--message", "hello",
    ], backend=backend) == 0

    assert backend.calls[-1] == ("update", {
        "board": "project-a", "task_id": "t_12345678", "message": "hello",
    })


def test_explicit_empty_idempotency_keys_are_not_silently_discarded() -> None:
    backend = FakeBackend(resolved_board="shop-bridge")

    assert main([
        "start", "--board", "project-a", "--title", "x",
        "--source", "claude-code", "--idempotency-key", "",
    ], backend=backend) == 0
    assert backend.calls[-1][1]["idempotency_key"] == ""

    assert main([
        "update", "--board", "project-a", "--task", "t_12345678",
        "--message", "x", "--idempotency-key", "",
    ], backend=backend) == 0
    assert backend.calls[-1][1]["idempotency_key"] == ""


def test_public_cli_accepts_hyphen_prefixed_user_data(capsys) -> None:
    backend = FakeBackend()

    assert main([
        "start", "--board", "project-a", "--title", "--help",
        "--source", "manual",
    ], backend=backend) == 0
    assert main([
        "update", "--board", "project-a", "--task", "t_12345678",
        "--message", "--author",
    ], backend=backend) == 0
    assert main([
        "done", "--board", "project-a", "--task", "t_12345678",
        "--result-file", "--raw", "--summary", "--summary-text",
    ], backend=backend) == 0
    assert main([
        "block", "--board", "project-a", "--task", "t_12345678",
        "--reason", "--kind",
    ], backend=backend) == 0

    assert capsys.readouterr().out == "t_12345678\n"
    assert backend.calls == [
        ("start", {"board": "project-a", "title": "--help", "source": "manual"}),
        ("update", {
            "board": "project-a", "task_id": "t_12345678", "message": "--author",
        }),
        ("done", {
            "board": "project-a", "task_id": "t_12345678",
            "result": None, "result_file": Path("--raw"),
            "summary": "--summary-text",
        }),
        ("block", {
            "board": "project-a", "task_id": "t_12345678", "reason": "--kind",
        }),
    ]


def test_done_without_mapped_board_fails_cleanly(monkeypatch, capsys) -> None:
    backend = FakeBackend()
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    result = main(["done", "--task", "t_12345678"], backend=backend)

    assert result == 1
    assert "no mapped board" in capsys.readouterr().err
    assert backend.calls == [("resolve_board", {"cwd": Path.cwd().resolve()})]


def test_board_resolution_os_error_fails_without_traceback(monkeypatch, capsys) -> None:
    class OSErrorBackend(FakeBackend):
        def resolve_board(self, *, cwd: Path) -> str:
            raise OSError("cannot resolve cwd")

    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    result = main(["done", "--task", "t_12345678"], backend=OSErrorBackend())

    assert result == 1
    assert "cannot resolve cwd" in capsys.readouterr().err


def test_malformed_board_metadata_never_falls_back_to_environment(
    monkeypatch, capsys,
) -> None:
    class MalformedBackend(FakeBackend):
        def resolve_board(self, *, cwd: Path) -> str:
            raise RuntimeError("missing default_workdir")

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")

    result = main(["done", "--task", "t_12345678"], backend=MalformedBackend())

    assert result == 1
    assert "missing default_workdir" in capsys.readouterr().err


def test_repository_bin_runs_without_package_install(
    tmp_path: Path, reviewed_release
) -> None:
    pin = (REPO / "patches/hermes-agent-supported-upstream").read_text().strip()
    carried_head = [
        line
        for line in (REPO / "patches/hermes-agent-carried-commits").read_text().splitlines()
        if line and not line.startswith("#")
    ][-1]
    home = tmp_path / "home"
    checkout = home / ".hermes/hermes-agent"
    checkout.mkdir(parents=True)
    # 어댑터는 선택자가 가리키는 릴리스를 실행한다. 체크아웃은 검토된 업스트림만
    # 제공하므로 반입된 끝 커밋에 위치하는 일이 없다.
    reviewed_release(checkout, pin.lower(), carried_head.lower())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git = fake_bin / "git"
    git.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  *"rev-parse origin/main") printf "%s\\n" "{pin}" ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    hermes = fake_bin / "hermes"
    hermes.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "Hermes Agent test (upstream {pin})"\n',
        encoding="utf-8",
    )
    git.chmod(0o755)
    hermes.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env.pop("HERMES_AGENT_REPO", None)

    result = subprocess.run(
        ["bash", str(REPO / "bin/kanban-adapter"), "--help"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "kanban-adapter" in result.stdout


def test_session_viewer_bin_runs_without_package_install() -> None:
    result = subprocess.run(
        ["bash", str(REPO / "bin/ai-session-viewer"), "--help"],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "AI Session Viewer" in result.stdout
