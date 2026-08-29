from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SUPPORTED_SHA = (REPO / "patches/hermes-agent-supported-upstream").read_text(
    encoding="utf-8"
).strip()
SETUP = REPO / "scripts/setup.sh"
UNINSTALL = REPO / "scripts/uninstall.sh"
PLUGIN_SOURCE = REPO / "integrations/hermes/hermes-kanban"
CARRIED_HEAD = [
    line for line in (REPO / "patches/hermes-agent-carried-commits").read_text(
        encoding="utf-8"
    ).splitlines()
    if line and not line.startswith("#")
][-1]

HELP_TOKENS = (
    "--board --assignee --tenant --created-by --initial-status --observation "
    "--idempotency-key --title-file --json --name --author --summary --kind "
    "task_id task_ids"
)

FAKE_HERMES = f"""\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$HERMES_TEST_LOG"
case "$*" in
  --version) printf 'Hermes Agent v0.19.0 upstream %s\\n' "$HERMES_TEST_UPSTREAM";;
  *"--help") echo '{HELP_TOKENS}';;
  "kanban boards list --json") printf '[]\\n';;
  "plugins list --json") printf '[{{"name":"hermes-kanban","status":"disabled"}}]\\n';;
  "plugins enable --no-allow-tool-override hermes-kanban")
    mkdir -p "${{HERMES_HOME:-$HOME/.hermes}}"
    printf '%s\\n' 'plugins: [hermes-kanban]' > "${{HERMES_HOME:-$HOME/.hermes}}/config.yaml"
    chmod 600 "${{HERMES_HOME:-$HOME/.hermes}}/config.yaml";;
  "plugins disable hermes-kanban")
    mkdir -p "${{HERMES_HOME:-$HOME/.hermes}}"
    printf '%s\\n' 'plugins: []' > "${{HERMES_HOME:-$HOME/.hermes}}/config.yaml"
    chmod 600 "${{HERMES_HOME:-$HOME/.hermes}}/config.yaml";;
esac
exit 0
"""

FAKE_GIT = """\
#!/usr/bin/env bash
if [[ "$1" == "-C" ]]; then shift 2; fi
case "$*" in
  '-c credential.helper= -c core.askPass= -c http.extraHeader= ls-remote https://github.com/NousResearch/hermes-agent.git refs/heads/main')
    printf '%s\\trefs/heads/main\\n' "$GIT_TEST_OFFICIAL_UPSTREAM";;
  'rev-parse origin/main') printf '%s\\n' "$GIT_TEST_UPSTREAM";;
  *) echo "unexpected git call: $*" >&2; exit 1;;
esac
"""


def environment(tmp_path: Path) -> dict[str, str]:
    fixture_root = tmp_path / "unified-kanban"
    for name in ("bin", "scripts", "patches", "src", "integrations"):
        shutil.copytree(REPO / name, fixture_root / name, dirs_exist_ok=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "hermes").write_text(FAKE_HERMES, encoding="utf-8")
    (fake_bin / "hermes").chmod(0o755)
    (fake_bin / "git").write_text(FAKE_GIT, encoding="utf-8")
    (fake_bin / "git").chmod(0o755)
    agent_repo = tmp_path / "hermes-agent"
    agent_repo.mkdir()
    carried = fixture_root / "patches/hermes-agent-carried-commits"
    python = fake_bin / "python3"
    # Stand in for immutable release construction: build the sibling release at
    # its real path with an executable Hermes, and refuse a missing reviewed
    # bundle exactly as the real release manager does.
    python.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == */scripts/verify-carried-bundle.py ]]; then exit 0; fi\n"
        "if [[ $1 == */scripts/hermes-release-manager.py && $2 == prepare ]]; then\n"
        "  if [[ ! -f $6 ]]; then\n"
        "    echo \"expected stable regular file: $6\" >&2\n"
        "    exit 1\n"
        "  fi\n"
        "  release=\"$3.releases/release-$5\"\n"
        "  mkdir -p \"$release/venv/bin\"\n"
        "  cp \"$FAKE_HERMES_EXECUTABLE\" \"$release/venv/bin/hermes\"\n"
        "  chmod +x \"$release/venv/bin/hermes\"\n"
        "  printf '%s\\n' \"$release\"\n"
        "  exit 0\n"
        "fi\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / ".local/state"),
        "XDG_CACHE_HOME": str(tmp_path / ".cache"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HERMES_TEST_LOG": str(tmp_path / "hermes.log"),
        "HERMES_TEST_UPSTREAM": SUPPORTED_SHA[:8],
        "FAKE_HERMES_EXECUTABLE": str(fake_bin / "hermes"),
        "GIT_TEST_UPSTREAM": SUPPORTED_SHA,
        "GIT_TEST_OFFICIAL_UPSTREAM": SUPPORTED_SHA,
        "HERMES_AGENT_REPO": str(agent_repo),
        "HERMES_CARRIED_COMMITS_FILE": str(carried),
        "_TEST_SETUP": str(fixture_root / "scripts/setup.sh"),
        "_TEST_ROOT": str(fixture_root),
    }


def run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    fixture_script = Path(env["_TEST_ROOT"]) / "scripts" / script.name
    actual_script = fixture_script if script in (SETUP, UNINSTALL) else script
    process_env = {key: value for key, value in env.items() if not key.startswith("_TEST_")}
    return subprocess.run(
        ["bash", str(actual_script), "--no-restart", *args], env=process_env, cwd=REPO,
        text=True, capture_output=True, check=False,
    )


def hermes_calls(env: dict[str, str]) -> list[str]:
    log = Path(env["HERMES_TEST_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def plugin_target(tmp_path: Path) -> Path:
    return tmp_path / ".hermes/plugins/hermes-kanban"



def create_verified_bootstrap_artifacts(tmp_path: Path, agent_repo: Path) -> None:
    hermes_home = tmp_path / ".hermes"
    (agent_repo / ".git").mkdir(parents=True, exist_ok=True)
    (agent_repo / ".git/HEAD").write_text(SUPPORTED_SHA + "\n", encoding="utf-8")
    (agent_repo / ".hermes-bootstrap-complete").write_text(
        f'{{\n  "schemaVersion": 1,\n  "pinnedCommit": "{SUPPORTED_SHA}"\n}}\n', encoding="utf-8"
    )
    (agent_repo / ".install_method").write_text("git\n", encoding="utf-8")
    for executable in (agent_repo / "hermes", agent_repo / "venv/bin/python", hermes_home / "bin/uv"):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    launcher = tmp_path / ".local/bin/hermes"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\n"
        f'exec "{agent_repo}/venv/bin/python" "{agent_repo}/hermes" "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)

def test_setup_installs_and_enables_repo_plugin_idempotently(tmp_path: Path) -> None:
    env = environment(tmp_path)

    first = run(SETUP, env, "--skip-smoke")
    second = run(SETUP, env, "--skip-smoke")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    link = plugin_target(tmp_path)
    assert link.is_symlink()
    assert os.readlink(link) == str(Path(env["_TEST_ROOT"]) / "integrations/hermes/hermes-kanban")
    enables = [
        call for call in hermes_calls(env)
        if call == "plugins enable --no-allow-tool-override hermes-kanban"
    ]
    assert "--version" in hermes_calls(env)
    assert "version" not in hermes_calls(env)
    assert len(enables) == 2


def test_setup_migrates_legacy_standalone_symlink(tmp_path: Path) -> None:
    env = environment(tmp_path)
    legacy = Path(env["_TEST_ROOT"]).parent / "hermes-kanban/plugin/hermes-kanban"
    legacy.mkdir(parents=True)
    target = plugin_target(tmp_path)
    target.parent.mkdir(parents=True)
    target.symlink_to(legacy)

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode == 0, result.stderr
    assert target.is_symlink()
    assert os.readlink(target) == str(Path(env["_TEST_ROOT"]) / "integrations/hermes/hermes-kanban")


def test_setup_refuses_foreign_plugin_symlink_before_writes(tmp_path: Path) -> None:
    env = environment(tmp_path)
    foreign = tmp_path / "foreign-plugin"
    foreign.mkdir()
    target = plugin_target(tmp_path)
    target.parent.mkdir(parents=True)
    target.symlink_to(foreign)

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode != 0
    assert os.readlink(target) == str(foreign)
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert not (tmp_path / ".claude").exists()


def test_setup_refuses_foreign_symlink_that_only_looks_like_legacy(tmp_path: Path) -> None:
    env = environment(tmp_path)
    foreign = tmp_path / "foreign/hermes-kanban/plugin/hermes-kanban"
    foreign.mkdir(parents=True)
    target = plugin_target(tmp_path)
    target.parent.mkdir(parents=True)
    target.symlink_to(foreign)

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode != 0
    assert os.readlink(target) == str(foreign)
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_refuses_non_symlink_plugin_path(tmp_path: Path) -> None:
    env = environment(tmp_path)
    target = plugin_target(tmp_path)
    target.parent.mkdir(parents=True)
    target.mkdir()

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode != 0
    assert target.is_dir()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_uses_frozen_snapshot_after_official_main_advances(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    env["GIT_TEST_OFFICIAL_UPSTREAM"] = "a" * 40

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_setup_refuses_missing_carried_bundle_before_any_write(tmp_path: Path) -> None:
    env = environment(tmp_path)
    (Path(env["_TEST_ROOT"]) / "patches/hermes-agent-carried.bundle").unlink()

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode != 0
    assert "hermes-agent-carried.bundle" in result.stderr
    assert not (tmp_path / ".hermes").exists()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert not (tmp_path / ".claude").exists()


def test_setup_refuses_carried_manifest_without_reviewed_commits(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    Path(env["HERMES_CARRIED_COMMITS_FILE"]).write_text(
        "# reviewed carried commits\n", encoding="utf-8"
    )

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode != 0
    assert "empty carried commit manifest" in result.stderr
    assert not (tmp_path / ".hermes").exists()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert not Path(env["HERMES_AGENT_REPO"] + ".releases").exists()


def test_setup_refuses_missing_agent_checkout_before_any_write(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env["HERMES_AGENT_REPO"] = str(tmp_path / "does-not-exist")

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode != 0
    assert "Hermes Agent checkout not found" in result.stderr
    assert not plugin_target(tmp_path).exists()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert not any("plugins enable" in call for call in hermes_calls(env))


def test_setup_dry_run_makes_no_plugin_changes(tmp_path: Path) -> None:
    env = environment(tmp_path)

    result = run(SETUP, env, "--skip-smoke", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".hermes").exists()
    assert not any("plugins enable" in call for call in hermes_calls(env))


def test_uninstall_disables_and_removes_only_owned_plugin_symlink(tmp_path: Path) -> None:
    env = environment(tmp_path)
    assert run(SETUP, env, "--skip-smoke").returncode == 0

    result = run(UNINSTALL, env)

    assert result.returncode == 0, result.stderr
    assert not plugin_target(tmp_path).exists()
    assert "plugins disable hermes-kanban" in hermes_calls(env)

    foreign = tmp_path / "foreign-plugin"
    foreign.mkdir()
    target = plugin_target(tmp_path)
    target.symlink_to(foreign)
    refused = run(UNINSTALL, env)
    assert refused.returncode != 0
    assert target.is_symlink()
    assert os.readlink(target) == str(foreign)


def test_uninstall_without_installed_plugin_skips_disable(tmp_path: Path) -> None:
    env = environment(tmp_path)
    assert run(SETUP, env, "--skip-smoke").returncode == 0
    plugin_target(tmp_path).unlink()

    result = run(UNINSTALL, env)

    assert result.returncode == 0, result.stderr
    assert not any("plugins disable" in call for call in hermes_calls(env))



def test_fresh_macos_bootstrap_survives_activation_failure_and_retries_deferred_smoke(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    fixture_root = Path(env["_TEST_ROOT"])
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    source_hermes = tmp_path / "fake-hermes-source"
    source_hermes.write_text(FAKE_HERMES, encoding="utf-8")
    source_hermes.chmod(0o755)
    env["FAKE_HERMES_EXECUTABLE"] = str(source_hermes)
    (fake_bin / "hermes").unlink()
    for command in ("uv", "npm"):
        executable = fake_bin / command
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin"
    Path(env["HERMES_AGENT_REPO"]).rmdir()
    bootstrap_log = tmp_path / "bootstrap.log"
    env["BOOTSTRAP_TEST_LOG"] = str(bootstrap_log)
    bootstrap = fixture_root / "scripts/bootstrap-hermes-macos.sh"
    bootstrap.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "if [ \"${1:-}\" = --status ]; then\n"
        "  if [ -f \"${XDG_STATE_HOME}/unified-kanban/hermes-bootstrap.receipt\" ]; then echo bootstrap-complete; else echo bootstrap-absent; fi\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' \"$@\" >>\"$BOOTSTRAP_TEST_LOG\"\n"
        "repo=$1\n"
        "home=$2\n"
        "mkdir -p \"$repo\" \"$HOME/.local/bin\" "
        "\"${XDG_STATE_HOME}/unified-kanban\"\n"
        "cp \"$FAKE_HERMES_EXECUTABLE\" \"$HOME/.local/bin/hermes\"\n"
        "chmod +x \"$HOME/.local/bin/hermes\"\n"
        "cat >\"${XDG_STATE_HOME}/unified-kanban/hermes-bootstrap.receipt\" <<EOF\n"
        "format=unified-kanban-hermes-bootstrap-receipt-v1\n"
        f"upstream={SUPPORTED_SHA}\n"
        "agent_repo=$repo\n"
        "hermes_home=$home\n"
        "status=bootstrap-complete\n"
        "python_requirement=3.11\n"
        "node_major=26\n"
        "toolchain_resolution=moving-patch-and-tool-versions\n"
        "EOF\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o755)
    bundle = fixture_root / "patches/hermes-agent-carried.bundle"
    bundle.unlink()

    failed = run(SETUP, env)

    assert failed.returncode != 0
    assert Path(env["HERMES_AGENT_REPO"]).is_dir(), failed.stderr
    assert (tmp_path / ".local/bin/hermes").is_file()
    assert bootstrap_log.read_text(encoding="utf-8").splitlines() == [
        env["HERMES_AGENT_REPO"],
        str(tmp_path / ".hermes"),
    ]
    assert hermes_calls(env) == []

    shutil.copy2(REPO / "patches/hermes-agent-carried.bundle", bundle)
    retried = run(SETUP, env)
    rerun = run(SETUP, env)

    assert retried.returncode == 0, retried.stderr
    assert rerun.returncode == 0, rerun.stderr
    assert "authenticated smoke deferred" in retried.stdout.lower()
    assert "scripts/kanban-smoke.sh" in retried.stdout
    assert "authenticated smoke deferred" in rerun.stdout.lower()
    assert "scripts/kanban-smoke.sh" in rerun.stdout
    assert bootstrap_log.read_text(encoding="utf-8").splitlines() == [
        env["HERMES_AGENT_REPO"],
        str(tmp_path / ".hermes"),
    ]
    assert not any("kanban boards list --json" in call for call in hermes_calls(env))


def test_bootstrap_managed_setup_finds_uv_in_hermes_home_bin(tmp_path: Path) -> None:
    env = environment(tmp_path)
    create_verified_bootstrap_artifacts(tmp_path, Path(env["HERMES_AGENT_REPO"]))
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    managed_bin = tmp_path / ".hermes/bin"
    managed_bin.mkdir(parents=True, exist_ok=True)
    shutil.copy2(shutil.which("uv") or "", managed_bin / "uv")
    (fake_bin / "uv").unlink(missing_ok=True)
    (fake_bin / "npm").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "npm").chmod(0o755)
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin"
    state = tmp_path / ".local/state/unified-kanban"
    state.mkdir(parents=True)
    state.chmod(0o700)
    receipt = state / "hermes-bootstrap.receipt"
    receipt.write_text(
        "format=unified-kanban-hermes-bootstrap-receipt-v1\n"
        f"upstream={SUPPORTED_SHA}\n"
        f"agent_repo={env['HERMES_AGENT_REPO']}\n"
        f"hermes_home={tmp_path / '.hermes'}\n"
        "status=bootstrap-complete\n"
        "python_requirement=3.11\n"
        "node_major=26\n"
        "toolchain_resolution=moving-patch-and-tool-versions\n",
        encoding="utf-8",
    )
    receipt.chmod(0o600)
    bootstrap = Path(env["_TEST_ROOT"]) / "scripts/bootstrap-hermes-macos.sh"
    bootstrap.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --status ]; then printf 'bootstrap-complete\\n'; exit 0; fi\n"
        "exit 97\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o755)

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode == 0, result.stderr
