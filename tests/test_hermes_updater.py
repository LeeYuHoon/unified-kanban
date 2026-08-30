"""Tests for the immutable Hermes release updater.

The updater never mutates the Hermes checkout. It prepares an immutable release
under ``<HERMES_AGENT_REPO>.releases`` and activates it by swapping one regular
selector file inside a path transaction, restarting only the services that were
running. These tests exercise that contract end to end with a faked release
manager, so the real path transaction, state markers, and managed launcher are
used unchanged.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts/update-hermes-if-needed.sh"
RELEASE_MANAGER = ROOT / "scripts/hermes-release-manager.py"
SUPPORTED_SHA = (ROOT / "patches/hermes-agent-supported-upstream").read_text(
    encoding="utf-8"
).strip()
CARRIED_HEAD = [
    line
    for line in (ROOT / "patches/hermes-agent-carried-commits")
    .read_text(encoding="utf-8")
    .splitlines()
    if line and not line.startswith("#")
][-1]

FAKE_GIT = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"
case "$*" in
  *"ls-remote https://github.com/NousResearch/hermes-agent.git refs/heads/main")
    if [[ -n "${FAKE_LS_REMOTE_FAIL:-}" ]]; then exit 1; fi
    printf '%s\\trefs/heads/main\\n' "$FAKE_OFFICIAL_UPSTREAM"
    ;;
  *) echo "refused git call: $*" >&2; exit 1 ;;
esac
"""

FAKE_UV = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$UV_TEST_LOG"
echo "uv must not run: the release manager is faked in tests" >&2
exit 1
"""

FAKE_LAUNCHCTL = """\
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LAUNCHCTL_LOG"
case "$1" in
  bootout)
    if [[ -n "${HERMES_TEST_LAUNCHD_ACTUAL:-}" && -z "${FAKE_LAUNCHCTL_STICKY_LOADED:-}" ]]; then rm -f "$HERMES_TEST_LAUNCHD_ACTUAL"; fi
    exit 0 ;;
  bootstrap)
    printf '%s\n' "$3" > "$FAKE_BOOTSTRAP_PLIST_LOG"
    if [[ -n "${HERMES_TEST_LAUNCHD_ACTUAL:-}" ]]; then printf 'match\n' > "$HERMES_TEST_LAUNCHD_ACTUAL"; fi
    exit 0 ;;
  kickstart)
    if [[ -n "${FAKE_LAUNCHCTL_FAIL_KICKSTART:-}" ]]; then exit 115; fi
    exit 0 ;;
  print)
    if [[ -n "${HERMES_TEST_LAUNCHD_ACTUAL:-}" && -f "$HERMES_TEST_LAUNCHD_ACTUAL" ]]; then exit 0; fi
    if [[ -n "${FAKE_LAUNCHCTL_PRINT_INDETERMINATE:-}" ]]; then
      printf 'launchd query unavailable\n' >&2
      exit 77
    fi
    printf 'Bad request.\nCould not find service "ai.hermes.gateway" in domain for user gui: %s\n' "$(id -u)" >&2
    exit 113 ;;
  *) exit 114 ;;
esac
"""


FAKE_CURL = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CURL_TEST_LOG"
if [[ -f "$HERMES_TEST_DASHBOARD_STATE" ]]; then exit 0; else exit 7; fi
"""

# Installed by the faked release manager at <release>/venv/bin/hermes, so it is
# only reachable through the managed launcher and the active selector.
FAKE_GATEWAY_PYTHON = """\
#!/usr/bin/env bash
release="$(cd "$(dirname "$0")/../.." && pwd -P)"
/usr/bin/python3 - "$release" <<'PY'
import os
import plistlib
import sys

release = sys.argv[1]
payload = {
    "Label": "ai.hermes.gateway",
    "ProgramArguments": [f"{release}/venv/bin/python", "-m", "hermes_cli.main"],
    "EnvironmentVariables": {
        "HERMES_HOME": os.environ["HERMES_HOME"],
    },
}
sys.stdout.buffer.write(plistlib.dumps(payload))
PY
"""


FAKE_HERMES_RUNTIME = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$HERMES_TEST_LOG"
if [[ -n "${HERMES_TEST_AUTHORITY_REPORT:-}" ]]; then
  # Bash reallocates descriptors above 9 for its own bookkeeping, so a bash
  # `: <&10` probe reports its internal reuse rather than what was inherited.
  # fstat from a freshly exec'd process is the only honest measurement.
  descriptors=$(/usr/bin/python3 -c '
import os


def probe(fd):
    try:
        os.fstat(fd)
    except OSError:
        return f"fd{fd}=closed"
    return f"fd{fd}=open"


print(" ".join(probe(fd) for fd in (9, 10)))
')
  printf '%s %s dir=%s txfd=%s ledgerfd=%s delegated=%s gitdir=%s\\n' \\
    "$*" "$descriptors" \\
    "${UNIFIED_KANBAN_TRANSACTION_DIR-unset}" \\
    "${UNIFIED_KANBAN_TRANSACTION_FD-unset}" "${UNIFIED_KANBAN_TOKEN_LEDGER_FD-unset}" \\
    "${HERMES_DELEGATED_CHILD_CONTEXT-unset}" "${GIT_DIR-unset}" \\
    >> "$HERMES_TEST_AUTHORITY_REPORT"
fi
printf '%s\\n' "$PWD" > "$HERMES_TEST_RELEASE_MARKER"
if [[ -n "${HERMES_TEST_FAIL_COMMAND:-}" && "$*" == "$HERMES_TEST_FAIL_COMMAND" ]]; then
  exit 42
fi
case "$*" in
  "dashboard --stop")
    if [[ ! -f "$HERMES_TEST_DASHBOARD_STATE" ]]; then
      echo "dashboard is not running" >&2
      exit 1
    fi
    rm -f "$HERMES_TEST_DASHBOARD_STATE"
    ;;
  "dashboard --host "*)
    if [[ -n "${HERMES_TEST_DASHBOARD_START_FAIL:-}" ]]; then exit 1; fi
    printf '%s' "${HERMES_DELEGATED_CHILD_CONTEXT-unset}" > "$HERMES_TEST_DASHBOARD_ENV"
    printf '%s\\n' "$0" > "$HERMES_TEST_DASHBOARD_STATE"
    ;;
  "gateway restart")
    if [[ -n "${HERMES_TEST_LAUNCHD_ACTUAL:-}" ]]; then
      if [[ -n "${HERMES_TEST_RESTART_STALE_ONCE:-}" && ! -e "$HERMES_TEST_RESTART_STALE_ONCE" ]]; then
        touch "$HERMES_TEST_RESTART_STALE_ONCE"; printf 'stale\\n' > "$HERMES_TEST_LAUNCHD_ACTUAL"
      else printf 'match\\n' > "$HERMES_TEST_LAUNCHD_ACTUAL"; fi
    fi
    ;;
  "gateway status")
    supervision=""
    if [[ -n "${HERMES_TEST_GATEWAY_SUPERVISION:-}" && -f "$HERMES_TEST_GATEWAY_SUPERVISION" ]]; then
      IFS= read -r supervision < "$HERMES_TEST_GATEWAY_SUPERVISION"
    fi
    if [[ -z "${HERMES_TEST_GATEWAY_SUPERVISION:-}" || "$supervision" == "supervised" ]]; then
      echo "Gateway is supervised by launchd"
    else
      echo "Gateway service is not loaded"
    fi
    ;;
  "gateway stop") ;;
  "gateway start")
    if [[ -n "${HERMES_TEST_GATEWAY_SUPERVISION:-}" && -z "${HERMES_TEST_GATEWAY_NEVER_SUPERVISED:-}" ]]; then
      printf 'supervised\\n' > "$HERMES_TEST_GATEWAY_SUPERVISION"
    fi
    if [[ -n "${HERMES_TEST_LAUNCHD_ACTUAL:-}" && -z "${HERMES_TEST_GATEWAY_NEVER_SUPERVISED:-}" ]]; then
      printf 'match\\n' > "$HERMES_TEST_LAUNCHD_ACTUAL"
    fi
    ;;
  *) echo "unexpected hermes call: $*" >&2; exit 1 ;;
esac
exit 0
"""


def _python_shim(real_python: str) -> str:
    """Fake only the release construction; everything else is the real helper."""
    return (
        "#!/usr/bin/env bash\n"
        'if [[ $1 == */scripts/reload-macos-launchd-service.py ]]; then\n'
        '  \"$FAKE_HERMES_RUNTIME\" gateway restart || exit $?\n'
        '  if [[ -n "${HERMES_TEST_RESTART_STALE_ONCE:-}" && -e "$HERMES_TEST_RESTART_STALE_ONCE" ]]; then\n'
        '    \"$FAKE_HERMES_RUNTIME\" gateway stop || exit $?\n'
        '    \"$FAKE_HERMES_RUNTIME\" gateway start || exit $?\n'
        '  fi\n'
        '  exit 0\n'
        'fi\n'
        'if [[ $1 == */scripts/verify-macos-launchd-service.py ]]; then\n'
        '  if [[ -n "${UNIFIED_KANBAN_TRANSACTION_DIR:-}${UNIFIED_KANBAN_TRANSACTION_FD:-}${UNIFIED_KANBAN_TRANSACTION_LEDGER_FD:-}" || -e /dev/fd/9 || -e /dev/fd/10 ]]; then exit 88; fi\n'
        '  if [[ ${3:-} == --expected-release && -n ${4:-} ]]; then\n'
        f"    {shlex.quote(real_python)} - \"$2\" \"$4\" <<'PY'\n"
        'import plistlib, sys\n'
        'payload = plistlib.loads(open(sys.argv[1], "rb").read())\n'
        'raise SystemExit(0 if payload.get("ProgramArguments", [None])[0] == sys.argv[2] + "/venv/bin/python" else 90)\n'
        'PY\n'
        '    [[ $? == 0 ]] || exit $?\n'
        '    if [[ -n "${FAKE_PY_SWAP_SELECTOR_DURING_VERIFY:-}" ]]; then printf \'%s\' "$FAKE_PY_SWAP_CONTENT" > "$FAKE_PY_SWAP_SELECTOR_DURING_VERIFY.concurrent"; mv -f -- "$FAKE_PY_SWAP_SELECTOR_DURING_VERIFY.concurrent" "$FAKE_PY_SWAP_SELECTOR_DURING_VERIFY"; exit 92; fi\n'
        '  elif [[ ${3:-} != --allow-unsealed-environment ]]; then exit 89; fi\n'
        '  if [[ -z "${HERMES_TEST_LAUNCHD_ACTUAL:-}" ]]; then exit 0; fi\n'
        '  actual=""; IFS= read -r actual < "$HERMES_TEST_LAUNCHD_ACTUAL"\n'
        '  [[ "$actual" == match ]]\n'
        '  exit $?\n'
        'fi\n'
        'if [[ $1 == */scripts/verify-carried-bundle.py ]]; then\n'
        '  printf \'%s\\n\' "$*" >> "$BUNDLE_TEST_LOG"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ $1 == */scripts/hermes-release-manager.py && $2 == prepare ]]; then\n'
        '  printf \'%s\\n\' "$*" >> "$PREPARE_TEST_LOG"\n'
        '  if [[ -n "${PREPARE_TEST_FAIL:-}" ]]; then\n'
        '    echo "release preparation failed" >&2\n'
        "    exit 1\n"
        "  fi\n"
        '  release="$3.releases/release-$5"\n'
        '  mkdir -p "$release/venv/bin"\n'
        '  chmod 700 "$3.releases"\n'
        '  cp "$FAKE_HERMES_RUNTIME" "$release/venv/bin/hermes"\n'
        '  cp "$FAKE_GATEWAY_PYTHON" "$release/venv/bin/python"\n'
        '  chmod +x "$release/venv/bin/hermes" "$release/venv/bin/python"\n'
        '  printf \'%s\\n\' "$release"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ $1 == */scripts/path-transaction.py && $2 == rollback-ledger ]]; then\n'
        f"  {shlex.quote(sys.executable)} \"$@\"\n"
        '  status=$?\n'
        '  if [[ $status == 0 && -n ${FAKE_PY_SWAP_AFTER_ROLLBACK_TARGET:-} ]]; then printf \'%s\' "$FAKE_PY_SWAP_CONTENT" > "$FAKE_PY_SWAP_AFTER_ROLLBACK_TARGET.concurrent"; mv -f -- "$FAKE_PY_SWAP_AFTER_ROLLBACK_TARGET.concurrent" "$FAKE_PY_SWAP_AFTER_ROLLBACK_TARGET"; fi\n'
        '  exit $status\n'
        'fi\n'
        'if [[ $1 == */scripts/path-transaction.py && $2 == replace-file ]]; then\n'
        "  target=$5\n"
        '  if [[ -n ${FAKE_PY_FAIL_REPLACE_TARGET:-} && $target == "$FAKE_PY_FAIL_REPLACE_TARGET" ]]; then\n'
        "    exit 91\n"
        "  fi\n"
        f"  {shlex.quote(real_python)} \"$@\"\n"
        "  status=$?\n"
        '  if [[ $status == 0 && -n ${FAKE_PY_SWAP_AFTER_TARGET:-} && $target == "$FAKE_PY_SWAP_AFTER_TARGET" ]]; then\n'
        '    printf \'%s\' "$FAKE_PY_SWAP_CONTENT" > "$target.concurrent"\n'
        '    mv -f -- "$target.concurrent" "$target"\n'
        "  fi\n"
        '  if [[ $status == 0 && -n ${FAKE_PY_KILL_PARENT_AFTER_TARGET:-} && $target == "$FAKE_PY_KILL_PARENT_AFTER_TARGET" ]]; then\n'
        '    kill -9 "$PPID"\n'
        "  fi\n"
        "  exit $status\n"
        "fi\n"
        'if [[ $1 == */scripts/update-state.py && $2 == remove && -n ${FAKE_PY_FAIL_STATE_REMOVE:-} ]]; then\n'
        '  echo "injected state removal failure" >&2\n'
        "  exit 93\n"
        "fi\n"
        'if [[ $1 == */scripts/path-transaction.py && $2 == checkpoint && -n ${FAKE_PY_KILL_PARENT_AFTER_CHECKPOINT:-} ]]; then\n'
        f"  {shlex.quote(real_python)} \"$@\"\n"
        "  status=$?\n"
        '  if [[ $status == 0 ]]; then kill -9 "$PPID"; fi\n'
        "  exit $status\n"
        "fi\n"
        f"exec {shlex.quote(real_python)} \"$@\"\n"
    )


def environment(tmp_path: Path, *, install_launcher: bool = True) -> dict[str, str]:
    fixture_root = tmp_path / "unified-kanban"
    for name in ("scripts", "patches", "src", "integrations"):
        shutil.copytree(ROOT / name, fixture_root / name)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    # ~/Library is a pre-existing macOS platform directory, not updater-owned.
    # Native tool launchers may populate Library/Caches while an update runs.
    (home / "Library").mkdir()
    agent_repo = tmp_path / "hermes-agent"
    agent_repo.mkdir()
    # The checkout is a read-only input. Anything written into it is a defect.
    (agent_repo / "README").write_text("read-only input\n", encoding="utf-8")
    trusted_python = shlex.quote(sys.executable)
    runtime = tmp_path / "hermes-runtime"
    runtime.write_text(
        FAKE_HERMES_RUNTIME.replace("/usr/bin/python3", trusted_python),
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    gateway_python = tmp_path / "gateway-python"
    gateway_python.write_text(
        FAKE_GATEWAY_PYTHON.replace("/usr/bin/python3", trusted_python),
        encoding="utf-8",
    )
    gateway_python.chmod(0o755)
    for name, body in (
        ("git", FAKE_GIT),
        ("curl", FAKE_CURL),
        ("launchctl", FAKE_LAUNCHCTL),
        ("uv", FAKE_UV),
    ):
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    test_updater = fixture_root / "scripts/update-hermes-if-needed.sh"
    updater_source = test_updater.read_text(encoding="utf-8")
    assert "/bin/launchctl" in updater_source
    test_updater.write_text(
        updater_source.replace(
            "/bin/launchctl", shlex.quote(str(fake_bin / "launchctl"))
        ),
        encoding="utf-8",
    )
    python = fake_bin / "python3"
    python.write_text(_python_shim(sys.executable), encoding="utf-8")
    python.chmod(0o755)

    if install_launcher:
        install_managed_launcher(fixture_root, agent_repo, home / ".local/bin/hermes")

    return {
        **os.environ,
        "HOME": str(home),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "HERMES_AGENT_REPO": str(agent_repo),
        "HERMES_DASHBOARD_READY_ATTEMPTS": "2",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_HERMES_RUNTIME": str(runtime),
        "FAKE_GATEWAY_PYTHON": str(gateway_python),
        "FAKE_GIT_LOG": str(tmp_path / "git.log"),
        "FAKE_LAUNCHCTL_LOG": str(tmp_path / "launchctl.log"),
        "FAKE_BOOTSTRAP_PLIST_LOG": str(tmp_path / "bootstrap-plist.log"),
        "FAKE_OFFICIAL_UPSTREAM": SUPPORTED_SHA,
        "UV_TEST_LOG": str(tmp_path / "uv.log"),
        "CURL_TEST_LOG": str(tmp_path / "curl.log"),
        "PREPARE_TEST_LOG": str(tmp_path / "prepare.log"),
        "BUNDLE_TEST_LOG": str(tmp_path / "bundle.log"),
        "HERMES_TEST_LOG": str(tmp_path / "hermes.log"),
        "HERMES_TEST_RELEASE_MARKER": str(tmp_path / "hermes-release-marker"),
        "HERMES_TEST_DASHBOARD_STATE": str(tmp_path / "dashboard-running"),
        "HERMES_TEST_DASHBOARD_ENV": str(tmp_path / "dashboard-env"),
        "_TEST_UPDATER": str(fixture_root / "scripts/update-hermes-if-needed.sh"),
        "_TEST_ROOT": str(fixture_root),
        "_TEST_AGENT_REPO": str(agent_repo),
    }


def install_managed_launcher(fixture_root: Path, agent_repo: Path, launcher: Path) -> None:
    candidate = launcher.parent / ".launcher-candidate"
    subprocess.run(
        [
            sys.executable,
            str(fixture_root / "scripts/hermes-release-manager.py"),
            "render-launcher",
            str(agent_repo),
            SUPPORTED_SHA,
            CARRIED_HEAD,
            str(candidate),
            "--baseline-absent",
        ],
        check=True,
        capture_output=True,
    )
    candidate.replace(launcher)
    launcher.chmod(0o755)


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    process_env = {key: value for key, value in env.items() if not key.startswith("_TEST_")}
    return subprocess.run(
        ["bash", env["_TEST_UPDATER"], *args],
        env=process_env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def release_root(env: dict[str, str]) -> Path:
    return Path(env["_TEST_AGENT_REPO"] + ".releases")


def selector(env: dict[str, str]) -> Path:
    return release_root(env) / "current"


def previous_selector(env: dict[str, str]) -> Path:
    return release_root(env) / "previous"


def target_release(env: dict[str, str]) -> Path:
    return release_root(env) / f"release-{CARRIED_HEAD}"


def gateway_plist(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / "Library/LaunchAgents/ai.hermes.gateway.plist"


def state_dir(env: dict[str, str]) -> Path:
    return Path(env["HERMES_HOME"]) / "state"


def pending_file(env: dict[str, str]) -> Path:
    return state_dir(env) / "hermes-kanban-update.pending"


def lock_dir(env: dict[str, str]) -> Path:
    return state_dir(env) / "hermes-kanban-update.lock"


def log_lines(env: dict[str, str], key: str) -> list[str]:
    path = Path(env[key])
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def pending_is_absent(env: dict[str, str]) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(env["_TEST_ROOT"]) / "scripts/update-state.py"),
            "read",
            "pending",
            str(pending_file(env)),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 3


def checkout_snapshot(env: dict[str, str]) -> dict[str, tuple[int, int, bytes]]:
    root = Path(env["_TEST_AGENT_REPO"])
    snapshot: dict[str, tuple[int, int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        payload = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        snapshot[str(path.relative_to(root))] = (info.st_dev, info.st_ino, payload)
    return snapshot


def activate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = run(env)
    assert result.returncode == 0, result.stderr
    return result


def test_updater_source_has_no_mutable_checkout_operations() -> None:
    source = UPDATER.read_text(encoding="utf-8")

    for forbidden in (
        "hermes update",
        "git-fail-closed-update-wrapper",
        "reset --hard",
        "cherry-pick",
        "merge --ff-only",
        "setup.sh --skip-smoke",
    ):
        assert forbidden not in source, forbidden


def test_activation_never_writes_into_the_hermes_checkout(tmp_path: Path) -> None:
    env = environment(tmp_path)
    before = checkout_snapshot(env)

    activate(env)

    assert checkout_snapshot(env) == before
    assert log_lines(env, "FAKE_GIT_LOG") == [
        "-c credential.helper= -c core.askPass= -c http.extraHeader= "
        "ls-remote https://github.com/NousResearch/hermes-agent.git refs/heads/main"
    ]
    assert log_lines(env, "UV_TEST_LOG") == []


def test_activation_selects_the_reviewed_release_and_restarts_the_gateway(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)

    result = activate(env)

    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"
    assert selector(env).stat().st_mode & 0o777 == 0o600
    plist = gateway_plist(env)
    payload = plistlib.loads(plist.read_bytes())
    assert payload["ProgramArguments"][0] == str(target_release(env) / "venv/bin/python")
    assert plist.stat().st_mode & 0o777 == 0o600
    assert log_lines(env, "HERMES_TEST_LOG") == ["gateway restart", "gateway status"]
    assert f"release {CARRIED_HEAD} is active" in result.stdout
    assert pending_is_absent(env)
    assert not lock_dir(env).exists()
    # The restarted service reached the reviewed release through the selector.
    assert Path(env["HERMES_TEST_RELEASE_MARKER"]).exists()


def test_activation_publishes_the_prior_current_as_durable_previous(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    prior = release_root(env) / f"release-{'b' * 40}"
    release_root(env).mkdir(mode=0o700)
    selector(env).write_text(f"{prior}\n", encoding="utf-8")
    selector(env).chmod(0o600)

    activate(env)

    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"
    assert previous_selector(env).read_text(encoding="utf-8") == f"{prior}\n"
    assert previous_selector(env).stat().st_mode & 0o777 == 0o600


def test_activation_refuses_a_hardlinked_previous_reference_before_replacement(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    prior = release_root(env) / f"release-{'b' * 40}"
    release_root(env).mkdir(mode=0o700)
    selector(env).write_text(f"{prior}\n", encoding="utf-8")
    selector(env).chmod(0o600)
    foreign_peer = tmp_path / "foreign-previous-peer"
    foreign_peer.write_text(f"{prior}\n", encoding="utf-8")
    foreign_peer.chmod(0o644)
    os.link(foreign_peer, previous_selector(env))
    identity = foreign_peer.stat().st_ino
    before = foreign_peer.read_bytes()
    before_mode = foreign_peer.stat().st_mode

    result = run(env)

    assert result.returncode != 0
    assert "private regular file" in result.stderr
    assert selector(env).read_text(encoding="utf-8") == f"{prior}\n"
    assert foreign_peer.stat().st_ino == identity
    assert previous_selector(env).stat().st_ino == identity
    assert foreign_peer.read_bytes() == before
    assert foreign_peer.stat().st_mode == before_mode
    assert foreign_peer.stat().st_nlink == 2


def test_previous_is_durable_before_current_moves_on_an_interrupted_activation(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    prior = release_root(env) / f"release-{'b' * 40}"
    release_root(env).mkdir(mode=0o700)
    selector(env).write_text(f"{prior}\n", encoding="utf-8")
    selector(env).chmod(0o600)
    env["FAKE_PY_KILL_PARENT_AFTER_TARGET"] = str(selector(env))

    interrupted = run(env)

    assert interrupted.returncode != 0
    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"
    assert previous_selector(env).read_text(encoding="utf-8") == f"{prior}\n"


def test_activation_leaves_the_managed_launcher_and_its_binding_untouched(
    tmp_path: Path,
) -> None:
    """An update must not rewrite the launcher's retained-original binding.

    Uninstall decides restore-versus-remove by re-deriving that binding and
    requiring the installed launcher to match byte for byte. An updater that
    re-rendered the launcher would silently unbind the retained backup, so
    uninstall would refuse or, worse, act on the wrong baseline.
    """
    env = environment(tmp_path, install_launcher=False)
    launcher = Path(env["HOME"]) / ".local/bin/hermes"
    original = tmp_path / "original-launcher"
    original.write_bytes(b"#!/bin/sh\necho ORIGINAL\n")
    candidate = launcher.parent / ".launcher-candidate"
    subprocess.run(
        [
            sys.executable,
            str(Path(env["_TEST_ROOT"]) / "scripts/hermes-release-manager.py"),
            "render-launcher",
            env["_TEST_AGENT_REPO"],
            SUPPORTED_SHA,
            CARRIED_HEAD,
            str(candidate),
            "--baseline-file",
            str(original),
        ],
        check=True,
        capture_output=True,
    )
    candidate.replace(launcher)
    launcher.chmod(0o755)
    before = launcher.read_bytes()
    identity = (launcher.stat().st_dev, launcher.stat().st_ino)
    assert b"# unified-kanban-hermes-baseline sha256:" in before

    activate(env)

    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"
    assert launcher.read_bytes() == before
    assert (launcher.stat().st_dev, launcher.stat().st_ino) == identity


def test_activation_uses_one_normal_form_for_a_denormalized_checkout(
    tmp_path: Path,
) -> None:
    """A trailing separator must not move the release root into the checkout."""
    env = environment(tmp_path)
    env["HERMES_AGENT_REPO"] = env["_TEST_AGENT_REPO"] + "/"
    before = checkout_snapshot(env)

    activate(env)

    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"
    assert checkout_snapshot(env) == before
    assert not (Path(env["_TEST_AGENT_REPO"]) / ".releases").exists()


def test_traversal_in_the_checkout_path_fails_before_any_write(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env["HERMES_AGENT_REPO"] = env["_TEST_AGENT_REPO"] + "/.."

    result = run(env)

    assert result.returncode != 0
    assert "HERMES_AGENT_REPO" in result.stderr
    assert not selector(env).exists()
    assert not state_dir(env).exists()
    assert log_lines(env, "PREPARE_TEST_LOG") == []


def test_up_to_date_run_prepares_nothing_and_leaves_services_alone(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    activate(env)
    for key in ("PREPARE_TEST_LOG", "HERMES_TEST_LOG", "FAKE_GIT_LOG", "BUNDLE_TEST_LOG"):
        Path(env[key]).unlink(missing_ok=True)
    selected_before = selector(env).stat()

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("SKIPPED:")
    assert log_lines(env, "PREPARE_TEST_LOG") == []
    assert log_lines(env, "HERMES_TEST_LOG") == []
    assert log_lines(env, "FAKE_GIT_LOG") == []
    assert log_lines(env, "BUNDLE_TEST_LOG") == []
    after = selector(env).stat()
    assert (after.st_dev, after.st_ino) == (selected_before.st_dev, selected_before.st_ino)
    assert not lock_dir(env).exists()


def test_up_to_date_run_writes_no_state_and_takes_no_lock(tmp_path: Path) -> None:
    """A run with nothing to do must not create state or take the lock.

    The no-change decision is made from two reads, so it has to happen before
    the state directory and the lock directory are created.
    """
    env = environment(tmp_path)
    activate(env)
    shutil.rmtree(state_dir(env))
    for key in ("PREPARE_TEST_LOG", "HERMES_TEST_LOG", "FAKE_GIT_LOG", "BUNDLE_TEST_LOG"):
        Path(env[key]).unlink(missing_ok=True)

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("SKIPPED:")
    assert not state_dir(env).exists()
    assert not lock_dir(env).exists()
    assert log_lines(env, "PREPARE_TEST_LOG") == []
    assert log_lines(env, "HERMES_TEST_LOG") == []
    assert log_lines(env, "FAKE_GIT_LOG") == []


def test_selection_of_a_missing_release_is_not_up_to_date(tmp_path: Path) -> None:
    env = environment(tmp_path)
    activate(env)
    shutil.rmtree(target_release(env))
    selected = selector(env).read_text(encoding="utf-8")
    Path(env["HERMES_TEST_LOG"]).unlink(missing_ok=True)

    stale = run(env, "--check")

    assert stale.returncode == 0, stale.stderr
    assert stale.stdout.startswith("UPDATE_AVAILABLE:")

    repaired = run(env)

    assert repaired.returncode == 0, repaired.stderr
    assert not repaired.stdout.startswith("SKIPPED")
    assert target_release(env).is_dir()
    assert selector(env).read_text(encoding="utf-8") == selected
    assert log_lines(env, "HERMES_TEST_LOG") == ["gateway restart", "gateway status"]


def test_check_reports_selection_without_touching_anything(tmp_path: Path) -> None:
    env = environment(tmp_path)

    behind = run(env, "--check")

    assert behind.returncode == 0, behind.stderr
    assert behind.stdout.startswith("UPDATE_AVAILABLE:")
    assert not selector(env).exists()
    assert not state_dir(env).exists()
    assert log_lines(env, "PREPARE_TEST_LOG") == []
    assert log_lines(env, "FAKE_GIT_LOG") == []

    activate(env)
    current = run(env, "--check")

    assert current.returncode == 0, current.stderr
    assert current.stdout.strip() == "UP_TO_DATE"
    assert pending_is_absent(env)
    assert not lock_dir(env).exists()


def test_running_dashboard_is_restarted_onto_the_new_release(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env["HERMES_DELEGATED_CHILD_CONTEXT"] = "attacker"
    Path(env["HERMES_TEST_DASHBOARD_STATE"]).write_text("running\n", encoding="utf-8")

    activate(env)

    calls = log_lines(env, "HERMES_TEST_LOG")
    assert "dashboard --stop" in calls
    assert any(call.startswith("dashboard --host ") for call in calls)
    assert calls[-2:] == ["gateway restart", "gateway status"]
    assert Path(env["HERMES_TEST_DASHBOARD_STATE"]).exists()
    assert Path(env["HERMES_TEST_DASHBOARD_ENV"]).read_text(encoding="utf-8") == "unset"


def test_stopped_dashboard_is_not_started_by_an_update(tmp_path: Path) -> None:
    env = environment(tmp_path)

    activate(env)

    calls = log_lines(env, "HERMES_TEST_LOG")
    assert calls == ["gateway restart", "gateway status"]
    assert not Path(env["HERMES_TEST_DASHBOARD_STATE"]).exists()


def test_gateway_restart_failure_rolls_the_selector_back(tmp_path: Path) -> None:
    env = environment(tmp_path)
    activate(env)
    # Re-point the selector at a superseded release so the next run activates.
    superseded = release_root(env) / f"release-{'b' * 40}"
    superseded.mkdir()
    (superseded / "venv" / "bin").mkdir(parents=True)
    shutil.copy(env["FAKE_HERMES_RUNTIME"], superseded / "venv/bin/hermes")
    (superseded / "venv/bin/hermes").chmod(0o755)
    selector(env).write_text(f"{superseded}\n", encoding="utf-8")
    prior_payload = plistlib.loads(gateway_plist(env).read_bytes())
    prior_payload["ProgramArguments"][0] = str(superseded / "venv/bin/python")
    prior_payload["EnvironmentVariables"].pop("HERMES_DISABLE_LAZY_INSTALLS")
    prior_payload["EnvironmentVariables"].pop("HERMES_LAZY_INSTALL_TARGET")
    prior_payload.pop("Umask")
    prior_plist = plistlib.dumps(prior_payload, sort_keys=True)
    gateway_plist(env).write_bytes(prior_plist)
    gateway_plist(env).chmod(0o600)
    failing = dict(env)
    failing["HERMES_TEST_FAIL_COMMAND"] = "gateway restart"

    result = run(failing)

    assert result.returncode != 0
    assert selector(env).read_text(encoding="utf-8") == f"{superseded}\n"
    assert not previous_selector(env).exists()
    assert gateway_plist(env).read_bytes() == prior_plist
    launchctl_calls = log_lines(env, "FAKE_LAUNCHCTL_LOG")
    assert launchctl_calls == [
        f"bootout gui/{os.getuid()}/ai.hermes.gateway",
        f"bootstrap gui/{os.getuid()} {Path(env['FAKE_BOOTSTRAP_PLIST_LOG']).read_text(encoding='utf-8').strip()}",
        f"kickstart -k gui/{os.getuid()}/ai.hermes.gateway",
    ]
    assert "Hermes services could not be restarted" not in result.stderr
    assert not pending_is_absent(env)
    assert not lock_dir(env).exists()


def test_dashboard_compensation_rejects_symlinked_release_components(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    activate(env)
    prior = release_root(env) / f"release-{'8' * 40}"
    prior.mkdir()
    foreign = tmp_path / "foreign-venv"
    (foreign / "bin").mkdir(parents=True)
    marker = tmp_path / "foreign-executed"
    foreign_cli = foreign / "bin/hermes"
    foreign_cli.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 0\n", encoding="utf-8"
    )
    foreign_cli.chmod(0o755)
    os.symlink(foreign, prior / "venv")
    selector(env).write_text(f"{prior}\n", encoding="utf-8")
    Path(env["HERMES_TEST_DASHBOARD_STATE"]).write_text("running\n", encoding="utf-8")
    gateway_plist(env).unlink()
    failing = dict(env)
    failing["HERMES_TEST_FAIL_COMMAND"] = "gateway restart"

    result = run(failing)

    assert result.returncode != 0
    assert not marker.exists()
    assert "retained opening capabilities" in result.stderr


def test_indeterminate_launchd_query_retains_private_recovery_plist(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    activate(env)
    superseded = release_root(env) / f"release-{'7' * 40}"
    (superseded / "venv/bin").mkdir(parents=True)
    shutil.copy(env["FAKE_HERMES_RUNTIME"], superseded / "venv/bin/hermes")
    (superseded / "venv/bin/hermes").chmod(0o755)
    selector(env).write_text(f"{superseded}\n", encoding="utf-8")
    loaded = tmp_path / "loaded-launchd-state"
    failing = dict(env)
    failing.update(
        {
            "HERMES_TEST_FAIL_COMMAND": "gateway restart",
            "HERMES_TEST_LAUNCHD_ACTUAL": str(loaded),
            "FAKE_LAUNCHCTL_FAIL_KICKSTART": "1",
            "FAKE_LAUNCHCTL_PRINT_INDETERMINATE": "1",
        }
    )

    result = run(failing)

    assert result.returncode != 0
    assert not loaded.exists()
    retained = Path(env["FAKE_BOOTSTRAP_PLIST_LOG"]).read_text(encoding="utf-8").strip()
    assert Path(retained).is_file()
    assert "retained private update recovery" in result.stderr
    assert not lock_dir(env).exists()


def test_ambiguous_loaded_job_retains_private_recovery_plist(tmp_path: Path) -> None:
    env = environment(tmp_path)
    activate(env)
    superseded = release_root(env) / f"release-{'9' * 40}"
    (superseded / "venv/bin").mkdir(parents=True)
    shutil.copy(env["FAKE_HERMES_RUNTIME"], superseded / "venv/bin/hermes")
    (superseded / "venv/bin/hermes").chmod(0o755)
    selector(env).write_text(f"{superseded}\n", encoding="utf-8")
    loaded = tmp_path / "loaded-launchd-state"
    failing = dict(env)
    failing.update(
        {
            "HERMES_TEST_FAIL_COMMAND": "gateway restart",
            "HERMES_TEST_LAUNCHD_ACTUAL": str(loaded),
            "FAKE_LAUNCHCTL_FAIL_KICKSTART": "1",
            "FAKE_LAUNCHCTL_STICKY_LOADED": "1",
        }
    )

    result = run(failing)

    assert result.returncode != 0
    assert loaded.exists()
    retained = Path(env["FAKE_BOOTSTRAP_PLIST_LOG"]).read_text(encoding="utf-8").strip()
    assert Path(retained).is_file()
    assert "retained private update recovery" in result.stderr
    assert not lock_dir(env).exists()


def test_absent_prior_plist_with_valid_selector_deactivates_gateway(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    activate(env)
    superseded = release_root(env) / f"release-{'e' * 40}"
    (superseded / "venv/bin").mkdir(parents=True)
    shutil.copy(env["FAKE_HERMES_RUNTIME"], superseded / "venv/bin/hermes")
    (superseded / "venv/bin/hermes").chmod(0o755)
    selector(env).write_text(f"{superseded}\n", encoding="utf-8")
    gateway_plist(env).unlink()
    failing = dict(env)
    failing["HERMES_TEST_FAIL_COMMAND"] = "gateway restart"

    result = run(failing)

    assert result.returncode != 0
    assert selector(env).read_text(encoding="utf-8") == f"{superseded}\n"
    assert not gateway_plist(env).exists()
    assert log_lines(env, "FAKE_LAUNCHCTL_LOG") == [
        f"bootout gui/{os.getuid()}/ai.hermes.gateway",
        f"print gui/{os.getuid()}/ai.hermes.gateway",
    ]


@pytest.mark.parametrize("raced_path", ["selector", "plist"])
def test_post_rollback_foreign_successor_cannot_be_compensation_authority(
    tmp_path: Path, raced_path: str
) -> None:
    env = environment(tmp_path)
    activate(env)
    superseded = release_root(env) / f"release-{'f' * 40}"
    (superseded / "venv/bin").mkdir(parents=True)
    shutil.copy(env["FAKE_HERMES_RUNTIME"], superseded / "venv/bin/hermes")
    (superseded / "venv/bin/hermes").chmod(0o755)
    selector(env).write_text(f"{superseded}\n", encoding="utf-8")
    prior_payload = plistlib.loads(gateway_plist(env).read_bytes())
    prior_payload["ProgramArguments"][0] = str(superseded / "venv/bin/python")
    prior_plist = plistlib.dumps(prior_payload, sort_keys=True)
    gateway_plist(env).write_bytes(prior_plist)
    gateway_plist(env).chmod(0o600)
    canonical = selector(env) if raced_path == "selector" else gateway_plist(env)
    foreign = (
        f"{release_root(env)}/release-{'c' * 40}\n".encode()
        if raced_path == "selector"
        else b"FOREIGN-PLIST-SUCCESSOR"
    )
    Path(env["HERMES_TEST_LOG"]).unlink(missing_ok=True)
    racing = dict(env)
    racing.update(
        {
            "HERMES_TEST_FAIL_COMMAND": "gateway restart",
            "FAKE_PY_SWAP_AFTER_ROLLBACK_TARGET": str(canonical),
            "FAKE_PY_SWAP_CONTENT": foreign.decode(),
        }
    )

    result = run(racing)

    assert result.returncode != 0
    assert canonical.read_bytes() == foreign
    bootstrap = Path(env["FAKE_BOOTSTRAP_PLIST_LOG"]).read_text(encoding="utf-8").strip()
    assert bootstrap != str(gateway_plist(env))
    assert "/unified-kanban-update." in bootstrap
    assert log_lines(env, "HERMES_TEST_LOG").count("gateway restart") == 1


def test_unsupervised_gateway_rolls_the_selector_back(tmp_path: Path) -> None:
    env = environment(tmp_path)
    supervision = tmp_path / "gateway-supervision"
    supervision.write_text("unmanaged\n", encoding="utf-8")
    env.update(
        {
            "HERMES_TEST_GATEWAY_SUPERVISION": str(supervision),
            "HERMES_TEST_GATEWAY_NEVER_SUPERVISED": "1",
        }
    )

    result = run(env)

    assert result.returncode != 0
    assert "launchd did not supervise" in result.stderr
    assert not selector(env).exists()
    assert not gateway_plist(env).exists()
    assert not pending_is_absent(env)
    assert not lock_dir(env).exists()
    assert log_lines(env, "FAKE_LAUNCHCTL_LOG") == [
        f"bootout gui/{os.getuid()}/ai.hermes.gateway",
        f"print gui/{os.getuid()}/ai.hermes.gateway",
    ]


def test_stale_loaded_gateway_program_is_recovered_before_activation_commits(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    loaded = tmp_path / "loaded-launchd-program"
    env.update(
        {
            "HERMES_TEST_LAUNCHD_ACTUAL": str(loaded),
            "HERMES_TEST_RESTART_STALE_ONCE": str(tmp_path / "stale-loaded-once"),
        }
    )

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert loaded.read_text(encoding="utf-8") == "match\n"
    assert log_lines(env, "HERMES_TEST_LOG")[-4:] == [
        "gateway restart",
        "gateway stop",
        "gateway start",
        "gateway status",
    ]


def test_activation_preserves_a_foreign_selector_successor(tmp_path: Path) -> None:
    env = environment(tmp_path)
    foreign = f"{release_root(env)}/release-{'c' * 40}\n"
    racing = dict(env)
    racing.update(
        {
            "FAKE_PY_SWAP_AFTER_TARGET": str(selector(env)),
            "FAKE_PY_SWAP_CONTENT": foreign,
            "HERMES_TEST_FAIL_COMMAND": "gateway restart",
        }
    )

    result = run(racing)

    assert result.returncode != 0
    # The successor inode is neither adopted as our publication nor removed.
    assert selector(env).read_text(encoding="utf-8") == foreign
    assert "operation identity no longer present" in result.stderr
    assert log_lines(env, "HERMES_TEST_LOG") == []
    assert target_release(env).is_dir()


def test_rollback_failure_after_service_disturbance_skips_compensation(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    foreign = f"{release_root(env)}/release-{'d' * 40}\n"
    racing = dict(env)
    racing.update(
        {
            "FAKE_PY_SWAP_SELECTOR_DURING_VERIFY": str(selector(env)),
            "FAKE_PY_SWAP_CONTENT": foreign,
        }
    )

    result = run(racing)

    assert result.returncode != 0
    assert selector(env).read_text(encoding="utf-8") == foreign
    assert "foreign paths were preserved" in result.stderr
    calls = log_lines(env, "HERMES_TEST_LOG")
    assert calls.count("gateway restart") == 1
    assert calls.count("gateway status") == 1


def test_moved_official_upstream_reports_next_update_without_blocking_snapshot(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    env["FAKE_OFFICIAL_UPSTREAM"] = "d" * 40

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert "Notice: official Hermes main is" in result.stderr
    assert "activating reviewed snapshot" in result.stderr
    assert log_lines(env, "PREPARE_TEST_LOG") != []
    assert selector(env).exists()
    assert pending_is_absent(env)


def test_unreadable_official_upstream_does_not_block_reviewed_snapshot(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    env["FAKE_LS_REMOTE_FAIL"] = "1"

    result = run(env)

    assert result.returncode == 0, result.stderr
    assert "official Hermes main could not be read" in result.stderr
    assert "activating reviewed snapshot" in result.stderr
    assert log_lines(env, "PREPARE_TEST_LOG") != []
    assert selector(env).exists()


def test_foreign_launcher_blocks_activation(tmp_path: Path) -> None:
    env = environment(tmp_path)
    launcher = Path(env["HOME"]) / ".local/bin/hermes"
    launcher.write_text("#!/bin/sh\necho FOREIGN\n", encoding="utf-8")
    launcher.chmod(0o755)

    result = run(env)

    assert result.returncode != 0
    assert "Hermes launcher unified-kanban did not install" in result.stderr
    assert log_lines(env, "PREPARE_TEST_LOG") == []
    assert not selector(env).exists()


def test_missing_launcher_blocks_activation(tmp_path: Path) -> None:
    env = environment(tmp_path, install_launcher=False)

    result = run(env)

    assert result.returncode != 0
    assert "managed Hermes launcher is missing" in result.stderr
    assert "run ./scripts/setup.sh first" in result.stderr
    assert not selector(env).exists()
    assert log_lines(env, "PREPARE_TEST_LOG") == []


def test_symlinked_launcher_blocks_activation(tmp_path: Path) -> None:
    env = environment(tmp_path, install_launcher=False)
    launcher = Path(env["HOME"]) / ".local/bin/hermes"
    real = tmp_path / "elsewhere-hermes"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    os.symlink(real, launcher)

    result = run(env)

    assert result.returncode != 0
    assert "managed Hermes launcher is missing" in result.stderr
    assert launcher.is_symlink()
    assert not selector(env).exists()


def test_prepare_only_builds_the_release_without_selecting_it(tmp_path: Path) -> None:
    env = environment(tmp_path)

    result = run(env, "--prepare-only")

    assert result.returncode == 0, result.stderr
    assert f"PREPARED: {target_release(env)}" in result.stdout
    assert target_release(env).is_dir()
    assert not selector(env).exists()
    assert log_lines(env, "HERMES_TEST_LOG") == []
    assert pending_is_absent(env)
    assert len(log_lines(env, "BUNDLE_TEST_LOG")) == 1


def test_no_restart_activation_keeps_the_pending_restart(tmp_path: Path) -> None:
    env = environment(tmp_path)

    result = run(env, "--no-restart")

    assert result.returncode == 0, result.stderr
    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"
    assert log_lines(env, "HERMES_TEST_LOG") == []
    assert not pending_is_absent(env)

    completed = run(env)

    assert completed.returncode == 0, completed.stderr
    assert "RESUMING" in completed.stdout
    assert log_lines(env, "HERMES_TEST_LOG") == ["gateway restart", "gateway status"]
    assert pending_is_absent(env)


def clear_lock_left_by_a_killed_run(env: dict[str, str]) -> None:
    """SIGKILL leaves the lock behind on purpose.

    The updater never reclaims a lock it cannot prove is its own, so completing
    a killed run is a deliberate manual step. That refusal has its own tests;
    here it only has to be performed so the recovery path can be exercised.
    """
    assert lock_dir(env).is_symlink()
    lock_dir(env).unlink()


@pytest.mark.parametrize(
    "crash_hook", ["FAKE_PY_KILL_PARENT_AFTER_TARGET", "FAKE_PY_KILL_PARENT_AFTER_CHECKPOINT"]
)
def test_interrupted_activation_is_resumed_instead_of_reported_up_to_date(
    tmp_path: Path, crash_hook: str
) -> None:
    env = environment(tmp_path)
    crashing = dict(env)
    crashing[crash_hook] = str(selector(env))

    crashed = run(crashing)

    assert crashed.returncode != 0
    # The selection survived the kill, so a rerun must finish the restart
    # instead of declaring the installation up to date.
    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"
    assert not pending_is_absent(env)
    assert log_lines(env, "HERMES_TEST_LOG") == []
    clear_lock_left_by_a_killed_run(env)

    recovered = run(env)

    assert recovered.returncode == 0, recovered.stderr
    assert "RESUMING" in recovered.stdout
    assert log_lines(env, "HERMES_TEST_LOG") == ["gateway restart", "gateway status"]
    assert pending_is_absent(env)
    assert not lock_dir(env).exists()


def test_interrupted_run_that_never_activated_reactivates(tmp_path: Path) -> None:
    env = environment(tmp_path)
    failing = dict(env)
    failing["FAKE_PY_FAIL_REPLACE_TARGET"] = str(selector(env))

    failed = run(failing)

    assert failed.returncode != 0
    assert not selector(env).exists()
    assert not pending_is_absent(env)

    recovered = run(env)

    assert recovered.returncode == 0, recovered.stderr
    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"
    assert pending_is_absent(env)


def test_existing_release_is_reverified_before_reuse(tmp_path: Path) -> None:
    env = environment(tmp_path)
    activate(env)
    superseded = release_root(env) / f"release-{'e' * 40}"
    superseded.mkdir()
    selector(env).write_text(f"{superseded}\n", encoding="utf-8")
    Path(env["PREPARE_TEST_LOG"]).unlink(missing_ok=True)
    Path(env["BUNDLE_TEST_LOG"]).unlink(missing_ok=True)
    reverify_fails = dict(env)
    reverify_fails["PREPARE_TEST_FAIL"] = "1"

    result = run(reverify_fails)

    assert result.returncode != 0
    assert len(log_lines(env, "PREPARE_TEST_LOG")) == 1
    assert selector(env).read_text(encoding="utf-8") == f"{superseded}\n"


def test_pending_cleanup_failure_does_not_undo_a_committed_activation(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    uncleanable = dict(env)
    uncleanable["FAKE_PY_FAIL_STATE_REMOVE"] = "1"

    result = run(uncleanable)

    # The activation and the restart both completed; a cleanup fault afterwards
    # is not an incomplete update and must not roll the selection back.
    assert result.returncode == 0, result.stderr
    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"
    assert log_lines(env, "HERMES_TEST_LOG") == ["gateway restart", "gateway status"]
    assert "pending marker could not be cleared" in result.stderr
    assert not pending_is_absent(env)

    completed = run(env)

    assert completed.returncode == 0, completed.stderr
    assert "RESUMING" in completed.stdout
    assert pending_is_absent(env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HERMES_DASHBOARD_PORT", "not-a-port"),
        ("HERMES_DASHBOARD_PORT", "70000"),
        ("HERMES_DASHBOARD_PORT", "0"),
        ("HERMES_DASHBOARD_READY_ATTEMPTS", "0"),
        ("HERMES_DASHBOARD_READY_ATTEMPTS", "a[$(touch /tmp/unified-kanban-injected)]"),
        ("HERMES_DASHBOARD_HOST", "127.0.0.1 --proxy http://attacker.invalid"),
    ],
)
def test_malformed_dashboard_settings_fail_before_managed_writes(
    tmp_path: Path, name: str, value: str
) -> None:
    env = environment(tmp_path)
    env[name] = value

    result = run(env)

    assert result.returncode != 0
    assert name in result.stderr
    assert not selector(env).exists()
    assert not state_dir(env).exists()
    assert log_lines(env, "PREPARE_TEST_LOG") == []
    assert not Path("/tmp/unified-kanban-injected").exists()


def test_ipv6_dashboard_host_is_probed_as_a_bracketed_literal(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env["HERMES_DASHBOARD_HOST"] = "::1"

    activate(env)

    probes = log_lines(env, "CURL_TEST_LOG")
    assert probes
    assert all("http://[::1]:9119/api/status" in probe for probe in probes)


def test_external_commands_do_not_inherit_transaction_authority(tmp_path: Path) -> None:
    env = environment(tmp_path)
    report = tmp_path / "authority-report"
    env["HERMES_TEST_AUTHORITY_REPORT"] = str(report)
    env["GIT_DIR"] = str(tmp_path / "attacker-git-dir")
    env["HERMES_DELEGATED_CHILD_CONTEXT"] = "attacker"
    Path(env["HERMES_TEST_DASHBOARD_STATE"]).write_text("running\n", encoding="utf-8")

    activate(env)

    reports = report.read_text(encoding="utf-8").splitlines()
    assert len(reports) >= 3
    for line in reports:
        assert "fd9=closed fd10=closed" in line
        assert "dir=unset txfd=unset ledgerfd=unset" in line
        assert "delegated=unset" in line
        assert "gitdir=unset" in line


def test_relative_hermes_home_is_rejected_before_any_command(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env["HERMES_HOME"] = "relative/hermes"

    result = run(env)

    assert result.returncode != 0
    assert "HERMES_HOME must be an absolute path" in result.stderr
    assert not selector(env).exists()


def test_relative_agent_repo_is_rejected_before_any_command(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env["HERMES_AGENT_REPO"] = "relative/hermes-agent"

    result = run(env)

    assert result.returncode != 0
    assert "HERMES_AGENT_REPO must be an absolute path" in result.stderr


def test_custom_agent_repo_selects_its_own_sibling_release_root(tmp_path: Path) -> None:
    env = environment(tmp_path)
    custom = tmp_path / "custom" / "hermes-agent"
    custom.mkdir(parents=True)
    env["HERMES_AGENT_REPO"] = str(custom)
    env["_TEST_AGENT_REPO"] = str(custom)
    install_managed_launcher(
        Path(env["_TEST_ROOT"]), custom, Path(env["HOME"]) / ".local/bin/hermes"
    )

    activate(env)

    assert selector(env) == tmp_path / "custom/hermes-agent.releases/current"
    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"
    assert not (tmp_path / "hermes-agent.releases").exists()


def test_refuses_unsupported_python_before_managed_write(tmp_path: Path) -> None:
    env = environment(tmp_path)
    stub = Path(env["PATH"].split(os.pathsep)[0]) / "python3"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *'sys.version_info >= (3, 11)'* ]]; then exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    result = run(env)

    assert result.returncode != 0
    assert "Python 3.11 or newer is required" in result.stderr
    assert not selector(env).exists()
    assert not state_dir(env).exists()


def test_lock_without_owner_pid_is_preserved(tmp_path: Path) -> None:
    env = environment(tmp_path)
    lock_dir(env).parent.mkdir(parents=True, exist_ok=True)
    lock_dir(env).parent.chmod(0o700)
    os.symlink("legacy-token", lock_dir(env))

    result = run(env)

    assert result.returncode != 0
    assert "another update holds the lock" in result.stderr
    assert os.readlink(lock_dir(env)) == "legacy-token"
    assert not selector(env).exists()


def test_stale_lock_with_dead_owner_is_preserved_fail_closed(tmp_path: Path) -> None:
    env = environment(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    dead.wait()
    lock_dir(env).parent.mkdir(parents=True, exist_ok=True)
    lock_dir(env).parent.chmod(0o700)
    os.symlink(f"{dead.pid}:token", lock_dir(env))

    result = run(env)

    assert result.returncode != 0
    assert "refusing automatic removal" in result.stderr
    assert os.readlink(lock_dir(env)) == f"{dead.pid}:token"
    assert not selector(env).exists()


def test_unknown_option_is_refused(tmp_path: Path) -> None:
    env = environment(tmp_path)

    result = run(env, "--wipe-everything")

    assert result.returncode == 2
    assert "Unknown option" in result.stderr


def test_legacy_force_flag_remains_a_no_op(tmp_path: Path) -> None:
    env = environment(tmp_path)

    result = run(env, "--force", "--no-restart")

    assert result.returncode == 0, result.stderr
    assert selector(env).read_text(encoding="utf-8") == f"{target_release(env)}\n"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"/not/a/release\n",
        b"relative/release-" + b"f" * 40 + b"\n",
        b"/tmp/release-" + b"f" * 40 + b"\n/tmp/second\n",
    ],
)
def test_corrupt_selector_fails_closed(tmp_path: Path, payload: bytes) -> None:
    env = environment(tmp_path)
    release_root(env).mkdir(mode=0o700, exist_ok=True)
    selector(env).write_bytes(payload)

    result = run(env, "--check")

    assert result.returncode != 0
    assert "Hermes release selector" in result.stderr
    assert selector(env).read_bytes() == payload


def test_symlinked_selector_is_refused(tmp_path: Path) -> None:
    env = environment(tmp_path)
    release_root(env).mkdir(mode=0o700, exist_ok=True)
    os.symlink(target_release(env), selector(env))

    result = run(env)

    assert result.returncode != 0
    assert "Hermes release selector is not a regular file" in result.stderr
    assert selector(env).is_symlink()
