from __future__ import annotations

import os
from pathlib import Path
import plistlib
import runpy
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/verify-macos-launchd-service.py"


def write_plist(
    path: Path,
    program: str,
    *,
    label: str = "ai.hermes.gateway",
    arguments: list[str] | None = None,
) -> None:
    release_root = Path(program).parents[3]
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": arguments or [program, "-m", "hermes_cli.main"],
                "EnvironmentVariables": {
                    "HERMES_DISABLE_LAZY_INSTALLS": "1",
                    "HERMES_LAZY_INSTALL_TARGET": str(release_root / "lazy-packages"),
                },
                "Umask": 0o077,
            }
        )
    )


def fake_launchctl(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "program"
    command = tmp_path / "launchctl"
    command.write_text(
        "#!/bin/sh\n"
        f"exec 3< {shlex.quote(str(state))}\n"
        "IFS= read -r program <&3\n"
        "printf 'gui/501/ai.hermes.gateway = {\\n\\tstate = running\\n\\tprogram = %s\\n\\targuments = {\\n' \"$program\"\n"
        "printf '\\t\\t%s\\n' \"$program\"\n"
        "while IFS= read -r argument <&3; do printf '\\t\\t%s\\n' \"$argument\"; done\n"
        "printf '\\t}\\n\\tenvironment = {\\n'\n"
        "printf '\\t\\tHERMES_DISABLE_LAZY_INSTALLS => 1\\n'\n"
        f"printf '\\t\\tHERMES_LAZY_INSTALL_TARGET => {tmp_path / 'releases/lazy-packages'}\\n'\n"
        "printf '\\t}\\n}\\n'\n",
        encoding="utf-8",
    )
    command.chmod(0o755)
    return command, state


def managed_program(tmp_path: Path) -> str:
    release_root = tmp_path / "releases"
    program = release_root / ("release-" + "a" * 40) / "venv/bin/python"
    program.parent.mkdir(parents=True)
    release_root.chmod(0o700)
    program.write_text("python\n", encoding="utf-8")
    program.chmod(0o755)
    return str(program)


def run_helper(
    plist: Path,
    launchctl: Path,
    state: Path,
    *,
    allow_unsealed_environment: bool = False,
    expected_release: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
            sys.executable,
            str(HELPER),
            str(plist),
            "--launchctl",
            str(launchctl),
            "--stable-seconds",
            "0",
        ]
    if allow_unsealed_environment:
        command.append("--allow-unsealed-environment")
    if expected_release is not None:
        command.extend(("--expected-release", str(expected_release)))
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )


def test_identity_detects_same_inode_in_place_rewrite(tmp_path: Path) -> None:
    identity = runpy.run_path(str(HELPER))["identity"]
    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_bytes(b"before")
    before = plist.stat()
    plist.write_bytes(b"after!")
    os.utime(
        plist,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
    )
    after = plist.stat()

    assert before.st_ino == after.st_ino
    assert identity(before) != identity(after)


def test_rejects_plist_bound_to_a_different_expected_release(tmp_path: Path) -> None:
    expected = managed_program(tmp_path)
    plist = tmp_path / "ai.hermes.gateway.plist"
    write_plist(plist, expected)
    launchctl, state = fake_launchctl(tmp_path)
    state.write_text(f"{expected}\n-m\nhermes_cli.main\n", encoding="utf-8")
    different = Path(expected).parents[3] / f"release-{'b' * 40}"

    result = run_helper(
        plist, launchctl, state, expected_release=different
    )

    assert result.returncode != 0
    assert "expected release" in result.stderr


def test_rejects_loaded_job_from_a_stale_release(tmp_path: Path) -> None:
    expected = managed_program(tmp_path)
    plist = tmp_path / "ai.hermes.gateway.plist"
    write_plist(plist, expected)
    launchctl, state = fake_launchctl(tmp_path)
    state.write_text(
        "/releases/old/venv/bin/python\n-m\nhermes_cli.main\n", encoding="utf-8"
    )

    result = run_helper(plist, launchctl, state)

    assert result.returncode != 0
    assert "loaded launchd program does not match plist" in result.stderr


def test_accepts_stably_loaded_exact_plist_program(tmp_path: Path) -> None:
    expected = managed_program(tmp_path)
    plist = tmp_path / "ai.hermes.gateway.plist"
    write_plist(plist, expected)
    launchctl, state = fake_launchctl(tmp_path)
    state.write_text(f"{expected}\n-m\nhermes_cli.main\n", encoding="utf-8")

    result = run_helper(plist, launchctl, state)

    assert result.returncode == 0, result.stderr


def test_sealed_mode_accepts_exact_preserved_plist_environment(tmp_path: Path) -> None:
    expected = managed_program(tmp_path)
    plist = tmp_path / "ai.hermes.gateway.plist"
    write_plist(plist, expected)
    payload = plistlib.loads(plist.read_bytes())
    payload["EnvironmentVariables"]["PATH"] = "/usr/bin:/bin"
    plist.write_bytes(plistlib.dumps(payload))
    launchctl, state = fake_launchctl(tmp_path)
    launchctl.write_text(
        launchctl.read_text(encoding="utf-8").replace(
            "printf '\\t}\\n}\\n'",
            "printf '\\t\\tPATH => /usr/bin:/bin\\n"
            "\\t\\tOSLogRateLimit => 64\\n"
            "\\t\\tXPC_SERVICE_NAME => ai.hermes.gateway\\n"
            "\\t}\\n}\\n'",
        ),
        encoding="utf-8",
    )
    state.write_text(f"{expected}\n-m\nhermes_cli.main\n", encoding="utf-8")

    result = run_helper(plist, launchctl, state)

    assert result.returncode == 0, result.stderr


def test_rejects_loaded_job_with_stale_arguments(tmp_path: Path) -> None:
    expected = managed_program(tmp_path)
    arguments = [expected, "-m", "hermes_cli.main", "gateway", "run"]
    plist = tmp_path / "ai.hermes.gateway.plist"
    write_plist(plist, expected, arguments=arguments)
    launchctl, state = fake_launchctl(tmp_path)
    state.write_text(
        "\n".join([expected, "-m", "hermes_cli.main", "gateway", "status"]) + "\n",
        encoding="utf-8",
    )

    result = run_helper(plist, launchctl, state)

    assert result.returncode != 0
    assert "loaded launchd arguments do not match plist" in result.stderr


def test_rejects_non_gateway_label(tmp_path: Path) -> None:
    expected = managed_program(tmp_path)
    plist = tmp_path / "other.plist"
    write_plist(plist, expected, label="attacker.selected.label")
    launchctl, state = fake_launchctl(tmp_path)
    state.write_text(f"{expected}\n-m\nhermes_cli.main\n", encoding="utf-8")

    result = run_helper(plist, launchctl, state)

    assert result.returncode != 0
    assert "unexpected launchd label" in result.stderr


def test_rejects_loaded_job_without_sealed_environment(tmp_path: Path) -> None:
    expected = managed_program(tmp_path)
    plist = tmp_path / "ai.hermes.gateway.plist"
    write_plist(plist, expected)
    launchctl, state = fake_launchctl(tmp_path)
    launchctl.write_text(
        launchctl.read_text(encoding="utf-8").replace(
            "HERMES_DISABLE_LAZY_INSTALLS => 1",
            "HERMES_DISABLE_LAZY_INSTALLS => 0",
        ),
        encoding="utf-8",
    )
    state.write_text(f"{expected}\n-m\nhermes_cli.main\n", encoding="utf-8")

    result = run_helper(plist, launchctl, state)

    assert result.returncode != 0
    assert "loaded launchd environment does not match plist" in result.stderr


def test_rejects_lazy_target_outside_program_release_root(tmp_path: Path) -> None:
    expected = managed_program(tmp_path)
    plist = tmp_path / "ai.hermes.gateway.plist"
    write_plist(plist, expected)
    payload = plistlib.loads(plist.read_bytes())
    payload["EnvironmentVariables"]["HERMES_LAZY_INSTALL_TARGET"] = str(
        tmp_path / "foreign-lazy-packages"
    )
    plist.write_bytes(plistlib.dumps(payload))
    launchctl, state = fake_launchctl(tmp_path)
    state.write_text(f"{expected}\n-m\nhermes_cli.main\n", encoding="utf-8")

    result = run_helper(plist, launchctl, state)

    assert result.returncode != 0
    assert "managed release root" in result.stderr


def test_legacy_mode_rejects_extra_loaded_environment(tmp_path: Path) -> None:
    expected = managed_program(tmp_path)
    plist = tmp_path / "ai.hermes.gateway.plist"
    write_plist(plist, expected)
    launchctl, state = fake_launchctl(tmp_path)
    launchctl.write_text(
        launchctl.read_text(encoding="utf-8").replace(
            "printf '\\t}\\n}\\n'",
            "printf '\\t\\tFOREIGN_INJECTED => 1\\n\\t}\\n}\\n'",
        ),
        encoding="utf-8",
    )
    state.write_text(f"{expected}\n-m\nhermes_cli.main\n", encoding="utf-8")

    result = run_helper(
        plist,
        launchctl,
        state,
        allow_unsealed_environment=True,
    )

    assert result.returncode != 0
    assert "loaded launchd environment does not match plist" in result.stderr
