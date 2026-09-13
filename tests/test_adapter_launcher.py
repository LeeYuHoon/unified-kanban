"""공유 훅은 HOME이 아닌 자기 저장소의 호환성 래퍼만 실행한다."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from kanban_adapter import claude_hook

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def selected_launcher(tmp_path, monkeypatch, reviewed_release):
    """실제 래퍼와 호환성 게이트 뒤에 FD 관측용 CLI만 둔다."""
    repo = tmp_path / "selected-worktree"
    shutil.copytree(ROOT / "src/kanban_adapter", repo / "src/kanban_adapter")
    (repo / "bin").mkdir()
    shutil.copy2(ROOT / "bin/kanban-adapter", repo / "bin/kanban-adapter")
    (repo / "patches").mkdir()
    upstream, carried = "a" * 40, "b" * 40
    (repo / "patches/hermes-agent-supported-upstream").write_text(upstream + "\n")
    manifest = repo / "patches/hermes-agent-carried-commits"
    manifest.write_text(carried + "\n")
    agent = tmp_path / "hermes-agent"
    agent.mkdir()
    reviewed_release(agent, upstream, carried)
    monkeypatch.setenv("HERMES_AGENT_REPO", str(agent))
    python_bin = tmp_path / "python-bin"
    python_bin.mkdir()
    (python_bin / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(python_bin) + os.pathsep + os.environ["PATH"])
    home = tmp_path / "home"
    foreign = home / ".local/bin/kanban-adapter"
    foreign.parent.mkdir(parents=True)
    marker = tmp_path / "foreign-ran"
    foreign.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf foreign\n")
    foreign.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(claude_hook, "__file__", str(repo / "src/kanban_adapter/claude_hook.py"))
    (repo / "src/kanban_adapter/cli.py").write_text(
        'import json, os, sys\n'
        'from pathlib import Path\n'
        'args = sys.argv[1:]\n'
        'title = Path(args[0].split("=", 1)[1]).read_text()\n'
        'result = Path(args[2]).read_text()\n'
        'os.write(int(args[3].split("=", 1)[1]), b"receipt")\n'
        'print(json.dumps({"cwd": os.getcwd(), "title": title, "result": result, '
        '"module": __file__}))\n'
    )
    project = tmp_path / "project"
    project.mkdir()
    return repo, project, marker, foreign


@pytest.mark.parametrize("foreign_present", [True, False])
def test_same_repository_wrapper_preserves_anonymous_fds_and_cwd(selected_launcher, foreign_present):
    repo, project, marker, foreign = selected_launcher
    if not foreign_present:
        foreign.unlink()
    with tempfile.TemporaryFile() as title, tempfile.TemporaryFile() as result, tempfile.TemporaryFile() as receipt:
        title.write(b"private title")
        result.write(b"private result")
        title.seek(0)
        result.seek(0)
        output = claude_hook.run_adapter([
            f"--title-file=/dev/fd/{title.fileno()}",
            "--result-file", f"/dev/fd/{result.fileno()}",
            f"--conversation-receipt-fd={receipt.fileno()}",
        ], project)
        assert not marker.exists(), "foreign HOME adapter executed"
        assert json.loads(output) == {
            "cwd": str(project.resolve()), "title": "private title", "result": "private result",
            "module": str(repo / "src/kanban_adapter/cli.py"),
        }
        receipt.seek(0)
        assert receipt.read() == b"receipt"


def test_selected_wrapper_keeps_compatibility_gate(selected_launcher):
    repo, project, marker, _ = selected_launcher
    (repo / "patches/hermes-agent-carried-commits").write_text("c" * 40 + "\n")
    with pytest.raises(RuntimeError, match="kanban-adapter failed"):
        claude_hook.run_adapter([], project)
    assert not marker.exists()


@pytest.mark.parametrize("layout", ["missing", "foreign-symlink", "wheel"])
def test_unavailable_repository_wrapper_fails_closed(selected_launcher, monkeypatch, layout):
    repo, project, marker, foreign = selected_launcher
    adapter = repo / "bin/kanban-adapter"
    adapter.unlink()
    if layout == "foreign-symlink":
        adapter.symlink_to(foreign)
    elif layout == "wheel":
        module = repo / "site-packages/kanban_adapter/claude_hook.py"
        module.parent.mkdir(parents=True)
        module.touch()
        monkeypatch.setattr(claude_hook, "__file__", str(module))
    with pytest.raises(RuntimeError, match="repository-owned.*scripts/setup.sh"):
        claude_hook.run_adapter([], project)
    assert not marker.exists()
