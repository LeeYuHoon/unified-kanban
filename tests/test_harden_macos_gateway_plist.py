from __future__ import annotations

import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/harden-macos-gateway-plist.py"
SETUP = ROOT / "scripts/setup.sh"


def test_renders_private_sealed_gateway_plist(tmp_path: Path) -> None:
    release_root = tmp_path / "hermes-agent.releases"
    release = release_root / ("release-" + "a" * 40)
    python = release / "venv/bin/python"
    python.parent.mkdir(parents=True)
    release_root.chmod(0o700)
    python.write_text("python\n", encoding="utf-8")
    python.chmod(0o755)
    source = tmp_path / "ai.hermes.gateway.plist"
    source.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hermes.gateway",
                "ProgramArguments": [str(python), "-m", "hermes_cli.main", "gateway", "run"],
                "EnvironmentVariables": {"PATH": "/usr/bin:/bin"},
                "RunAtLoad": True,
            }
        )
    )
    source.chmod(0o644)
    candidate = tmp_path / "candidate.plist"

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(candidate), str(release_root)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = plistlib.loads(candidate.read_bytes())
    assert payload["EnvironmentVariables"]["HERMES_DISABLE_LAZY_INSTALLS"] == "1"
    assert payload["EnvironmentVariables"]["HERMES_LAZY_INSTALL_TARGET"] == str(
        release_root / "lazy-packages"
    )
    assert payload["Umask"] == 0o077
    info = candidate.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.getuid()
    assert info.st_nlink == 1


def test_setup_identity_hardens_and_reloads_gateway_plist() -> None:
    source = SETUP.read_text(encoding="utf-8")

    assert 'GATEWAY_PLIST="$HOME/Library/LaunchAgents/ai.hermes.gateway.plist"' in source
    gateway_block = source[source.index("if ((NO_RESTART == 0)); then") :]
    generate = "from hermes_cli.gateway import generate_launchd_plist"
    assert generate in gateway_block
    harden = 'python3 "$REPO_ROOT/scripts/harden-macos-gateway-plist.py"'
    assert harden in gateway_block
    generate_at = gateway_block.index(generate)
    harden_at = gateway_block.index(harden)
    replace_at = gateway_block.index('path-transaction.py" replace-file', harden_at)
    target_mode_at = gateway_block.index("--target-mode 0600", replace_at)
    checkpoint_at = gateway_block.index("checkpoint_stage", replace_at)
    reload_at = gateway_block.index(
        '"$REPO_ROOT/scripts/reload-macos-launchd-service.py"', checkpoint_at
    )
    install_at = gateway_block.index("--force --start-now --start-on-login", reload_at)
    verify_at = gateway_block.index(
        'ensure_macos_gateway_supervised "$HERMES_CLI" sealed "${HERMES_RELEASE:-}"',
        install_at,
    )
    assert (
        generate_at
        < harden_at
        < replace_at
        < target_mode_at
        < checkpoint_at
        < reload_at
        < install_at
        < verify_at
    )
    assert '"$HERMES_CLI" gateway stop' not in gateway_block
    assert '"$HERMES_CLI" gateway start' not in gateway_block


def test_helper_does_not_offer_unsafe_direct_replacement(tmp_path: Path) -> None:
    release_root = tmp_path / "hermes-agent.releases"
    release = release_root / ("release-" + "b" * 40)
    python = release / "venv/bin/python"
    python.parent.mkdir(parents=True)
    release_root.chmod(0o700)
    python.write_text("python\n", encoding="utf-8")
    python.chmod(0o755)
    source = tmp_path / "ai.hermes.gateway.plist"
    source.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hermes.gateway",
                "ProgramArguments": [str(python), "-m", "hermes_cli.main"],
                "EnvironmentVariables": {},
            }
        )
    )
    source.chmod(0o600)
    candidate = tmp_path / "candidate.plist"

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(candidate), str(release_root), "--replace"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert source.exists()
    assert not candidate.exists()
