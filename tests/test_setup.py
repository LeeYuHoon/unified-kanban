from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "scripts" / "setup.sh"
UNINSTALL = REPO / "scripts" / "uninstall.sh"
SUPPORTED_SHA = (REPO / "patches/hermes-agent-supported-upstream").read_text(
    encoding="utf-8"
).strip()
CARRIED_HEAD = [
    line for line in (REPO / "patches/hermes-agent-carried-commits").read_text(
        encoding="utf-8"
    ).splitlines()
    if line and not line.startswith("#")
][-1]


# Publishing the receipt through the real producer keeps the fixture from
# drifting away from the format the runtime gate has to trust.
RELEASE_RECEIPT_HELPER = '''\
"""Give a faked release the producer's own completion receipt."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

manager, agent_repo, upstream, carried = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("hermes_release_manager", manager)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
layout = helper.release_layout(agent_repo, upstream, carried)
if not (layout.release / ".git").is_dir():
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main"],
        cwd=layout.release,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-q", "--allow-empty", "-m", "reviewed release",
        ],
        cwd=layout.release,
        check=True,
        capture_output=True,
    )
if not (layout.release / helper._COMPLETION_RECEIPT).exists():
    # This setup harness uses the publication manifest's carried SHA without
    # importing its Git objects, so no synthetic commit can have that chosen
    # object id. Install the exact producer stamp bytes/mode, bypass only that
    # impossible preimage check, and still let the real producer construct and
    # durably publish the complete receipt-v2 payload.
    stamp = layout.release / helper._BYTECODE_FINGERPRINT
    stamp.write_text(f"git:refs/heads/main:{carried}", encoding="utf-8")
    stamp.chmod(0o600)
    helper._verify_bytecode_fingerprint = lambda _release, _carried: None
    helper._publish_completion_receipt(layout, upstream, carried)
'''


def install_fake_hermes(
    root: Path,
    *,
    boards: list[str] | None = None,
    compatible: bool = True,
    missing_option: str | None = None,
    option_replacements: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    log = root / "hermes.log"
    script = fake_bin / "hermes"
    def options(text: str) -> str:
        if not compatible:
            return ""
        if missing_option:
            text = text.replace(missing_option, "")
        for old, new in (option_replacements or {}).items():
            text = text.replace(old, new)
        return text

    compatibility_cases = (
        f"  'kanban --help') printf '%s\\n' '{options('--board')}';;\n"
        f"  'kanban create --help') printf '%s\\n' "
        f"'{options('--assignee --tenant --created-by --initial-status --observation --idempotency-key --title-file --json')}';;\n"
        f"  'kanban boards list --help') printf '%s\\n' '{options('--json')}';;\n"
        f"  'kanban boards create --help') printf '%s\\n' '{options('--name')}';;\n"
        f"  'kanban comment --help') printf '%s\\n' "
        f"'{options('--author --idempotency-key')}';;\n"
        f"  'kanban complete --help') printf '%s\\n' '{options('--summary')}';;\n"
        f"  'kanban block --help') printf '%s\\n' '{options('--kind')}';;\n"
        "  'kanban show --help') echo 'task_id';;\n"
        "  'kanban archive --help') echo 'task_ids';;\n"
    )
    script.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_HERMES_LOG\"\n"
        # Probe inherited descriptors with fstat from a freshly exec'd process.
        # `: <&N` only reports bash's own bookkeeping and claims fd10 is open
        # after the parent closed it, and `readlink /dev/fd/N` always fails on
        # Darwin because /dev/fd/N is a character device - both make the leak
        # assertion vacuous.
        "if [[ -n \"${FAKE_AUTHORITY_REPORT:-}\" && ( \"$*\" == 'gateway restart' "
        "|| \"$*\" == 'gateway install --force --start-now --start-on-login' "
        "|| \"$*\" == 'gateway status' || \"$*\" == 'gateway stop' "
        "|| \"$*\" == 'gateway start' "
        "|| \"$*\" == 'kanban --board '*' create '* || \"$*\" == 'kanban boards list --json' ) ]]; then\n"
        "  descriptors=$(/usr/bin/python3 -c '\n"
        "import os\n"
        "def probe(fd):\n"
        "    try:\n"
        "        os.fstat(fd)\n"
        "    except OSError:\n"
        "        return \"fd%d=closed\" % fd\n"
        "    return \"fd%d=open\" % fd\n"
        "print(\" \".join(probe(fd) for fd in (9, 10)))\n"
        "')\n"
        "  printf '%s | %s dir=%s txfd=%s ledgerfd=%s\\n' \"$*\" \"$descriptors\" "
        "\"${UNIFIED_KANBAN_TRANSACTION_DIR-unset}\" \"${UNIFIED_KANBAN_TRANSACTION_FD-unset}\" "
        "\"${UNIFIED_KANBAN_TOKEN_LEDGER_FD-unset}\" >> \"$FAKE_AUTHORITY_REPORT\"\n"
        "fi\n"
        "if [[ -n \"${FAKE_FAIL_COMMAND:-}\" && \"$*\" == \"$FAKE_FAIL_COMMAND\" ]]; then exit 42; fi\n"
        "if [[ -n \"${FAKE_FAIL_COMMAND_ONCE:-}\" && \"$*\" == \"$FAKE_FAIL_COMMAND_ONCE\" "
        "&& ! -e \"$FAKE_FAIL_ONCE_MARKER\" ]]; then\n"
        "  touch \"$FAKE_FAIL_ONCE_MARKER\"\n"
        "  if [[ -n \"${FAKE_GATEWAY_STATE:-}\" ]]; then printf 'altered\\n' > \"$FAKE_GATEWAY_STATE\"; fi\n"
        "  exit 42\n"
        "fi\n"
        "case \"$*\" in\n"
        f"  version) echo \"Hermes Agent v0.18.2 upstream ${{FAKE_HERMES_UPSTREAM:-{SUPPORTED_SHA[:8]}}}\";;\n"
        + compatibility_cases +
        "  'kanban boards list --json')\n"
        "    if [[ -n \"${FAKE_SWAP_ENVRC:-}\" ]]; then\n"
        "      rm -f -- \"$FAKE_SWAP_ENVRC\"\n"
        "      ln -s -- \"$FAKE_SWAP_TARGET\" \"$FAKE_SWAP_ENVRC\"\n"
        "    fi\n"
        "    printf '%s\\n' \"$FAKE_BOARDS_JSON\";;\n"
        "  'kanban boards create '*) echo 'created';;\n"
        "  'kanban --board '*' create '*) printf '%s\\n' '{\"id\":\"t_smoke\",\"status\":\"running\",\"observation\":true}';;\n"
        "  'kanban --board '*' comment '*) :;;\n"
        "  'kanban --board '*' complete '*) :;;\n"
        "  'kanban --board '*' show '*) printf '%s\\n' 'smoke-update-ok status:    done smoke-done-ok status:    archived';;\n"
        "  'kanban --board '*' archive '*) :;;\n"
        "  'plugins list --json') printf '[{\"name\":\"hermes-kanban\",\"status\":\"%s\"}]\\n' \"${FAKE_PLUGIN_STATUS:-disabled}\";;\n"
        "  'plugins enable --no-allow-tool-override hermes-kanban')\n"
        "    mkdir -p \"${HERMES_HOME:-$HOME/.hermes}\"\n"
        "    printf '%s\\n' 'plugins: [hermes-kanban]' > \"${HERMES_HOME:-$HOME/.hermes}/config.yaml\";;\n"
        "  'plugins disable hermes-kanban')\n"
        "    mkdir -p \"${HERMES_HOME:-$HOME/.hermes}\"\n"
        "    printf '%s\\n' 'plugins: []' > \"${HERMES_HOME:-$HOME/.hermes}/config.yaml\";;\n"
        "  'gateway restart')\n"
        "    if [[ -n \"${FAKE_GATEWAY_STATE:-}\" ]]; then printf 'running\\n' > \"$FAKE_GATEWAY_STATE\"; fi\n"
        "    echo 'restarted';;\n"
        "  'gateway install --force --start-now --start-on-login')\n"
        "    if [[ -n \"${FAKE_GATEWAY_STATE:-}\" ]]; then printf 'running\\n' > \"$FAKE_GATEWAY_STATE\"; fi\n"
        "    plist=\"$HOME/Library/LaunchAgents/ai.hermes.gateway.plist\"\n"
        "    mkdir -p \"$(dirname \"$plist\")\"\n"
        "    gateway_python=\"$(dirname \"$0\")/python\"\n"
        "    /usr/bin/python3 -c 'import pathlib,plistlib,sys; p=pathlib.Path(sys.argv[1]); p.write_bytes(plistlib.dumps({\"Label\":\"ai.hermes.gateway\",\"ProgramArguments\":[sys.argv[2],\"-m\",\"hermes_cli.main\",\"gateway\",\"run\"],\"EnvironmentVariables\":{\"PATH\":\"/usr/bin:/bin\"}})); p.chmod(0o600)' \"$plist\" \"$gateway_python\"\n"
        "    if [[ -n \"${FAKE_GATEWAY_SUPERVISION:-}\" ]]; then\n"
        "      if [[ -n \"${FAKE_INSTALL_UNMANAGED_ONCE:-}\" && ! -e \"$FAKE_INSTALL_UNMANAGED_ONCE\" ]]; then\n"
        "        touch \"$FAKE_INSTALL_UNMANAGED_ONCE\"\n"
        "        printf 'unmanaged\\n' > \"$FAKE_GATEWAY_SUPERVISION\"\n"
        "      else\n"
        "        printf 'supervised\\n' > \"$FAKE_GATEWAY_SUPERVISION\"\n"
        "      fi\n"
        "    fi\n"
        "    if [[ -n \"${FAKE_LAUNCHD_ACTUAL:-}\" ]]; then\n"
        "      if [[ -n \"${FAKE_INSTALL_STALE_LAUNCHD_ONCE:-}\" && ! -e \"$FAKE_INSTALL_STALE_LAUNCHD_ONCE\" ]]; then\n"
        "        touch \"$FAKE_INSTALL_STALE_LAUNCHD_ONCE\"; printf 'stale\\n' > \"$FAKE_LAUNCHD_ACTUAL\"\n"
        "      else printf 'match\\n' > \"$FAKE_LAUNCHD_ACTUAL\"; fi\n"
        "    fi\n"
        "    echo 'installed';;\n"
        "  'gateway status')\n"
        "    supervision=''\n"
        "    if [[ -n \"${FAKE_GATEWAY_SUPERVISION:-}\" && -f \"$FAKE_GATEWAY_SUPERVISION\" ]]; then IFS= read -r supervision < \"$FAKE_GATEWAY_SUPERVISION\"; fi\n"
        "    if [[ -z \"${FAKE_GATEWAY_SUPERVISION:-}\" || \"$supervision\" == 'supervised' ]]; then\n"
        "      echo 'Gateway is supervised by launchd'\n"
        "    else\n"
        "      echo 'Gateway service is not loaded'\n"
        "    fi;;\n"
        "  'gateway start')\n"
        "    if [[ -n \"${FAKE_GATEWAY_STATE:-}\" ]]; then printf 'running\\n' > \"$FAKE_GATEWAY_STATE\"; fi\n"
        "    if [[ -n \"${FAKE_GATEWAY_SUPERVISION:-}\" ]]; then\n"
        "      if [[ -n \"${FAKE_INSTALL_UNMANAGED_ONCE:-}\" && ! -e \"$FAKE_INSTALL_UNMANAGED_ONCE\" ]]; then\n"
        "        touch \"$FAKE_INSTALL_UNMANAGED_ONCE\"; printf 'unmanaged\\n' > \"$FAKE_GATEWAY_SUPERVISION\"\n"
        "      elif [[ -z \"${FAKE_GATEWAY_NEVER_SUPERVISED:-}\" ]]; then printf 'supervised\\n' > \"$FAKE_GATEWAY_SUPERVISION\"; fi\n"
        "    fi\n"
        "    if [[ -n \"${FAKE_LAUNCHD_ACTUAL:-}\" ]]; then\n"
        "      if [[ -n \"${FAKE_INSTALL_STALE_LAUNCHD_ONCE:-}\" && ! -e \"$FAKE_INSTALL_STALE_LAUNCHD_ONCE\" ]]; then\n"
        "        touch \"$FAKE_INSTALL_STALE_LAUNCHD_ONCE\"; printf 'stale\\n' > \"$FAKE_LAUNCHD_ACTUAL\"\n"
        "      elif [[ -z \"${FAKE_GATEWAY_NEVER_SUPERVISED:-}\" ]]; then printf 'match\\n' > \"$FAKE_LAUNCHD_ACTUAL\"; fi\n"
        "    fi\n"
        "    echo 'started';;\n"
        "  'gateway uninstall')\n"
        "    if [[ -n \"${FAKE_GATEWAY_STATE:-}\" ]]; then printf 'stopped\\n' > \"$FAKE_GATEWAY_STATE\"; fi\n"
        "    echo 'uninstalled';;\n"
        "  'gateway stop')\n"
        "    if [[ -n \"${FAKE_GATEWAY_STATE:-}\" ]]; then printf 'stopped\\n' > \"$FAKE_GATEWAY_STATE\"; fi\n"
        "    echo 'stopped';;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    git = fake_bin / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == '-C' ]]; then shift 2; fi\n"
        "case \"$*\" in\n"
        f"  '-c credential.helper= -c core.askPass= -c http.extraHeader= ls-remote https://github.com/NousResearch/hermes-agent.git refs/heads/main') echo \"${{FAKE_OFFICIAL_UPSTREAM:-{SUPPORTED_SHA}}} refs/heads/main\";;\n"
        # No fake answer for `rev-parse HEAD`: nothing may decide compatibility
        # from a checkout HEAD that no flow ever moves onto the carried tip.
        f"  'rev-parse origin/main') echo \"${{FAKE_GIT_UPSTREAM:-{SUPPORTED_SHA}}}\";;\n"
        "  'reset --hard '*) exit 0;;\n"
        "  'cat-file -e '*'{commit}')\n"
        "    if [[ -n \"${FAKE_REQUIRE_BUNDLE:-}\" && ! -f \"$FAKE_BUNDLE_IMPORTED\" ]]; then exit 1; fi;;\n"
        "  'fetch '*'+refs/heads/*:refs/unified-kanban/carried/*') touch \"$FAKE_BUNDLE_IMPORTED\";;\n"
        "  'cherry HEAD '*) echo \"- ${*: -1}\";;\n"
        "  *) echo \"unexpected git call: $*\" >&2; exit 1;;\n"
        "esac\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    return fake_bin, log


def run_script(
    script: Path,
    home: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_STATE_HOME"] = str(home / ".local/state")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    if script == SETUP and env_extra is None:
        env_extra, _ = fake_env(home)
    if env_extra:
        env.update(env_extra)
    fake_bin = home / "fake-bin"
    if fake_bin.is_dir() and str(fake_bin) not in env["PATH"].split(os.pathsep):
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    fixture_script = home / "unified-kanban/scripts" / script.name
    actual_script = (
        fixture_script
        if script in (SETUP, UNINSTALL) and fixture_script.exists()
        else script
    )
    process_env = {key: value for key, value in env.items() if not key.startswith("_TEST_")}
    return subprocess.run(
        ["bash", str(actual_script), *args], cwd=REPO, env=process_env,
        text=True, capture_output=True, check=False,
    )


def fake_env(
    tmp_path: Path,
    *,
    boards: list[str] | None = None,
    compatible: bool = True,
    missing_option: str | None = None,
    option_replacements: dict[str, str] | None = None,
) -> tuple[dict[str, str], Path]:
    (tmp_path / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)
    fixture_root = tmp_path / "unified-kanban"
    for name in ("bin", "scripts", "patches", "src", "integrations"):
        shutil.copytree(REPO / name, fixture_root / name, dirs_exist_ok=True)
    fake_bin, log = install_fake_hermes(
        tmp_path,
        boards=boards,
        compatible=compatible,
        missing_option=missing_option,
        option_replacements=option_replacements,
    )
    agent_repo = tmp_path / "fake-hermes-agent"
    agent_repo.mkdir(exist_ok=True)
    carried = fixture_root / "patches/hermes-agent-carried-commits"
    receipt_helper = tmp_path / "publish-fake-release-receipt.py"
    receipt_helper.write_text(RELEASE_RECEIPT_HELPER, encoding="utf-8")
    python = fake_bin / "python3"
    python.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == */scripts/verify-carried-bundle.py ]]; then exit 0; fi\n"
        "if [[ $1 == */scripts/reload-macos-launchd-service.py ]]; then\n"
        "  for attempt in 1 2 3 4 5; do\n"
        "    \"$FAKE_HERMES_EXECUTABLE\" gateway stop || exit $?\n"
        "    \"$FAKE_HERMES_EXECUTABLE\" gateway start || exit $?\n"
        "    supervision=supervised; actual=match\n"
        "    if [[ -n ${FAKE_GATEWAY_SUPERVISION:-} && -f $FAKE_GATEWAY_SUPERVISION ]]; then IFS= read -r supervision < \"$FAKE_GATEWAY_SUPERVISION\"; fi\n"
        "    if [[ -n ${FAKE_LAUNCHD_ACTUAL:-} && -f $FAKE_LAUNCHD_ACTUAL ]]; then IFS= read -r actual < \"$FAKE_LAUNCHD_ACTUAL\"; fi\n"
        "    if [[ $supervision == supervised && $actual == match ]]; then exit 0; fi\n"
        "  done\n"
        "  echo 'gateway activation failed: launchd did not supervise the reviewed immutable release' >&2\n"
        "  exit 1\n"
        "fi\n"
        "if [[ $1 == */scripts/verify-macos-launchd-service.py ]]; then\n"
        "  if [[ -n \"${UNIFIED_KANBAN_TRANSACTION_DIR:-}${UNIFIED_KANBAN_TRANSACTION_FD:-}${UNIFIED_KANBAN_TRANSACTION_LEDGER_FD:-}\" || -e /dev/fd/9 || -e /dev/fd/10 ]]; then exit 88; fi\n"
        "  if [[ -z \"${FAKE_LAUNCHD_ACTUAL:-}\" ]]; then exit 0; fi\n"
        "  actual=''; IFS= read -r actual < \"$FAKE_LAUNCHD_ACTUAL\"\n"
        "  [[ \"$actual\" == match ]]\n"
        "  exit $?\n"
        "fi\n"
        "if [[ $1 == -c && $2 == *generate_launchd_plist* ]]; then\n"
        "  program=\"$(dirname \"$0\")/python\"\n"
        f"  {shlex.quote(sys.executable)} -c 'import plistlib,sys; sys.stdout.buffer.write(plistlib.dumps({{\"Label\":\"ai.hermes.gateway\",\"ProgramArguments\":[sys.argv[1],\"-m\",\"hermes_cli.main\",\"gateway\",\"run\",\"--replace\",\"--external-supervisor\"],\"EnvironmentVariables\":{{\"PATH\":\"/usr/bin:/bin\"}},\"RunAtLoad\":True}}))' \"$program\"\n"
        "  exit $?\n"
        "fi\n"
        "if [[ $1 == */scripts/hermes-release-manager.py && $2 == prepare ]]; then\n"
        "  release=\"$3.releases/release-$5\"\n"
        "  mkdir -p \"$release/venv/bin\"\n"
        "  chmod 0700 \"$3.releases\"\n"
        "  cp \"$0\" \"$release/venv/bin/python\"\n"
        "  chmod +x \"$release/venv/bin/python\"\n"
        "  cp \"$FAKE_HERMES_EXECUTABLE\" \"$release/venv/bin/hermes\"\n"
        "  chmod +x \"$release/venv/bin/hermes\"\n"
        # The faked build still has to leave what the producer leaves: an
        # installed host is only usable when the release carries the real
        # completion receipt the runtime gate validates.
        f"  PATH=/usr/bin:/bin {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(receipt_helper))} \"$1\" \"$3\" \"$4\" \"$5\" || exit 1\n"
        "  printf '%s\\n' \"$release\"\n"
        "  exit 0\n"
        "fi\n"
        "if [[ $1 == */scripts/path-transaction.py && $2 == begin && -n ${FAKE_PY_REMOVE_AFTER_BEGIN:-} ]]; then\n"
        f"  {shlex.quote(sys.executable)} \"$@\"\n"
        "  status=$?\n"
        "  if [[ $status == 0 ]]; then rm -f -- \"$FAKE_PY_REMOVE_AFTER_BEGIN\"; fi\n"
        "  exit $status\n"
        "fi\n"
        "if [[ $1 == */scripts/path-transaction.py && $2 == checkpoint && -n ${FAKE_PY_KILL_PARENT_AFTER_CHECKPOINT:-} ]]; then\n"
        f"  {shlex.quote(sys.executable)} \"$@\"\n"
        "  status=$?\n"
        "  if [[ $status == 0 ]]; then kill -9 \"$PPID\"; fi\n"
        "  exit $status\n"
        "fi\n"
        "if [[ $1 == */scripts/path-transaction.py && $2 == replace-file ]]; then\n"
        "  target=$5\n"
        "  if [[ -n ${FAKE_PY_SWAP_BEFORE_TARGET:-} && $target == \"$FAKE_PY_SWAP_BEFORE_TARGET\" ]]; then\n"
        "    printf '%s' \"${FAKE_PY_SWAP_CONTENT:-export CONCURRENT=yes\\n}\" > \"$target.concurrent\"\n"
        "    mv -f -- \"$target.concurrent\" \"$target\"\n"
        "  fi\n"
        "  if [[ -n ${FAKE_PY_SWAP_OTHER_ON_TARGET:-} && $target == \"$FAKE_PY_SWAP_OTHER_ON_TARGET\" ]]; then\n"
        "    other=$FAKE_PY_SWAP_OTHER_PATH\n"
        "    printf '%s' \"${FAKE_PY_SWAP_CONTENT:-export CONCURRENT=yes\\n}\" > \"$other.concurrent\"\n"
        "    mv -f -- \"$other.concurrent\" \"$other\"\n"
        "  fi\n"
        "  if [[ -n ${FAKE_PY_FAIL_REPLACE_TARGET:-} && $target == \"$FAKE_PY_FAIL_REPLACE_TARGET\" ]]; then exit 91; fi\n"
        f"  {shlex.quote(sys.executable)} \"$@\"\n"
        "  status=$?\n"
        "  if [[ $status == 0 && -n ${FAKE_PY_SWAP_AFTER_TARGET:-} && $target == \"$FAKE_PY_SWAP_AFTER_TARGET\" ]]; then\n"
        "    printf '%s' \"${FAKE_PY_SWAP_CONTENT:-export CONCURRENT=yes\\n}\" > \"$target.concurrent\"\n"
        "    mv -f -- \"$target.concurrent\" \"$target\"\n"
        "  fi\n"
        "  if [[ $status == 0 && -n ${FAKE_PY_KILL_PARENT_AFTER_TARGET:-} && $target == \"$FAKE_PY_KILL_PARENT_AFTER_TARGET\" ]]; then\n"
        "    kill -9 \"$PPID\"\n"
        "  fi\n"
        "  exit $status\n"
        "fi\n"
        "if [[ $1 == */scripts/path-transaction.py && $2 == remove-file && -n ${FAKE_PY_FAIL_REMOVE_TARGET:-} && $4 == \"$FAKE_PY_FAIL_REMOVE_TARGET\" ]]; then exit 92; fi\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_HERMES_LOG": str(log),
        "FAKE_HERMES_EXECUTABLE": str(fake_bin / "hermes"),
        "FAKE_BOARDS_JSON": json.dumps([{"slug": x} for x in (boards or [])]),
        "HERMES_AGENT_REPO": str(agent_repo),
        "HERMES_CARRIED_COMMITS_FILE": str(carried),
        "_TEST_SETUP": str(fixture_root / "scripts/setup.sh"),
        "_TEST_ROOT": str(fixture_root),
    }, log


def test_setup_dry_run_does_not_write(tmp_path: Path) -> None:
    result = run_script(SETUP, tmp_path, "--dry-run", "--no-restart", "--skip-smoke")
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_dry_run_with_default_restart_does_not_require_release(
    tmp_path: Path,
) -> None:
    result = run_script(SETUP, tmp_path, "--dry-run", "--skip-smoke")

    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_default_invocation_accepts_empty_original_args(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["unified-kanban-smoke"])
    result = run_script(SETUP, tmp_path, env_extra=env)

    assert result.returncode == 0, result.stderr


def test_setup_rejects_abbreviated_carried_manifest_before_writing(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path)
    carried = Path(env["HERMES_CARRIED_COMMITS_FILE"])
    carried.write_text("deadbeef\n", encoding="utf-8")
    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert result.returncode != 0
    assert "invalid carried commit manifest" in result.stderr
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_dry_run_simulates_legacy_plugin_migration(tmp_path: Path) -> None:
    legacy = tmp_path / "hermes-kanban/plugin/hermes-kanban"
    legacy.mkdir(parents=True)
    plugin_link = tmp_path / ".hermes/plugins/hermes-kanban"
    plugin_link.parent.mkdir(parents=True)
    plugin_link.symlink_to(legacy)
    env, _ = fake_env(tmp_path)


    result = run_script(
        SETUP,
        tmp_path,
        "--dry-run",
        "--no-restart",
        "--skip-smoke",
        env_extra=env,
    )

    assert result.returncode == 0, result.stderr
    assert "Migrating legacy hermes-kanban plugin symlink" in result.stdout
    assert plugin_link.is_symlink()
    assert plugin_link.resolve() == legacy


def test_setup_environment_cannot_authorize_foreign_plugin_migration(
    tmp_path: Path,
) -> None:
    foreign = tmp_path / "foreign/hermes-kanban"
    foreign.mkdir(parents=True)
    plugin_link = tmp_path / ".hermes/plugins/hermes-kanban"
    plugin_link.parent.mkdir(parents=True)
    plugin_link.symlink_to(foreign)
    env, _ = fake_env(tmp_path)
    env["UNIFIED_KANBAN_LEGACY_HERMES_PLUGIN_SOURCE"] = str(foreign)

    result = run_script(SETUP, tmp_path, "--skip-smoke", env_extra=env)

    assert result.returncode != 0
    assert "Refusing foreign Hermes plugin symlink" in result.stderr
    assert plugin_link.readlink() == foreign


def test_setup_is_idempotent_and_uses_repo_symlinks(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path)
    first = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env,
    )
    hermes_launcher = tmp_path / ".local/bin/hermes"
    selector = Path(env["HERMES_AGENT_REPO"] + ".releases/current")
    first_launcher_identity = (hermes_launcher.stat().st_dev, hermes_launcher.stat().st_ino)
    first_selector_identity = (selector.stat().st_dev, selector.stat().st_ino)
    second = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env,
    )
    assert first.returncode == second.returncode == 0
    assert (hermes_launcher.stat().st_dev, hermes_launcher.stat().st_ino) == first_launcher_identity
    assert (selector.stat().st_dev, selector.stat().st_ino) == first_selector_identity
    selected_release = Path(selector.read_text(encoding="utf-8").strip())
    assert selected_release == Path(env["HERMES_AGENT_REPO"] + ".releases") / (
        "release-" + CARRIED_HEAD
    )
    managed_hermes = subprocess.run(
        [str(hermes_launcher), "version"],
        env={**os.environ, **env, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert managed_hermes.returncode == 0, managed_hermes.stderr
    assert "Hermes Agent" in managed_hermes.stdout
    adapter_link = tmp_path / ".local/bin/kanban-adapter"
    assert adapter_link.is_symlink()
    assert adapter_link.resolve() == Path(env["_TEST_ROOT"]) / "bin/kanban-adapter"
    linked_cli = subprocess.run(
        ["bash", str(adapter_link), "--help"],
        env={**os.environ, **env, "HOME": str(tmp_path)},
        text=True, capture_output=True, check=False,
    )
    assert linked_cli.returncode == 0, linked_cli.stderr
    viewer_link = tmp_path / ".local/bin/ai-session-viewer"
    assert viewer_link.is_symlink()
    assert viewer_link.resolve() == Path(env["_TEST_ROOT"]) / "bin/ai-session-viewer"
    linked_viewer = subprocess.run(
        ["bash", str(viewer_link), "--help"],
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert linked_viewer.returncode == 0, linked_viewer.stderr
    assert "AI Session Viewer" in linked_viewer.stdout
    hook_link = tmp_path / ".local/bin/claude-kanban-hook"
    linked_hook = subprocess.run(
        ["bash", str(hook_link)],
        env={**os.environ, **env, "HOME": str(tmp_path)},
        text=True, capture_output=True, check=False,
    )
    assert linked_hook.returncode == 2
    assert "usage: claude-kanban-hook" in linked_hook.stderr
    codex_hook_link = tmp_path / ".local/bin/codex-kanban-hook"
    linked_codex_hook = subprocess.run(
        ["bash", str(codex_hook_link)],
        env={**os.environ, **env, "HOME": str(tmp_path)},
        text=True, capture_output=True, check=False,
    )
    assert linked_codex_hook.returncode == 2
    assert "usage: codex-kanban-hook" in linked_codex_hook.stderr
    assert "Hermes Dashboard" in first.stdout
    assert "Project directory" in first.stdout


def test_setup_link_failure_does_not_install_broken_hook_settings(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path)
    bin_dir = tmp_path / ".local/bin"
    bin_dir.mkdir(parents=True)
    bin_dir.chmod(0o500)
    try:
        result = run_script(
            SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
        )
    finally:
        bin_dir.chmod(0o700)

    assert result.returncode != 0
    for settings in (tmp_path / ".claude/settings.json", tmp_path / ".codex/hooks.json"):
        assert not settings.exists() or "unified-kanban-managed-v1" not in settings.read_text(
            encoding="utf-8"
        )


@pytest.mark.parametrize("suffix", ["/", "//", "/./", "/."])
def test_setup_keeps_the_release_root_beside_a_denormalized_checkout(
    tmp_path: Path, suffix: str
) -> None:
    """``$REPO.releases`` in shell and ``with_name`` in Python must agree.

    A trailing separator makes plain concatenation name a hidden directory
    *inside* the Hermes checkout while every Python caller keeps using the
    sibling, so setup would install a launcher that reads a selector setup
    never wrote.
    """
    env, _log = fake_env(tmp_path)
    checkout = Path(env["HERMES_AGENT_REPO"])
    env["HERMES_AGENT_REPO"] = f"{checkout}{suffix}"

    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)

    assert result.returncode == 0, result.stderr
    selector = checkout.with_name(f"{checkout.name}.releases") / "current"
    assert selector.is_file()
    assert not (checkout / ".releases").exists()
    assert sorted(path.name for path in checkout.iterdir()) == []
    launcher = tmp_path / ".local/bin/hermes"
    assert shlex.quote(str(selector)) in launcher.read_text(encoding="utf-8")


def test_setup_and_uninstall_round_trip_a_denormalized_checkout(tmp_path: Path) -> None:
    env, _log = fake_env(tmp_path)
    checkout = Path(env["HERMES_AGENT_REPO"])
    env["HERMES_AGENT_REPO"] = f"{checkout}/"
    installed = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)
    assert installed.returncode == 0, installed.stderr
    selector = checkout.with_name(f"{checkout.name}.releases") / "current"
    assert selector.is_file()

    removed = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert removed.returncode == 0, removed.stderr
    assert not selector.exists()
    assert not (tmp_path / ".local/bin/hermes").exists()
    assert not (checkout / ".releases").exists()


def test_setup_rejects_traversal_in_the_hermes_checkout_before_writes(
    tmp_path: Path,
) -> None:
    env, _log = fake_env(tmp_path)
    env["HERMES_AGENT_REPO"] = env["HERMES_AGENT_REPO"] + "/.."

    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)

    assert result.returncode != 0
    assert "HERMES_AGENT_REPO" in result.stderr
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert not (tmp_path / ".local/bin/hermes").exists()
    # The refusal lands before any release directory is constructed.
    assert not any(path.name.endswith(".releases") for path in tmp_path.iterdir())


def test_setup_rejects_non_absolute_custom_hermes_checkout_before_writes(
    tmp_path: Path,
) -> None:
    env, _ = fake_env(tmp_path)
    env["HERMES_AGENT_REPO"] = "~/custom/hermes-agent"

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert result.returncode != 0
    assert "must be an absolute path" in result.stderr
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


@pytest.mark.parametrize("restart_args", [(), ("--no-restart",)])
def test_setup_warns_that_running_hermes_clients_keep_stale_plugin_code(
    tmp_path: Path, restart_args: tuple[str, ...],
) -> None:
    result = run_script(SETUP, tmp_path, *restart_args, "--skip-smoke")

    assert result.returncode == 0, result.stderr
    assert "running Hermes CLI, TUI, and Desktop chat processes" in result.stderr
    assert "version already loaded in memory" in result.stderr
    assert "must be quit and relaunched" in result.stderr
    assert "before testing a new prompt" in result.stderr
    assert "restarting the gateway does not reload those clients" in result.stderr


def test_setup_prints_stale_client_warning_after_gateway_install(tmp_path: Path) -> None:
    env_extra, _ = fake_env(tmp_path)
    env = os.environ.copy()
    env.update(env_extra)
    env["HOME"] = str(tmp_path)
    env["XDG_STATE_HOME"] = str(tmp_path / ".local/state")
    env["XDG_CACHE_HOME"] = str(tmp_path / ".cache")

    result = subprocess.run(
        ["bash", str(SETUP), "--skip-smoke"],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode == 0, result.stdout
    assert result.stdout.index("installed") < result.stdout.index("WARNING: running Hermes")


def test_setup_recovers_unmanaged_gateway_fallback_to_launchd(
    tmp_path: Path,
) -> None:
    env, log = fake_env(tmp_path)
    supervision = tmp_path / "gateway-supervision"
    env.update(
        {
            "FAKE_GATEWAY_SUPERVISION": str(supervision),
            "FAKE_INSTALL_UNMANAGED_ONCE": str(tmp_path / "install-fell-back"),
        }
    )

    result = run_script(SETUP, tmp_path, "--skip-smoke", env_extra=env)

    assert result.returncode == 0, result.stderr
    assert supervision.read_text(encoding="utf-8") == "supervised\n"
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.count("gateway install --force --start-now --start-on-login") == 0
    assert calls.count("gateway status") == 1
    assert calls.count("gateway stop") == 2
    assert calls.count("gateway start") == 2


def test_setup_rejects_stale_loaded_launchd_program_despite_supervised_status(
    tmp_path: Path,
) -> None:
    env, log = fake_env(tmp_path)
    loaded = tmp_path / "loaded-launchd-program"
    env.update(
        {
            "FAKE_LAUNCHD_ACTUAL": str(loaded),
            "FAKE_INSTALL_STALE_LAUNCHD_ONCE": str(tmp_path / "stale-loaded-once"),
        }
    )

    result = run_script(SETUP, tmp_path, "--skip-smoke", env_extra=env)

    assert result.returncode == 0, result.stderr
    assert loaded.read_text(encoding="utf-8") == "match\n"
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.count("gateway stop") == 2
    assert calls.count("gateway start") == 2


def test_setup_refuses_persistently_unsupervised_gateway_and_rolls_back(
    tmp_path: Path,
) -> None:
    env, log = fake_env(tmp_path)
    supervision = tmp_path / "gateway-supervision"
    env.update(
        {
            "FAKE_GATEWAY_SUPERVISION": str(supervision),
            "FAKE_INSTALL_UNMANAGED_ONCE": str(tmp_path / "install-fell-back"),
            "FAKE_GATEWAY_NEVER_SUPERVISED": "1",
        }
    )

    result = run_script(SETUP, tmp_path, "--skip-smoke", env_extra=env)

    assert result.returncode != 0
    assert "launchd did not supervise" in result.stderr
    assert not (tmp_path / ".local/bin/hermes").exists()
    assert not (tmp_path / ".hermes/hermes-agent.releases/current").exists()
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.count("gateway status") == 0
    assert calls.count("gateway stop") == 5
    assert calls.count("gateway start") == 5
    assert calls.count("gateway uninstall") == 1


def test_setup_quotes_hook_commands_for_shell_paths(tmp_path: Path) -> None:
    home = tmp_path / "home with space;touch SHOULD_NOT_EXIST"
    result = run_script(SETUP, home, "--no-restart", "--skip-smoke")
    assert result.returncode == 0, result.stderr
    settings = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
    command = next(
        hook["command"]
        for group in settings["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
        if "unified-kanban-managed-v1" in hook.get("command", "")
    )

    executed = subprocess.run(
        command,
        shell=True,
        input="{}",
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache")},
        check=False,
    )

    assert executed.returncode == 0, executed.stderr
    assert not (REPO / "SHOULD_NOT_EXIST").exists()


def test_setup_merges_claude_hooks_and_uninstall_preserves_existing_hooks(
    tmp_path: Path,
) -> None:
    env, _ = fake_env(tmp_path)
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    existing = {
        "model": "opus",
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [
                    {"type": "command", "command": "/keep/existing-hook"},
                    {
                        "type": "command",
                        "command": f"{tmp_path}/.local/bin/claude-kanban-hook prompt",
                        "timeout": 30,
                    },
                ],
            }],
        },
    }
    settings.write_text(json.dumps(existing), encoding="utf-8")

    first = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)
    second = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)
    assert first.returncode == second.returncode == 0, second.stderr

    hook_link = tmp_path / ".local/bin/claude-kanban-hook"
    assert hook_link.is_symlink()
    assert hook_link.resolve() == tmp_path / "unified-kanban/bin/claude-kanban-hook"
    installed = json.loads(settings.read_text(encoding="utf-8"))
    assert installed["model"] == "opus"
    commands = {
        event: [
            hook["command"]
            for group in installed["hooks"][event]
            for hook in group.get("hooks", [])
        ]
        for event in ("UserPromptSubmit", "Stop", "SessionEnd")
    }
    prefix = shlex.quote(str(hook_link))
    marker = "# unified-kanban-managed-v1"
    assert commands["UserPromptSubmit"].count(f"{prefix} prompt {marker}") == 1
    assert commands["Stop"].count(f"{prefix} stop {marker}") == 1
    assert commands["SessionEnd"].count(f"{prefix} session-end {marker}") == 1
    assert "/keep/existing-hook" in commands["UserPromptSubmit"]

    removed = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)
    assert removed.returncode == 0, removed.stderr
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["model"] == "opus"
    assert after["hooks"]["UserPromptSubmit"] == existing["hooks"]["UserPromptSubmit"]
    assert "Stop" not in after["hooks"]
    assert "SessionEnd" not in after["hooks"]
    assert not hook_link.exists()
    assert not (tmp_path / ".local/bin/ai-session-viewer").exists()


def test_setup_merges_codex_hooks_and_uninstall_preserves_existing_hooks(
    tmp_path: Path,
) -> None:
    env, _ = fake_env(tmp_path)
    settings = tmp_path / ".codex/hooks.json"
    settings.parent.mkdir(parents=True)
    existing = {
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [{"type": "command", "command": "/keep/codex-hook"}],
            }],
        },
    }
    settings.write_text(json.dumps(existing), encoding="utf-8")

    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env).returncode == 0
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env).returncode == 0

    hook_link = tmp_path / ".local/bin/codex-kanban-hook"
    installed = json.loads(settings.read_text(encoding="utf-8"))
    commands = {
        event: [
            hook["command"]
            for group in installed["hooks"][event]
            for hook in group.get("hooks", [])
        ]
        for event in ("UserPromptSubmit", "Stop")
    }
    prefix = shlex.quote(str(hook_link))
    marker = "# unified-kanban-managed-v1"
    assert commands["UserPromptSubmit"].count(f"{prefix} prompt {marker}") == 1
    assert commands["Stop"].count(f"{prefix} stop {marker}") == 1
    assert "/keep/codex-hook" in commands["UserPromptSubmit"]

    removed = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)
    assert removed.returncode == 0, removed.stderr
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["hooks"]["UserPromptSubmit"] == existing["hooks"]["UserPromptSubmit"]
    assert "Stop" not in after["hooks"]
    assert not hook_link.exists()


def test_setup_preflights_malformed_codex_before_installing_anything(
    tmp_path: Path,
) -> None:
    claude = tmp_path / ".claude/settings.json"
    codex = tmp_path / ".codex/hooks.json"
    claude.parent.mkdir(parents=True)
    codex.parent.mkdir(parents=True)
    original = {"model": "keep", "hooks": {}}
    claude.write_text(json.dumps(original), encoding="utf-8")
    codex.write_text('{"hooks":{"Stop":"not-a-list"}}', encoding="utf-8")

    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke")

    assert result.returncode != 0
    assert json.loads(claude.read_text(encoding="utf-8")) == original
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert not (tmp_path / ".local/state/unified-kanban").exists()


def test_setup_refuses_foreign_symlink(tmp_path: Path) -> None:
    target = tmp_path / ".local/bin/kanban-adapter"
    target.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.write_text("keep", encoding="utf-8")
    target.symlink_to(foreign)
    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke")
    assert result.returncode != 0
    assert target.resolve() == foreign


def test_setup_refuses_foreign_session_viewer_symlink(tmp_path: Path) -> None:
    target = tmp_path / ".local/bin/ai-session-viewer"
    target.parent.mkdir(parents=True, exist_ok=True)
    foreign = tmp_path / "foreign-viewer"
    foreign.write_text("keep", encoding="utf-8")
    target.symlink_to(foreign)

    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke")

    assert result.returncode != 0
    assert target.is_symlink()
    assert target.resolve() == foreign
    assert foreign.read_text(encoding="utf-8") == "keep"


def test_setup_rejects_unsafe_board_slug(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "safe;$(touch PWNED)",
    )
    assert result.returncode == 2
    assert not (project / ".envrc").exists()


@pytest.mark.parametrize("option", ["--project-dir", "--board"])
def test_setup_rejects_missing_option_value_without_installing(
    tmp_path: Path, option: str
) -> None:
    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", option)
    assert result.returncode == 2
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_refuses_project_envrc_symlink_without_changing_target(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    shared = tmp_path / "shared.envrc"
    shared.write_text("export KEEP_ME=yes\n", encoding="utf-8")
    envrc = project / ".envrc"
    envrc.symlink_to(shared)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert envrc.is_symlink()
    assert envrc.resolve() == shared
    assert shared.read_text(encoding="utf-8") == "export KEEP_ME=yes\n"


def test_setup_rejects_incompatible_hermes_before_writing(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, compatible=False)
    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)
    assert result.returncode != 0
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_rejects_unsupported_python_before_managed_writes(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path)
    python = tmp_path / "fake-bin/python3"
    python.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    python.chmod(0o755)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert result.returncode != 0
    assert "Python 3.11 or newer is required" in result.stderr
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_uninstall_rejects_unsupported_python_without_removing_install(
    tmp_path: Path,
) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    adapter = tmp_path / ".local/bin/kanban-adapter"
    assert adapter.is_symlink()
    python = tmp_path / "fake-bin/python3"
    python.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    python.chmod(0o755)

    result = run_script(UNINSTALL, tmp_path, "--no-restart")

    assert result.returncode != 0
    assert "Python 3.11 or newer is required" in result.stderr
    assert adapter.is_symlink()


def test_setup_uses_frozen_snapshot_after_official_main_advances(
    tmp_path: Path,
) -> None:
    env, _ = fake_env(tmp_path)
    env["FAKE_OFFICIAL_UPSTREAM"] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_setup_ignores_environment_pin_override(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path)
    expected = tmp_path / "invalid-hermes-upstream"
    expected.write_text("not-a-commit\n", encoding="utf-8")
    env["HERMES_EXPECTED_UPSTREAM_FILE"] = str(expected)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


@pytest.mark.parametrize(
    "missing_option",
    [
        "--board", "--assignee", "--tenant", "--created-by",
        "--initial-status", "--observation", "--idempotency-key", "--json",
        "--title-file", "--name", "--author",
        "--summary", "--kind",
    ],
)
def test_setup_rejects_each_missing_hermes_option(
    tmp_path: Path, missing_option: str
) -> None:
    env, _ = fake_env(tmp_path, missing_option=missing_option)
    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)
    assert result.returncode != 0
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


@pytest.mark.parametrize(
    ("required", "lookalike"),
    [
        ("--board", "--board-id"),
        ("--json", "--json-output"),
        ("--summary", "--summary-file"),
        ("--name", "--name-prefix"),
    ],
)
def test_setup_rejects_lookalike_hermes_options(
    tmp_path: Path, required: str, lookalike: str
) -> None:
    env, _ = fake_env(tmp_path, option_replacements={required: lookalike})
    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)
    assert result.returncode != 0
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


@pytest.mark.parametrize(
    "invalid_boards",
    [
        {"slug": "shop-bridge"},
        [1],
        [{"slug": 1}],
        [{"name": "missing-slug"}],
    ],
)
def test_setup_fails_closed_on_invalid_board_json_schema(
    tmp_path: Path, invalid_boards: object
) -> None:
    env, log = fake_env(tmp_path)
    env["FAKE_BOARDS_JSON"] = json.dumps(invalid_boards)
    project = tmp_path / "project"
    project.mkdir()

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    log_lines = log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("kanban boards create shop-bridge ") for line in log_lines)
    assert not (project / ".envrc").exists()


def test_setup_missing_board_fails_before_any_host_write(tmp_path: Path) -> None:
    env, log = fake_env(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    result = run_script(
        SETUP,
        tmp_path,
        "--no-restart",
        "--skip-smoke",
        "--project-dir",
        str(project),
        "--board",
        "missing-board",
        env_extra=env,
    )

    assert result.returncode != 0
    assert "Hermes board does not exist: missing-board" in result.stderr
    assert not (project / ".envrc").exists()
    assert not (tmp_path / ".local").exists()
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".hermes").exists()
    calls = log.read_text(encoding="utf-8").splitlines()
    assert "kanban boards create --name missing-board missing-board" not in calls


def test_setup_missing_smoke_board_fails_before_any_host_write(tmp_path: Path) -> None:
    env, log = fake_env(tmp_path, boards=[])

    result = run_script(SETUP, tmp_path, "--no-restart", env_extra=env)

    assert result.returncode != 0
    assert "Hermes smoke board does not exist: unified-kanban-smoke" in result.stderr
    assert not (tmp_path / ".local").exists()
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".hermes").exists()
    calls = log.read_text(encoding="utf-8").splitlines()
    assert "kanban boards create --name Unified Kanban Smoke unified-kanban-smoke" not in calls


def test_project_routing_requires_board_and_uninstall_removes_managed_line(tmp_path: Path) -> None:
    env, log = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text("export KEEP_ME=yes\n", encoding="utf-8")
    args = (
        "--no-restart", "--skip-smoke", "--project-dir", str(project),
        "--board", "shop-bridge",
    )
    assert run_script(SETUP, tmp_path, *args, env_extra=env).returncode == 0
    assert run_script(SETUP, tmp_path, *args, env_extra=env).returncode == 0
    lines = envrc.read_text(encoding="utf-8").splitlines()
    assert "export KEEP_ME=yes" in lines
    assert lines.count("export HERMES_KANBAN_BOARD=shop-bridge # unified-kanban") == 1
    assert "kanban boards create --name shop-bridge shop-bridge" not in log.read_text(encoding="utf-8")

    result = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)
    assert result.returncode == 0, result.stderr
    assert envrc.read_text(encoding="utf-8") == "export KEEP_ME=yes\n"


def test_project_routing_collapses_physical_path_aliases_before_uninstall(
    tmp_path: Path,
) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    alias = f"{project.parent}//{project.name}"
    first = run_script(
        SETUP,
        tmp_path,
        "--no-restart",
        "--skip-smoke",
        "--project-dir",
        alias,
        "--board",
        "shop-bridge",
        env_extra=env,
    )
    second = run_script(
        SETUP,
        tmp_path,
        "--no-restart",
        "--skip-smoke",
        "--project-dir",
        str(project),
        "--board",
        "shop-bridge",
        env_extra=env,
    )

    assert first.returncode == second.returncode == 0
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    assert json.loads(state.read_text(encoding="utf-8")) == [str(project.resolve())]
    uninstall = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)
    assert uninstall.returncode == 0, uninstall.stderr


def test_setup_rerun_through_repo_symlink_uses_physical_managed_sources(
    tmp_path: Path,
) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    first = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )
    assert first.returncode == 0, first.stderr
    fixture_root = tmp_path / "unified-kanban"
    alias = tmp_path / "repo-alias"
    alias.symlink_to(fixture_root, target_is_directory=True)
    process_env = {
        key: value for key, value in {**os.environ, **env}.items()
        if not key.startswith("_TEST_")
    }
    process_env["HOME"] = str(tmp_path)
    process_env["XDG_STATE_HOME"] = str(tmp_path / ".local/state")
    process_env["XDG_CACHE_HOME"] = str(tmp_path / ".cache")

    second = subprocess.run(
        ["bash", str(alias / "scripts/setup.sh"), "--no-restart", "--skip-smoke"],
        cwd=alias,
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    plugin = tmp_path / ".hermes/plugins/hermes-kanban"
    assert plugin.resolve() == (
        fixture_root / "integrations/hermes/hermes-kanban"
    ).resolve()

    uninstall = subprocess.run(
        ["bash", str(alias / "scripts/uninstall.sh"), "--no-restart"],
        cwd=alias,
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert uninstall.returncode == 0, uninstall.stderr
    assert not plugin.exists() and not plugin.is_symlink()


def test_routing_preflight_does_not_follow_replaced_project_symlink(
    tmp_path: Path,
) -> None:
    original = tmp_path / "managed-project"
    original.mkdir()
    persisted = str(original.resolve())
    original.rmdir()
    foreign = tmp_path / "foreign-project"
    foreign.mkdir()
    (foreign / ".envrc").write_text(
        "export FOREIGN=yes # unified-kanban\n", encoding="utf-8"
    )
    original.symlink_to(foreign, target_is_directory=True)
    state = tmp_path / "managed-projects.json"
    state.write_text(json.dumps([persisted]), encoding="utf-8")
    metadata = tmp_path / "preflight.json"
    paths = tmp_path / "paths.bin"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/render-project-routing.py"),
            "preflight",
            str(state),
            str(metadata),
            str(paths),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(metadata.read_text(encoding="utf-8"))["projects"] == [persisted]
    assert paths.read_bytes() == b""


def test_project_registration_does_not_adopt_replaced_project_symlink(
    tmp_path: Path,
) -> None:
    env, _ = fake_env(tmp_path, boards=["first-board", "second-board"])
    original = tmp_path / "first-project"
    original.mkdir()
    first = run_script(
        SETUP,
        tmp_path,
        "--no-restart",
        "--skip-smoke",
        "--project-dir",
        str(original),
        "--board",
        "first-board",
        env_extra=env,
    )
    assert first.returncode == 0, first.stderr
    persisted = str(original.resolve())
    moved = tmp_path / "moved-first-project"
    original.rename(moved)
    foreign = tmp_path / "foreign-project"
    foreign.mkdir()
    foreign_envrc = foreign / ".envrc"
    foreign_bytes = b"export FOREIGN=yes # unified-kanban\n"
    foreign_envrc.write_bytes(foreign_bytes)
    foreign_identity = (foreign_envrc.stat().st_dev, foreign_envrc.stat().st_ino)
    original.symlink_to(foreign, target_is_directory=True)
    second_project = tmp_path / "second-project"
    second_project.mkdir()

    second = run_script(
        SETUP,
        tmp_path,
        "--no-restart",
        "--skip-smoke",
        "--project-dir",
        str(second_project),
        "--board",
        "second-board",
        env_extra=env,
    )

    assert second.returncode == 0, second.stderr
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    assert json.loads(state.read_text(encoding="utf-8")) == [
        persisted,
        str(second_project.resolve()),
    ]
    uninstall = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)
    assert uninstall.returncode == 0, uninstall.stderr
    assert foreign_envrc.read_bytes() == foreign_bytes
    assert (foreign_envrc.stat().st_dev, foreign_envrc.stat().st_ino) == foreign_identity


def test_project_routing_rerun_preserves_noop_file_identities(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    args = (
        "--no-restart",
        "--skip-smoke",
        "--project-dir",
        str(project),
        "--board",
        "shop-bridge",
    )

    first = run_script(SETUP, tmp_path, *args, env_extra=env)
    assert first.returncode == 0, first.stderr
    envrc = project / ".envrc"
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    before = {
        path: (os.lstat(path).st_dev, os.lstat(path).st_ino)
        for path in (envrc, state)
    }

    second = run_script(SETUP, tmp_path, *args, env_extra=env)

    assert second.returncode == 0, second.stderr
    assert {
        path: (os.lstat(path).st_dev, os.lstat(path).st_ino)
        for path in (envrc, state)
    } == before


def test_corrupt_state_does_not_modify_envrc(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text("export KEEP_ME=yes\n", encoding="utf-8")
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True)
    state.write_text("not-json", encoding="utf-8")

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert envrc.read_text(encoding="utf-8") == "export KEEP_ME=yes\n"


def test_corrupt_state_fails_before_board_or_cache_side_effects(tmp_path: Path) -> None:
    env, log = fake_env(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True)
    state.write_text("not-json", encoding="utf-8")

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    log_lines = log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("kanban boards create shop-bridge ") for line in log_lines)
    assert not (tmp_path / ".cache/unified-kanban").exists()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


@pytest.mark.parametrize("invalid_state", [{}, [1], ["relative"], ["/tmp/x", "/tmp/x"]])
def test_setup_rejects_invalid_state_schema_without_modifying_envrc(
    tmp_path: Path, invalid_state: object
) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text("export KEEP_ME=yes\n", encoding="utf-8")
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps(invalid_state), encoding="utf-8")

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert envrc.read_text(encoding="utf-8") == "export KEEP_ME=yes\n"


def test_setup_refuses_managed_state_symlink(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign-state.json"
    foreign.write_text("[]\n", encoding="utf-8")
    state.symlink_to(foreign)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert state.is_symlink()
    assert foreign.read_text(encoding="utf-8") == "[]\n"
    assert not (project / ".envrc").exists()


@pytest.mark.parametrize("target_kind", ["state", "envrc"])
@pytest.mark.parametrize("object_kind", ["directory", "fifo"])
def test_setup_rejects_non_regular_object_before_board_creation(
    tmp_path: Path, target_kind: str, object_kind: str
) -> None:
    env, log = fake_env(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    if target_kind == "state":
        target = tmp_path / ".local/state/unified-kanban/managed-projects.json"
        target.parent.mkdir(parents=True)
    else:
        target = project / ".envrc"
    if object_kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    log_lines = log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("kanban boards create shop-bridge ") for line in log_lines)
    assert target.exists()
    assert not (tmp_path / ".cache/unified-kanban").exists()


def test_setup_rejects_envrc_symlink_swap_during_board_lookup(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text("export KEEP_ME=yes\n", encoding="utf-8")
    external = tmp_path / "external.envrc"
    external.write_text("export EXTERNAL=yes\n", encoding="utf-8")
    env["FAKE_SWAP_ENVRC"] = str(envrc)
    env["FAKE_SWAP_TARGET"] = str(external)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert envrc.is_symlink()
    assert envrc.resolve() == external
    assert external.read_text(encoding="utf-8") == "export EXTERNAL=yes\n"
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    assert not state.exists()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_rolls_back_state_when_envrc_replace_fails(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc_original = b"export KEEP_ME=yes\n"
    envrc.write_bytes(envrc_original)
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True)
    state_original = b"[]\n"
    state.write_bytes(state_original)
    env["FAKE_PY_FAIL_REPLACE_TARGET"] = str(envrc)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert state.read_bytes() == state_original
    assert envrc.read_bytes() == envrc_original
    assert sorted(path.name for path in project.iterdir()) == [".envrc"]
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_uninstall_does_not_replace_envrc_changed_to_symlink(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    args = (
        "--no-restart", "--skip-smoke", "--project-dir", str(project),
        "--board", "shop-bridge",
    )
    assert run_script(SETUP, tmp_path, *args, env_extra=env).returncode == 0
    envrc = project / ".envrc"
    envrc.unlink()
    shared = tmp_path / "shared.envrc"
    shared.write_text("export USER_MANAGED=yes\n", encoding="utf-8")
    envrc.symlink_to(shared)

    result = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert result.returncode == 0, result.stderr
    assert envrc.is_symlink()
    assert envrc.resolve() == shared
    assert shared.read_text(encoding="utf-8") == "export USER_MANAGED=yes\n"


def test_uninstall_validates_state_before_removing_adapter_link(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    adapter = tmp_path / ".local/bin/kanban-adapter"
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("not-json", encoding="utf-8")

    result = run_script(UNINSTALL, tmp_path, "--no-restart")

    assert result.returncode != 0
    assert adapter.is_symlink()


@pytest.mark.parametrize(
    "invalid_state",
    [
        {},
        {"project": "/tmp/project"},
        [1],
        ["relative/project"],
        ["/absolute/project", "/absolute/project"],
    ],
)
def test_uninstall_rejects_invalid_state_schema_before_changes(
    tmp_path: Path, invalid_state: object
) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    adapter = tmp_path / ".local/bin/kanban-adapter"
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(invalid_state), encoding="utf-8")

    result = run_script(UNINSTALL, tmp_path, "--no-restart")

    assert result.returncode != 0
    assert adapter.is_symlink()
    assert state.exists()


@pytest.mark.parametrize("state_kind", ["directory", "fifo"])
def test_uninstall_rejects_non_regular_state_object_before_changes(
    tmp_path: Path, state_kind: str
) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    adapter = tmp_path / ".local/bin/kanban-adapter"
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    if state_kind == "directory":
        state.mkdir()
    else:
        os.mkfifo(state)

    result = run_script(UNINSTALL, tmp_path, "--no-restart")

    assert result.returncode != 0
    assert adapter.is_symlink()
    assert state.exists()


def test_uninstall_preflights_all_envrc_files_before_any_change(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_envrc = first / ".envrc"
    second_envrc = second / ".envrc"
    managed = "export HERMES_KANBAN_BOARD=board # unified-kanban\n"
    first_envrc.write_text("export KEEP=yes\n" + managed, encoding="utf-8")
    second_envrc.write_bytes(b"\xff\xfe" + managed.encode())
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.write_text(json.dumps([str(first), str(second)]), encoding="utf-8")
    first_before = first_envrc.read_bytes()

    result = run_script(UNINSTALL, tmp_path, "--no-restart")

    assert result.returncode != 0
    assert first_envrc.read_bytes() == first_before
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()
    assert state.exists()


def test_uninstall_rolls_back_when_second_replace_fails(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    projects = [tmp_path / "first", tmp_path / "second"]
    managed = "export HERMES_KANBAN_BOARD=board # unified-kanban\n"
    originals: dict[Path, bytes] = {}
    for index, project in enumerate(projects):
        project.mkdir()
        envrc = project / ".envrc"
        envrc.write_text(f"export KEEP_{index}=yes\n" + managed, encoding="utf-8")
        originals[envrc] = envrc.read_bytes()
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.write_text(json.dumps([str(project) for project in projects]), encoding="utf-8")

    result = run_script(
        UNINSTALL,
        tmp_path,
        "--no-restart",
        env_extra={"FAKE_PY_FAIL_REPLACE_TARGET": str(projects[1] / ".envrc")},
    )

    assert result.returncode != 0
    for envrc, original in originals.items():
        assert envrc.read_bytes() == original
        assert sorted(path.name for path in envrc.parent.iterdir()) == [".envrc"]
    assert state.exists()
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_fails_if_frozen_state_disappears_after_begin(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc_original = b"export KEEP=yes\nexport HERMES_KANBAN_BOARD=board # unified-kanban\n"
    envrc.write_bytes(envrc_original)
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.write_text(json.dumps([str(project)]), encoding="utf-8")

    result = run_script(
        UNINSTALL,
        tmp_path,
        "--no-restart",
        env_extra={"FAKE_PY_REMOVE_AFTER_BEGIN": str(state)},
    )

    assert result.returncode != 0
    assert envrc.read_bytes() == envrc_original
    assert not state.exists()
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_preserves_envrc_replaced_during_temp_preparation(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text(
        "export ORIGINAL=yes\nexport HERMES_KANBAN_BOARD=board # unified-kanban\n",
        encoding="utf-8",
    )
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state_original = json.dumps([str(project)]).encode()
    state.write_bytes(state_original)
    result = run_script(
        UNINSTALL, tmp_path, "--no-restart",
        env_extra={
            "FAKE_PY_SWAP_BEFORE_TARGET": str(envrc),
            "FAKE_PY_SWAP_CONTENT": "export CONCURRENT=yes\n",
        },
    )

    assert result.returncode != 0
    assert envrc.read_text(encoding="utf-8") == "export CONCURRENT=yes\n"
    assert state.read_bytes() == state_original
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_does_not_claim_envrc_replaced_immediately_after_commit(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text(
        "export ORIGINAL=yes\nexport HERMES_KANBAN_BOARD=board # unified-kanban\n",
        encoding="utf-8",
    )
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state_original = json.dumps([str(project)]).encode()
    state.write_bytes(state_original)
    result = run_script(
        UNINSTALL, tmp_path, "--no-restart",
        env_extra={
            "FAKE_PY_SWAP_AFTER_TARGET": str(envrc),
            "FAKE_PY_SWAP_CONTENT": "export CONCURRENT=yes\n",
        },
    )

    assert result.returncode != 0
    assert envrc.read_text(encoding="utf-8") == "export CONCURRENT=yes\n"
    assert state.read_bytes() == state_original
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_preserves_state_replaced_after_envrc_commit(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc_original = b"export KEEP=yes\nexport HERMES_KANBAN_BOARD=board # unified-kanban\n"
    envrc.write_bytes(envrc_original)
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.write_text(json.dumps([str(project)]), encoding="utf-8")
    result = run_script(
        UNINSTALL, tmp_path, "--no-restart",
        env_extra={
            "FAKE_PY_SWAP_OTHER_ON_TARGET": str(envrc),
            "FAKE_PY_SWAP_OTHER_PATH": str(state),
            "FAKE_PY_SWAP_CONTENT": '["/concurrent/project"]\n',
        },
    )

    assert result.returncode != 0
    assert envrc.read_bytes() == envrc_original
    assert state.read_text(encoding="utf-8") == '["/concurrent/project"]\n'
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_does_not_overwrite_concurrent_change_during_rollback(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    projects = [tmp_path / "first", tmp_path / "second"]
    envrcs = []
    managed = "export HERMES_KANBAN_BOARD=board # unified-kanban\n"
    for index, project in enumerate(projects):
        project.mkdir()
        envrc = project / ".envrc"
        envrc.write_text(f"export KEEP_{index}=yes\n" + managed, encoding="utf-8")
        envrcs.append(envrc)
    second_original = envrcs[1].read_bytes()
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state_original = json.dumps([str(project) for project in projects]).encode()
    state.write_bytes(state_original)
    transaction_tmp = tmp_path / "transaction-tmp"
    transaction_tmp.mkdir()
    result = run_script(
        UNINSTALL, tmp_path, "--no-restart",
        env_extra={
            "FAKE_PY_SWAP_OTHER_ON_TARGET": str(envrcs[1]),
            "FAKE_PY_SWAP_OTHER_PATH": str(envrcs[0]),
            "FAKE_PY_SWAP_CONTENT": "export CONCURRENT=yes\n",
            "FAKE_PY_FAIL_REPLACE_TARGET": str(envrcs[1]),
            "TMPDIR": str(transaction_tmp),
        },
    )

    assert result.returncode != 0
    assert envrcs[0].read_text(encoding="utf-8") == "export CONCURRENT=yes\n"
    assert envrcs[1].read_bytes() == second_original
    assert state.read_bytes() == state_original
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()
    assert list(transaction_tmp.iterdir()) == []


def test_uninstall_rolls_back_when_state_removal_fails(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    original = b"export KEEP=yes\nexport HERMES_KANBAN_BOARD=board # unified-kanban\n"
    envrc.write_bytes(original)
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.write_text(json.dumps([str(project)]), encoding="utf-8")

    result = run_script(
        UNINSTALL,
        tmp_path,
        "--no-restart",
        env_extra={"FAKE_PY_FAIL_REMOVE_TARGET": str(state)},
    )

    assert result.returncode != 0
    assert envrc.read_bytes() == original
    assert sorted(path.name for path in project.iterdir()) == [".envrc"]
    assert state.exists()
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_preflight_failure_preserves_all_routing_and_install_paths(
    tmp_path: Path,
) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    managed = "export HERMES_KANBAN_BOARD=board # unified-kanban\n"
    first_envrc = first / ".envrc"
    first_original = "export KEEP=yes\n" + managed
    first_envrc.write_text(first_original, encoding="utf-8")
    (second / ".envrc").mkdir()
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state_original = json.dumps([str(first), str(second)])
    state.write_text(state_original, encoding="utf-8")

    result = run_script(UNINSTALL, tmp_path, "--no-restart")

    assert result.returncode != 0
    assert first_envrc.read_text(encoding="utf-8") == first_original
    assert (second / ".envrc").is_dir()
    assert state.read_text(encoding="utf-8") == state_original
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_removes_only_managed_link(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path)
    unrelated = tmp_path / ".local/bin/unrelated"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep", encoding="utf-8")
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env).returncode == 0
    result = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_setup_rolls_back_all_prior_stages_when_gateway_install_fails(
    tmp_path: Path,
) -> None:
    env, log = fake_env(tmp_path, boards=["rollback-board"])
    env["FAKE_FAIL_COMMAND"] = "gateway start"
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text("export KEEP=yes\n", encoding="utf-8")
    claude_settings = tmp_path / ".claude/settings.json"
    claude_settings.parent.mkdir(parents=True)
    original_settings = '{"model":"keep"}\n'
    claude_settings.write_text(original_settings, encoding="utf-8")

    result = run_script(
        SETUP,
        tmp_path,
        "--skip-smoke",
        "--project-dir",
        str(project),
        "--board",
        "rollback-board",
        env_extra=env,
    )

    assert result.returncode != 0
    assert envrc.read_text(encoding="utf-8") == "export KEEP=yes\n"
    assert claude_settings.read_text(encoding="utf-8") == original_settings
    assert not (tmp_path / ".codex/hooks.json").exists()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert not (tmp_path / ".local/bin/claude-kanban-hook").exists()
    assert not (tmp_path / ".local/bin/codex-kanban-hook").exists()
    assert not (tmp_path / ".local/bin/ai-session-viewer").exists()
    assert not (tmp_path / ".hermes/plugins/hermes-kanban").exists()
    assert not (tmp_path / ".hermes/config.yaml").exists()
    assert not (tmp_path / ".local/state/unified-kanban/managed-projects.json").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".local").exists()
    assert not (tmp_path / ".hermes").exists()
    assert (tmp_path / ".claude").is_dir()
    calls = log.read_text(encoding="utf-8").splitlines()
    assert "plugins disable hermes-kanban" not in calls
    assert "kanban boards delete rollback-board" not in calls


def test_uninstall_rolls_back_all_prior_stages_when_gateway_restart_fails(
    tmp_path: Path,
) -> None:
    env, log = fake_env(tmp_path, boards=["rollback-board"])
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text("export KEEP=yes\n", encoding="utf-8")
    setup_args = (
        "--no-restart",
        "--skip-smoke",
        "--project-dir",
        str(project),
        "--board",
        "rollback-board",
    )
    installed = run_script(SETUP, tmp_path, *setup_args, env_extra=env)
    assert installed.returncode == 0, installed.stderr
    settings = tmp_path / ".claude/settings.json"
    settings_before = settings.read_bytes()
    envrc_before = envrc.read_bytes()
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state_before = state.read_bytes()
    config = tmp_path / ".hermes/config.yaml"
    config_before = config.read_bytes()

    uninstall_env = dict(env)
    uninstall_env["FAKE_PLUGIN_STATUS"] = "enabled"
    uninstall_env["FAKE_FAIL_COMMAND"] = "gateway restart"
    result = run_script(UNINSTALL, tmp_path, env_extra=uninstall_env)

    assert result.returncode != 0
    assert settings.read_bytes() == settings_before
    assert envrc.read_bytes() == envrc_before
    assert state.read_bytes() == state_before
    assert config.read_bytes() == config_before
    for name in (
        "kanban-adapter",
        "claude-kanban-hook",
        "codex-kanban-hook",
        "ai-session-viewer",
    ):
        assert (tmp_path / ".local/bin" / name).is_symlink()
    assert (tmp_path / ".hermes/plugins/hermes-kanban").is_symlink()
    calls = log.read_text(encoding="utf-8").splitlines()
    assert "plugins disable hermes-kanban" in calls
    assert "plugins enable --no-allow-tool-override hermes-kanban" in calls


def test_setup_compensates_gateway_install_that_fails_after_side_effect(
    tmp_path: Path,
) -> None:
    env, log = fake_env(tmp_path)
    gateway_state = tmp_path / "gateway-state"
    env.update(
        {
            "FAKE_FAIL_COMMAND_ONCE": "gateway start",
            "FAKE_FAIL_ONCE_MARKER": str(tmp_path / "install-failed-once"),
            "FAKE_GATEWAY_STATE": str(gateway_state),
        }
    )

    result = run_script(SETUP, tmp_path, "--skip-smoke", env_extra=env)

    assert result.returncode != 0
    assert gateway_state.read_text(encoding="utf-8") == "stopped\n"
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.count("gateway start") == 1
    assert calls.count("gateway uninstall") == 1
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_restores_original_gateway_service_after_install_failure(
    tmp_path: Path,
) -> None:
    env, log = fake_env(tmp_path)
    launcher = tmp_path / ".local/bin/hermes"
    launcher.parent.mkdir(parents=True)
    original = Path(env["FAKE_HERMES_EXECUTABLE"]).read_bytes()
    launcher.write_bytes(original)
    launcher.chmod(0o755)
    plist = tmp_path / "Library/LaunchAgents/ai.hermes.gateway.plist"
    baseline_plist = plistlib.dumps(
        {
            "Label": "ai.hermes.gateway",
            "ProgramArguments": [str(tmp_path / "fake-bin/python3"), "-m", "hermes_cli.main"],
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin"},
        }
    )
    plist.write_bytes(baseline_plist)
    plist.chmod(0o644)
    gateway_state = tmp_path / "gateway-state"
    env.update(
        {
            "FAKE_FAIL_COMMAND_ONCE": "gateway start",
            "FAKE_FAIL_ONCE_MARKER": str(tmp_path / "install-failed-once"),
            "FAKE_GATEWAY_STATE": str(gateway_state),
        }
    )

    result = run_script(SETUP, tmp_path, "--skip-smoke", env_extra=env)

    assert result.returncode != 0
    assert launcher.read_bytes() == original
    assert plist.read_bytes() == baseline_plist
    assert gateway_state.read_text(encoding="utf-8") == "running\n"
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.count("gateway start") == 2


def test_setup_restores_original_hermes_launcher_and_selector_on_later_failure(
    tmp_path: Path,
) -> None:
    env, _log = fake_env(tmp_path)
    launcher = tmp_path / ".local/bin/hermes"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    original = b"#!/bin/sh\necho ORIGINAL\n"
    launcher.write_bytes(original)
    launcher.chmod(0o755)
    config = tmp_path / ".hermes/config.yaml"
    env["FAKE_PY_FAIL_REPLACE_TARGET"] = str(config)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert result.returncode != 0
    assert launcher.read_bytes() == original
    selector = Path(env["HERMES_AGENT_REPO"] + ".releases/current")
    assert not selector.exists()


def test_setup_preserves_foreign_hermes_launcher_successor_on_rollback(
    tmp_path: Path,
) -> None:
    env, _log = fake_env(tmp_path)
    launcher = tmp_path / ".local/bin/hermes"
    config = tmp_path / ".hermes/config.yaml"
    foreign = "#!/bin/sh\necho FOREIGN\n"
    env.update(
        {
            "FAKE_PY_SWAP_AFTER_TARGET": str(launcher),
            "FAKE_PY_SWAP_CONTENT": foreign,
            "FAKE_PY_FAIL_REPLACE_TARGET": str(config),
        }
    )

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert result.returncode != 0
    assert launcher.read_text(encoding="utf-8") == foreign
    assert "foreign paths were preserved" in result.stderr


def crash_after_selector_replacement(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Leave a stage operation receipt that no checkpoint ever consumed."""
    env, _log = fake_env(tmp_path)
    selector = Path(env["HERMES_AGENT_REPO"] + ".releases/current")
    crashing_env = dict(env)
    crashing_env["FAKE_PY_KILL_PARENT_AFTER_TARGET"] = str(selector)

    crashed = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=crashing_env
    )

    assert crashed.returncode != 0
    assert selector.exists()
    transaction = tmp_path / ".local/state/unified-kanban/setup-transaction"
    stages = sorted(transaction.glob("stage-*.json"))
    assert stages, "the crash must leave an uncheckpointed stage receipt"
    return env, selector


def test_setup_recovers_crash_after_replace_without_interference(
    tmp_path: Path,
) -> None:
    """The positive control for the foreign-successor recovery test.

    A crash between ``replace-file`` and ``checkpoint`` leaves an operation
    receipt the manifest does not yet manage. Nothing interfered with the host,
    so the next setup has to roll that operation back and complete.
    """
    env, selector = crash_after_selector_replacement(tmp_path)

    recovered = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert recovered.returncode == 0, recovered.stderr
    assert selector.exists()
    assert not (tmp_path / ".local/state/unified-kanban/setup-transaction").exists()
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_setup_recovery_rolls_back_the_uncheckpointed_operation(
    tmp_path: Path,
) -> None:
    """Recovery must undo the operation, not merely tolerate its receipt."""
    env, selector = crash_after_selector_replacement(tmp_path)
    # Fail the rerun on the same leaf immediately after recovery, so the only
    # way the selector can end up absent is that recovery restored the
    # pre-crash baseline before this run snapshotted it.
    failing_env = dict(env)
    failing_env["FAKE_PY_FAIL_REPLACE_TARGET"] = str(selector)

    recovered = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=failing_env
    )

    assert recovered.returncode != 0
    assert not selector.exists()


def test_setup_crash_recovery_preserves_foreign_selector_successor(
    tmp_path: Path,
) -> None:
    env, selector = crash_after_selector_replacement(tmp_path)
    foreign = b"/foreign/release-" + b"f" * 40 + b"\n"
    successor = selector.with_name("current.foreign")
    successor.write_bytes(foreign)
    successor.replace(selector)

    recovered = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert recovered.returncode != 0
    assert selector.read_bytes() == foreign
    # The refusal must name the substituted leaf and the identity that no
    # longer matches the operation receipt, not any generic recovery fault.
    assert "stale setup transaction" in recovered.stderr
    assert "operation identity no longer present" in recovered.stderr
    assert str(selector) in recovered.stderr
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_recovers_checkpointed_crash_and_completes(tmp_path: Path) -> None:
    env, _log = fake_env(tmp_path)
    crashing_env = dict(env)
    crashing_env["FAKE_PY_KILL_PARENT_AFTER_CHECKPOINT"] = "1"

    crashed = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=crashing_env
    )
    assert crashed.returncode != 0

    recovered = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert recovered.returncode == 0, recovered.stderr
    selector = Path(env["HERMES_AGENT_REPO"] + ".releases/current")
    assert selector.exists()
    assert not (tmp_path / ".local/state/unified-kanban/setup-transaction").exists()


def test_uninstall_compensates_gateway_restart_that_fails_after_side_effect(
    tmp_path: Path,
) -> None:
    env, log = fake_env(tmp_path)
    installed = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )
    assert installed.returncode == 0, installed.stderr
    gateway_state = tmp_path / "gateway-state"
    uninstall_env = dict(env)
    uninstall_env.update(
        {
            "FAKE_PLUGIN_STATUS": "enabled",
            "FAKE_FAIL_COMMAND_ONCE": "gateway restart",
            "FAKE_FAIL_ONCE_MARKER": str(tmp_path / "restart-failed-once"),
            "FAKE_GATEWAY_STATE": str(gateway_state),
        }
    )

    result = run_script(UNINSTALL, tmp_path, env_extra=uninstall_env)

    assert result.returncode != 0
    assert gateway_state.read_text(encoding="utf-8") == "running\n"
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.count("gateway restart") == 2
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_restores_original_hermes_launcher_and_removes_selector(
    tmp_path: Path,
) -> None:
    env, _log = fake_env(tmp_path)
    launcher = tmp_path / ".local/bin/hermes"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    original = b"#!/bin/sh\necho ORIGINAL\n"
    launcher.write_bytes(original)
    launcher.chmod(0o755)
    installed = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )
    assert installed.returncode == 0, installed.stderr
    assert launcher.read_bytes() != original

    removed = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert removed.returncode == 0, removed.stderr
    assert launcher.read_bytes() == original
    selector = Path(env["HERMES_AGENT_REPO"] + ".releases/current")
    assert not selector.exists()
    assert not (
        tmp_path
        / ".local/state/unified-kanban/hermes-launcher.before-unified-kanban"
    ).exists()


def test_uninstall_removes_managed_hermes_launcher_when_no_original_existed(
    tmp_path: Path,
) -> None:
    env, _log = fake_env(tmp_path)
    installed = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )
    assert installed.returncode == 0, installed.stderr
    launcher = tmp_path / ".local/bin/hermes"
    assert launcher.exists()

    removed = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert removed.returncode == 0, removed.stderr
    assert not launcher.exists()


def test_uninstall_is_idempotent_after_original_launcher_is_restored(
    tmp_path: Path,
) -> None:
    env, original, _managed = install_with_original_launcher(tmp_path)

    first = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)
    assert first.returncode == 0, first.stderr
    launcher = tmp_path / ".local/bin/hermes"
    restored_identity = (launcher.stat().st_dev, launcher.stat().st_ino)

    second = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert second.returncode == 0, second.stderr
    assert launcher.read_bytes() == original
    assert (launcher.stat().st_dev, launcher.stat().st_ino) == restored_identity


def test_uninstall_is_idempotent_after_managed_launcher_is_removed(
    tmp_path: Path,
) -> None:
    env, _log = fake_env(tmp_path)
    installed = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )
    assert installed.returncode == 0, installed.stderr

    first = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)
    assert first.returncode == 0, first.stderr
    launcher = tmp_path / ".local/bin/hermes"
    assert not launcher.exists()

    second = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert second.returncode == 0, second.stderr
    assert not launcher.exists()


def launcher_backup(home: Path) -> Path:
    return home / ".local/state/unified-kanban/hermes-launcher.before-unified-kanban"


def install_with_original_launcher(tmp_path: Path) -> tuple[dict[str, str], bytes, bytes]:
    """Install over an existing Hermes launcher and return the managed bytes."""
    env, _log = fake_env(tmp_path)
    launcher = tmp_path / ".local/bin/hermes"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    original = b"#!/bin/sh\necho ORIGINAL\n"
    launcher.write_bytes(original)
    launcher.chmod(0o755)
    installed = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )
    assert installed.returncode == 0, installed.stderr
    managed = launcher.read_bytes()
    assert managed != original
    assert launcher_backup(tmp_path).read_bytes() == original
    return env, original, managed


def test_uninstall_refuses_foreign_replacement_of_retained_launcher_backup(
    tmp_path: Path,
) -> None:
    env, _original, managed = install_with_original_launcher(tmp_path)
    backup = launcher_backup(tmp_path)
    foreign = b"#!/bin/sh\necho FOREIGN\n"
    backup.write_bytes(foreign)

    removed = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert removed.returncode != 0
    assert (tmp_path / ".local/bin/hermes").read_bytes() == managed
    assert backup.read_bytes() == foreign
    assert Path(env["HERMES_AGENT_REPO"] + ".releases/current").exists()


def test_uninstall_refuses_removed_launcher_backup(tmp_path: Path) -> None:
    env, _original, managed = install_with_original_launcher(tmp_path)
    launcher_backup(tmp_path).unlink()

    removed = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert removed.returncode != 0
    assert (tmp_path / ".local/bin/hermes").read_bytes() == managed
    assert Path(env["HERMES_AGENT_REPO"] + ".releases/current").exists()


def test_uninstall_refuses_injected_launcher_backup_when_none_existed(
    tmp_path: Path,
) -> None:
    env, _log = fake_env(tmp_path)
    installed = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )
    assert installed.returncode == 0, installed.stderr
    launcher = tmp_path / ".local/bin/hermes"
    managed = launcher.read_bytes()
    backup = launcher_backup(tmp_path)
    assert not backup.exists()
    injected = b"#!/bin/sh\necho INJECTED\n"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(injected)

    removed = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert removed.returncode != 0
    assert launcher.read_bytes() == managed
    assert backup.read_bytes() == injected


def test_uninstall_preserves_launcher_backup_replaced_during_restore(
    tmp_path: Path,
) -> None:
    env, _original, managed = install_with_original_launcher(tmp_path)
    launcher = tmp_path / ".local/bin/hermes"
    backup = launcher_backup(tmp_path)
    foreign = "#!/bin/sh\necho FOREIGN\n"
    uninstall_env = dict(env)
    uninstall_env.update(
        {
            "FAKE_PY_SWAP_OTHER_ON_TARGET": str(launcher),
            "FAKE_PY_SWAP_OTHER_PATH": str(backup),
            "FAKE_PY_SWAP_CONTENT": foreign,
        }
    )

    removed = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=uninstall_env)

    assert removed.returncode != 0
    assert backup.read_text(encoding="utf-8") == foreign
    assert launcher.read_bytes() == managed
    assert Path(env["HERMES_AGENT_REPO"] + ".releases/current").exists()


def test_setup_rerun_never_retains_its_own_managed_launcher(tmp_path: Path) -> None:
    env, _log = fake_env(tmp_path)
    launcher = tmp_path / ".local/bin/hermes"

    first = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)
    assert first.returncode == 0, first.stderr
    managed = launcher.read_bytes()
    identity = (launcher.stat().st_dev, launcher.stat().st_ino)

    second = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)

    assert second.returncode == 0, second.stderr
    assert not launcher_backup(tmp_path).exists()
    assert launcher.read_bytes() == managed
    assert (launcher.stat().st_dev, launcher.stat().st_ino) == identity

    removed = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert removed.returncode == 0, removed.stderr
    assert not launcher.exists()


def test_setup_refuses_rerun_when_retained_launcher_backup_was_deleted(
    tmp_path: Path,
) -> None:
    env, _original, managed = install_with_original_launcher(tmp_path)
    launcher_backup(tmp_path).unlink()

    rerun = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)

    assert rerun.returncode != 0
    assert "retained original backup is missing" in rerun.stderr
    assert (tmp_path / ".local/bin/hermes").read_bytes() == managed
    assert not launcher_backup(tmp_path).exists()


def test_external_commands_do_not_inherit_transaction_authority(tmp_path: Path) -> None:
    env, _log = fake_env(
        tmp_path, boards=["authority-board", "unified-kanban-smoke"]
    )
    report = tmp_path / "authority-report"
    env["FAKE_AUTHORITY_REPORT"] = str(report)
    env["FAKE_GATEWAY_SUPERVISION"] = str(tmp_path / "gateway-supervision")
    env["FAKE_INSTALL_UNMANAGED_ONCE"] = str(tmp_path / "install-fell-back")
    project = tmp_path / "project"
    project.mkdir()

    setup_result = run_script(
        SETUP,
        tmp_path,
        "--project-dir",
        str(project),
        "--board",
        "authority-board",
        env_extra=env,
    )

    assert setup_result.returncode == 0, setup_result.stderr
    setup_reports = report.read_text(encoding="utf-8").splitlines()
    scrubbed = [
        line for line in setup_reports
        if line.startswith("gateway ")
        or " create " in line
    ]
    retained = [
        line for line in setup_reports if line.startswith("kanban boards list --json ")
    ]
    assert any(line.startswith("gateway start ") for line in scrubbed)
    assert any(line.startswith("gateway status ") for line in scrubbed)
    assert any(line.startswith("gateway stop ") for line in scrubbed)
    assert any(line.startswith("gateway start ") for line in scrubbed)
    assert any(" create " in line for line in scrubbed)
    assert all(
        "fd9=closed fd10=closed dir=unset txfd=unset ledgerfd=unset" in line
        for line in scrubbed
    )
    # Positive control: the same probe must observe the authority on the calls
    # setup makes while still holding it, so a regression that stops scrubbing
    # cannot pass silently. Board lookups run both before the transaction runner
    # takes over and again inside it, so only the in-transaction ones see it.
    assert any(
        "fd9=open fd10=open" in line and "txfd=9 ledgerfd=10" in line
        for line in retained
    )

    report.unlink()
    uninstall_env = dict(env)
    uninstall_env["FAKE_PLUGIN_STATUS"] = "enabled"
    uninstall_result = run_script(UNINSTALL, tmp_path, env_extra=uninstall_env)

    assert uninstall_result.returncode == 0, uninstall_result.stderr
    uninstall_reports = report.read_text(encoding="utf-8").splitlines()
    assert len(uninstall_reports) == 1
    assert uninstall_reports[0].startswith("gateway restart ")
    assert (
        "fd9=closed fd10=closed dir=unset txfd=unset ledgerfd=unset"
        in uninstall_reports[0]
    )
