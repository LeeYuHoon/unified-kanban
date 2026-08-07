#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CARRIED_COMMITS_FILE="${HERMES_CARRIED_COMMITS_FILE:-$REPO_ROOT/patches/hermes-agent-carried-commits}"
CARRIED_BUNDLE="${HERMES_CARRIED_BUNDLE:-$REPO_ROOT/patches/hermes-agent-carried.bundle}"
STATE_DIR="$HERMES_HOME/state"
STATE_FILE="$STATE_DIR/hermes-kanban-last-applied-sha"
PENDING_FILE="$STATE_DIR/hermes-kanban-update.pending"
LOCK_DIR="$STATE_DIR/hermes-kanban-update.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
DASHBOARD_HOST="${HERMES_DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-9119}"
DASHBOARD_READY_ATTEMPTS="${HERMES_DASHBOARD_READY_ATTEMPTS:-20}"
# A dashboard bound to all interfaces cannot be probed at its bind address.
PROBE_HOST="$DASHBOARD_HOST"
if [[ "$DASHBOARD_HOST" == "0.0.0.0" ]]; then
  PROBE_HOST="127.0.0.1"
fi
CHECK=0
PREPARE_ONLY=0
LOCK_HELD=0

while (($#)); do
  case "$1" in
    --check) CHECK=1 ;;
    --prepare-only) PREPARE_ONLY=1 ;;
    # Backward-compatible no-op: dirty checkouts are never safe to update.
    --force) ;;
    -h|--help) echo "Usage: ./scripts/update-hermes-if-needed.sh [--check] [--prepare-only]"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v git >/dev/null 2>&1 || {
  echo "git is required" >&2
  exit 1
}
command -v hermes >/dev/null 2>&1 || {
  echo "hermes CLI is required" >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  echo "curl is required" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required" >&2
  exit 1
}
if ! SUPPORTED_UPSTREAM="$(PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
  'from kanban_adapter.compatibility import read_supported_upstream; print(read_supported_upstream())')"; then
  echo "Unable to trust the repository Hermes upstream pin." >&2
  exit 1
fi
[[ -d "$AGENT_REPO" ]] || {
  echo "Hermes Agent checkout is missing: $AGENT_REPO" >&2
  exit 1
}

if [[ "$CHECK" == 1 ]]; then
  echo "Fetching origin main into $AGENT_REPO"
  git -C "$AGENT_REPO" fetch origin main
  if ! TARGET_SHA="$(git -C "$AGENT_REPO" rev-parse origin/main)"; then
    echo "could not resolve origin/main while checking supported upstream" >&2
    exit 1
  fi
  if [[ "$TARGET_SHA" != "$SUPPORTED_UPSTREAM" ]]; then
    echo "Refusing unsupported Hermes upstream $TARGET_SHA; unified-kanban requires $SUPPORTED_UPSTREAM." >&2
    exit 1
  fi
  BEHIND="$(git -C "$AGENT_REPO" rev-list --count HEAD..origin/main)"
  if [[ "$BEHIND" == 0 ]]; then
    echo "UP_TO_DATE"
  else
    echo "UPDATE_AVAILABLE: $BEHIND commits behind origin/main"
  fi
  exit 0
fi

release_lock() {
  if [[ "$LOCK_HELD" == 1 ]]; then
    rm -rf "$LOCK_DIR"
  fi
}
trap release_lock EXIT
trap 'exit 129' INT
trap 'exit 143' TERM

acquire_lock() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    OWNER_PID="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
    if [[ -z "$OWNER_PID" ]] || kill -0 "$OWNER_PID" 2>/dev/null; then
      echo "another update holds the lock: $LOCK_DIR${OWNER_PID:+ (pid $OWNER_PID)}" >&2
      exit 1
    fi
    echo "Removing stale lock left by dead pid $OWNER_PID"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || {
      echo "another update holds the lock: $LOCK_DIR" >&2
      exit 1
    }
  fi
  LOCK_HELD=1
  printf '%s\n' "$$" > "$LOCK_PID_FILE"
}

dashboard_ready() {
  curl --fail --silent --show-error --max-time 2 \
    "http://$PROBE_HOST:$DASHBOARD_PORT/api/status" >/dev/null 2>&1
}

stop_dashboard() {
  echo "Stopping Dashboard before restart"
  hermes dashboard --stop || echo "Dashboard was already stopped"
  STOPPED=0
  for ((attempt = 1; attempt <= DASHBOARD_READY_ATTEMPTS; attempt++)); do
    if ! dashboard_ready; then
      STOPPED=1
      break
    fi
    sleep 0.25
  done
  [[ "$STOPPED" == 1 ]] || {
    echo "Dashboard did not stop on $PROBE_HOST:$DASHBOARD_PORT" >&2
    exit 1
  }
}

start_dashboard() {
  echo "Starting Dashboard on $DASHBOARD_HOST:$DASHBOARD_PORT"
  mkdir -p "$HERMES_HOME/logs"
  nohup env \
    -u HERMES_DELEGATED_CHILD_CONTEXT \
    -u HERMES_KANBAN_TASK \
    -u HERMES_KANBAN_WORKSPACE \
    -u HERMES_KANBAN_BOARD \
    -u HERMES_SESSION_ID \
    hermes dashboard \
    --host "$DASHBOARD_HOST" \
    --port "$DASHBOARD_PORT" \
    --no-open \
    --skip-build \
    >> "$HERMES_HOME/logs/dashboard.log" 2>&1 </dev/null &
  DASHBOARD_READY=0
  for ((attempt = 1; attempt <= DASHBOARD_READY_ATTEMPTS; attempt++)); do
    if dashboard_ready; then
      DASHBOARD_READY=1
      break
    fi
    sleep 0.25
  done
  [[ "$DASHBOARD_READY" == 1 ]] || {
    echo "Dashboard restart failed on $DASHBOARD_HOST:$DASHBOARD_PORT" >&2
    exit 1
  }
}

carried_commit_status() {
  local commit="$1" output sign sha matched=""
  output="$(git -C "$AGENT_REPO" cherry HEAD "$commit")" || return 1
  if [[ -z "$output" ]]; then
    printf '%s\n' '-'
    return 0
  fi
  while read -r sign sha; do
    [[ -n "$sha" ]] || continue
    if [[ "$sha" == "$commit"* || "$commit" == "$sha"* ]]; then
      [[ -z "$matched" ]] || {
        echo "ambiguous carried commit status for $commit: $output" >&2
        return 1
      }
      matched="$sign"
    fi
  done <<< "$output"
  [[ "$matched" == "+" || "$matched" == "-" ]] || {
    echo "could not determine carried commit status for $commit: $output" >&2
    return 1
  }
  printf '%s\n' "$matched"
}

ensure_carried_commit_objects() {
  [[ -f "$CARRIED_COMMITS_FILE" ]] || return 0
  local commit missing=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    commit="${line%%#*}"
    commit="${commit//[[:space:]]/}"
    [[ -n "$commit" ]] || continue
    if ! git -C "$AGENT_REPO" cat-file -e "$commit^{commit}" 2>/dev/null; then
      missing=1
      break
    fi
  done < "$CARRIED_COMMITS_FILE"
  ((missing)) || return 0

  [[ -f "$CARRIED_BUNDLE" && ! -L "$CARRIED_BUNDLE" ]] || {
    echo "required Hermes carried commits are unavailable and bundle is missing: $CARRIED_BUNDLE" >&2
    exit 1
  }
  echo "Importing repository-contained Hermes carried commits"
  git -C "$AGENT_REPO" fetch "$CARRIED_BUNDLE" \
    '+refs/heads/*:refs/unified-kanban/carried/*'

  while IFS= read -r line || [[ -n "$line" ]]; do
    commit="${line%%#*}"
    commit="${commit//[[:space:]]/}"
    [[ -n "$commit" ]] || continue
    git -C "$AGENT_REPO" cat-file -e "$commit^{commit}" 2>/dev/null || {
      echo "carried commit is absent after bundle import: $commit" >&2
      exit 1
    }
  done < "$CARRIED_COMMITS_FILE"
}

apply_carried_commits() {
  [[ -f "$CARRIED_COMMITS_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    commit="${line%%#*}"
    commit="${commit//[[:space:]]/}"
    [[ -n "$commit" ]] || continue
    CHERRY_STATUS="$(carried_commit_status "$commit")" || exit 1
    case "$CHERRY_STATUS" in
      "+")
        echo "Reapplying carried Hermes commit $commit"
        git -C "$AGENT_REPO" cherry-pick "$commit"
        ;;
      "-")
        echo "Carried Hermes commit already applied: $commit"
        ;;
    esac
  done < "$CARRIED_COMMITS_FILE"
}

carried_commits_need_repair() {
  [[ -f "$CARRIED_COMMITS_FILE" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    commit="${line%%#*}"
    commit="${commit//[[:space:]]/}"
    [[ -n "$commit" ]] || continue
    CHERRY_STATUS="$(carried_commit_status "$commit")" || exit 1
    case "$CHERRY_STATUS" in
      "+") return 0 ;;
      "-") ;;
    esac
  done < "$CARRIED_COMMITS_FILE"
  return 1
}

applied_state_needs_repair() {
  [[ -f "$STATE_FILE" ]] || return 1
  local applied_sha current_sha
  applied_sha="$(sed -n '1p' "$STATE_FILE")"
  applied_sha="${applied_sha%$'\r'}"
  if ! current_sha="$(git -C "$AGENT_REPO" rev-parse origin/main)"; then
    echo "could not resolve origin/main while checking applied update state" >&2
    exit 1
  fi
  [[ "$applied_sha" != "$current_sha" ]]
}

refuse_dirty_checkout() {
  if [[ -n "$(git -C "$AGENT_REPO" status --porcelain)" ]]; then
    echo "refusing dirty Hermes checkout: $AGENT_REPO" >&2
    exit 1
  fi
}

# Everything that must happen after the Hermes checkout reaches origin/main.
# The pending marker survives any failure here so a rerun resumes the repair;
# the applied SHA is recorded only once all of it has succeeded.
run_post_update_repair() {
  apply_carried_commits
  if ((PREPARE_ONLY)); then
    echo "Hermes checkout prepared; returning to unified-kanban setup"
  else
    echo "Re-running unified-kanban setup"
    "$REPO_ROOT/scripts/setup.sh" --skip-smoke
    if [[ "$DASHBOARD_WAS_RUNNING" == 1 ]]; then
      stop_dashboard
      start_dashboard
    else
      echo "Dashboard was not running; leaving it stopped"
    fi
  fi
  APPLIED_SHA="$(git -C "$AGENT_REPO" rev-parse origin/main)"
  printf '%s\n' "$APPLIED_SHA" > "$STATE_FILE.tmp.$$"
  mv "$STATE_FILE.tmp.$$" "$STATE_FILE"
  rm -f "$PENDING_FILE"
  echo "Recorded applied SHA in $STATE_FILE"
  echo "Hermes Agent updated to origin/main ($APPLIED_SHA)"
}

mkdir -p "$STATE_DIR"
acquire_lock

echo "Fetching origin main into $AGENT_REPO"
git -C "$AGENT_REPO" fetch origin main
if ! TARGET_SHA="$(git -C "$AGENT_REPO" rev-parse origin/main)"; then
  echo "could not resolve origin/main while checking supported upstream" >&2
  exit 1
fi
if [[ "$TARGET_SHA" != "$SUPPORTED_UPSTREAM" ]]; then
  echo "Refusing unsupported Hermes upstream $TARGET_SHA; unified-kanban requires $SUPPORTED_UPSTREAM." >&2
  exit 1
fi
ensure_carried_commit_objects
BEHIND="$(git -C "$AGENT_REPO" rev-list --count HEAD..origin/main)"

if [[ "$BEHIND" == 0 ]]; then
  if [[ -f "$PENDING_FILE" ]]; then
    refuse_dirty_checkout
    PENDING_SHA="$(sed -n '1p' "$PENDING_FILE")"
    DASHBOARD_WAS_RUNNING="$(sed -n '2p' "$PENDING_FILE")"
    [[ "$DASHBOARD_WAS_RUNNING" == 1 ]] || DASHBOARD_WAS_RUNNING=0
    echo "RESUMING: completing interrupted update ($PENDING_SHA)"
    run_post_update_repair
    exit 0
  fi
  if carried_commits_need_repair || applied_state_needs_repair; then
    refuse_dirty_checkout
    DASHBOARD_WAS_RUNNING=0
    if dashboard_ready; then
      DASHBOARD_WAS_RUNNING=1
    fi
    echo "REPAIRING: Hermes matches origin/main but post-update state is incomplete"
    run_post_update_repair
    exit 0
  fi
  echo "SKIPPED: Hermes Agent already matches origin/main"
  exit 0
fi
echo "Hermes Agent is $BEHIND commits behind origin/main"

refuse_dirty_checkout

DASHBOARD_WAS_RUNNING=0
if dashboard_ready; then
  DASHBOARD_WAS_RUNNING=1
fi

printf '%s\n%s\n' "$TARGET_SHA" "$DASHBOARD_WAS_RUNNING" > "$PENDING_FILE.tmp.$$"
mv "$PENDING_FILE.tmp.$$" "$PENDING_FILE"

echo "Updating Hermes Agent to $TARGET_SHA"
hermes update --yes --branch main

REMAINING="$(git -C "$AGENT_REPO" rev-list --count HEAD..origin/main)"
[[ "$REMAINING" == 0 ]] || {
  echo "update verification failed: still $REMAINING commits behind origin/main" >&2
  exit 1
}

run_post_update_repair
