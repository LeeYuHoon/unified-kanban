"""명시적 네이티브 실행기의 자식 전용 권한과 환경 보존을 검증한다."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts/launch-collected-agent.sh"
GUARDS = ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK")


@pytest.fixture
def producer(tmp_path):
    """실제 에이전트 없이 임시 경로에만 파일을 만드는 실행 파일을 둔다."""
    binary = tmp_path / "bin"
    binary.mkdir()
    for agent in ("claude", "codex"):
        executable = binary / agent
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "root = pathlib.Path(os.environ['OUTPUT'])\n"
            "root.mkdir(exist_ok=True)\n"
            "with (root / 'session').open('a') as stream: stream.write('native')\n"
            "print(json.dumps({'args': sys.argv[1:], 'env': dict(os.environ)}))\n"
            "sys.exit(int(os.environ.get('CHILD_STATUS', '0')))\n"
        )
        executable.chmod(0o700)
    config = tmp_path / "fixture-config"
    config.touch()
    env = {
        "PATH": str(binary), "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex home"),
        "HERMES_HOME": str(tmp_path / "hermes home"), "HERMES_TEST_VALUE": "공백 $*\n",
        "UNIFIED_KANBAN_CONVERSATION_CONFIG": str(config),
        "OUTPUT": str(tmp_path / "output"),
    }
    return env


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_private_new_session_preserves_args_env_and_parent_umask(producer, agent):
    assert LAUNCHER.is_file(), "명시적 실행기가 아직 없다"
    args = ["", "space value", "$HOME;*", "한글\n줄", "--native-option"]
    result = subprocess.run(
        ["/bin/bash", "-c", 'umask 022; /bin/bash "$@"; status=$?; printf "MASK=%s\\n" "$(umask)" >&2; exit "$status"',
         "caller", str(LAUNCHER), agent, *args],
        env=producer, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["args"] == args
    assert {key: observed["env"][key] for key in producer} == producer
    output = Path(producer["OUTPUT"])
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "session").stat().st_mode) == 0o600
    assert "MASK=0022" in result.stderr


@pytest.mark.parametrize("guard", GUARDS)
@pytest.mark.parametrize("value", ["true", "0", ""])
def test_guard_refuses_without_child(producer, guard, value):
    producer[guard] = value
    result = subprocess.run(["/bin/bash", str(LAUNCHER), "codex"], env=producer, capture_output=True)
    assert result.returncode != 0
    assert not Path(producer["OUTPUT"]).exists()


@pytest.mark.parametrize("config", [None, "", "/missing/fixture-config"])
def test_missing_config_refuses_without_child(producer, config):
    if config is None:
        producer.pop("UNIFIED_KANBAN_CONVERSATION_CONFIG")
    else:
        producer["UNIFIED_KANBAN_CONVERSATION_CONFIG"] = config
    result = subprocess.run(["/bin/bash", str(LAUNCHER), "claude"], env=producer, capture_output=True)
    assert result.returncode != 0
    assert not Path(producer["OUTPUT"]).exists()


@pytest.mark.parametrize("args", [[], ["invalid"], ["../bin/codex"], ["--help"]])
def test_invalid_agent_fails_closed(producer, args):
    result = subprocess.run(["/bin/bash", str(LAUNCHER), *args], env=producer, capture_output=True)
    assert result.returncode != 0
    assert not Path(producer["OUTPUT"]).exists()


def test_existing_file_is_not_repaired_and_child_status_is_preserved(producer):
    output = Path(producer["OUTPUT"])
    output.mkdir(mode=0o755)
    session = output / "session"
    session.touch()
    session.chmod(0o644)
    producer["CHILD_STATUS"] = "37"
    result = subprocess.run(["/bin/bash", str(LAUNCHER), "codex", "resume"], env=producer, capture_output=True)
    assert result.returncode == 37
    assert stat.S_IMODE(session.stat().st_mode) == 0o644
