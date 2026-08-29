from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "scripts/bootstrap-hermes-macos.sh"


def rendered_bootstrap(tmp_path: Path, replacements: dict[str, str]) -> Path:
    fixture_root = tmp_path / "repo"
    (fixture_root / "scripts").mkdir(parents=True)
    (fixture_root / "patches").mkdir()
    for name in (
        "hermes-agent-bootstrap-manifest",
        "hermes-agent-supported-upstream",
    ):
        (fixture_root / "patches" / name).write_bytes(
            (REPO / "patches" / name).read_bytes()
        )
    fake_git = fixture_root / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = -C ]; then repo=$2; shift 2; else exit 91; fi\n"
        "case \"${1:-}\" in\n"
        "  rev-parse) cat \"$repo/.git/HEAD\" ;;;\n"
        "  symbolic-ref) exit 1 ;;;\n"
        "  diff-index) test ! -e \"$repo/.git/dirty\" && printf '#!/bin/sh\\nexit 0\\n' | /usr/bin/cmp -s - \"$repo/hermes\" ;;;\n"
        "  ls-files) test \"${4:-}\" = hermes ;;;\n"
        "  *) exit 92 ;;;\n"
        "esac\n".replace(";;;", ";;"),
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    text = BOOTSTRAP.read_text(encoding="utf-8").replace(
        "/usr/bin/git", str(fake_git)
    )
    for old, new in replacements.items():
        text = text.replace(old, new)
    rendered = fixture_root / "scripts/bootstrap-hermes-macos.sh"
    rendered.write_text(text, encoding="utf-8")
    rendered.chmod(0o755)
    return rendered



def installer_artifact_commands() -> str:
    return (
        'mkdir -p "$HERMES_AGENT_REPO/.git" "$HERMES_AGENT_REPO/venv/bin" "$HERMES_HOME/bin" "$HOME/.local/bin"\n'
        'printf "%s\\n" "10b388300a63d83857fac3ca4f8b05b64e01bc50" >"$HERMES_AGENT_REPO/.git/HEAD"\n'
        "printf '{\\n  \"schemaVersion\": 1,\\n  \"pinnedCommit\": \"10b388300a63d83857fac3ca4f8b05b64e01bc50\",\\n  \"pinnedBranch\": \"main\",\\n  \"completedAt\": \"2026-08-30T00:00:00.000Z\"\\n}\\n' >\"$HERMES_AGENT_REPO/.hermes-bootstrap-complete\"\n"
        'printf "git\\n" >"$HERMES_AGENT_REPO/.install_method"\n'
        'printf "#!/bin/sh\\nexit 0\\n" >"$HERMES_AGENT_REPO/hermes"\n'
        'printf "#!/bin/sh\\nexit 0\\n" >"$HERMES_AGENT_REPO/venv/bin/python"\n'
        'printf "#!/bin/sh\\nexit 0\\n" >"$HERMES_HOME/bin/uv"\n'
        'chmod 755 "$HERMES_AGENT_REPO/hermes" "$HERMES_AGENT_REPO/venv/bin/python" "$HERMES_HOME/bin/uv"\n'
        'cat >"$HOME/.local/bin/hermes" <<LAUNCHER_EOF\n'
        '#!/usr/bin/env bash\n'
        'unset PYTHONPATH\n'
        'unset PYTHONHOME\n'
        'exec "$HERMES_AGENT_REPO/venv/bin/python" "$HERMES_AGENT_REPO/hermes" "\\$@"\n'
        'LAUNCHER_EOF\n'
        'chmod 755 "$HOME/.local/bin/hermes"\n'
    )


def create_bootstrap_artifacts(home: Path, agent_repo: Path, hermes_home: Path) -> None:
    (agent_repo / ".git").mkdir(parents=True, exist_ok=True)
    (agent_repo / ".git/HEAD").write_text("10b388300a63d83857fac3ca4f8b05b64e01bc50\n", encoding="utf-8")
    (agent_repo / ".hermes-bootstrap-complete").write_text(
        '{\n  "schemaVersion": 1,\n  "pinnedCommit": "10b388300a63d83857fac3ca4f8b05b64e01bc50",\n  "pinnedBranch": "main",\n  "completedAt": "2026-08-30T00:00:00.000Z"\n}\n', encoding="utf-8"
    )
    (agent_repo / ".install_method").write_text("git\n", encoding="utf-8")
    for executable in (agent_repo / "hermes", agent_repo / "venv/bin/python", hermes_home / "bin/uv"):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    launcher = home / ".local/bin/hermes"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\n"
        f'exec "{agent_repo}/venv/bin/python" "{agent_repo}/hermes" "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)

def test_bootstrap_rejects_installer_digest_mismatch_without_host_writes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    installer_ran = tmp_path / "installer-ran"
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = --output ]; then output=$2; shift 2; else shift; fi\n"
        "done\n"
        f"printf '#!/bin/sh\\ntouch {installer_ran}\\n' > \"$output\"\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {"/usr/bin/curl": str(curl), "/usr/bin/uname": str(uname)},
    )
    agent_repo = home / ".hermes/hermes-agent"
    hermes_home = home / ".hermes"
    result = subprocess.run(
        ["/bin/bash", str(rendered), str(agent_repo), str(hermes_home)],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "XDG_STATE_HOME": str(home / ".local/state"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "digest" in result.stderr.lower()
    assert not installer_ran.exists()
    assert not agent_repo.exists()
    assert not (home / ".local/state/unified-kanban/hermes-bootstrap.receipt").exists()


def test_bootstrap_uses_exact_pinned_installer_argv_and_scrubbed_environment(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = --output ]; then output=$2; shift 2; else shift; fi\n"
        "done\n"
        "/bin/cat >\"$output\" <<'EOF'\n"
        "#!/bin/bash\n"
        "printf '%s\\n' \"$@\" >\"$HOME/installer-argv\"\n"
        "env | LC_ALL=C sort >\"$HOME/installer-env\"\n"
        + installer_artifact_commands()
        + "EOF\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    shasum = fake_bin / "shasum"
    shasum.write_text(
        "#!/bin/sh\n"
        "tmp=$(/usr/bin/mktemp ${TMPDIR:-/tmp}/fake-shasum.XXXXXX) || exit 1\n"
        "trap '/bin/rm -f \"$tmp\"' EXIT HUP INT TERM\n"
        "/bin/cat >\"$tmp\"\n"
        "if /usr/bin/grep -q 'HERMES_AGENT_REPO/.hermes-bootstrap-complete' \"$tmp\"; then\n"
        "  printf '%s  -\\n' c0380bc1f78d3d662a77663ce20cc17e14cbc4bec35e61ab7a33bac5f3afed2d\n"
        "else /usr/bin/shasum -a 256 \"$tmp\"; fi\n",
        encoding="utf-8",
    )
    shasum.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {
            "/usr/bin/curl": str(curl),
            "/usr/bin/uname": str(uname),
            "/usr/bin/shasum": str(shasum),
        },
    )
    agent_repo = home / "custom/hermes-agent"
    hermes_home = home / "custom/hermes-home"
    state_home = home / "custom/state"
    result = subprocess.run(
        ["/bin/bash", str(rendered), str(agent_repo), str(hermes_home)],
        env={
            "HOME": str(home),
            "PATH": "/malicious/bin",
            "TMPDIR": "/tmp",
            "XDG_STATE_HOME": str(state_home),
            "GIT_CONFIG_GLOBAL": "/secret/gitconfig",
            "PYTHONPATH": "/secret/python",
            "UV_INDEX_URL": "https://secret.invalid",
            "NPM_CONFIG_REGISTRY": "https://secret.invalid",
            "GIT_ASKPASS": "/secret/askpass",
            "SSH_ASKPASS": "/secret/askpass",
            "UNIFIED_KANBAN_TRANSACTION_DIR": "/secret/transaction",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (home / "installer-argv").read_text(encoding="utf-8").splitlines() == [
        "--commit",
        "10b388300a63d83857fac3ca4f8b05b64e01bc50",
        "--dir",
        str(agent_repo),
        "--hermes-home",
        str(hermes_home),
        "--skip-setup",
        "--non-interactive",
    ]
    child_env = (home / "installer-env").read_text(encoding="utf-8")
    assert f"HERMES_AGENT_REPO={agent_repo}\n" in child_env
    assert f"HERMES_HOME={hermes_home}\n" in child_env
    for forbidden in (
        "GIT_CONFIG_GLOBAL",
        "PYTHONPATH",
        "UV_INDEX_URL",
        "NPM_CONFIG_REGISTRY",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "UNIFIED_KANBAN_TRANSACTION_DIR",
    ):
        assert forbidden not in child_env
    receipt = (state_home / "unified-kanban/hermes-bootstrap.receipt").read_text(
        encoding="utf-8"
    )
    assert "status=bootstrap-complete\n" in receipt
    assert "python_requirement=3.11\n" in receipt
    assert "node_major=26\n" in receipt
    assert "toolchain_resolution=moving-patch-and-tool-versions\n" in receipt



def test_bootstrap_refuses_partial_receipt_before_fetching_installer(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = home / ".local/state"
    receipt = state_home / "unified-kanban/hermes-bootstrap.receipt"
    receipt.parent.mkdir(parents=True)
    receipt.parent.chmod(0o700)
    receipt.write_text("partial\n", encoding="utf-8")
    receipt.chmod(0o600)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl_log = tmp_path / "curl-ran"
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!/bin/sh\n/usr/bin/touch {curl_log}\nexit 1\n", encoding="utf-8"
    )
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {"/usr/bin/curl": str(curl), "/usr/bin/uname": str(uname)},
    )

    result = subprocess.run(
        [
            "/bin/bash",
            str(rendered),
            str(home / ".hermes/hermes-agent"),
            str(home / ".hermes"),
        ],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "XDG_STATE_HOME": str(state_home),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "partial" in result.stderr.lower() or "existing" in result.stderr.lower()
    assert receipt.read_text(encoding="utf-8") == "partial\n"
    assert not curl_log.exists()



def test_bootstrap_default_receipt_matches_setup_state_authority(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = --output ]; then output=$2; shift 2; else shift; fi\n"
        "done\n"
        "/bin/cat >\"$output\" <<'EOF'\n"
        "#!/bin/bash\n"
        f"{installer_artifact_commands()}"
        "EOF\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    shasum = fake_bin / "shasum"
    shasum.write_text(
        "#!/bin/sh\n"
        "tmp=$(/usr/bin/mktemp ${TMPDIR:-/tmp}/fake-shasum.XXXXXX) || exit 1\n"
        "trap '/bin/rm -f \"$tmp\"' EXIT HUP INT TERM\n"
        "/bin/cat >\"$tmp\"\n"
        "if /usr/bin/grep -q 'HERMES_AGENT_REPO/.hermes-bootstrap-complete' \"$tmp\"; then\n"
        "  printf '%s  -\\n' c0380bc1f78d3d662a77663ce20cc17e14cbc4bec35e61ab7a33bac5f3afed2d\n"
        "else /usr/bin/shasum -a 256 \"$tmp\"; fi\n",
        encoding="utf-8",
    )
    shasum.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {
            "/usr/bin/curl": str(curl),
            "/usr/bin/uname": str(uname),
            "/usr/bin/shasum": str(shasum),
        },
    )
    agent_repo = home / ".hermes/hermes-agent"
    hermes_home = home / ".hermes"

    result = subprocess.run(
        ["/bin/bash", str(rendered), str(agent_repo), str(hermes_home)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (home / ".local/state/unified-kanban/hermes-bootstrap.receipt").is_file()
    assert not (hermes_home / "state/unified-kanban/hermes-bootstrap.receipt").exists()


def test_bootstrap_refuses_non_darwin_before_fetch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl_log = tmp_path / "curl-ran"
    curl = fake_bin / "curl"
    curl.write_text(f"#!/bin/sh\n/usr/bin/touch {curl_log}\nexit 1\n", encoding="utf-8")
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Linux\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {"/usr/bin/curl": str(curl), "/usr/bin/uname": str(uname)},
    )

    result = subprocess.run(
        ["/bin/bash", str(rendered), str(home / ".hermes/hermes-agent"), str(home / ".hermes")],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "only on macos" in result.stderr.lower()
    assert not curl_log.exists()


def test_bootstrap_refuses_existing_foreign_marker_before_fetch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    agent_repo = home / ".hermes/hermes-agent"
    agent_repo.mkdir(parents=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl_log = tmp_path / "curl-ran"
    curl = fake_bin / "curl"
    curl.write_text(f"#!/bin/sh\n/usr/bin/touch {curl_log}\nexit 1\n", encoding="utf-8")
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {"/usr/bin/curl": str(curl), "/usr/bin/uname": str(uname)},
    )

    result = subprocess.run(
        ["/bin/bash", str(rendered), str(agent_repo), str(home / ".hermes")],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "existing or partial" in result.stderr.lower()
    assert not curl_log.exists()


def test_bootstrap_refuses_nested_symlink_ancestor_before_fetch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    real = home / "real"
    nested = real / "nested"
    nested.mkdir(parents=True)
    linked = home / "linked"
    linked.symlink_to(real, target_is_directory=True)
    agent_repo = linked / "nested/hermes-agent"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl_log = tmp_path / "curl-ran"
    curl = fake_bin / "curl"
    curl.write_text(f"#!/bin/sh\n/usr/bin/touch {curl_log}\nexit 1\n", encoding="utf-8")
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {"/usr/bin/curl": str(curl), "/usr/bin/uname": str(uname)},
    )

    result = subprocess.run(
        ["/bin/bash", str(rendered), str(agent_repo), str(home / ".hermes")],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert not curl_log.exists()



def test_bootstrap_status_refuses_hardlinked_receipt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    agent_repo = home / ".hermes/hermes-agent"
    hermes_home = home / ".hermes"
    create_bootstrap_artifacts(home, agent_repo, hermes_home)
    state_dir = home / ".local/state/unified-kanban"
    state_dir.mkdir(parents=True)
    state_dir.chmod(0o700)
    receipt = state_dir / "hermes-bootstrap.receipt"
    receipt.write_text(
        "format=unified-kanban-hermes-bootstrap-receipt-v1\n"
        "upstream=10b388300a63d83857fac3ca4f8b05b64e01bc50\n"
        f"agent_repo={agent_repo}\n"
        f"hermes_home={hermes_home}\n"
        "status=bootstrap-complete\n"
        "python_requirement=3.11\n"
        "node_major=26\n"
        "toolchain_resolution=moving-patch-and-tool-versions\n",
        encoding="utf-8",
    )
    receipt.chmod(0o600)
    (state_dir / "receipt-alias").hardlink_to(receipt)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {"/usr/bin/uname": str(uname)},
    )

    result = subprocess.run(
        ["/bin/bash", str(rendered), "--status", str(agent_repo), str(hermes_home)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "hard" in result.stderr.lower() or "link" in result.stderr.lower()



def bootstrap_receipt_bytes(agent_repo: Path, hermes_home: Path) -> bytes:
    return (
        "format=unified-kanban-hermes-bootstrap-receipt-v1\n"
        "upstream=10b388300a63d83857fac3ca4f8b05b64e01bc50\n"
        f"agent_repo={agent_repo}\n"
        f"hermes_home={hermes_home}\n"
        "status=bootstrap-complete\n"
        "python_requirement=3.11\n"
        "node_major=26\n"
        "toolchain_resolution=moving-patch-and-tool-versions\n"
    ).encode()


def status_fixture(
    tmp_path: Path,
    *,
    receipt_suffix: bytes = b"",
    receipt_mode: int = 0o600,
    state_mode: int = 0o700,
    symlink_state_home: bool = False,
    target_suffix: tuple[str, bytes] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir()
    agent_repo = home / ".hermes/hermes-agent"
    hermes_home = home / ".hermes"
    create_bootstrap_artifacts(home, agent_repo, hermes_home)
    real_state_home = home / "real-state"
    state_dir = real_state_home / "unified-kanban"
    state_dir.mkdir(parents=True)
    state_dir.chmod(state_mode)
    state_home = real_state_home
    if symlink_state_home:
        state_home = home / "state-link"
        state_home.symlink_to(real_state_home, target_is_directory=True)
    receipt = state_dir / "hermes-bootstrap.receipt"
    receipt.write_bytes(bootstrap_receipt_bytes(agent_repo, hermes_home) + receipt_suffix)
    receipt.chmod(receipt_mode)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    rendered = rendered_bootstrap(tmp_path, {"/usr/bin/uname": str(uname)})
    if target_suffix is not None:
        target, suffix = target_suffix
        targets = {
            "manifest": rendered.parent.parent / "patches/hermes-agent-bootstrap-manifest",
            "receipt": receipt,
            "head": agent_repo / ".git/HEAD",
            "method": agent_repo / ".install_method",
            "launcher": home / ".local/bin/hermes",
            "marker": agent_repo / ".hermes-bootstrap-complete",
            "hermes": agent_repo / "hermes",
        }
        targets[target].write_bytes(targets[target].read_bytes() + suffix)
    result = subprocess.run(
        ["/bin/bash", str(rendered), "--status", str(agent_repo), str(hermes_home)],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "XDG_STATE_HOME": str(state_home),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    return result, receipt


@pytest.mark.parametrize("target", ["manifest", "receipt", "head", "method", "launcher"])
@pytest.mark.parametrize("suffix", [b"\n", b"\n\n"])
def test_bootstrap_status_rejects_noncanonical_trailing_bytes(
    tmp_path: Path, target: str, suffix: bytes
) -> None:
    result, _ = status_fixture(tmp_path, target_suffix=(target, suffix))
    assert result.returncode != 0
    assert "digest" in result.stderr.lower() or "marker" in result.stderr.lower()


def test_bootstrap_status_rejects_malformed_marker_and_modified_checkout(
    tmp_path: Path,
) -> None:
    marker_result, _ = status_fixture(
        tmp_path / "marker", target_suffix=("marker", b'extra-field\n')
    )
    assert marker_result.returncode != 0
    assert "marker" in marker_result.stderr.lower()

    dirty_result, _ = status_fixture(
        tmp_path / "dirty", target_suffix=("hermes", b'# modified\n')
    )
    assert dirty_result.returncode != 0
    assert "tracked" in dirty_result.stderr.lower()



def test_bootstrap_status_requires_exact_private_modes(tmp_path: Path) -> None:
    receipt_result, _ = status_fixture(tmp_path / "receipt", receipt_mode=0o644)
    state_result, _ = status_fixture(tmp_path / "state", state_mode=0o755)
    assert receipt_result.returncode != 0
    assert state_result.returncode != 0
    assert "0600" in receipt_result.stderr or "mode" in receipt_result.stderr.lower()
    assert "0700" in state_result.stderr or "mode" in state_result.stderr.lower()


def test_bootstrap_status_refuses_symlinked_state_authority(tmp_path: Path) -> None:
    result, _ = status_fixture(tmp_path, symlink_state_home=True)
    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()


def test_bootstrap_preserves_receipt_successor_created_by_installer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    successor_commands = (
        'mkdir -p "$HOME/.local/state/unified-kanban"\n'
        'printf "foreign-successor\\n" >"$HOME/.local/state/unified-kanban/hermes-bootstrap.receipt"\n'
        'chmod 600 "$HOME/.local/state/unified-kanban/hermes-bootstrap.receipt"\n'
    )
    curl.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = --output ]; then output=$2; shift 2; else shift; fi\n"
        "done\n"
        "/bin/cat >\"$output\" <<'EOF'\n"
        "#!/bin/bash\n"
        f"{installer_artifact_commands()}"
        f"{successor_commands}"
        "EOF\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    shasum = fake_bin / "shasum"
    shasum.write_text(
        "#!/bin/sh\n"
        "tmp=$(/usr/bin/mktemp ${TMPDIR:-/tmp}/fake-shasum.XXXXXX) || exit 1\n"
        "trap '/bin/rm -f \"$tmp\"' EXIT HUP INT TERM\n"
        "/bin/cat >\"$tmp\"\n"
        "if /usr/bin/grep -q 'HERMES_AGENT_REPO/.hermes-bootstrap-complete' \"$tmp\"; then\n"
        "  printf '%s  -\\n' c0380bc1f78d3d662a77663ce20cc17e14cbc4bec35e61ab7a33bac5f3afed2d\n"
        "else /usr/bin/shasum -a 256 \"$tmp\"; fi\n",
        encoding="utf-8",
    )
    shasum.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {
            "/usr/bin/curl": str(curl),
            "/usr/bin/uname": str(uname),
            "/usr/bin/shasum": str(shasum),
        },
    )
    receipt = home / ".local/state/unified-kanban/hermes-bootstrap.receipt"
    result = subprocess.run(
        ["/bin/bash", str(rendered), str(home / ".hermes/hermes-agent"), str(home / ".hermes")],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert receipt.exists(), result.stderr
    assert receipt.read_text(encoding="utf-8") == "foreign-successor\n"


def test_setup_delegates_bootstrap_identity_to_status_helper() -> None:
    setup = (REPO / "scripts/setup.sh").read_text(encoding="utf-8")

    assert "unified-kanban-hermes-bootstrap-receipt-v1" not in setup
    assert "c0380bc1f78d3d662a77663ce20cc17e14cbc4bec35e61ab7a33bac5f3afed2d" not in setup
    assert '"$BOOTSTRAP_HELPER" --status' in setup
