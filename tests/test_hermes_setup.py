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
    # 불변 릴리스 생성을 대체한다. 실제 경로에 실행 가능한 Hermes를 포함한 형제
    # 릴리스를 빌드하고, 실제 릴리스 관리자와 똑같이 검토된 번들이 없으면 거부한다.
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
        "if [[ $1 == */scripts/path-transaction.py && ${2:-} == replace-file "
        "&& ${5:-} == \"$HOME/.local/bin/hermes\" "
        "&& -n ${HERMES_TEST_ACTIVATION_FAIL_ONCE:-} "
        "&& ! -e $HERMES_TEST_ACTIVATION_FAIL_ONCE ]]; then\n"
        "  : >\"$HERMES_TEST_ACTIVATION_FAIL_ONCE\"\n"
        "  echo 'intentional activation failure after selector publication' >&2\n"
        "  exit 97\n"
        "fi\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    parent_environment = {
        key: value for key, value in os.environ.items() if not key.startswith("HERMES_")
    }
    return {
        **parent_environment,
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / ".hermes"),
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


def test_setup_refuses_existing_checkout_beneath_writable_ancestor_before_any_write(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    original_repo = Path(env["HERMES_AGENT_REPO"])
    unsafe_parent = tmp_path / "unsafe-agent-parent"
    unsafe_parent.mkdir(mode=0o700)
    agent_repo = unsafe_parent / "hermes-agent"
    original_repo.rename(agent_repo)
    unsafe_parent.chmod(0o777)
    env["HERMES_AGENT_REPO"] = str(agent_repo)

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode != 0
    assert "writable ancestor" in result.stderr.lower()
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



def test_fresh_macos_dry_run_refuses_without_bootstrap_or_artifacts(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    (fake_bin / "hermes").unlink()
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin"
    Path(env["HERMES_AGENT_REPO"]).rmdir()

    result = run(SETUP, env, "--dry-run", "--skip-smoke")

    assert result.returncode != 0
    assert "dry-run will not bootstrap a host" in result.stderr
    assert not Path(env["HERMES_AGENT_REPO"]).exists()
    release_root = Path(env["HERMES_AGENT_REPO"] + ".releases")
    assert not release_root.exists()
    assert not (release_root / "current").exists()
    assert not (Path(env["XDG_STATE_HOME"]) / "unified-kanban/hermes-bootstrap.receipt").exists()
    assert not (tmp_path / ".local/bin/hermes").exists()
    assert not plugin_target(tmp_path).exists()
    for relative in (
        ".local/bin/kanban-adapter",
        ".local/bin/claude-kanban-hook",
        ".local/bin/codex-kanban-hook",
        ".local/bin/ai-session-viewer",
        ".claude/settings.json",
        ".codex/hooks.json",
    ):
        assert not (tmp_path / relative).exists()


def test_fresh_macos_bootstrap_survives_activation_failure_and_retries_deferred_smoke(
    tmp_path: Path,
    monkeypatch,
) -> None:
    foreign_hermes_home = tmp_path.parent / "foreign-hermes-home"
    foreign_activation_marker = tmp_path.parent / "foreign-activation-marker"
    monkeypatch.setenv("HERMES_HOME", str(foreign_hermes_home))
    monkeypatch.setenv(
        "HERMES_TEST_ACTIVATION_FAIL_ONCE", str(foreign_activation_marker)
    )
    env = environment(tmp_path)
    assert env["HERMES_HOME"] == str(tmp_path / ".hermes")
    fixture_root = Path(env["_TEST_ROOT"])
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    source_hermes = tmp_path / "fake-hermes-source"
    source_hermes.write_text(FAKE_HERMES, encoding="utf-8")
    source_hermes.chmod(0o755)
    env["FAKE_HERMES_EXECUTABLE"] = str(source_hermes)
    (fake_bin / "hermes").unlink()
    managed_python = tmp_path / "bootstrap-managed-python3"
    managed_python.write_bytes((fake_bin / "python3").read_bytes())
    managed_python.chmod(0o755)
    (fake_bin / "python3").write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    (fake_bin / "python3").chmod(0o755)
    env["FAKE_MANAGED_PYTHON"] = str(managed_python)
    for command in ("uv", "npm"):
        executable = fake_bin / command
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin"
    Path(env["HERMES_AGENT_REPO"]).rmdir()
    bootstrap_log = tmp_path / "bootstrap.log"
    env["BOOTSTRAP_TEST_LOG"] = str(bootstrap_log)
    activation_failure_marker = tmp_path / "activation-failed-once"
    env["HERMES_TEST_ACTIVATION_FAIL_ONCE"] = str(activation_failure_marker)
    bootstrap = fixture_root / "scripts/bootstrap-hermes-macos.sh"
    bootstrap.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "if [ \"${1:-}\" = --validate-paths ]; then printf 'paths-safe\\n'; exit 0; fi\n"
        "if [ \"${1:-}\" = --status ]; then\n"
        "  if [ -f \"${XDG_STATE_HOME}/unified-kanban/hermes-bootstrap.receipt\" ]; then echo bootstrap-complete; else echo bootstrap-absent; fi\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' \"$@\" >>\"$BOOTSTRAP_TEST_LOG\"\n"
        "repo=$1\n"
        "home=$2\n"
        "mkdir -p \"$repo/venv/bin\" \"$HOME/.local/bin\" "
        "\"${XDG_STATE_HOME}/unified-kanban\"\n"
        "ln -s \"$FAKE_MANAGED_PYTHON\" \"$repo/venv/bin/python3\"\n"
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
    failed = run(SETUP, env)

    assert failed.returncode != 0
    assert Path(env["HERMES_AGENT_REPO"]).is_dir(), failed.stderr
    assert (tmp_path / ".local/bin/hermes").is_file()
    assert bootstrap_log.read_text(encoding="utf-8").splitlines() == [
        env["HERMES_AGENT_REPO"],
        str(tmp_path / ".hermes"),
    ]
    assert activation_failure_marker.is_file(), failed.stderr
    assert not any("plugins enable" in call for call in hermes_calls(env))
    assert not Path(env["HERMES_AGENT_REPO"] + ".releases/current").exists()
    assert (
        Path(env["HERMES_AGENT_REPO"] + ".releases") / f"release-{CARRIED_HEAD}"
    ).is_dir()
    assert not plugin_target(tmp_path).exists()
    for relative in (
        ".local/bin/kanban-adapter",
        ".local/bin/claude-kanban-hook",
        ".local/bin/codex-kanban-hook",
        ".local/bin/ai-session-viewer",
        ".claude/settings.json",
        ".codex/hooks.json",
    ):
        assert not (tmp_path / relative).exists()

    retried = run(SETUP, env)
    selector = Path(env["HERMES_AGENT_REPO"] + ".releases/current")
    bootstrap_receipt = (
        Path(env["XDG_STATE_HOME"]) / "unified-kanban/hermes-bootstrap.receipt"
    )
    expected_release = str(
        Path(env["HERMES_AGENT_REPO"] + ".releases") / f"release-{CARRIED_HEAD}"
    )
    selector_after_retry = selector.read_bytes()
    receipt_after_retry = bootstrap_receipt.read_bytes()
    integration_after_retry = {
        relative: (tmp_path / relative).read_bytes()
        for relative in (".claude/settings.json", ".codex/hooks.json")
    }
    plugin_config_after_retry = (tmp_path / ".hermes/config.yaml").read_bytes()
    rerun = run(SETUP, env)
    skipped_smoke = run(SETUP, env, "--skip-smoke")

    assert retried.returncode == 0, retried.stderr
    assert rerun.returncode == 0, rerun.stderr
    assert skipped_smoke.returncode == 0, skipped_smoke.stderr
    assert "authenticated smoke deferred" in retried.stdout.lower()
    assert "scripts/kanban-smoke.sh" in retried.stdout
    assert "authenticated smoke deferred" in rerun.stdout.lower()
    assert "scripts/kanban-smoke.sh" in rerun.stdout
    assert "authenticated smoke deferred" not in skipped_smoke.stdout.lower()
    assert "scripts/kanban-smoke.sh" not in skipped_smoke.stdout
    assert selector_after_retry == (expected_release + "\n").encode()
    assert selector.read_bytes() == selector_after_retry
    assert bootstrap_receipt.read_bytes() == receipt_after_retry
    assert plugin_target(tmp_path).is_symlink()
    assert os.readlink(plugin_target(tmp_path)) == str(
        Path(env["_TEST_ROOT"]) / "integrations/hermes/hermes-kanban"
    )
    assert {
        relative: (tmp_path / relative).read_bytes()
        for relative in integration_after_retry
    } == integration_after_retry
    plugin_config_after_rerun = (tmp_path / ".hermes/config.yaml").read_bytes()
    assert plugin_config_after_rerun == plugin_config_after_retry
    assert plugin_config_after_rerun.count(b"hermes-kanban") == 1
    assert bootstrap_log.read_text(encoding="utf-8").splitlines() == [
        env["HERMES_AGENT_REPO"],
        str(tmp_path / ".hermes"),
    ]
    assert not any("kanban boards list --json" in call for call in hermes_calls(env))
    assert not foreign_hermes_home.exists()
    assert not foreign_activation_marker.exists()


def test_setup_invokes_status_for_candidate_only_bootstrap_retry(tmp_path: Path) -> None:
    env = environment(tmp_path)
    state = tmp_path / ".local/state/unified-kanban"
    state.mkdir(parents=True)
    (state / ".hermes-bootstrap.receipt.retry-authority").write_text(
        "candidate\n", encoding="utf-8"
    )
    status_marker = tmp_path / "status-called"
    ambient_python_marker = tmp_path / "ambient-python-called"
    bootstrap = Path(env["_TEST_ROOT"]) / "scripts/bootstrap-hermes-macos.sh"
    bootstrap.write_text(
        "#!/bin/bash\n"
        'if [ "${1:-}" = --validate-paths ]; then echo paths-safe; exit 0; fi\n'
        f'if [ "${1:-}" = --status ]; then /usr/bin/touch {shlex.quote(str(status_marker))}; exit 73; fi\n'
        "exit 74\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o755)
    fake_python = Path(env["PATH"].split(":", 1)[0]) / "python3"
    fake_python.write_text(
        f"#!/bin/sh\n/usr/bin/touch {shlex.quote(str(ambient_python_marker))}\nexit 75\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    result = run(SETUP, env)

    assert result.returncode != 0
    assert status_marker.exists()
    assert not ambient_python_marker.exists()


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
        "if [ \"${1:-}\" = --validate-paths ]; then printf 'paths-safe\\n'; exit 0; fi\n"
        "if [ \"${1:-}\" = --status ]; then printf 'bootstrap-complete\\n'; exit 0; fi\n"
        "exit 97\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o755)

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode == 0, result.stderr
