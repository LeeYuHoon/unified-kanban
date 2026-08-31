from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clear_hermes_auxiliary_process_context(monkeypatch) -> None:
    """위임된 CI 워커 안에서도 부모 프로세스의 테스트 기준 상태를 안정적으로 유지한다."""
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)


@pytest.fixture
def reviewed_release():
    """설치된 호스트가 실제로 실행하는 불변 릴리스를 빌드한다.

    Hermes 체크아웃은 반입된 끝 커밋으로 절대 이동하지 않으므로, 현실적인
    픽스처는 검토된 업스트림 상태의 체크아웃과, 생산자 영수증이 있는 릴리스
    하나 및 그 릴리스를 가리키는 선택자를 담은 형제 릴리스 루트로 구성된다.
    """
    helper_path = ROOT / "scripts" / "hermes-release-manager.py"
    spec = importlib.util.spec_from_file_location("hermes_release_manager", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    def build(agent_repo: Path, upstream: str, carried: str, *, select: bool = True):
        layout = helper.release_layout(agent_repo, upstream, carried)
        layout.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        layout.release.mkdir(mode=0o700)
        subprocess.run(
            ["git", "init", "-q", "--initial-branch=main"],
            cwd=layout.release,
            check=True,
            capture_output=True,
        )
        (layout.release / "pyproject.toml").write_text(
            "[project]\nname = \"hermes-agent\"\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "-A"], cwd=layout.release, check=True, capture_output=True)
        subprocess.run(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-qm", "reviewed release",
            ],
            cwd=layout.release,
            check=True,
            capture_output=True,
        )
        executables = layout.release / "venv" / "bin"
        executables.mkdir(parents=True)
        (executables / "python").symlink_to(sys.executable)
        hermes = executables / "hermes"
        hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hermes.chmod(0o755)
        release_info = layout.release.stat()
        receipt = {
            "version": 2,
            "release_identity": [release_info.st_dev, release_info.st_ino],
            "upstream": upstream,
            "carried": carried,
        }
        receipt_path = layout.release / helper._COMPLETION_RECEIPT
        receipt_path.write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        receipt_path.chmod(0o600)
        if select:
            layout.selector.write_text(f"{layout.release}\n", encoding="utf-8")
            layout.selector.chmod(0o600)
        return layout

    return build
