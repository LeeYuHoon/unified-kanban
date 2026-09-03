from __future__ import annotations

import shlex
import subprocess
import sys
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
        'mkdir -p "$HERMES_AGENT_REPO/.git" "$HERMES_AGENT_REPO/venv/bin" "$HERMES_AGENT_REPO/.hermes-runtime/python/generation-test/cpython-3.11-macos-aarch64-none/bin" "$HERMES_HOME/bin" "$HERMES_HOME/node/bin" "$HOME/.local/bin"\n'
        'printf "%s\\n" "63279301bcbdc185c1b07b98a9312eb0c862f26d" >"$HERMES_AGENT_REPO/.git/HEAD"\n'
        "printf '{\\n  \"schemaVersion\": 1,\\n  \"pinnedCommit\": \"63279301bcbdc185c1b07b98a9312eb0c862f26d\",\\n  \"pinnedBranch\": \"main\",\\n  \"completedAt\": \"2026-08-30T00:00:00.000Z\"\\n}\\n' >\"$HERMES_AGENT_REPO/.hermes-bootstrap-complete\"\n"
        'printf "git\\n" >"$HERMES_AGENT_REPO/.install_method"\n'
        'printf "#!/bin/sh\\nexit 0\\n" >"$HERMES_AGENT_REPO/hermes"\n'
        "cat >\"$HERMES_AGENT_REPO/.hermes-runtime/python/generation-test/cpython-3.11-macos-aarch64-none/bin/python3.11\" <<'PYTHON_EOF'\n"
        '#!/bin/sh\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n'
        'PYTHON_EOF\n'
        'ln -s "$HERMES_AGENT_REPO/.hermes-runtime/python/generation-test/cpython-3.11-macos-aarch64-none/bin/python3.11" "$HERMES_AGENT_REPO/venv/bin/python"\n'
        'ln -s python "$HERMES_AGENT_REPO/venv/bin/python3"\n'
        'printf "#!/bin/sh\\nexit 0\\n" >"$HERMES_HOME/bin/uv"\n'
        "cat >\"$HERMES_HOME/node/bin/node\" <<'NODE_EOF'\n"
        '#!/bin/sh\n'
        '[ "${1:-}" = -p ] && printf "26\\n"\n'
        'NODE_EOF\n'
        'chmod 755 "$HERMES_AGENT_REPO/hermes" "$HERMES_AGENT_REPO/.hermes-runtime/python/generation-test/cpython-3.11-macos-aarch64-none/bin/python3.11" "$HERMES_HOME/bin/uv" "$HERMES_HOME/node/bin/node"\n'
        'cat >"$HOME/.local/bin/hermes" <<LAUNCHER_EOF\n'
        '#!/usr/bin/env bash\n'
        'unset PYTHONPATH\n'
        'unset PYTHONHOME\n'
        'exec "$HERMES_AGENT_REPO/venv/bin/python" "$HERMES_AGENT_REPO/hermes" "\\$@"\n'
        'LAUNCHER_EOF\n'
        'chmod 755 "$HOME/.local/bin/hermes"\n'
    )


def run_fresh_bootstrap_fixture(
    tmp_path: Path,
    installer_commands: str,
    *,
    before_run: object | None = None,
    replacements: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, dict[str, str]]:
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
        + installer_commands
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
        "  printf '%s  -\\n' 5854b15670b51a8daae8f59ddfa917062de9f74be261eb73b4b8d719710f8968\n"
        "else /usr/bin/shasum -a 256 \"$tmp\"; fi\n",
        encoding="utf-8",
    )
    shasum.chmod(0o755)
    command_replacements = {
        "/usr/bin/curl": str(curl),
        "/usr/bin/uname": str(uname),
        "/usr/bin/shasum": str(shasum),
    }
    command_replacements.update(replacements or {})
    rendered = rendered_bootstrap(tmp_path, command_replacements)
    agent_repo = home / ".hermes/hermes-agent"
    hermes_home = home / ".hermes"
    state_home = home / ".local/state"
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "XDG_STATE_HOME": str(state_home),
    }
    if before_run is not None:
        before_run(home, state_home)
    result = subprocess.run(
        ["/bin/bash", str(rendered), str(agent_repo), str(hermes_home)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, rendered, agent_repo, state_home, env


def create_bootstrap_artifacts(home: Path, agent_repo: Path, hermes_home: Path) -> None:
    (agent_repo / ".git").mkdir(parents=True, exist_ok=True)
    (agent_repo / ".git/HEAD").write_text("63279301bcbdc185c1b07b98a9312eb0c862f26d\n", encoding="utf-8")
    (agent_repo / ".hermes-bootstrap-complete").write_text(
        '{\n  "schemaVersion": 1,\n  "pinnedCommit": "63279301bcbdc185c1b07b98a9312eb0c862f26d",\n  "pinnedBranch": "main",\n  "completedAt": "2026-08-30T00:00:00.000Z"\n}\n', encoding="utf-8"
    )
    (agent_repo / ".install_method").write_text("git\n", encoding="utf-8")
    for executable in (agent_repo / "hermes", hermes_home / "bin/uv"):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    python = agent_repo / "venv/bin/python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python_target = (
        agent_repo
        / ".hermes-runtime/python/generation-test/cpython-3.11-macos-aarch64-none/bin/python3.11"
    )
    python_target.parent.mkdir(parents=True, exist_ok=True)
    python_target.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8"
    )
    python_target.chmod(0o755)
    python.symlink_to(python_target)
    (python.parent / "python3").symlink_to("python")
    node = hermes_home / "node/bin/node"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text('#!/bin/sh\n[ "${1:-}" = -p ] && printf "26\\n"\n', encoding="utf-8")
    node.chmod(0o755)
    launcher = home / ".local/bin/hermes"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\n"
        f'exec "{agent_repo}/venv/bin/python" "{agent_repo}/hermes" "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)

def test_bootstrap_rejects_home_beneath_writable_ancestor_before_download(
    tmp_path: Path,
) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o700)
    home = unsafe_parent / "home"
    home.mkdir(mode=0o700)
    unsafe_parent.chmod(0o777)
    curl_ran = tmp_path / "curl-ran"
    fake_bin = tmp_path / "fake-bin-unsafe-home"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!/bin/sh\ntouch {curl_ran}\nexit 1\n", encoding="utf-8"
    )
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\n'\n", encoding="utf-8")
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
            "XDG_STATE_HOME": str(home / ".local/state"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ancestor" in result.stderr.lower()
    assert not curl_ran.exists()
    assert list(home.iterdir()) == []


def test_bootstrap_preflights_absent_checkout_ancestry_before_temp_or_download(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    unsafe_parent = tmp_path / "unsafe-checkout-parent"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_parent.chmod(0o777)
    agent_repo = unsafe_parent / "hermes-agent"
    hermes_home = home / ".hermes"
    curl_marker = tmp_path / "curl-ran"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        f'#!/bin/sh\n/usr/bin/touch {shlex.quote(str(curl_marker))}\nexit 99\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    rendered = rendered_bootstrap(tmp_path, {"/usr/bin/curl": str(fake_curl)})
    before = set(home.iterdir())

    result = subprocess.run(
        ["/bin/bash", str(rendered), str(agent_repo), str(hermes_home)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ancestor" in result.stderr.lower()
    assert not curl_marker.exists()
    assert set(home.iterdir()) == before


def test_bootstrap_rejects_a_writable_root_anchor_before_download(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home-root-anchor"
    home.mkdir()
    curl_ran = tmp_path / "curl-ran-root-anchor"
    fake_bin = tmp_path / "fake-bin-root-anchor"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(f"#!/bin/sh\ntouch {curl_ran}\nexit 1\n", encoding="utf-8")
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        "#!/bin/sh\n"
        "if [ \"${3:-}\" = / ]; then printf '1:2:0:777:1\n'; "
        "else exec /usr/bin/stat \"$@\"; fi\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {
            "/usr/bin/curl": str(curl),
            "/usr/bin/uname": str(uname),
            "/usr/bin/stat": str(fake_stat),
        },
    )

    result = subprocess.run(
        [
            "/bin/bash",
            str(rendered),
            str(home / ".hermes/hermes-agent"),
            str(home / ".hermes"),
        ],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ancestor" in result.stderr.lower()
    assert not curl_ran.exists()
    assert list(home.iterdir()) == []


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


def test_bootstrap_rejects_unsafe_system_perl_before_download(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-system-tools"
    fake_bin.mkdir()
    curl_ran = tmp_path / "curl-ran"
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!/bin/sh\n/usr/bin/touch {shlex.quote(str(curl_ran))}\nexit 74\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    unsafe_perl = fake_bin / "perl"
    unsafe_perl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    unsafe_perl.chmod(0o755)
    rendered = rendered_bootstrap(
        tmp_path,
        {
            "/usr/bin/curl": str(curl),
            "/usr/bin/uname": str(uname),
            "/usr/bin/perl": str(unsafe_perl),
        },
    )
    agent_repo = home / ".hermes/hermes-agent"

    result = subprocess.run(
        ["/bin/bash", str(rendered), str(agent_repo), str(home / ".hermes")],
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
    assert "system Perl" in result.stderr
    assert not curl_ran.exists()


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
        "printf '%s\\n' \"$output\" >\"$HOME/download-output\"\n"
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
        "  printf '%s  -\\n' 5854b15670b51a8daae8f59ddfa917062de9f74be261eb73b4b8d719710f8968\n"
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
    hostile_tmpdir = tmp_path / "hostile-tmp"
    hostile_tmpdir.mkdir()
    result = subprocess.run(
        ["/bin/bash", str(rendered), str(agent_repo), str(hermes_home)],
        env={
            "HOME": str(home),
            "PATH": "/malicious/bin",
            "TMPDIR": str(hostile_tmpdir),
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
    download_output = Path((home / "download-output").read_text(encoding="utf-8").strip())
    assert download_output.parent.parent == home
    assert download_output.parent.name.startswith(".unified-kanban-hermes-bootstrap.")
    assert list(hostile_tmpdir.iterdir()) == []
    assert (home / "installer-argv").read_text(encoding="utf-8").splitlines() == [
        "--commit",
        "63279301bcbdc185c1b07b98a9312eb0c862f26d",
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
    assert f"TMPDIR={download_output.parent}\n" in child_env
    assert str(hostile_tmpdir) not in child_env
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
        "  printf '%s  -\\n' 5854b15670b51a8daae8f59ddfa917062de9f74be261eb73b4b8d719710f8968\n"
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
        "upstream=63279301bcbdc185c1b07b98a9312eb0c862f26d\n"
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
        "upstream=63279301bcbdc185c1b07b98a9312eb0c862f26d\n"
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
    python_body: str | None = None,
    node_body: str | None = None,
    remove_node: bool = False,
    unsafe_runtime_parent: tuple[str, str] | None = None,
    crash_left_receipt_candidate: bool = False,
    unpublished_receipt_candidate: bool = False,
    python_link_target: Path | None = None,
    python_target_trailing_newline: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir()
    agent_repo = home / ".hermes/hermes-agent"
    hermes_home = home / ".hermes"
    create_bootstrap_artifacts(home, agent_repo, hermes_home)
    if python_link_target is not None:
        python = agent_repo / "venv/bin/python"
        python.unlink()
        python.symlink_to(python_link_target)
    if python_target_trailing_newline:
        python = agent_repo / "venv/bin/python"
        safe_target = python.resolve()
        newline_target = safe_target.with_name(safe_target.name + "\n")
        newline_target.write_bytes(safe_target.read_bytes())
        newline_target.chmod(0o755)
        python.unlink()
        python.symlink_to(newline_target)
    if python_body is not None:
        (agent_repo / "venv/bin/python").write_text(python_body, encoding="utf-8")
    if node_body is not None:
        (hermes_home / "node/bin/node").write_text(node_body, encoding="utf-8")
    if remove_node:
        (hermes_home / "node/bin/node").unlink()
    if unsafe_runtime_parent is not None:
        runtime, unsafe_kind = unsafe_runtime_parent
        if runtime == "python":
            parent = agent_repo / "venv/bin"
        elif runtime == "node":
            parent = hermes_home / "node/bin"
        else:
            parent = hermes_home / "bin"
        if unsafe_kind == "writable":
            parent.chmod(0o777)
        else:
            real_parent = home / f"real-{runtime}-runtime-parent"
            parent.rename(real_parent)
            parent.symlink_to(real_parent, target_is_directory=True)
    real_state_home = home / "real-state"
    state_dir = real_state_home / "unified-kanban"
    state_dir.mkdir(parents=True)
    state_dir.chmod(state_mode)
    state_home = real_state_home
    if symlink_state_home:
        state_home = home / "state-link"
        state_home.symlink_to(real_state_home, target_is_directory=True)
    receipt = state_dir / "hermes-bootstrap.receipt"
    receipt_source = (
        state_dir / ".hermes-bootstrap.receipt.unpublished"
        if unpublished_receipt_candidate
        else receipt
    )
    receipt_source.write_bytes(bootstrap_receipt_bytes(agent_repo, hermes_home) + receipt_suffix)
    receipt_source.chmod(receipt_mode)
    if crash_left_receipt_candidate:
        (state_dir / ".hermes-bootstrap.receipt.crash-left").hardlink_to(receipt)
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


def test_bootstrap_status_rejects_unverified_python_requirement(tmp_path: Path) -> None:
    result, _ = status_fixture(
        tmp_path,
        python_body='#!/bin/sh\nprintf "python-too-old\\n"\n',
    )

    assert result.returncode != 0
    assert "Python 3.11" in result.stderr


@pytest.mark.parametrize(
    ("node_body", "remove_node"),
    [
        ('#!/bin/sh\nprintf "25\\n"\n', False),
        (None, True),
    ],
)
def test_bootstrap_status_rejects_unverified_node_major(
    tmp_path: Path, node_body: str | None, remove_node: bool
) -> None:
    result, _ = status_fixture(
        tmp_path,
        node_body=node_body,
        remove_node=remove_node,
    )

    assert result.returncode != 0
    assert "Node 26" in result.stderr


def test_bootstrap_cleans_partial_preinstaller_staging_before_retry(
    tmp_path: Path,
) -> None:
    def leave_partial(_home: Path, state_home: Path) -> None:
        state_dir = state_home / "unified-kanban"
        state_dir.mkdir(parents=True, mode=0o700)
        partial = state_dir / ".hermes-bootstrap.staging.interrupted"
        partial.write_bytes(b"partial")
        partial.chmod(0o600)

    result, rendered, _agent_repo, state_home, _env = run_fresh_bootstrap_fixture(
        tmp_path,
        installer_artifact_commands(),
        before_run=leave_partial,
    )
    state_dir = state_home / "unified-kanban"

    assert result.returncode == 0, result.stderr
    assert not list(state_dir.glob(".hermes-bootstrap.staging.*"))
    source = rendered.read_text(encoding="utf-8")
    stage = source.index('.hermes-bootstrap.staging.')
    arm = source.index('/bin/mv "$receipt_staging" "$receipt_tmp"', stage)
    installer = source.index("/usr/bin/env -i \\\n", arm)
    assert stage < arm < installer


def test_bootstrap_rejects_filesystem_root_hermes_home_before_download(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "downloaded"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(f"#!/bin/sh\n/usr/bin/touch {shlex.quote(str(marker))}\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    rendered = rendered_bootstrap(tmp_path, {"/usr/bin/curl": str(fake_curl)})
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        ["/bin/bash", str(rendered), str(home / "agent"), "/./"],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "HERMES_HOME must not be the filesystem root" in result.stderr
    assert not marker.exists()


def test_bootstrap_rejects_python_symlink_target_with_trailing_newline(
    tmp_path: Path,
) -> None:
    result, _receipt = status_fixture(
        tmp_path / "fixture", python_target_trailing_newline=True
    )

    assert result.returncode != 0
    assert "target is not lexically normalized" in result.stderr


def test_bootstrap_status_rejects_python_symlink_outside_managed_runtime(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-python"
    outside.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8"
    )
    outside.chmod(0o755)

    result, _receipt = status_fixture(tmp_path / "fixture", python_link_target=outside)

    assert result.returncode != 0
    assert "escapes the managed runtime" in result.stderr


def test_bootstrap_status_disarms_exact_candidate_when_checkout_is_absent(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    agent_repo = home / ".hermes/hermes-agent"
    hermes_home = home / ".hermes"
    state_dir = home / ".local/state/unified-kanban"
    state_dir.mkdir(parents=True, mode=0o700)
    candidate = state_dir / ".hermes-bootstrap.receipt.armed"
    candidate.write_bytes(bootstrap_receipt_bytes(agent_repo, hermes_home))
    candidate.chmod(0o600)
    rendered = rendered_bootstrap(tmp_path, {})

    result = subprocess.run(
        ["/bin/bash", str(rendered), "--status", str(agent_repo), str(hermes_home)],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "XDG_STATE_HOME": str(home / ".local/state"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "bootstrap-absent\n"
    assert not candidate.exists()
    assert not agent_repo.exists()


def test_signal_after_candidate_arm_cannot_resume_into_installer(
    tmp_path: Path,
) -> None:
    installer_ran = tmp_path / "installer-ran"
    fake_mv = tmp_path / "mv"
    fake_mv.write_text(
        "#!/bin/sh\n"
        '/bin/mv "$@" || exit $?\n'
        '/bin/kill -TERM "$PPID"\n',
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)
    result, _rendered, _agent_repo, state_home, _env = run_fresh_bootstrap_fixture(
        tmp_path,
        installer_artifact_commands()
        + f'/usr/bin/touch {shlex.quote(str(installer_ran))}\nexit 77\n',
        replacements={"/bin/mv": str(fake_mv)},
    )

    assert result.returncode == 143
    assert not installer_ran.exists()
    assert not list((state_home / "unified-kanban").glob(".hermes-bootstrap.receipt.*"))


def test_installer_failure_retains_preinstalled_receipt_candidate(
    tmp_path: Path,
) -> None:
    installer_ran = tmp_path / "installer-ran"
    result, rendered, _agent_repo, state_home, env = run_fresh_bootstrap_fixture(
        tmp_path,
        installer_artifact_commands()
        + f'/usr/bin/touch {shlex.quote(str(installer_ran))}\nexit 77\n',
    )
    state_dir = state_home / "unified-kanban"

    assert result.returncode == 77
    assert installer_ran.exists(), result.stderr
    assert not (state_dir / "hermes-bootstrap.receipt").exists()
    candidates = list(state_dir.glob(".hermes-bootstrap.receipt.*"))
    assert len(candidates) == 1
    assert candidates[0].read_bytes() == bootstrap_receipt_bytes(
        _agent_repo, _agent_repo.parent
    )

    recovered = subprocess.run(
        ["/bin/bash", str(rendered), "--status", str(_agent_repo), str(_agent_repo.parent)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout == "bootstrap-complete\n"
    assert (state_dir / "hermes-bootstrap.receipt").is_file()
    assert not candidates[0].exists()


@pytest.mark.parametrize(
    ("kind", "failure_count", "receipt_published"),
    [("file", 1, False), ("directory", 1, True), ("directory", 2, True)],
)
def test_bootstrap_fails_closed_at_each_receipt_publication_fsync(
    tmp_path: Path, kind: str, failure_count: int, receipt_published: bool
) -> None:
    real_python = shlex.quote(sys.executable)
    original_exec = f'exec {real_python} "$@"'
    injected_exec = (
        'case "${2:-}" in\n'
        '  *os.mkdir*) ;;\n'
        '  *os.fsync*)\n'
        '    fsync_kind=file\n'
        '    case "$2" in *O_DIRECTORY*) fsync_kind=directory ;; esac\n'
        '    counter="$HOME/fsync-${fsync_kind}-count"\n'
        '    count=0\n'
        '    [ ! -f "$counter" ] || read -r count <"$counter"\n'
        '    count=$((count + 1))\n'
        '    printf "%s\\n" "$count" >"$counter"\n'
        f'    [ "$fsync_kind:$count" != "{kind}:{failure_count}" ] || exit 74\n'
        '    ;;\n'
        'esac\n'
        f'{original_exec}'
    )
    commands = installer_artifact_commands().replace(original_exec, injected_exec)
    assert commands != installer_artifact_commands()

    result, rendered, agent_repo, state_home, env = run_fresh_bootstrap_fixture(
        tmp_path, commands
    )
    receipt = state_home / "unified-kanban/hermes-bootstrap.receipt"

    assert result.returncode != 0
    assert "durability" in result.stderr.lower()
    assert receipt.exists() is receipt_published
    if receipt_published:
        repaired = subprocess.run(
            ["/bin/bash", str(rendered), "--status", str(agent_repo), str(agent_repo.parent)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert repaired.returncode == 0, repaired.stderr
        assert repaired.stdout == "bootstrap-complete\n"


@pytest.mark.parametrize("runtime", ["python", "node", "uv"])
@pytest.mark.parametrize("unsafe_kind", ["writable", "symlink"])
def test_bootstrap_status_rejects_unsafe_managed_runtime_parent(
    tmp_path: Path, runtime: str, unsafe_kind: str
) -> None:
    result, _ = status_fixture(
        tmp_path,
        unsafe_runtime_parent=(runtime, unsafe_kind),
    )

    assert result.returncode != 0
    assert runtime in result.stderr.lower()
    assert "ancestor" in result.stderr.lower() or "symlink" in result.stderr.lower()


def test_bootstrap_state_directory_creation_fsyncs_each_containing_parent() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    helper_start = source.index("mkdir_private_durable()")
    helper_end = source.index("\n}\n", helper_start)
    helper = source[helper_start:helper_end]

    create = helper.index("os.mkdir(")
    parent_fsync = helper.index("os.fsync(parent_fd)")

    assert create < parent_fsync
    assert (
        "if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700:"
        in helper
    )
    assert 'fsync_directory "$(/usr/bin/dirname "$state_dir")"' in source
    assert 'mkdir_private_durable "$state_dir" "Hermes bootstrap state"' in source


def test_unpublished_candidate_recovery_fsyncs_file_before_publication(
    tmp_path: Path,
) -> None:
    result, receipt = status_fixture(
        tmp_path,
        unpublished_receipt_candidate=True,
        python_body=(
            '#!/bin/sh\n'
            'case "${2:-}" in *O_DIRECTORY*) ;; *os.fsync*) exit 74 ;; esac\n'
            'printf "python-3.11-ok\\n"\n'
        ),
    )
    candidate = receipt.parent / ".hermes-bootstrap.receipt.unpublished"

    assert result.returncode != 0
    assert not receipt.exists()
    assert candidate.exists()
    assert "durability" in result.stderr.lower()


def test_bootstrap_status_publishes_verified_unpublished_receipt_candidate(
    tmp_path: Path,
) -> None:
    result, receipt = status_fixture(tmp_path, unpublished_receipt_candidate=True)
    candidate = receipt.parent / ".hermes-bootstrap.receipt.unpublished"

    assert result.returncode == 0, result.stderr
    assert result.stdout == "bootstrap-complete\n"
    assert receipt.is_file()
    assert receipt.stat().st_nlink == 1
    assert not candidate.exists()


def test_bootstrap_status_recovers_crash_left_published_receipt_candidate(
    tmp_path: Path,
) -> None:
    result, receipt = status_fixture(tmp_path, crash_left_receipt_candidate=True)
    candidate = receipt.parent / ".hermes-bootstrap.receipt.crash-left"

    assert result.returncode == 0, result.stderr
    assert result.stdout == "bootstrap-complete\n"
    assert not candidate.exists()
    assert receipt.stat().st_nlink == 1


@pytest.mark.parametrize("unsafe_kind", ["writable", "symlink"])
def test_published_candidate_recovery_authenticates_python_before_cleanup(
    tmp_path: Path, unsafe_kind: str
) -> None:
    result, receipt = status_fixture(
        tmp_path,
        crash_left_receipt_candidate=True,
        unsafe_runtime_parent=("python", unsafe_kind),
    )
    candidate = receipt.parent / ".hermes-bootstrap.receipt.crash-left"

    assert result.returncode != 0
    assert candidate.exists()
    assert receipt.stat().st_nlink == 2
    assert "ancestor" in result.stderr.lower() or "symlink" in result.stderr.lower()


def test_bootstrap_status_rejects_receipt_durability_failure(tmp_path: Path) -> None:
    result, receipt = status_fixture(
        tmp_path,
        python_body=(
            '#!/bin/sh\n'
            'case "${2:-}" in *os.fsync*) exit 74 ;; esac\n'
            'printf "python-3.11-ok\\n"\n'
        ),
    )

    assert result.returncode != 0
    assert "durab" in result.stderr.lower()
    assert receipt.is_file()


def test_bootstrap_fsyncs_receipt_before_and_after_namespace_publication() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")

    stage = source.index('expected_receipt >"$receipt_staging"')
    stage_fsync = source.index('system_sync_path "$receipt_staging"', stage)
    arm = source.index('/bin/mv "$receipt_staging" "$receipt_tmp"', stage_fsync)
    arm_fsync = source.index('system_sync_path "$state_dir"', arm)
    installer = source.index('"$installer"', arm_fsync)
    file_fsync = source.index('fsync_regular "$receipt_tmp"', installer)
    publish = source.index('/bin/ln "$receipt_tmp" "$receipt"', file_fsync)
    publish_fsync = source.index('fsync_directory "$state_dir"', publish)
    cleanup = source.index('/bin/rm -f "$receipt_tmp"', publish_fsync)
    cleanup_fsync = source.index('fsync_directory "$state_dir"', cleanup)

    assert (
        stage
        < stage_fsync
        < arm
        < arm_fsync
        < installer
        < file_fsync
        < publish
        < publish_fsync
        < cleanup
        < cleanup_fsync
    )


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
        "  printf '%s  -\\n' 5854b15670b51a8daae8f59ddfa917062de9f74be261eb73b4b8d719710f8968\n"
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


def test_authoritative_docs_describe_bootstrap_filesystem_security_contract() -> None:
    maintenance = (REPO / "docs/maintenance.md").read_text(encoding="utf-8")

    for contract in (
        "Python 3.11",
        "Node 26",
        "fsync",
        "intermediate ancestry",
        "containing parent",
        "private component",
        "ambient Python",
        "managed uv parent",
        "before private",
        "same-inode",
        "normal setup",
        "before installer",
        "system Perl",
        "absolute symlink",
        "raw symlink target bytes",
        "filesystem root",
        "staging-only",
        "complete stable bytes",
        "selector",
        "writable ancestor",
        "Descriptor-relative",
        "TOCTOU",
    ):
        assert contract in maintenance

    assert "before" in maintenance and "parent" in maintenance


def test_setup_delegates_bootstrap_identity_to_status_helper() -> None:
    setup = (REPO / "scripts/setup.sh").read_text(encoding="utf-8")

    assert "unified-kanban-hermes-bootstrap-receipt-v1" not in setup
    assert "5854b15670b51a8daae8f59ddfa917062de9f74be261eb73b4b8d719710f8968" not in setup
    assert '"$BOOTSTRAP_HELPER" --status' in setup
