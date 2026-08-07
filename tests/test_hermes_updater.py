from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts/update-hermes-if-needed.sh"
SUPPORTED_SHA = (ROOT / "patches/hermes-agent-supported-upstream").read_text(
    encoding="utf-8"
).strip()

HELP_TOKENS = (
    "--board --assignee --tenant --created-by --initial-status --observation "
    "--json --name --author --summary --kind task_id task_ids"
)

FAKE_GIT = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GIT_TEST_LOG"
if [[ "$1" == "-C" ]]; then shift 2; fi
case "$*" in
  "fetch origin main") ;;
  "fetch "*" +refs/heads/*:refs/unified-kanban/carried/*")
    touch "$GIT_TEST_BUNDLE_IMPORTED"
    ;;
  "rev-list --count HEAD..origin/main")
    if [[ -f "$GIT_TEST_UPDATED" ]]; then echo 0; else cat "$GIT_TEST_BEHIND"; fi
    ;;
  "status --porcelain")
    if [[ -f "$GIT_TEST_DIRTY" ]]; then cat "$GIT_TEST_DIRTY"; fi
    ;;
  "rev-parse origin/main")
    if [[ -n "${GIT_TEST_REV_PARSE_FAIL:-}" ]]; then exit 1; fi
    if [[ -f "$GIT_TEST_UPDATED" || "$(cat "$GIT_TEST_BEHIND")" != 0 ]]; then
      echo "$GIT_TEST_SHA_AFTER"
    else
      echo "$GIT_TEST_SHA"
    fi
    ;;
  "cat-file -e "*"^{commit}")
    if [[ -n "${GIT_TEST_REQUIRE_BUNDLE:-}" && ! -f "$GIT_TEST_BUNDLE_IMPORTED" ]]; then
      exit 1
    fi
    ;;
  "cherry HEAD deadbeef")
    if [[ -f "$GIT_TEST_CARRIED_APPLIED" ]]; then
      echo "- deadbeef"
    else
      echo "+ deadbeef"
    fi
    ;;
  "cherry-pick deadbeef")
    touch "$GIT_TEST_CARRIED_APPLIED"
    ;;
  "cherry HEAD cafef00d")
    if [[ -f "$GIT_TEST_CARRIED_APPLIED" ]]; then
      echo "- deadbeef"
    fi
    if [[ -f "$GIT_TEST_CARRIED_APPLIED_2" ]]; then
      echo "- cafef00d"
    else
      echo "+ cafef00d"
    fi
    ;;
  "cherry-pick cafef00d")
    touch "$GIT_TEST_CARRIED_APPLIED_2"
    ;;
  *) echo "unexpected git call: $*" >&2; exit 1 ;;
esac
exit 0
"""

FAKE_HERMES = f"""\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$HERMES_TEST_LOG"
case "$*" in
  "version")
    if [[ -f "$GIT_TEST_UPDATED" ]]; then
      sha="$GIT_TEST_SHA_AFTER"
    else
      sha="$GIT_TEST_SHA"
    fi
    printf 'Hermes Agent v0.19.0 upstream %s\\n' "${{sha:0:8}}"
    ;;
  *"--help") echo '{HELP_TOKENS}';;
  "update --yes --branch main")
    if [[ -n "${{HERMES_TEST_UPDATE_FAIL:-}}" ]]; then exit 1; fi
    touch "$GIT_TEST_UPDATED"
    ;;
  "dashboard --stop")
    if [[ ! -f "$HERMES_TEST_DASHBOARD_STATE" ]]; then
      echo "dashboard is not running" >&2
      exit 1
    fi
    rm -f "$HERMES_TEST_DASHBOARD_STATE"
    ;;
  "dashboard --host "*)
    if [[ -n "${{HERMES_TEST_DASHBOARD_START_FAIL:-}}" ]]; then exit 1; fi
    printf '%s' "${{HERMES_DELEGATED_CHILD_CONTEXT-unset}}" > "$HERMES_TEST_DASHBOARD_ENV"
    touch "$HERMES_TEST_DASHBOARD_STATE"
    ;;
  "kanban boards list --json") printf '[]\\n';;
  "plugins enable --no-allow-tool-override hermes-kanban") ;;
  "gateway restart") ;;
  *) echo "unexpected hermes call: $*" >&2; exit 1 ;;
esac
exit 0
"""

FAKE_CURL = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CURL_TEST_LOG"
if [[ -f "$HERMES_TEST_DASHBOARD_STATE" ]]; then exit 0; else exit 7; fi
"""


def environment(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    agent_repo = tmp_path / "hermes-agent"
    agent_repo.mkdir()
    for name, body in (("git", FAKE_GIT), ("hermes", FAKE_HERMES), ("curl", FAKE_CURL)):
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    behind = tmp_path / "behind"
    behind.write_text("0\n", encoding="utf-8")
    carried = tmp_path / "carried-commits"
    carried.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "HERMES_AGENT_REPO": str(agent_repo),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GIT_TEST_LOG": str(tmp_path / "git.log"),
        "GIT_TEST_BEHIND": str(behind),
        "GIT_TEST_UPDATED": str(tmp_path / "updated"),
        "GIT_TEST_DIRTY": str(tmp_path / "dirty"),
        "GIT_TEST_CARRIED_APPLIED": str(tmp_path / "carried-applied"),
        "GIT_TEST_CARRIED_APPLIED_2": str(tmp_path / "carried-applied-2"),
        "GIT_TEST_BUNDLE_IMPORTED": str(tmp_path / "bundle-imported"),
        "GIT_TEST_SHA": SUPPORTED_SHA,
        "GIT_TEST_SHA_AFTER": SUPPORTED_SHA,
        "HERMES_TEST_LOG": str(tmp_path / "hermes.log"),
        "CURL_TEST_LOG": str(tmp_path / "curl.log"),
        "HERMES_TEST_DASHBOARD_STATE": str(tmp_path / "dashboard-running"),
        "HERMES_TEST_DASHBOARD_ENV": str(tmp_path / "dashboard-env"),
        "HERMES_CARRIED_COMMITS_FILE": str(carried),
    }


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(UPDATER), *args], env=env, cwd=ROOT,
        text=True, capture_output=True, check=False,
    )


def set_behind(env: dict[str, str], count: int) -> None:
    Path(env["GIT_TEST_BEHIND"]).write_text(f"{count}\n", encoding="utf-8")
    if count > 0:
        env["GIT_TEST_SHA"] = "0123abcd0123abcd0123abcd0123abcd0123abcd"
    else:
        env["GIT_TEST_SHA"] = env["GIT_TEST_SHA_AFTER"]


def test_refuses_unsupported_origin_before_hermes_update(tmp_path: Path) -> None:
    env = environment(tmp_path)
    Path(env["GIT_TEST_BEHIND"]).write_text("1\n", encoding="utf-8")
    env["GIT_TEST_SHA_AFTER"] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    result = run(env)

    assert result.returncode != 0
    assert "unsupported Hermes upstream" in result.stderr
    assert "update --yes --branch main" not in hermes_calls(env)
    assert not pending_file(env).exists()


def clear_logs(env: dict[str, str]) -> None:
    for key in ("GIT_TEST_LOG", "HERMES_TEST_LOG", "CURL_TEST_LOG"):
        Path(env[key]).unlink(missing_ok=True)


def git_calls(env: dict[str, str]) -> list[str]:
    log = Path(env["GIT_TEST_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def hermes_calls(env: dict[str, str]) -> list[str]:
    log = Path(env["HERMES_TEST_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def state_file(env: dict[str, str]) -> Path:
    return Path(env["HERMES_HOME"]) / "state/hermes-kanban-last-applied-sha"


def pending_file(env: dict[str, str]) -> Path:
    return Path(env["HERMES_HOME"]) / "state/hermes-kanban-update.pending"


def lock_dir(env: dict[str, str]) -> Path:
    return Path(env["HERMES_HOME"]) / "state/hermes-kanban-update.lock"


def plugin_link(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".hermes/plugins/hermes-kanban"


def test_up_to_date_skips_update_and_setup(tmp_path: Path) -> None:
    env = environment(tmp_path)

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert "SKIPPED" in result.stdout
    agent = env["HERMES_AGENT_REPO"]
    assert git_calls(env) == [
        f"-C {agent} fetch origin main",
        f"-C {agent} rev-parse origin/main",
        f"-C {agent} rev-list --count HEAD..origin/main",
    ]
    assert hermes_calls(env) == []
    assert not state_file(env).exists()
    assert not pending_file(env).exists()
    assert not lock_dir(env).exists()


def test_up_to_date_repairs_stale_applied_upstream_state(tmp_path: Path) -> None:
    env = environment(tmp_path)
    applied = state_file(env)
    applied.parent.mkdir(parents=True)
    applied.write_text("stale-upstream-sha\n", encoding="utf-8")

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert "REPAIRING" in result.stdout
    assert "update --yes --branch main" not in hermes_calls(env)
    assert "plugins enable --no-allow-tool-override hermes-kanban" in hermes_calls(env)
    assert applied.read_text(encoding="utf-8").strip() == env["GIT_TEST_SHA"]
    assert not pending_file(env).exists()
    assert not lock_dir(env).exists()


def test_up_to_date_accepts_crlf_applied_upstream_state(tmp_path: Path) -> None:
    env = environment(tmp_path)
    applied = state_file(env)
    applied.parent.mkdir(parents=True)
    applied.write_bytes((env["GIT_TEST_SHA"] + "\r\n").encode())

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert "SKIPPED" in result.stdout
    assert hermes_calls(env) == []


def test_applied_state_check_fails_closed_when_upstream_ref_is_unreadable(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    applied = state_file(env)
    applied.parent.mkdir(parents=True)
    applied.write_text("stale-upstream-sha\n", encoding="utf-8")
    env["GIT_TEST_REV_PARSE_FAIL"] = "1"

    result = run(env)

    assert result.returncode != 0
    assert "could not resolve origin/main" in result.stderr
    assert hermes_calls(env) == []
    assert applied.read_text(encoding="utf-8") == "stale-upstream-sha\n"


def test_up_to_date_repairs_missing_carried_commit(tmp_path: Path) -> None:
    env = environment(tmp_path)
    Path(env["HERMES_CARRIED_COMMITS_FILE"]).write_text(
        "# Kanban dashboard delete routing\ndeadbeef\n"
        "# Observation cards\ncafef00d\n",
        encoding="utf-8",
    )

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert "REPAIRING" in result.stdout
    for commit in ("deadbeef", "cafef00d"):
        assert any(
            call.endswith(f"cherry HEAD {commit}") for call in git_calls(env)
        )
        assert any(
            call.endswith(f"cherry-pick {commit}") for call in git_calls(env)
        )
    calls = hermes_calls(env)
    assert "update --yes --branch main" not in calls
    assert "plugins enable --no-allow-tool-override hermes-kanban" in calls


def test_missing_carried_objects_are_imported_from_repository_bundle(tmp_path: Path) -> None:
    env = environment(tmp_path)
    Path(env["HERMES_CARRIED_COMMITS_FILE"]).write_text(
        "deadbeef\n", encoding="utf-8"
    )
    bundle = tmp_path / "hermes-agent-carried.bundle"
    bundle.write_bytes(b"synthetic bundle")
    env["HERMES_CARRIED_BUNDLE"] = str(bundle)
    env["GIT_TEST_REQUIRE_BUNDLE"] = "1"

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert Path(env["GIT_TEST_BUNDLE_IMPORTED"]).exists()
    assert any(
        call == (
            f"-C {env['HERMES_AGENT_REPO']} fetch {bundle} "
            "+refs/heads/*:refs/unified-kanban/carried/*"
        )
        for call in git_calls(env)
    )


def test_repair_reapplies_only_the_missing_carried_commit(tmp_path: Path) -> None:
    env = environment(tmp_path)
    Path(env["HERMES_CARRIED_COMMITS_FILE"]).write_text(
        "deadbeef\ncafef00d\n", encoding="utf-8"
    )
    # First carried commit already applied; only the second is missing.
    Path(env["GIT_TEST_CARRIED_APPLIED"]).touch()

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert "REPAIRING" in result.stdout
    assert not any(
        call.endswith("cherry-pick deadbeef") for call in git_calls(env)
    )
    assert any(
        call.endswith("cherry-pick cafef00d") for call in git_calls(env)
    )


def test_stacked_cherry_output_selects_the_requested_commit(tmp_path: Path) -> None:
    env = environment(tmp_path)
    Path(env["HERMES_CARRIED_COMMITS_FILE"]).write_text(
        "deadbeef\ncafef00d\n", encoding="utf-8"
    )
    Path(env["GIT_TEST_CARRIED_APPLIED"]).touch()

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert any(
        call.endswith("cherry HEAD cafef00d") for call in git_calls(env)
    )
    assert any(
        call.endswith("cherry-pick cafef00d") for call in git_calls(env)
    )


def test_up_to_date_repair_refuses_dirty_checkout(tmp_path: Path) -> None:
    env = environment(tmp_path)
    Path(env["HERMES_CARRIED_COMMITS_FILE"]).write_text(
        "# Kanban dashboard delete routing\ndeadbeef\n", encoding="utf-8"
    )
    Path(env["GIT_TEST_DIRTY"]).write_text(" M local-change.py\n", encoding="utf-8")

    result = run(env)

    assert result.returncode != 0
    assert "dirty" in result.stderr
    assert not any(call.endswith("cherry-pick deadbeef") for call in git_calls(env))
    assert all("plugins enable" not in call for call in hermes_calls(env))


def test_pending_repair_refuses_dirty_checkout(tmp_path: Path) -> None:
    env = environment(tmp_path)
    pending = pending_file(env)
    pending.parent.mkdir(parents=True)
    pending.write_text(f"{env['GIT_TEST_SHA']}\n0\n", encoding="utf-8")
    Path(env["GIT_TEST_DIRTY"]).write_text(" M local-change.py\n", encoding="utf-8")

    result = run(env)

    assert result.returncode != 0
    assert "dirty" in result.stderr
    assert pending.exists()
    assert all("plugins enable" not in call for call in hermes_calls(env))


def test_update_path_runs_unified_setup_and_persists_current_sha(tmp_path: Path) -> None:
    env = environment(tmp_path)
    set_behind(env, 2)

    result = run(env)

    assert result.returncode == 0, result.stderr
    calls = hermes_calls(env)
    assert "update --yes --branch main" in calls
    # The unified setup ran: plugin enabled, gateway restarted, no dedicated
    # hermes-kanban board created.
    assert "plugins enable --no-allow-tool-override hermes-kanban" in calls
    assert "gateway restart" in calls
    assert not any(
        call.startswith("kanban boards create") and "--help" not in call
        for call in calls
    )
    assert plugin_link(env).is_symlink()
    recorded = state_file(env).read_text(encoding="utf-8").strip()
    assert recorded == env["GIT_TEST_SHA_AFTER"]
    assert not pending_file(env).exists()
    assert not lock_dir(env).exists()


def test_dashboard_restart_drops_delegated_child_context(tmp_path: Path) -> None:
    env = environment(tmp_path)
    set_behind(env, 2)
    env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    Path(env["HERMES_TEST_DASHBOARD_STATE"]).touch()

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert Path(env["HERMES_TEST_DASHBOARD_ENV"]).read_text() == "unset"


def test_update_reapplies_configured_carried_commits_idempotently(tmp_path: Path) -> None:
    env = environment(tmp_path)
    set_behind(env, 2)
    Path(env["HERMES_CARRIED_COMMITS_FILE"]).write_text(
        "# Kanban dashboard delete routing\ndeadbeef\n"
        "# Observation cards\ncafef00d\n",
        encoding="utf-8",
    )

    first = run(env)

    assert first.returncode == 0, first.stderr
    for commit in ("deadbeef", "cafef00d"):
        assert any(
            call.endswith(f"cherry HEAD {commit}") for call in git_calls(env)
        )
        assert any(
            call.endswith(f"cherry-pick {commit}") for call in git_calls(env)
        )

    clear_logs(env)
    pending_file(env).write_text(
        f"{env['GIT_TEST_SHA_AFTER']}\n0\n", encoding="utf-8"
    )
    second = run(env)

    assert second.returncode == 0, second.stderr
    for commit in ("deadbeef", "cafef00d"):
        assert any(
            call.endswith(f"cherry HEAD {commit}") for call in git_calls(env)
        )
        assert not any(
            call.endswith(f"cherry-pick {commit}") for call in git_calls(env)
        )


def test_dirty_checkout_is_refused_even_with_legacy_force_flag(tmp_path: Path) -> None:
    env = environment(tmp_path)
    set_behind(env, 2)
    Path(env["GIT_TEST_DIRTY"]).write_text(" M lifecycle.py\n", encoding="utf-8")

    refused = run(env)

    assert refused.returncode != 0
    assert "dirty" in refused.stderr
    assert all("update" not in call for call in hermes_calls(env))
    assert not state_file(env).exists()

    forced = run(env, "--force")

    assert forced.returncode != 0
    assert "dirty" in forced.stderr
    assert all("update" not in call for call in hermes_calls(env))
    assert not state_file(env).exists()


def test_check_reports_without_mutating(tmp_path: Path) -> None:
    env = environment(tmp_path)
    set_behind(env, 2)

    available = run(env, "--check")

    assert available.returncode == 0, available.stderr
    assert "UPDATE_AVAILABLE" in available.stdout
    assert hermes_calls(env) == []
    assert not state_file(env).exists()
    assert not Path(env["HERMES_HOME"]).exists()

    set_behind(env, 0)
    up_to_date = run(env, "--check")

    assert up_to_date.returncode == 0, up_to_date.stderr
    assert "UP_TO_DATE" in up_to_date.stdout
    assert not Path(env["HERMES_HOME"]).exists()


def test_update_failure_leaves_state_untouched(tmp_path: Path) -> None:
    env = environment(tmp_path)
    set_behind(env, 2)
    env["HERMES_TEST_UPDATE_FAIL"] = "1"

    result = run(env)

    assert result.returncode != 0
    assert not state_file(env).exists()
    calls = hermes_calls(env)
    assert "update --yes --branch main" in calls
    assert all("plugins enable" not in call for call in calls)
    assert not lock_dir(env).exists()


def test_restart_failure_leaves_pending_marker_but_no_applied_state(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    set_behind(env, 2)
    Path(env["HERMES_TEST_DASHBOARD_STATE"]).touch()
    env["HERMES_TEST_DASHBOARD_START_FAIL"] = "1"
    env["HERMES_DASHBOARD_READY_ATTEMPTS"] = "2"

    result = run(env)

    assert result.returncode != 0
    assert "Dashboard restart failed" in result.stderr
    assert "dashboard --stop" in hermes_calls(env)
    assert not state_file(env).exists()
    assert pending_file(env).exists()
    assert not lock_dir(env).exists()


def test_partial_run_recovery_resumes_repair_instead_of_skipping(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    set_behind(env, 2)
    Path(env["HERMES_TEST_DASHBOARD_STATE"]).touch()
    env["HERMES_TEST_DASHBOARD_START_FAIL"] = "1"
    env["HERMES_DASHBOARD_READY_ATTEMPTS"] = "2"

    first = run(env)

    assert first.returncode != 0
    assert pending_file(env).exists()
    assert not state_file(env).exists()

    del env["HERMES_TEST_DASHBOARD_START_FAIL"]
    clear_logs(env)

    second = run(env)

    assert second.returncode == 0, second.stderr
    assert "SKIPPED" not in second.stdout
    calls = hermes_calls(env)
    # Repair reruns the unified setup and restarts the dashboard, but never
    # reruns the already-applied hermes update. The stop of the already-dead
    # dashboard must be tolerated.
    assert "update --yes --branch main" not in calls
    assert "plugins enable --no-allow-tool-override hermes-kanban" in calls
    assert calls[-2] == "dashboard --stop"
    assert calls[-1].startswith("dashboard --host ")
    recorded = state_file(env).read_text(encoding="utf-8").strip()
    assert recorded == env["GIT_TEST_SHA_AFTER"]
    assert not pending_file(env).exists()
    assert not lock_dir(env).exists()


def test_lock_without_owner_pid_is_preserved(tmp_path: Path) -> None:
    env = environment(tmp_path)
    set_behind(env, 2)
    lock_dir(env).mkdir(parents=True)

    result = run(env)

    assert result.returncode != 0
    assert "lock" in result.stderr
    assert git_calls(env) == []
    assert hermes_calls(env) == []
    assert lock_dir(env).exists()


def test_stale_lock_with_dead_owner_is_reclaimed(tmp_path: Path) -> None:
    env = environment(tmp_path)
    set_behind(env, 2)
    lock_dir(env).mkdir(parents=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    (lock_dir(env) / "pid").write_text(f"{dead.pid}\n", encoding="utf-8")

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert "stale lock" in result.stdout
    recorded = state_file(env).read_text(encoding="utf-8").strip()
    assert recorded == env["GIT_TEST_SHA_AFTER"]
    assert not lock_dir(env).exists()
