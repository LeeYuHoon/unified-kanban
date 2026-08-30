#!/usr/bin/env bash
# Activate the reviewed immutable Hermes release without ever mutating the
# Hermes checkout. The checkout named by HERMES_AGENT_REPO is a read-only input
# that only supplies the sibling release root; every release is constructed
# under <HERMES_AGENT_REPO>.releases/release-<carried> and selected by swapping
# one regular selector file inside a path transaction.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
OFFICIAL_REPO_URL="https://github.com/NousResearch/hermes-agent.git"
CHECK=0
PREPARE_ONLY=0
NO_RESTART=0
LOCK_HELD=0
LOCK_TOKEN=""
TRANSACTION_DIR=""
TRANSACTION_RECEIPT=""
TRANSACTION_TOKEN=""
TRANSACTION_STAGE=0
STAGE_RECEIPT=""
PENDING_RECEIPT=""
PRIOR_SELECTOR=""
PRIOR_GATEWAY_PLIST=""
RETAIN_TRANSACTION_DIR=0
DASHBOARD_WAS_RUNNING=0
SERVICES_DISTURBED=0

while (($#)); do
  case "$1" in
    --check) CHECK=1 ;;
    --prepare-only) PREPARE_ONLY=1 ;;
    --no-restart) NO_RESTART=1 ;;
    # Backward-compatible no-op: no checkout is ever mutated in place, so there
    # is nothing a caller could need to force past.
    --force) ;;
    -h|--help)
      echo "Usage: ./scripts/update-hermes-if-needed.sh [--check] [--prepare-only] [--no-restart]"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# Prerequisites are refused before anything managed is created. Immutable
# release publication requires macOS renamex_np, so an unsupported platform
# must stop here rather than half-way through a release build.
[[ "$(uname -s)" == "Darwin" ]] || {
  echo "Unified Kanban supports macOS only" >&2
  exit 1
}
for required in python3 git uv curl; do
  command -v "$required" >/dev/null 2>&1 || {
    echo "$required is required" >&2
    exit 1
  }
done
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Python 3.11 or newer is required" >&2
  exit 1
}
REAL_GIT="$(command -v git)"
REAL_UV="$(command -v uv)"
NPM_BIN="${HERMES_NPM_BIN:-$(command -v npm 2>/dev/null || true)}"
[[ -n "$NPM_BIN" && "$NPM_BIN" == /* ]] || {
  echo "An absolute npm executable is required to build the sealed Dashboard" >&2
  exit 1
}

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
[[ "$HERMES_HOME" == /* ]] || {
  echo "HERMES_HOME must be an absolute path: $HERMES_HOME" >&2
  exit 1
}
AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
[[ "$AGENT_REPO" == /* ]] || {
  echo "HERMES_AGENT_REPO must be an absolute path: $AGENT_REPO" >&2
  exit 1
}
# The selector this swaps must be the selector the installed launcher reads, so
# both sides derive it from one normal form instead of concatenating whatever
# spelling the environment supplied.
AGENT_REPO="$(PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m kanban_adapter.release_layout "$AGENT_REPO")" || exit 1
HERMES_RELEASE_ROOT="${AGENT_REPO}.releases"
HERMES_RELEASE_SELECTOR="$HERMES_RELEASE_ROOT/current"
HERMES_RELEASE_PREVIOUS="$HERMES_RELEASE_ROOT/previous"
HERMES_LAUNCHER="$HOME/.local/bin/hermes"
GATEWAY_PLIST="$HOME/Library/LaunchAgents/ai.hermes.gateway.plist"
STATE_DIR="$HERMES_HOME/state"
PENDING_FILE="$STATE_DIR/hermes-kanban-update.pending"
LOCK_DIR="$STATE_DIR/hermes-kanban-update.lock"
CARRIED_COMMITS_FILE="$REPO_ROOT/patches/hermes-agent-carried-commits"
CARRIED_BUNDLE="$REPO_ROOT/patches/hermes-agent-carried.bundle"
DASHBOARD_HOST="${HERMES_DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-9119}"
DASHBOARD_READY_ATTEMPTS="${HERMES_DASHBOARD_READY_ATTEMPTS:-20}"
# Both values reach an arithmetic context or a URL, so they are constrained
# before use rather than interpolated as whatever the environment supplied.
[[ "$DASHBOARD_PORT" =~ ^[0-9]{1,5}$ ]] && ((DASHBOARD_PORT >= 1 && DASHBOARD_PORT <= 65535)) || {
  echo "HERMES_DASHBOARD_PORT must be a TCP port number: $DASHBOARD_PORT" >&2
  exit 1
}
[[ "$DASHBOARD_READY_ATTEMPTS" =~ ^[0-9]{1,4}$ ]] && ((DASHBOARD_READY_ATTEMPTS >= 1)) || {
  echo "HERMES_DASHBOARD_READY_ATTEMPTS must be a positive integer: $DASHBOARD_READY_ATTEMPTS" >&2
  exit 1
}
[[ "$DASHBOARD_HOST" =~ ^[A-Za-z0-9.:_-]+$ ]] || {
  echo "HERMES_DASHBOARD_HOST must be a bare host or address: $DASHBOARD_HOST" >&2
  exit 1
}
# A dashboard bound to a wildcard address cannot be probed at its bind address,
# and an IPv6 literal has to be bracketed before it can go into a URL.
PROBE_HOST="$DASHBOARD_HOST"
case "$DASHBOARD_HOST" in
  0.0.0.0) PROBE_HOST="127.0.0.1" ;;
  ::|::1|0:0:0:0:0:0:0:0|0:0:0:0:0:0:0:1) PROBE_HOST="[::1]" ;;
  *:*) PROBE_HOST="[$DASHBOARD_HOST]" ;;
esac

if ! SUPPORTED_UPSTREAM="$(PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
  'from kanban_adapter.compatibility import read_supported_upstream; print(read_supported_upstream())')"; then
  echo "Unable to trust the repository Hermes upstream pin." >&2
  exit 1
fi
FINAL_CARRIED_COMMIT="$(python3 - "$CARRIED_COMMITS_FILE" <<'PY'
from pathlib import Path
import re, sys
values = []
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    value = raw.split("#", 1)[0].strip()
    if value:
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise SystemExit("invalid carried commit manifest")
        values.append(value)
if not values:
    raise SystemExit("empty carried commit manifest")
print(values[-1])
PY
)"
TARGET_RELEASE="$HERMES_RELEASE_ROOT/release-$FINAL_CARRIED_COMMIT"

# Every external command loses the transaction authority, the delegated Hermes
# turn context, and any ambient Git authority, and never inherits the retained
# transaction directory or ledger descriptors.
run_scrubbed() (
  for name in "${!GIT_@}"; do unset "$name"; done
  for name in "${!UNIFIED_KANBAN_@}"; do unset "$name"; done
  unset HERMES_DELEGATED_CHILD_CONTEXT HERMES_KANBAN_TASK HERMES_KANBAN_WORKSPACE
  unset HERMES_KANBAN_BOARD HERMES_SESSION_ID
  unset GITHUB_TOKEN GH_TOKEN SSH_ASKPASS
  exec 9<&-
  exec 10<&-
  "$@"
)

read_release_file() {
  python3 - "$1" "$HERMES_RELEASE_ROOT" <<'PY'
import os, re, stat, sys

selector = sys.argv[1]
release_root = sys.argv[2]
try:
    info = os.lstat(selector)
except FileNotFoundError:
    raise SystemExit(0)
if not stat.S_ISREG(info.st_mode):
    raise SystemExit(f"Hermes release selector is not a regular file: {selector}")
if (
    info.st_nlink != 1
    or info.st_uid != os.getuid()
    or stat.S_IMODE(info.st_mode) != 0o600
):
    raise SystemExit(f"Hermes release selector is not a private regular file: {selector}")
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
descriptor = os.open(selector, flags)
try:
    opened = os.fstat(descriptor)
    if (
        (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        or opened.st_nlink != 1
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
    ):
        raise SystemExit(f"Hermes release selector changed during read: {selector}")
    data = os.read(descriptor, 4096)
    if os.read(descriptor, 1):
        raise SystemExit(f"Hermes release selector is too large: {selector}")
finally:
    os.close(descriptor)
if not data.endswith(b"\n") or data.endswith(b"\n\n"):
    raise SystemExit(f"Hermes release selector is not canonical: {selector}")
try:
    release = data[:-1].decode("utf-8")
except UnicodeDecodeError:
    raise SystemExit(f"Hermes release selector is not UTF-8: {selector}")
if (
    "\n" in release
    or not os.path.isabs(release)
    or os.path.dirname(release) != release_root
    or re.fullmatch(r"release-[0-9a-f]{40}", os.path.basename(release)) is None
):
    raise SystemExit(f"Hermes release selector does not name a release: {selector}")
print(release)
PY
}

read_selected_release() {
  read_release_file "$HERMES_RELEASE_SELECTOR"
}

validated_release_cli() {
  python3 - "$1" "$HERMES_RELEASE_ROOT" <<'PY'
import os, stat, sys
from pathlib import Path

release = Path(sys.argv[1])
root = Path(sys.argv[2])
if release.parent != root:
    raise SystemExit(1)
components = (root, release, release / "venv", release / "venv/bin")
identities = []
for component in components:
    info = component.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise SystemExit(1)
    identities.append((component, info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns, info.st_ctime_ns))
cli = release / "venv/bin/hermes"
info = cli.lstat()
if (
    not stat.S_ISREG(info.st_mode)
    or info.st_nlink != 1
    or info.st_uid != os.getuid()
    or info.st_mode & 0o111 == 0
    or stat.S_IMODE(info.st_mode) & 0o022
):
    raise SystemExit(1)
identities.append((cli, info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns, info.st_ctime_ns))
for component, *expected in identities:
    current = component.lstat()
    observed = [current.st_dev, current.st_ino, current.st_mode, current.st_mtime_ns, current.st_ctime_ns]
    if observed != expected:
        raise SystemExit(1)
print(cli)
PY
}

read_pending_state() {
  set +e
  PENDING_STATE="$(python3 "$REPO_ROOT/scripts/update-state.py" read-receipt pending "$PENDING_FILE")"
  PENDING_STATUS=$?
  set -e
  if ((PENDING_STATUS != 0 && PENDING_STATUS != 3)); then
    echo "could not safely read pending update state" >&2
    exit 1
  fi
}

activation_required() {
  if [[ "$SELECTED_RELEASE" != "$TARGET_RELEASE" ]]; then
    return 0
  fi
  # Naming the reviewed release is not the same as running it: a selection whose
  # release is gone, or is no longer a real directory, is not up to date.
  if [[ -d "$TARGET_RELEASE" && ! -L "$TARGET_RELEASE" ]]; then
    return 1
  fi
  return 0
}

SELECTED_RELEASE="$(read_selected_release)"

if ((CHECK)); then
  if activation_required; then
    echo "UPDATE_AVAILABLE: reviewed release $FINAL_CARRIED_COMMIT is not selected"
  else
    echo "UP_TO_DATE"
  fi
  exit 0
fi

# Nothing to activate and nothing interrupted is decided from two reads, so it
# is decided before the state directory or the lock exists: a no-change run
# leaves the host exactly as it found it. Every other outcome falls through and
# is re-decided under the lock below, where a concurrent activation that
# published its marker between these two reads is settled.
if ((PREPARE_ONLY == 0)); then
  read_pending_state
  if ! activation_required && ((PENDING_STATUS == 3)); then
    echo "SKIPPED: Hermes release $FINAL_CARRIED_COMMIT is already selected"
    exit 0
  fi
fi

release_lock() {
  if [[ "$LOCK_HELD" == 1 ]]; then
    python3 "$REPO_ROOT/scripts/update-state.py" lock-release lock \
      "$LOCK_DIR" "$LOCK_TOKEN" \
      || echo "update lock identity changed; foreign successor was preserved" >&2
    LOCK_HELD=0
  fi
}

acquire_lock() {
  LOCK_TOKEN="$$:$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  if ! python3 "$REPO_ROOT/scripts/update-state.py" lock-acquire lock \
    "$LOCK_DIR" "$LOCK_TOKEN" 2>/dev/null; then
    local existing_token=""
    existing_token="$(python3 "$REPO_ROOT/scripts/update-state.py" \
      lock-read lock "$LOCK_DIR" 2>/dev/null || true)"
    OWNER_PID="${existing_token%%:*}"
    if [[ "$OWNER_PID" == "$existing_token" ]]; then
      OWNER_PID=""
    fi
    if [[ -z "$OWNER_PID" ]] || kill -0 "$OWNER_PID" 2>/dev/null; then
      echo "another update holds the lock: $LOCK_DIR${OWNER_PID:+ (pid $OWNER_PID)}" >&2
      exit 1
    fi
    echo "stale update lock has dead pid $OWNER_PID; refusing automatic removal: $LOCK_DIR" >&2
    exit 1
  fi
  LOCK_HELD=1
}

dashboard_ready() {
  run_scrubbed curl --fail --silent --show-error --max-time 2 \
    "http://$PROBE_HOST:$DASHBOARD_PORT/api/status" >/dev/null 2>&1
}

stop_dashboard() {
  local cli="${1:-$HERMES_LAUNCHER}"
  echo "Stopping Dashboard before restart"
  run_scrubbed "$cli" dashboard --stop \
    || echo "Dashboard was already stopped"
  local attempt
  for ((attempt = 1; attempt <= DASHBOARD_READY_ATTEMPTS; attempt++)); do
    if ! dashboard_ready; then
      return 0
    fi
    sleep 0.25
  done
  echo "Dashboard did not stop on $PROBE_HOST:$DASHBOARD_PORT" >&2
  return 1
}

start_dashboard() {
  local cli="${1:-$HERMES_LAUNCHER}"
  echo "Starting Dashboard on $DASHBOARD_HOST:$DASHBOARD_PORT"
  run_scrubbed nohup python3 "$REPO_ROOT/scripts/update-state.py" \
    exec-append-log log "$HERMES_HOME/logs/dashboard.log" \
    -- \
    "$cli" dashboard \
    --host "$DASHBOARD_HOST" \
    --port "$DASHBOARD_PORT" \
    --no-open \
    --skip-build \
    </dev/null &
  local attempt
  for ((attempt = 1; attempt <= DASHBOARD_READY_ATTEMPTS; attempt++)); do
    if dashboard_ready; then
      return 0
    fi
    sleep 0.25
  done
  echo "Dashboard restart failed on $DASHBOARD_HOST:$DASHBOARD_PORT" >&2
  return 1
}

# Services are restarted only after the selector already names the release they
# must run, so a restarted process always execs through the selection this run
# committed.
ensure_macos_gateway_supervised() {
  local cli="$1" expected_release="$2" attempt status_output
  if [[ "$(/usr/bin/uname -s)" != "Darwin" ]]; then
    return 0
  fi
  for attempt in 1 2 3 4 5; do
    status_output="$(run_scrubbed "$cli" gateway status 2>&1 || true)"
    if [[ "$status_output" == *"Gateway is supervised by launchd"* ]] \
      && run_scrubbed python3 "$REPO_ROOT/scripts/verify-macos-launchd-service.py" \
        "$GATEWAY_PLIST" --expected-release "$expected_release"; then
      return 0
    fi
    if ((attempt < 5)); then
      /bin/sleep "$attempt"
    fi
  done
  printf '%s\n' "$status_output" >&2
  echo "gateway activation failed: launchd did not supervise the reviewed immutable release" >&2
  return 1
}

restart_services() {
  if ((NO_RESTART)); then
    echo "Leaving Hermes services untouched on request"
    return 0
  fi
  SERVICES_DISTURBED=1
  if ((DASHBOARD_WAS_RUNNING)); then
    stop_dashboard || return 1
    start_dashboard || return 1
  else
    echo "Dashboard was not running; leaving it stopped"
  fi
  echo "Restarting Hermes gateway"
  if [[ "$(/usr/bin/uname -s)" == "Darwin" ]]; then
    run_scrubbed python3 "$REPO_ROOT/scripts/reload-macos-launchd-service.py" \
      "$GATEWAY_PLIST" || return 1
  else
    run_scrubbed "$HERMES_LAUNCHER" gateway restart || return 1
  fi
  ensure_macos_gateway_supervised "$HERMES_LAUNCHER" "$TARGET_RELEASE" || return 1
  return 0
}

# Compensation for a rolled-back activation: the services were started against
# a release that is no longer selected, so they are restarted against the
# restored selector. Failure here is reported, never hidden.
launchd_gateway_state() {
  local domain uid output status expected
  uid="$(/usr/bin/id -u)"
  domain="gui/$uid"
  if output="$(run_scrubbed /bin/launchctl print "$domain/ai.hermes.gateway" 2>&1)"; then
    return 0
  else
    status=$?
  fi
  expected="$(printf 'Bad request.\nCould not find service \"ai.hermes.gateway\" in domain for user gui: %s' "$uid")"
  if [[ $status -eq 113 && "$output" == "$expected" ]]; then
    return 1
  fi
  return 2
}

compensate_services() {
  local prior_selector="$1" prior_plist="$2" failed=0
  local prior_release="" prior_cli="" domain="gui/$(/usr/bin/id -u)"

  if [[ -f "$prior_selector" && ! -L "$prior_selector" ]]; then
    prior_release="$(read_release_file "$prior_selector")" || prior_release=""
    if [[ -n "$prior_release" ]]; then
      prior_cli="$(validated_release_cli "$prior_release")" || prior_cli=""
    fi
  fi

  if ((NO_RESTART)) || ((SERVICES_DISTURBED == 0)); then
    return 0
  fi
  if ((DASHBOARD_WAS_RUNNING)); then
    stop_dashboard "$HERMES_RELEASE/venv/bin/hermes" >/dev/null 2>&1 || true
    if [[ -n "$prior_cli" ]]; then
      start_dashboard "$prior_cli" >/dev/null 2>&1 || failed=1
    else
      failed=1
    fi
  fi

  # Never reopen the canonical selector or plist after rollback. The private
  # exports are exact opening snapshots retained behind the transaction's 0700
  # directory capability, so a foreign canonical successor cannot be executed.
  run_scrubbed /bin/launchctl bootout "$domain/ai.hermes.gateway" \
    >/dev/null 2>&1 || true
  if [[ -f "$prior_plist" && ! -L "$prior_plist" ]]; then
    if run_scrubbed /bin/launchctl bootstrap "$domain" "$prior_plist" \
      >/dev/null 2>&1 \
      && run_scrubbed /bin/launchctl kickstart -k "$domain/ai.hermes.gateway" \
        >/dev/null 2>&1 \
      && run_scrubbed python3 "$REPO_ROOT/scripts/verify-macos-launchd-service.py" \
        "$prior_plist" --allow-unsealed-environment >/dev/null 2>&1; then
      :
    else
      failed=1
      run_scrubbed /bin/launchctl bootout "$domain/ai.hermes.gateway" \
        >/dev/null 2>&1 || true
      if launchd_gateway_state; then
        RETAIN_TRANSACTION_DIR=1
      else
        local probe_status=$?
        if [[ $probe_status -ne 1 ]]; then
          RETAIN_TRANSACTION_DIR=1
        fi
      fi
    fi
  elif launchd_gateway_state; then
    failed=1
    RETAIN_TRANSACTION_DIR=1
  else
    local probe_status=$?
    if [[ $probe_status -ne 1 ]]; then
      failed=1
      RETAIN_TRANSACTION_DIR=1
    fi
  fi
  return "$failed"
}

finish_update() {
  local status=$? rollback_failed=0
  trap - EXIT
  if [[ -n "$TRANSACTION_RECEIPT" ]]; then
    if ((status == 0)) \
      && python3 "$REPO_ROOT/scripts/path-transaction.py" commit \
        "$TRANSACTION_RECEIPT" "$TRANSACTION_TOKEN"; then
      :
    else
      if ((status == 0)); then
        echo "update transaction receipt commit failed; rolling back" >&2
        status=1
      fi
      rollback_succeeded=1
      if ! python3 "$REPO_ROOT/scripts/path-transaction.py" rollback-ledger \
        "$TRANSACTION_RECEIPT" 10; then
        rollback_succeeded=0
        rollback_failed=1
        echo "update rollback was incomplete because managed identities changed; foreign paths were preserved" >&2
      fi
      if ((rollback_succeeded)) \
        && ! compensate_services "$PRIOR_SELECTOR" "$PRIOR_GATEWAY_PLIST"; then
        rollback_failed=1
        echo "Hermes services could not be restored from retained opening capabilities" >&2
      fi
      if ((rollback_failed)); then
        status=1
      fi
    fi
  fi
  if [[ -n "$TRANSACTION_DIR" ]]; then
    if ((RETAIN_TRANSACTION_DIR)); then
      echo "retained private update recovery after ambiguous launchd state: $TRANSACTION_DIR" >&2
      status=1
    elif ! python3 "$REPO_ROOT/scripts/path-transaction.py" cleanup-private-dir \
      "$TRANSACTION_DIR" 9; then
      echo "private update transaction cleanup failed" >&2
      status=1
    fi
    exec 9<&-
    exec 10<&-
  fi
  release_lock
  exit "$status"
}

next_stage_receipt() {
  TRANSACTION_STAGE=$((TRANSACTION_STAGE + 1))
  STAGE_RECEIPT="$TRANSACTION_DIR/stage-$TRANSACTION_STAGE.json"
}

checkpoint_stage() {
  TRANSACTION_TOKEN="$(python3 "$REPO_ROOT/scripts/path-transaction.py" checkpoint \
    "$TRANSACTION_RECEIPT" "$STAGE_RECEIPT" "$TRANSACTION_TOKEN")"
}

# The reviewed release is bound to the immutable official snapshot recorded in
# the repository pin. The moving official main is observed only to report that
# another update is available; it does not invalidate completed review evidence.
observe_newer_upstream() {
  local observed=""
  if ! observed="$(
    run_scrubbed env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
      GIT_TERMINAL_PROMPT=0 GIT_PAGER=cat \
      "$REAL_GIT" -c credential.helper= -c core.askPass= -c http.extraHeader= \
      ls-remote "$OFFICIAL_REPO_URL" refs/heads/main \
      | python3 -c 'import sys; fields=sys.stdin.read().split(); print(fields[0] if fields else "")'
  )"; then
    echo "Notice: official Hermes main could not be read; activating reviewed snapshot $SUPPORTED_UPSTREAM." >&2
    return 0
  fi
  if [[ "$observed" != "$SUPPORTED_UPSTREAM" ]]; then
    echo "Notice: official Hermes main is ${observed:-unreadable}; activating reviewed snapshot $SUPPORTED_UPSTREAM." >&2
  fi
}

# The release trap is armed before the lock exists so no signal can land in
# between and leave a lock this run owns but never releases.
trap release_lock EXIT
trap 'exit 129' INT
trap 'exit 143' TERM
python3 "$REPO_ROOT/scripts/update-state.py" ensure-dir directory "$STATE_DIR"
acquire_lock

# Re-read both durable references under the shared activation/GC lock.
SELECTED_RELEASE="$(read_selected_release)"
PREVIOUS_RELEASE="$(read_release_file "$HERMES_RELEASE_PREVIOUS")"

read_pending_state
PENDING_DASHBOARD=0
if ((PENDING_STATUS == 0)); then
  PENDING_RECEIPT="${PENDING_STATE%%$'\n'*}"
  PENDING_VALUES="${PENDING_STATE#*$'\n'}"
  PENDING_DASHBOARD="${PENDING_VALUES##*$'\n'}"
fi

ACTIVATION_REQUIRED=0
if activation_required; then
  ACTIVATION_REQUIRED=1
fi

if ((PREPARE_ONLY)); then
  observe_newer_upstream
  HERMES_RELEASE="$(python3 "$REPO_ROOT/scripts/hermes-release-manager.py" prepare \
    "$AGENT_REPO" "$SUPPORTED_UPSTREAM" "$FINAL_CARRIED_COMMIT" "$CARRIED_BUNDLE" \
    --uv "$REAL_UV" --npm "$NPM_BIN")"
  python3 "$REPO_ROOT/scripts/verify-carried-bundle.py" --hermes-repo "$HERMES_RELEASE"
  echo "PREPARED: $HERMES_RELEASE"
  exit 0
fi

# Nothing to activate and nothing interrupted: no release construction, no
# setup, no service restart, no state write.
if ((ACTIVATION_REQUIRED == 0)) && ((PENDING_STATUS == 3)); then
  echo "SKIPPED: Hermes release $FINAL_CARRIED_COMMIT is already selected"
  exit 0
fi

# The installed launcher must be the managed launcher for exactly this
# HERMES_AGENT_REPO layout. Otherwise the selector this run swaps is not the
# selector the installed launcher reads, and restarting services would hand
# control to something unified-kanban never produced.
[[ -f "$HERMES_LAUNCHER" && ! -L "$HERMES_LAUNCHER" ]] || {
  echo "managed Hermes launcher is missing: $HERMES_LAUNCHER" >&2
  echo "run ./scripts/setup.sh first" >&2
  exit 1
}
set +e
python3 "$REPO_ROOT/scripts/hermes-release-manager.py" launcher-baseline \
  "$AGENT_REPO" "$SUPPORTED_UPSTREAM" "$FINAL_CARRIED_COMMIT" "$HERMES_LAUNCHER" \
  >/dev/null
LAUNCHER_STATUS=$?
set -e
case "$LAUNCHER_STATUS" in
  0) ;;
  # Exit 3 is the release manager's "provably not our launcher" verdict; any
  # other failure is an internal fault and must not be read as a verdict.
  3)
    echo "refusing to activate a release for a Hermes launcher unified-kanban did not install: $HERMES_LAUNCHER" >&2
    echo "run ./scripts/setup.sh first" >&2
    exit 1 ;;
  *)
    echo "could not verify the installed Hermes launcher: $HERMES_LAUNCHER" >&2
    exit 1 ;;
esac

if ((ACTIVATION_REQUIRED)); then
  echo "Activating reviewed Hermes release $FINAL_CARRIED_COMMIT"
  observe_newer_upstream
else
  echo "RESUMING: completing the interrupted activation of $FINAL_CARRIED_COMMIT"
fi

# prepare is idempotent: it builds the release when absent and otherwise
# verifies the existing release exactly - source tree, Git metadata, dependency
# inventory, and completion receipt - before it may be reused.
HERMES_RELEASE="$(python3 "$REPO_ROOT/scripts/hermes-release-manager.py" prepare \
  "$AGENT_REPO" "$SUPPORTED_UPSTREAM" "$FINAL_CARRIED_COMMIT" "$CARRIED_BUNDLE" \
  --uv "$REAL_UV" --npm "$NPM_BIN")"
python3 "$REPO_ROOT/scripts/verify-carried-bundle.py" --hermes-repo "$HERMES_RELEASE"
[[ "$HERMES_RELEASE" == "$TARGET_RELEASE" ]] || {
  echo "prepared release is not the reviewed target: $HERMES_RELEASE" >&2
  exit 1
}

# A dashboard that is running now must be moved onto the new release, and a
# dashboard that was running before an interrupted run must come back.
DASHBOARD_WAS_RUNNING=0
if [[ "$PENDING_DASHBOARD" == 1 ]] || dashboard_ready; then
  DASHBOARD_WAS_RUNNING=1
fi

# The pending marker is published before the selector moves. A crash after the
# swap, or after the swap and before the services are restarted, is recovered by
# the next run instead of being reported as an up-to-date installation.
PENDING_RECEIPT="$(python3 "$REPO_ROOT/scripts/update-state.py" write pending \
  "$PENDING_FILE" "$FINAL_CARRIED_COMMIT" "$DASHBOARD_WAS_RUNNING")"

TRANSACTION_DIR="$(python3 - "$STATE_DIR" <<'PY'
import os, secrets, sys
from pathlib import Path

root = Path(sys.argv[1])
for _ in range(16):
    candidate = root / f"unified-kanban-update.{secrets.token_hex(16)}"
    try:
        os.mkdir(candidate, 0o700)
    except FileExistsError:
        continue
    print(candidate)
    break
else:
    raise SystemExit("could not allocate a private update transaction directory")
PY
)"
exec 9<"$TRANSACTION_DIR"
(umask 077 && : >"$TRANSACTION_DIR/token-ledger")
exec 10<>"$TRANSACTION_DIR/token-ledger"
export UNIFIED_KANBAN_TRANSACTION_DIR="$TRANSACTION_DIR"
export UNIFIED_KANBAN_TRANSACTION_FD=9
export UNIFIED_KANBAN_TOKEN_LEDGER_FD=10
RECEIPT_PATH="$TRANSACTION_DIR/receipt.json"
PRIOR_SELECTOR="$TRANSACTION_DIR/hermes-release-selector.before"
PRIOR_GATEWAY_PLIST="$TRANSACTION_DIR/ai.hermes.gateway.before.plist"
trap finish_update EXIT
# A resumed interrupted activation may have published the selector but not the
# plist. Bind both paths from the transaction's opening snapshot on every
# non-skipped completion attempt. The trap only receives rollback authority
# after both retained opening capabilities have been exported successfully.
TRANSACTION_TOKEN="$(python3 "$REPO_ROOT/scripts/path-transaction.py" begin \
  "$RECEIPT_PATH" "$HERMES_RELEASE_SELECTOR" "$HERMES_RELEASE_PREVIOUS" \
  "$GATEWAY_PLIST")"
python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
  "$RECEIPT_PATH" "$HERMES_RELEASE_SELECTOR" "$PRIOR_SELECTOR"
python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
  "$RECEIPT_PATH" "$GATEWAY_PLIST" "$PRIOR_GATEWAY_PLIST"
TRANSACTION_RECEIPT="$RECEIPT_PATH"
if ((ACTIVATION_REQUIRED)); then
  # Publish rollback authority before moving current. A crash after current moves
  # therefore cannot leave the prior known-good release undiscoverable.
  if [[ -n "$SELECTED_RELEASE" && "$SELECTED_RELEASE" != "$TARGET_RELEASE" ]]; then
    PREVIOUS_CANDIDATE="$TRANSACTION_DIR/hermes-release-previous"
    python3 "$REPO_ROOT/scripts/hermes-release-manager.py" render-reference \
      "$AGENT_REPO" "$SELECTED_RELEASE" "$PREVIOUS_CANDIDATE" >/dev/null
    next_stage_receipt
    python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
      "$TRANSACTION_RECEIPT" "$PREVIOUS_CANDIDATE" \
      "$HERMES_RELEASE_PREVIOUS" "$STAGE_RECEIPT" --absent-mode 0600
    checkpoint_stage
  fi
  SELECTOR_CANDIDATE="$TRANSACTION_DIR/hermes-release-selector"
  python3 "$REPO_ROOT/scripts/hermes-release-manager.py" render-selector \
    "$AGENT_REPO" "$SUPPORTED_UPSTREAM" "$FINAL_CARRIED_COMMIT" \
    "$SELECTOR_CANDIDATE" >/dev/null
  next_stage_receipt
  python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
    "$TRANSACTION_RECEIPT" "$SELECTOR_CANDIDATE" \
    "$HERMES_RELEASE_SELECTOR" "$STAGE_RECEIPT" --absent-mode 0600
  checkpoint_stage
fi

GATEWAY_PLIST_GENERATED="$TRANSACTION_DIR/ai.hermes.gateway.generated.plist"
GATEWAY_PLIST_CANDIDATE="$TRANSACTION_DIR/ai.hermes.gateway.hardened.plist"
run_scrubbed "$HERMES_RELEASE/venv/bin/python" -c \
  'from hermes_cli.gateway import generate_launchd_plist; print(generate_launchd_plist(), end="")' \
  >"$GATEWAY_PLIST_GENERATED"
chmod 0600 "$GATEWAY_PLIST_GENERATED"
run_scrubbed python3 "$REPO_ROOT/scripts/harden-macos-gateway-plist.py" \
  "$GATEWAY_PLIST_GENERATED" "$GATEWAY_PLIST_CANDIDATE" "$HERMES_RELEASE_ROOT"
next_stage_receipt
python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
  "$TRANSACTION_RECEIPT" "$GATEWAY_PLIST_CANDIDATE" \
  "$GATEWAY_PLIST" "$STAGE_RECEIPT" --absent-mode 0600 --target-mode 0600
checkpoint_stage

restart_services || {
  echo "Hermes services did not come back on the new release" >&2
  exit 1
}

if ((NO_RESTART)); then
  # The activation is complete but the services still run the previous release,
  # so the pending marker stays and the next ordinary run finishes the restart.
  echo "Hermes release $FINAL_CARRIED_COMMIT is selected at $TARGET_RELEASE"
  echo "Services were left untouched; rerun without --no-restart to restart them"
else
  # The activation and the restart both completed. Failing to clear the marker
  # afterwards is a cleanup fault, not an incomplete update, so it must not undo
  # a committed activation; the worst it costs is one redundant restart later.
  if python3 "$REPO_ROOT/scripts/update-state.py" remove pending \
    "$PENDING_FILE" "$PENDING_RECEIPT"; then
    PENDING_RECEIPT=""
  else
    echo "activation completed but the pending marker could not be cleared; the next run will restart services again" >&2
  fi
  echo "Hermes release $FINAL_CARRIED_COMMIT is active at $TARGET_RELEASE"
fi
