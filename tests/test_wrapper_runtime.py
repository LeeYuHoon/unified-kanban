"""실제 설치 링크와 격리된 release로 래퍼의 인터프리터 경계를 검증한다."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import venv

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPERS = ["claude-kanban-hook", "codex-kanban-hook", "kanban-adapter"]


@pytest.fixture
def runtime_tree(tmp_path, reviewed_release):
    """운영 훅 대신 import와 FD/환경만 관측하는 fixture 본문을 실행한다."""
    repo = tmp_path / "repo with spaces"
    shutil.copytree(ROOT / "bin", repo / "bin")
    shutil.copytree(ROOT / "src", repo / "src")
    shutil.copytree(ROOT / "patches", repo / "patches", ignore=shutil.ignore_patterns("*.bundle"))
    from kanban_adapter.compatibility import read_supported_upstream, read_carried_commits
    agent = tmp_path / "agent"
    agent.mkdir()
    layout = reviewed_release(agent, read_supported_upstream(), read_carried_commits()[-1])
    venv.EnvBuilder(with_pip=False).create(layout.release / "venv")
    probe = ('import json,sys,os\n'
             'import kanban_adapter.conversation_runtime\n'
             'print(json.dumps({"prefix":sys.prefix,"argv":sys.argv[1:],'
             '"guard":os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"),'
             '"task":os.environ.get("HERMES_KANBAN_TASK")}))\n')
    for module in ("claude_hook", "codex_hook", "cli"):
        (repo / f"src/kanban_adapter/{module}.py").write_text(
            probe if module != "claude_hook" else
            'def main():\n' + ''.join('    ' + line + '\n' for line in probe.splitlines()) + '    return 0\n')
    installed = tmp_path / "installed"
    installed.mkdir()
    for name in WRAPPERS:
        (installed / name).symlink_to(os.path.relpath(repo / "bin" / name, installed))
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "python3").write_text('#!/bin/sh\nprintf PRIVATE_PATH_CANARY >&2\nexit 93\n')
    (poison / "python3").chmod(0o755)
    (poison / "kanban_adapter.py").write_text('raise RuntimeError("PRIVATE_IMPORT_CANARY")\n')
    env = dict(os.environ, HOME=str(tmp_path), HERMES_AGENT_REPO=str(agent),
               PATH=f"{poison}:/usr/bin:/bin", PYTHONPATH=str(poison),
               PYTHONHOME=str(poison), HERMES_DELEGATED_CHILD_CONTEXT="fixture-child",
               HERMES_KANBAN_TASK="fixture-task")
    return repo, layout, installed, env


@pytest.mark.parametrize("name", WRAPPERS)
@pytest.mark.parametrize("installed", [False, True])
def test_setup_release_runtime_wins_over_hostile_path_without_repo_venv(runtime_tree, name, installed):
    repo, layout, links, env = runtime_tree
    result = subprocess.run([str((links if installed else repo / "bin") / name), "prompt"],
                            input="{}", text=True, capture_output=True, env=env, timeout=15)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {"prefix": str(layout.release / "venv"), "argv": ["prompt"],
                        "guard": "fixture-child", "task": "fixture-task"}
    assert "PRIVATE_" not in result.stdout + result.stderr


@pytest.mark.parametrize("name", WRAPPERS)
@pytest.mark.parametrize("damage", ["missing", "old", "receipt", "selector"])
def test_unusable_runtime_never_runs_body_or_leaks_input(runtime_tree, name, damage):
    repo, layout, links, env = runtime_tree
    executable = layout.release / "venv/bin/python"
    if damage in {"missing", "old"}:
        executable.unlink()
        if damage == "old":
            # macOS /usr/bin의 중계기는 python으로 이름을 바꾸면 실행되지 않는다.
            system = subprocess.run(["/usr/bin/python3", "-I", "-c", "import sys; print(sys.executable)"],
                                    capture_output=True, text=True, check=True)
            executable.symlink_to(system.stdout.strip())
            version = subprocess.run([str(executable), "-I", "-c", "import sys; print(sys.version_info < (3, 11))"],
                                     capture_output=True, text=True, check=True)
            if version.stdout.strip() != "True":
                pytest.skip("실제 구형 시스템 Python이 없는 호스트")
    elif damage == "selector":
        layout.selector.write_text(str(repo) + "\n")
    else:
        from kanban_adapter.release_layout import COMPLETION_RECEIPT_NAME
        (layout.release / COMPLETION_RECEIPT_NAME).write_text("{}")
    result = subprocess.run([str(links / name), "prompt"], input="PRIVATE_INPUT_CANARY",
                            text=True, capture_output=True, env=env, timeout=15)
    assert result.returncode == (1 if name == "kanban-adapter" else 0)
    assert "prefix" not in result.stdout
    assert "PRIVATE_" not in result.stdout + result.stderr
    assert ("compatibility-rejected" if damage in {"receipt", "selector"} else "collection-failed") in result.stderr
    if name == "claude-kanban-hook":
        assert "systemMessage" in json.loads(result.stdout)


@pytest.mark.parametrize("name", WRAPPERS)
def test_actual_old_path_python_is_only_bootstrap(runtime_tree, name):
    repo, layout, links, env = runtime_tree
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run([str(links / name), "prompt"], input="{}", text=True,
                            capture_output=True, env=env, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["prefix"] == str(layout.release / "venv")


@pytest.mark.parametrize("module,name", [("claude_hook", "claude-kanban-hook"), ("codex_hook", "codex-kanban-hook")])
def test_hook_to_sibling_adapter_keeps_runtime_and_guards(runtime_tree, module, name):
    repo, layout, links, env = runtime_tree
    body = ('import subprocess\nfrom pathlib import Path\n'
            'p = Path(__file__).resolve().parents[2] / "bin/kanban-adapter"\n'
            'subprocess.run([str(p), "sibling"], check=True)\n')
    if module == "claude_hook":
        body = 'def main():\n' + ''.join('    ' + line + '\n' for line in body.splitlines()) + '    return 0\n'
    (repo / f"src/kanban_adapter/{module}.py").write_text(body)
    result = subprocess.run([str(links / name), "prompt"], input="{}", text=True,
                            capture_output=True, env=env, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"prefix": str(layout.release / "venv"), "argv": ["sibling"],
                                       "guard": "fixture-child", "task": "fixture-task"}