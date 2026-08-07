from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SUPPORTED_SHA = (REPO / "patches/hermes-agent-supported-upstream").read_text(
    encoding="utf-8"
).strip()
SETUP = REPO / "scripts/setup.sh"
UNINSTALL = REPO / "scripts/uninstall.sh"
PLUGIN_SOURCE = REPO / "integrations/hermes/hermes-kanban"

HELP_TOKENS = (
    "--board --assignee --tenant --created-by --initial-status --observation "
    "--json --name --author --summary --kind task_id task_ids"
)

FAKE_HERMES = f"""\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$HERMES_TEST_LOG"
case "$*" in
  version) printf 'Hermes Agent v0.19.0 upstream %s\\n' "$HERMES_TEST_UPSTREAM";;
  *"--help") echo '{HELP_TOKENS}';;
  "kanban boards list --json") printf '[]\\n';;
esac
exit 0
"""

FAKE_GIT = """\
#!/usr/bin/env bash
if [[ "$1" == "-C" ]]; then shift 2; fi
case "$*" in
  'rev-parse origin/main') printf '%s\\n' "$GIT_TEST_UPSTREAM";;
  'cherry HEAD deadbeef') printf '%s deadbeef\\n' "$GIT_TEST_CARRIED_STATUS";;
  *) echo "unexpected git call: $*" >&2; exit 1;;
esac
"""


def environment(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "hermes").write_text(FAKE_HERMES, encoding="utf-8")
    (fake_bin / "hermes").chmod(0o755)
    (fake_bin / "git").write_text(FAKE_GIT, encoding="utf-8")
    (fake_bin / "git").chmod(0o755)
    agent_repo = tmp_path / "hermes-agent"
    agent_repo.mkdir()
    carried = tmp_path / "carried-commits"
    carried.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / ".local/state"),
        "XDG_CACHE_HOME": str(tmp_path / ".cache"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HERMES_TEST_LOG": str(tmp_path / "hermes.log"),
        "HERMES_TEST_UPSTREAM": SUPPORTED_SHA[:8],
        "GIT_TEST_UPSTREAM": SUPPORTED_SHA,
        "GIT_TEST_CARRIED_STATUS": "-",
        "HERMES_AGENT_REPO": str(agent_repo),
        "HERMES_CARRIED_COMMITS_FILE": str(carried),
    }


def run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), "--no-restart", *args], env=env, cwd=REPO,
        text=True, capture_output=True, check=False,
    )


def hermes_calls(env: dict[str, str]) -> list[str]:
    log = Path(env["HERMES_TEST_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def plugin_target(tmp_path: Path) -> Path:
    return tmp_path / ".hermes/plugins/hermes-kanban"


def test_setup_installs_and_enables_repo_plugin_idempotently(tmp_path: Path) -> None:
    env = environment(tmp_path)

    first = run(SETUP, env, "--skip-smoke")
    second = run(SETUP, env, "--skip-smoke")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    link = plugin_target(tmp_path)
    assert link.is_symlink()
    assert os.readlink(link) == str(PLUGIN_SOURCE)
    enables = [
        call for call in hermes_calls(env)
        if call == "plugins enable --no-allow-tool-override hermes-kanban"
    ]
    assert len(enables) == 2


def test_setup_migrates_legacy_standalone_symlink(tmp_path: Path) -> None:
    env = environment(tmp_path)
    legacy = tmp_path / "legacy/hermes-kanban/plugin/hermes-kanban"
    legacy.mkdir(parents=True)
    env["UNIFIED_KANBAN_LEGACY_HERMES_PLUGIN_SOURCE"] = str(legacy)
    target = plugin_target(tmp_path)
    target.parent.mkdir(parents=True)
    target.symlink_to(legacy)

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode == 0, result.stderr
    assert target.is_symlink()
    assert os.readlink(target) == str(PLUGIN_SOURCE)


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


def test_setup_refuses_upstream_mismatch_before_any_write(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env["HERMES_TEST_UPSTREAM"] = "aaaaaaaa"

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode != 0
    assert "Hermes version mismatch" in result.stderr
    assert "update-hermes-if-needed.sh" in result.stderr
    assert not (tmp_path / ".hermes").exists()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".local/state/unified-kanban").exists()


def test_setup_refuses_missing_carried_bundle_before_any_write(tmp_path: Path) -> None:
    env = environment(tmp_path)
    Path(env["HERMES_CARRIED_COMMITS_FILE"]).write_text(
        "# selected-board routing\ndeadbeef\n", encoding="utf-8"
    )
    env["GIT_TEST_CARRIED_STATUS"] = "+"
    env["HERMES_CARRIED_BUNDLE"] = str(tmp_path / "missing.bundle")

    result = run(SETUP, env, "--skip-smoke")

    assert result.returncode != 0
    assert "carried commits are unavailable and bundle is missing" in result.stderr
    assert not (tmp_path / ".hermes").exists()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert not (tmp_path / ".claude").exists()


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
