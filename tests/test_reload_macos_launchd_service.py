from __future__ import annotations

import importlib.util
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/reload-macos-launchd-service.py"
spec = importlib.util.spec_from_file_location("reload_macos_launchd_service", SCRIPT)
assert spec and spec.loader
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def plist(tmp_path: Path) -> Path:
    path = tmp_path / "ai.hermes.gateway.plist"
    path.write_bytes(plistlib.dumps({"Label": helper.LABEL, "ProgramArguments": ["/safe/python"]}))
    path.chmod(0o600)
    return path


def result(command: tuple[str, ...], code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, code, stdout, stderr)


def test_reload_loaded_service_retries_transient_bootstrap(tmp_path: Path) -> None:
    path = plist(tmp_path)
    calls: list[tuple[str, ...]] = []
    delays: list[float] = []
    bootstrap_results = iter((5, 0))

    def runner(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "print":
            return result(command)
        if command[1] == "bootstrap":
            return result(command, next(bootstrap_results))
        return result(command)

    helper.reload_service(path, runner=runner, sleeper=delays.append, uid=501)

    assert calls == [
        ("/bin/launchctl", "print", "gui/501/ai.hermes.gateway"),
        ("/bin/launchctl", "bootout", "gui/501/ai.hermes.gateway"),
        ("/bin/launchctl", "bootstrap", "gui/501", str(path)),
        ("/bin/launchctl", "bootstrap", "gui/501", str(path)),
        ("/bin/launchctl", "kickstart", "-k", "gui/501/ai.hermes.gateway"),
    ]
    assert delays == [1, 2]


def test_reload_accepts_only_exact_absent_contract(tmp_path: Path) -> None:
    path = plist(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "print":
            return result(command, 113, stderr=helper._absent_stderr(501))
        return result(command)

    helper.reload_service(path, runner=runner, sleeper=lambda _: None, uid=501)
    assert not any(command[1] == "bootout" for command in calls)


@pytest.mark.parametrize("code,stderr", [(113, "wrong\n"), (77, "unavailable\n")])
def test_reload_rejects_indeterminate_state(tmp_path: Path, code: int, stderr: str) -> None:
    path = plist(tmp_path)

    def runner(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return result(command, code, stderr=stderr)

    with pytest.raises(helper.ReloadError, match="indeterminate"):
        helper.reload_service(path, runner=runner, sleeper=lambda _: None, uid=501)


def test_reload_rejects_non_private_plist_before_launchctl(tmp_path: Path) -> None:
    path = plist(tmp_path)
    path.chmod(0o644)
    called = False

    def runner(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return result(command)

    with pytest.raises(helper.ReloadError, match="0600"):
        helper.reload_service(path, runner=runner, sleeper=lambda _: None, uid=501)
    assert not called
