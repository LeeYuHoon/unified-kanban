#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ORIGINAL_ARGS=("$@")
DRY_RUN=0
SKIP_SMOKE=0
NO_RESTART=0
PROJECT_DIR=""
BOARD=""
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/unified-kanban"
PROJECTS_STATE="$STATE_DIR/managed-projects.json"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
CLAUDE_HOOK_LINK="$HOME/.local/bin/claude-kanban-hook"
CODEX_SETTINGS="$HOME/.codex/hooks.json"
CODEX_HOOK_LINK="$HOME/.local/bin/codex-kanban-hook"
SESSION_VIEWER_LINK="$HOME/.local/bin/ai-session-viewer"

usage() {
  cat <<'EOF'
Usage: ./scripts/setup.sh [options]
  --dry-run                 Print actions without writing
  --skip-smoke              Do not run the smoke test
  --no-restart              Do not restart Hermes gateway
  --project-dir ABS_PATH    Project directory to configure
  --board SLUG              Board for --project-dir (required together)
EOF
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --skip-smoke) SKIP_SMOKE=1 ;;
    --no-restart) NO_RESTART=1 ;;
    --project-dir)
      if (($# < 2)) || [[ -z "$2" || "$2" == --* ]]; then
        echo "Missing value for --project-dir" >&2; exit 2
      fi
      PROJECT_DIR="$2"; shift 2; continue ;;
    --board)
      if (($# < 2)) || [[ -z "$2" || "$2" == --* ]]; then
        echo "Missing value for --board" >&2; exit 2
      fi
      BOARD="$2"; shift 2; continue ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ -n "$PROJECT_DIR" || -n "$BOARD" ]]; then
  if [[ -z "$PROJECT_DIR" || -z "$BOARD" ]]; then
    echo "--project-dir and --board must be provided together" >&2
    exit 2
  fi
  if [[ "$PROJECT_DIR" != /* ]]; then
    echo "--project-dir must be an absolute path" >&2
    exit 2
  fi
  if [[ ! "$BOARD" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || ((${#BOARD} > 64)); then
    echo "Invalid board slug: use 1-64 letters, digits, dot, underscore, or hyphen" >&2
    exit 2
  fi
  if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "Project directory does not exist: $PROJECT_DIR" >&2
    exit 1
  fi
  PROJECT_DIR="$(cd -P -- "$PROJECT_DIR" && pwd)"
fi

for cmd in python3 hermes git uv; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "Missing required command: $cmd" >&2; exit 1; }
done
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Python 3.11 or newer is required" >&2
  exit 1
}
hermes --version >/dev/null
has_help_token() {
  local text="$1" expected="$2"
  printf '%s\n' "$text" | python3 -c '
import re, sys
expected = sys.argv[1]
tokens = set(re.findall(r"--[A-Za-z0-9][A-Za-z0-9-]*|[A-Za-z_][A-Za-z0-9_-]*", sys.stdin.read()))
raise SystemExit(0 if expected in tokens else 1)
' "$expected"
}
validate_kanban_cli_contract() {
  local kanban_help create_help boards_list_help boards_create_help
  local comment_help complete_help block_help show_help archive_help
  kanban_help="$(hermes kanban --help)"
  create_help="$(hermes kanban create --help)"
  boards_list_help="$(hermes kanban boards list --help)"
  boards_create_help="$(hermes kanban boards create --help)"
  comment_help="$(hermes kanban comment --help)"
  complete_help="$(hermes kanban complete --help)"
  block_help="$(hermes kanban block --help)"
  show_help="$(hermes kanban show --help)"
  archive_help="$(hermes kanban archive --help)"
  has_help_token "$kanban_help" "--board" \
    && has_help_token "$create_help" "--assignee" \
    && has_help_token "$create_help" "--tenant" \
    && has_help_token "$create_help" "--created-by" \
    && has_help_token "$create_help" "--initial-status" \
    && has_help_token "$create_help" "--observation" \
    && has_help_token "$create_help" "--idempotency-key" \
    && has_help_token "$create_help" "--title-file" \
    && has_help_token "$create_help" "--json" \
    && has_help_token "$boards_list_help" "--json" \
    && has_help_token "$boards_create_help" "--name" \
    && has_help_token "$comment_help" "--author" \
    && has_help_token "$comment_help" "--idempotency-key" \
    && has_help_token "$complete_help" "--summary" \
    && has_help_token "$block_help" "--kind" \
    && has_help_token "$show_help" "task_id" \
    && has_help_token "$archive_help" "task_ids" || {
    echo "Installed Hermes Kanban CLI is incompatible with unified-kanban" >&2
    exit 1
  }
}

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
[[ "$HERMES_HOME" == /* ]] || {
  echo "HERMES_HOME must be an absolute path: $HERMES_HOME" >&2
  exit 1
}
HERMES_CONFIG="$HERMES_HOME/config.yaml"
HERMES_PLUGIN_SOURCE="$REPO_ROOT/integrations/hermes/hermes-kanban"
HERMES_PLUGIN_TARGET="$HERMES_HOME/plugins/hermes-kanban"
LEGACY_HERMES_PLUGIN_SOURCE="$(dirname "$REPO_ROOT")/hermes-kanban/plugin/hermes-kanban"
AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
[[ "$AGENT_REPO" == /* ]] || {
  echo "HERMES_AGENT_REPO must be an absolute path: $AGENT_REPO" >&2
  exit 1
}
# Derive one normal form before anything is computed from it. Concatenating
# ".releases" onto a denormalized path would name a hidden directory inside the
# Hermes checkout while every Python caller keeps using the sibling, so the
# launcher would read a selector setup never wrote.
AGENT_REPO="$(PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m kanban_adapter.release_layout "$AGENT_REPO")" || exit 1
HERMES_RELEASE_ROOT="${AGENT_REPO}.releases"
HERMES_RELEASE_SELECTOR="$HERMES_RELEASE_ROOT/current"
HERMES_LAUNCHER="$HOME/.local/bin/hermes"
HERMES_LAUNCHER_BACKUP="$STATE_DIR/hermes-launcher.before-unified-kanban"
GATEWAY_PLIST="$HOME/Library/LaunchAgents/ai.hermes.gateway.plist"
CARRIED_COMMITS_FILE="$REPO_ROOT/patches/hermes-agent-carried-commits"
CARRIED_BUNDLE="$REPO_ROOT/patches/hermes-agent-carried.bundle"
EXPECTED_UPSTREAM_FILE="$REPO_ROOT/patches/hermes-agent-supported-upstream"
REAL_GIT="$(command -v git)"
REAL_UV="$(command -v uv)"
NPM_BIN="${HERMES_NPM_BIN:-$(command -v npm 2>/dev/null || true)}"
[[ -n "$NPM_BIN" && "$NPM_BIN" == /* ]] || {
  echo "An absolute npm executable is required to build the sealed Dashboard" >&2
  exit 1
}
HERMES_PLUGIN_ENABLED=1
TRANSACTION_RECEIPT=""
TRANSACTION_TOKEN=""
TRANSACTION_DIR=""
TRANSACTION_STAGE=0
STAGE_RECEIPT=""
GATEWAY_RESTARTED=0

if [[ ! -d "$AGENT_REPO" ]]; then
  echo "Hermes Agent checkout not found at $AGENT_REPO; setup cannot verify compatibility." >&2
  exit 1
else

  [[ -f "$HERMES_PLUGIN_SOURCE/plugin.yaml" && -f "$HERMES_PLUGIN_SOURCE/__init__.py" ]] || {
    echo "repository Hermes plugin files are missing: $HERMES_PLUGIN_SOURCE" >&2
    exit 1
  }
  if ! SUPPORTED_UPSTREAM="$(PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
    'from kanban_adapter.compatibility import read_supported_upstream; print(read_supported_upstream())')"; then
    echo "Unable to trust the repository Hermes upstream pin: $EXPECTED_UPSTREAM_FILE" >&2
    exit 1
  fi
  FINAL_CARRIED_COMMIT="$(python3 - "$CARRIED_COMMITS_FILE" <<'PY'
from pathlib import Path
import re, sys
values=[]
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    value=raw.split("#",1)[0].strip()
    if value:
        if re.fullmatch(r"[0-9a-f]{40}",value) is None:
            raise SystemExit("invalid carried commit manifest")
        values.append(value)
if not values:
    raise SystemExit("empty carried commit manifest")
print(values[-1])
PY
)"
  TARGET_RELEASE="$HERMES_RELEASE_ROOT/release-$FINAL_CARRIED_COMMIT"
  if ((DRY_RUN)); then
    echo "DRY RUN: would prepare immutable Hermes release $FINAL_CARRIED_COMMIT"
    HERMES_CLI="$(command -v hermes)"
  else
    HERMES_RELEASE="$(python3 "$REPO_ROOT/scripts/hermes-release-manager.py" prepare \
      "$AGENT_REPO" "$SUPPORTED_UPSTREAM" "$FINAL_CARRIED_COMMIT" "$CARRIED_BUNDLE" \
      --uv "$REAL_UV" --npm "$NPM_BIN")"
    # The release the producer built and the release this shell will select must
    # be the same path, proven before anything on the host is mutated.
    [[ "$HERMES_RELEASE" == "$TARGET_RELEASE" ]] || {
      echo "prepared release is not the reviewed target: $HERMES_RELEASE" >&2
      exit 1
    }
    python3 "$REPO_ROOT/scripts/verify-carried-bundle.py" --hermes-repo "$HERMES_RELEASE"
    HERMES_CLI="$HERMES_RELEASE/venv/bin/hermes"
  fi
fi

hermes() {
  "$HERMES_CLI" "$@"
}

validate_kanban_cli_contract

run() {
  if ((DRY_RUN)); then
    printf 'DRY RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

finish_setup_transaction() {
  local status=$? rollback_failed=0
  trap - EXIT
  if [[ -n "$TRANSACTION_RECEIPT" ]]; then
    if ((status == 0)) \
      && python3 "$REPO_ROOT/scripts/path-transaction.py" commit \
        "$TRANSACTION_RECEIPT" "$TRANSACTION_TOKEN"; then
      :
    else
      if ((status == 0)); then
        echo "setup transaction receipt commit failed; rolling back" >&2
        status=1
      fi
      python3 "$REPO_ROOT/scripts/path-transaction.py" rollback-ledger \
        "$TRANSACTION_RECEIPT" 10 \
        || rollback_failed=1
      if ((GATEWAY_RESTARTED && rollback_failed == 0)); then
        if [[ -f "$GATEWAY_PLIST" && ! -L "$GATEWAY_PLIST" && -x "$HERMES_LAUNCHER" ]]; then
          run_without_transaction_authority "$HERMES_LAUNCHER" gateway stop \
            >/dev/null 2>&1 || true
          run_without_transaction_authority "$HERMES_LAUNCHER" gateway start \
            >/dev/null 2>&1 \
            && ensure_macos_gateway_supervised "$HERMES_LAUNCHER" allow-unsealed-rollback \
              >/dev/null 2>&1 \
            || rollback_failed=1
        elif [[ ! -e "$GATEWAY_PLIST" && ! -L "$GATEWAY_PLIST" ]]; then
          run_without_transaction_authority "$HERMES_RELEASE/venv/bin/hermes" gateway uninstall \
            >/dev/null 2>&1 || rollback_failed=1
        else
          rollback_failed=1
        fi
      fi
      if ((rollback_failed)); then
        echo "setup rollback was incomplete because managed identities changed; foreign paths were preserved" >&2
        status=1
      fi
    fi
  fi
  if [[ -n "$TRANSACTION_DIR" ]]; then
    if ! python3 "$REPO_ROOT/scripts/path-transaction.py" cleanup-private-dir \
      "$TRANSACTION_DIR" 9; then
      echo "private setup transaction cleanup failed" >&2
      status=1
    fi
    exec 9<&-
    exec 10<&-
  fi
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

stage_hermes_plugin_config() {
  local action="$1" baseline="$TRANSACTION_DIR/hermes-config-before-$TRANSACTION_STAGE"
  local candidate="$TRANSACTION_DIR/hermes-config-candidate-$TRANSACTION_STAGE"
  python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
    "$TRANSACTION_RECEIPT" "$HERMES_CONFIG" "$baseline"
  python3 "$REPO_ROOT/scripts/stage-hermes-plugin-config.py" \
    9 "$baseline" "$candidate" "$HERMES_PLUGIN_SOURCE" "$action" "$HERMES_CLI"
  next_stage_receipt
  python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
    "$TRANSACTION_RECEIPT" "$candidate" "$HERMES_CONFIG" "$STAGE_RECEIPT"
  checkpoint_stage
}

assert_link_replaceable() {
  local source="$1" target="$2"
  if [[ -L "$target" ]]; then
    if [[ "$(readlink "$target")" != "$source" ]]; then
      echo "Refusing to replace foreign symlink: $target" >&2
      exit 1
    fi
  elif [[ -e "$target" ]]; then
    echo "Refusing to replace non-symlink: $target" >&2
    exit 1
  fi
}

link_repo_file() {
  local source="$1" target="$2"
  assert_link_replaceable "$source" "$target"
  if ((DRY_RUN)); then
    run mkdir -p "$(dirname "$target")"
    run python3 "$REPO_ROOT/scripts/manage_repo_link.py" install "$source" "$target"
  else
    next_stage_receipt
    python3 "$REPO_ROOT/scripts/manage_repo_link.py" install "$source" "$target" \
      --receipt "$STAGE_RECEIPT"
    checkpoint_stage
  fi
}

assert_hermes_plugin_installable() {
  if [[ -L "$HERMES_PLUGIN_TARGET" ]]; then
    local actual
    actual="$(readlink "$HERMES_PLUGIN_TARGET")"
    if [[ "$actual" != "$HERMES_PLUGIN_SOURCE" && "$actual" != "$LEGACY_HERMES_PLUGIN_SOURCE" ]]; then
      echo "Refusing foreign Hermes plugin symlink: $HERMES_PLUGIN_TARGET -> $actual" >&2
      exit 1
    fi
  elif [[ -e "$HERMES_PLUGIN_TARGET" ]]; then
    echo "Refusing existing non-managed Hermes plugin path: $HERMES_PLUGIN_TARGET" >&2
    exit 1
  fi
}

if [[ -n "$PROJECT_DIR" ]]; then
  ENVRC="$PROJECT_DIR/.envrc"
  python3 - "$ENVRC" "$PROJECTS_STATE" <<'PY'
from pathlib import Path
import json, os, stat, sys


def read_regular(path, label):
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError(f"{label} changed during validation")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(fd)


envrc = Path(sys.argv[1])
state_file = Path(sys.argv[2])
envrc_bytes = read_regular(envrc, "project .envrc")
if envrc_bytes is not None:
    envrc_bytes.decode("utf-8")
state_bytes = read_regular(state_file, "managed-projects state")
if state_bytes is not None:
    projects = json.loads(state_bytes.decode("utf-8"))
    if not isinstance(projects, list):
        raise ValueError("managed-projects state must be a JSON list")
    if any(
        not isinstance(item, str) or not item or not Path(item).is_absolute()
        for item in projects
    ):
        raise ValueError("managed-projects entries must be non-empty absolute path strings")
    if len(set(projects)) != len(projects):
        raise ValueError("managed-projects entries must be unique")
PY
  BOARDS_JSON="$(hermes kanban boards list --json)"
  if python3 - "$BOARD" "$BOARDS_JSON" <<'PY'
import json, sys
slug, raw = sys.argv[1:]
try:
    boards = json.loads(raw)
except json.JSONDecodeError as exc:
    print(f"Invalid Hermes board JSON: {exc}", file=sys.stderr)
    raise SystemExit(2)
if not isinstance(boards, list) or any(
    not isinstance(item, dict) or not isinstance(item.get("slug"), str) or not item["slug"]
    for item in boards
):
    print("Invalid Hermes board list schema", file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(0 if any(item["slug"] == slug for item in boards) else 1)
PY
  then
    :
  else
    BOARD_LOOKUP_RC=$?
    if ((BOARD_LOOKUP_RC == 1)); then
      echo "Hermes board does not exist: $BOARD" >&2
      echo "Create it before setup: hermes kanban boards create --name '$BOARD' '$BOARD'" >&2
      exit 1
    else
      echo "Refusing setup because Hermes board data is invalid" >&2
      exit 1
    fi
  fi
fi

if ((SKIP_SMOKE == 0)); then
  SMOKE_BOARD="${KANBAN_SMOKE_BOARD:-unified-kanban-smoke}"
  BOARDS_JSON="$(hermes kanban boards list --json)"
  if python3 - "$SMOKE_BOARD" "$BOARDS_JSON" <<'PY'
import json, sys
slug, raw = sys.argv[1:]
try:
    boards = json.loads(raw)
except json.JSONDecodeError as exc:
    print(f"Invalid Hermes board JSON: {exc}", file=sys.stderr)
    raise SystemExit(2)
if not isinstance(boards, list) or any(
    not isinstance(item, dict) or not isinstance(item.get("slug"), str) or not item["slug"]
    for item in boards
):
    print("Invalid Hermes board list schema", file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(0 if any(item["slug"] == slug for item in boards) else 1)
PY
  then
    :
  else
    BOARD_LOOKUP_RC=$?
    if ((BOARD_LOOKUP_RC == 1)); then
      echo "Hermes smoke board does not exist: $SMOKE_BOARD" >&2
      echo "Create it before setup: hermes kanban boards create --name 'Unified Kanban Smoke' '$SMOKE_BOARD'" >&2
      exit 1
    else
      echo "Refusing setup because Hermes board data is invalid" >&2
      exit 1
    fi
  fi
fi

if ((HERMES_PLUGIN_ENABLED)); then
  assert_hermes_plugin_installable
fi
assert_link_replaceable "$REPO_ROOT/bin/kanban-adapter" "$HOME/.local/bin/kanban-adapter"
assert_link_replaceable "$REPO_ROOT/bin/claude-kanban-hook" "$CLAUDE_HOOK_LINK"
assert_link_replaceable "$REPO_ROOT/bin/codex-kanban-hook" "$CODEX_HOOK_LINK"
assert_link_replaceable "$REPO_ROOT/bin/ai-session-viewer" "$SESSION_VIEWER_LINK"
run python3 "$REPO_ROOT/scripts/install-claude-hooks.py" batch-validate \
  "$CLAUDE_SETTINGS" "$CLAUDE_HOOK_LINK" \
  "$CODEX_SETTINGS" "$CODEX_HOOK_LINK"

if ((!DRY_RUN)); then
  if [[ "${UNIFIED_KANBAN_SETUP_TRANSACTION_CHILD:-}" != 1 ]]; then
    exec python3 "$REPO_ROOT/scripts/setup-transaction-runner.py" \
      "$REPO_ROOT" "$STATE_DIR" "$REPO_ROOT/scripts/setup.sh" \
      ${ORIGINAL_ARGS[@]+"${ORIGINAL_ARGS[@]}"}
  fi
  TRANSACTION_DIR="${UNIFIED_KANBAN_TRANSACTION_DIR:?missing retained transaction directory}"
  [[ "${UNIFIED_KANBAN_TRANSACTION_FD:-}" == 9 ]] \
    || { echo "invalid retained transaction directory fd" >&2; exit 1; }
  [[ "${UNIFIED_KANBAN_TOKEN_LEDGER_FD:-}" == 10 ]] \
    || { echo "invalid retained transaction ledger fd" >&2; exit 1; }
  TRANSACTION_RECEIPT="$TRANSACTION_DIR/manifest.json"
  trap finish_setup_transaction EXIT
  TRANSACTION_PATHS=(
    "$HOME/.local/bin/kanban-adapter"
    "$CLAUDE_HOOK_LINK"
    "$CODEX_HOOK_LINK"
    "$SESSION_VIEWER_LINK"
    "$CLAUDE_SETTINGS"
    "$CODEX_SETTINGS"
    "$HERMES_PLUGIN_TARGET"
    "$HERMES_CONFIG"
    "$PROJECTS_STATE"
    "$HERMES_RELEASE_SELECTOR"
    "$HERMES_LAUNCHER"
    "$HERMES_LAUNCHER_BACKUP"
    "$GATEWAY_PLIST"
  )
  if [[ -n "$PROJECT_DIR" ]]; then
    TRANSACTION_PATHS+=("$PROJECT_DIR/.envrc")
  fi
  TRANSACTION_TOKEN="$(python3 "$REPO_ROOT/scripts/path-transaction.py" begin \
    "$TRANSACTION_RECEIPT" "${TRANSACTION_PATHS[@]}")"

  # The managed launcher carries a producer-issued binding to the exact bytes
  # this install displaces, so uninstall can never adopt a foreign, injected, or
  # deleted launcher backup. Both branches derive the binding from the frozen
  # transaction snapshot rather than from a re-read of the live path.
  RETAINED_HERMES_BACKUP="$TRANSACTION_DIR/hermes-launcher-retained"
  python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
    "$TRANSACTION_RECEIPT" "$HERMES_LAUNCHER_BACKUP" "$RETAINED_HERMES_BACKUP"
  BASELINE_ARGS=(--baseline-absent)
  if [[ -f "$RETAINED_HERMES_BACKUP" ]]; then
    BASELINE_ARGS=(--baseline-file "$RETAINED_HERMES_BACKUP")
  else
    ORIGINAL_HERMES_LAUNCHER="$TRANSACTION_DIR/hermes-launcher-original"
    python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
      "$TRANSACTION_RECEIPT" "$HERMES_LAUNCHER" "$ORIGINAL_HERMES_LAUNCHER"
    if [[ -f "$ORIGINAL_HERMES_LAUNCHER" ]]; then
      # A rerun finds our own launcher here, not the user's. Retaining it would
      # record the managed launcher as the "original" and make uninstall restore
      # a launcher whose selector it just deleted, so ask the producer whether
      # this is one of ours before treating it as something worth preserving.
      # Exit 3 is the release manager's "provably not our launcher" verdict.
      # Anything else is an internal fault and must never be read as a verdict,
      # so it fails closed with the reason instead of silently retaining.
      LAUNCHER_CLASSIFY_ERROR="$TRANSACTION_DIR/hermes-launcher-classify-error"
      set +e
      INSTALLED_BASELINE="$(python3 "$REPO_ROOT/scripts/hermes-release-manager.py" \
        launcher-baseline "$AGENT_REPO" "$SUPPORTED_UPSTREAM" "$FINAL_CARRIED_COMMIT" \
        "$ORIGINAL_HERMES_LAUNCHER" 2>"$LAUNCHER_CLASSIFY_ERROR")"
      INSTALLED_BASELINE_STATUS=$?
      set -e
      if ((INSTALLED_BASELINE_STATUS != 0 && INSTALLED_BASELINE_STATUS != 3)); then
        echo "Unable to classify the existing Hermes launcher: $HERMES_LAUNCHER" >&2
        cat "$LAUNCHER_CLASSIFY_ERROR" >&2
        exit 1
      fi
      if ((INSTALLED_BASELINE_STATUS == 3)); then
        BASELINE_ARGS=(--baseline-file "$ORIGINAL_HERMES_LAUNCHER")
        next_stage_receipt
        python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
          "$TRANSACTION_RECEIPT" "$ORIGINAL_HERMES_LAUNCHER" \
          "$HERMES_LAUNCHER_BACKUP" "$STAGE_RECEIPT" --absent-mode 0600
        checkpoint_stage
      elif [[ "$INSTALLED_BASELINE" != absent ]]; then
        echo "Refusing to reinstall over a managed Hermes launcher whose retained original backup is missing: $HERMES_LAUNCHER_BACKUP" >&2
        echo "Restore that backup, or remove $HERMES_LAUNCHER, before running setup again." >&2
        exit 1
      fi
    fi
  fi
  SELECTOR_CANDIDATE="$TRANSACTION_DIR/hermes-release-selector"
  LAUNCHER_CANDIDATE="$TRANSACTION_DIR/hermes-release-launcher"
  python3 "$REPO_ROOT/scripts/hermes-release-manager.py" render \
    "$AGENT_REPO" "$SUPPORTED_UPSTREAM" "$FINAL_CARRIED_COMMIT" \
    "$SELECTOR_CANDIDATE" "$LAUNCHER_CANDIDATE" "${BASELINE_ARGS[@]}" >/dev/null
  next_stage_receipt
  python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
    "$TRANSACTION_RECEIPT" "$SELECTOR_CANDIDATE" \
    "$HERMES_RELEASE_SELECTOR" "$STAGE_RECEIPT" --absent-mode 0600
  checkpoint_stage
  next_stage_receipt
  python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
    "$TRANSACTION_RECEIPT" "$LAUNCHER_CANDIDATE" \
    "$HERMES_LAUNCHER" "$STAGE_RECEIPT" --absent-mode 0755
  checkpoint_stage
  HERMES_CLI="$HERMES_LAUNCHER"
fi

run_without_transaction_authority() {
  if ((DRY_RUN)); then
    run "$@"
    return
  fi
  (
    unset UNIFIED_KANBAN_TRANSACTION_DIR
    unset UNIFIED_KANBAN_TRANSACTION_FD
    unset UNIFIED_KANBAN_TOKEN_LEDGER_FD
    exec 9<&-
    exec 10<&-
    run "$@"
  )
}

ensure_macos_gateway_supervised() {
  local cli="$1" policy="${2:-sealed}" expected_release="${3:-}" attempt status_output
  if ((DRY_RUN)) || [[ "$(/usr/bin/uname -s)" != "Darwin" ]]; then
    return 0
  fi
  for attempt in 1 2 3 4 5; do
    status_output="$(run_without_transaction_authority "$cli" gateway status 2>&1 || true)"
    if [[ "$status_output" == *"Gateway is supervised by launchd"* ]]; then
      if [[ "$policy" == "allow-unsealed-rollback" ]]; then
        if run_without_transaction_authority python3 \
          "$REPO_ROOT/scripts/verify-macos-launchd-service.py" \
          "$HOME/Library/LaunchAgents/ai.hermes.gateway.plist" \
          --allow-unsealed-environment; then
          return 0
        fi
      elif [[ -n "$expected_release" ]] && run_without_transaction_authority python3 \
        "$REPO_ROOT/scripts/verify-macos-launchd-service.py" \
        "$HOME/Library/LaunchAgents/ai.hermes.gateway.plist" \
        --expected-release "$expected_release"; then
        return 0
      fi
    fi
    if ((attempt < 5)); then
      /bin/sleep "$attempt"
    fi
  done
  printf '%s\n' "$status_output" >&2
  echo "gateway activation failed: launchd did not supervise the reviewed immutable release" >&2
  return 1
}

if [[ -n "$PROJECT_DIR" ]]; then
  LINE="export HERMES_KANBAN_BOARD=$BOARD # unified-kanban"
  if ((DRY_RUN)); then
    echo "DRY RUN: ensure $ENVRC contains: $LINE"
    echo "DRY RUN: register $PROJECT_DIR in $PROJECTS_STATE"
  else
    STATE_BEFORE="$TRANSACTION_DIR/routing-state-before"
    ENVRC_BEFORE="$TRANSACTION_DIR/routing-envrc-before"
    STATE_CANDIDATE="$TRANSACTION_DIR/routing-state-candidate"
    ENVRC_CANDIDATE="$TRANSACTION_DIR/routing-envrc-candidate"
    python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
      "$TRANSACTION_RECEIPT" "$PROJECTS_STATE" "$STATE_BEFORE"
    python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
      "$TRANSACTION_RECEIPT" "$ENVRC" "$ENVRC_BEFORE"
    python3 "$REPO_ROOT/scripts/render-project-routing.py" register \
      "$STATE_BEFORE" "$ENVRC_BEFORE" "$PROJECT_DIR" "$LINE" \
      "$STATE_CANDIDATE" "$ENVRC_CANDIDATE"
    next_stage_receipt
    python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
      "$TRANSACTION_RECEIPT" "$STATE_CANDIDATE" "$PROJECTS_STATE" "$STAGE_RECEIPT"
    checkpoint_stage
    next_stage_receipt
    python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
      "$TRANSACTION_RECEIPT" "$ENVRC_CANDIDATE" "$ENVRC" "$STAGE_RECEIPT" \
      --absent-mode 0644
    checkpoint_stage
  fi
fi

# Install every executable before settings can reference it. If a link cannot
# be created, provider settings remain untouched instead of retaining a broken
# managed hook command.
link_repo_file "$REPO_ROOT/bin/kanban-adapter" "$HOME/.local/bin/kanban-adapter"
link_repo_file "$REPO_ROOT/bin/claude-kanban-hook" "$CLAUDE_HOOK_LINK"
link_repo_file "$REPO_ROOT/bin/codex-kanban-hook" "$CODEX_HOOK_LINK"
link_repo_file "$REPO_ROOT/bin/ai-session-viewer" "$SESSION_VIEWER_LINK"
if ((DRY_RUN)); then
  run python3 "$REPO_ROOT/scripts/install-claude-hooks.py" batch-install \
    "$CLAUDE_SETTINGS" "$CLAUDE_HOOK_LINK" \
    "$CODEX_SETTINGS" "$CODEX_HOOK_LINK"
else
  next_stage_receipt
  python3 "$REPO_ROOT/scripts/install-claude-hooks.py" install \
    "$CLAUDE_SETTINGS" "$CLAUDE_HOOK_LINK" claude --receipt "$STAGE_RECEIPT"
  checkpoint_stage
  next_stage_receipt
  python3 "$REPO_ROOT/scripts/install-claude-hooks.py" install \
    "$CODEX_SETTINGS" "$CODEX_HOOK_LINK" codex --receipt "$STAGE_RECEIPT"
  checkpoint_stage
fi

if ((HERMES_PLUGIN_ENABLED)); then
  assert_hermes_plugin_installable
  if [[ -L "$HERMES_PLUGIN_TARGET" && "$(readlink "$HERMES_PLUGIN_TARGET")" != "$HERMES_PLUGIN_SOURCE" ]]; then
    echo "Migrating legacy hermes-kanban plugin symlink: $(readlink "$HERMES_PLUGIN_TARGET") -> $HERMES_PLUGIN_SOURCE"
    if ((DRY_RUN)); then
      run python3 "$REPO_ROOT/scripts/manage_repo_link.py" uninstall \
        "$LEGACY_HERMES_PLUGIN_SOURCE" "$HERMES_PLUGIN_TARGET"
      run mkdir -p "$(dirname "$HERMES_PLUGIN_TARGET")"
      run python3 "$REPO_ROOT/scripts/manage_repo_link.py" install \
        "$HERMES_PLUGIN_SOURCE" "$HERMES_PLUGIN_TARGET"
    else
      next_stage_receipt
      python3 "$REPO_ROOT/scripts/manage_repo_link.py" uninstall \
        "$LEGACY_HERMES_PLUGIN_SOURCE" "$HERMES_PLUGIN_TARGET" \
        --receipt "$STAGE_RECEIPT"
      checkpoint_stage
      link_repo_file "$HERMES_PLUGIN_SOURCE" "$HERMES_PLUGIN_TARGET"
    fi
  else
    link_repo_file "$HERMES_PLUGIN_SOURCE" "$HERMES_PLUGIN_TARGET"
  fi
  if ((DRY_RUN)); then
    echo "DRY RUN: stage Hermes plugin enablement and CAS-install $HERMES_CONFIG"
  else
    stage_hermes_plugin_config enable
  fi
fi

if ((NO_RESTART == 0)); then
  if ((!DRY_RUN)) && [[ "$(/usr/bin/uname -s)" == "Darwin" ]]; then
    GATEWAY_PLIST_GENERATED="$TRANSACTION_DIR/ai.hermes.gateway.generated.plist"
    GATEWAY_PLIST_CANDIDATE="$TRANSACTION_DIR/ai.hermes.gateway.hardened.plist"
    run_without_transaction_authority "$HERMES_RELEASE/venv/bin/python" -c \
      'from hermes_cli.gateway import generate_launchd_plist; print(generate_launchd_plist(), end="")' \
      >"$GATEWAY_PLIST_GENERATED"
    chmod 0600 "$GATEWAY_PLIST_GENERATED"
    python3 "$REPO_ROOT/scripts/harden-macos-gateway-plist.py" \
      "$GATEWAY_PLIST_GENERATED" "$GATEWAY_PLIST_CANDIDATE" "$HERMES_RELEASE_ROOT"
    next_stage_receipt
    python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
      "$TRANSACTION_RECEIPT" "$GATEWAY_PLIST_CANDIDATE" "$GATEWAY_PLIST" "$STAGE_RECEIPT" \
      --absent-mode 0600 --target-mode 0600
    checkpoint_stage
    GATEWAY_RESTARTED=1
    run_without_transaction_authority python3 \
      "$REPO_ROOT/scripts/reload-macos-launchd-service.py" "$GATEWAY_PLIST"
  else
    if ((!DRY_RUN)); then GATEWAY_RESTARTED=1; fi
    run_without_transaction_authority "$HERMES_CLI" gateway install \
      --force --start-now --start-on-login
  fi
  ensure_macos_gateway_supervised "$HERMES_CLI" sealed "${HERMES_RELEASE:-}"
fi
if ((SKIP_SMOKE == 0)); then
  run_without_transaction_authority "$REPO_ROOT/scripts/kanban-smoke.sh"
fi

cat <<'EOF'
Unified Kanban setup complete.
Next: open Hermes Dashboard > Kanban, create a board, and set Project directory.
The adapter will automatically select that board anywhere inside the project directory.
Claude Code and Codex hooks are installed. Restart each CLI before testing a new prompt.
The Hermes Agent plugin (hermes-kanban) records every Hermes turn on the same boards.
The ai-session-viewer command provides a read-only Claude, Codex, and Hermes session timeline.
EOF

if ((HERMES_PLUGIN_ENABLED)); then
  cat >&2 <<'EOF'
WARNING: running Hermes CLI, TUI, and Desktop chat processes keep the plugin
version already loaded in memory. They must be quit and relaunched before testing a new prompt;
restarting the gateway does not reload those clients.
EOF
fi
