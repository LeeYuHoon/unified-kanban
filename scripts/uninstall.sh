#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
command -v python3 >/dev/null 2>&1 || {
  echo "Missing required command: python3" >&2
  exit 1
}
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Python 3.11 or newer is required" >&2
  exit 1
}
NO_RESTART=0
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/unified-kanban"
PROJECTS_STATE="$STATE_DIR/managed-projects.json"

while (($#)); do
  case "$1" in
    --no-restart) NO_RESTART=1 ;;
    -h|--help) echo "Usage: ./scripts/uninstall.sh [--no-restart]"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

CLAUDE_SETTINGS="$HOME/.claude/settings.json"
CLAUDE_HOOK_LINK="$HOME/.local/bin/claude-kanban-hook"
CODEX_SETTINGS="$HOME/.codex/hooks.json"
CODEX_HOOK_LINK="$HOME/.local/bin/codex-kanban-hook"
SESSION_VIEWER_LINK="$HOME/.local/bin/ai-session-viewer"
TRANSACTION_DIR=""
TRANSACTION_RECEIPT=""
TRANSACTION_TOKEN=""
TRANSACTION_STAGE=0
STAGE_RECEIPT=""
GATEWAY_RESTARTED=0
python3 "$REPO_ROOT/scripts/install-claude-hooks.py" batch-validate \
  "$CLAUDE_SETTINGS" "$CLAUDE_HOOK_LINK" \
  "$CODEX_SETTINGS" "$CODEX_HOOK_LINK"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
[[ "$HERMES_HOME" == /* ]] || { echo "HERMES_HOME must be absolute" >&2; exit 1; }
HERMES_CONFIG="$HERMES_HOME/config.yaml"
HERMES_PLUGIN_SOURCE="$REPO_ROOT/integrations/hermes/hermes-kanban"
HERMES_PLUGIN_TARGET="$HERMES_HOME/plugins/hermes-kanban"
AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
[[ "$AGENT_REPO" == /* ]] || { echo "HERMES_AGENT_REPO must be absolute" >&2; exit 1; }
# The selector this removes and the launcher the release manager re-renders must
# be derived from the same normal form, or uninstall would refuse a launcher it
# installed itself.
AGENT_REPO="$(PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m kanban_adapter.release_layout "$AGENT_REPO")" || exit 1
HERMES_RELEASE_SELECTOR="${AGENT_REPO}.releases/current"
HERMES_LAUNCHER="$HOME/.local/bin/hermes"
HERMES_CLI="$HERMES_LAUNCHER"
HERMES_LAUNCHER_BACKUP="$STATE_DIR/hermes-launcher.before-unified-kanban"
HERMES_PLUGIN_MANAGED=0
if [[ -L "$HERMES_PLUGIN_TARGET" ]]; then
  if [[ "$(readlink "$HERMES_PLUGIN_TARGET")" != "$HERMES_PLUGIN_SOURCE" ]]; then
    echo "Refusing foreign Hermes plugin symlink: $HERMES_PLUGIN_TARGET" >&2
    exit 1
  fi
  [[ -x "$HERMES_CLI" ]] || {
    echo "Hermes CLI is required to disable the managed plugin before removal" >&2
    exit 1
  }
  HERMES_PLUGIN_MANAGED=1
elif [[ -e "$HERMES_PLUGIN_TARGET" ]]; then
  echo "Refusing existing non-managed Hermes plugin path: $HERMES_PLUGIN_TARGET" >&2
  exit 1
fi

finish_uninstall_transaction() {
  local status=$? rollback_failed=0
  trap - EXIT
  if [[ -n "$TRANSACTION_RECEIPT" ]]; then
    if ((status == 0)) \
      && python3 "$REPO_ROOT/scripts/path-transaction.py" commit \
        "$TRANSACTION_RECEIPT" "$TRANSACTION_TOKEN"; then
      :
    else
      if ((status == 0)); then
        echo "uninstall transaction receipt commit failed; rolling back" >&2
        status=1
      fi
      python3 "$REPO_ROOT/scripts/path-transaction.py" rollback-ledger \
        "$TRANSACTION_RECEIPT" 10 \
        || rollback_failed=1

      if ((GATEWAY_RESTARTED)); then
        run_without_transaction_authority hermes gateway restart \
          >/dev/null 2>&1 || rollback_failed=1
      fi
      if ((rollback_failed)); then
        echo "uninstall rollback was incomplete because managed identities changed; foreign paths were preserved" >&2
        status=1
      fi
    fi
  fi
  if [[ -n "$TRANSACTION_DIR" ]]; then
    if ! python3 "$REPO_ROOT/scripts/path-transaction.py" cleanup-private-dir \
      "$TRANSACTION_DIR" 9; then
      echo "private uninstall transaction cleanup failed" >&2
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
  local baseline="$TRANSACTION_DIR/hermes-config-before-$TRANSACTION_STAGE"
  local candidate="$TRANSACTION_DIR/hermes-config-candidate-$TRANSACTION_STAGE"
  python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
    "$TRANSACTION_RECEIPT" "$HERMES_CONFIG" "$baseline"
  python3 "$REPO_ROOT/scripts/stage-hermes-plugin-config.py" \
    9 "$baseline" "$candidate" "$HERMES_PLUGIN_SOURCE" disable "$HERMES_CLI"
  next_stage_receipt
  python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
    "$TRANSACTION_RECEIPT" "$candidate" "$HERMES_CONFIG" "$STAGE_RECEIPT"
  checkpoint_stage
}

run_without_transaction_authority() (
  unset UNIFIED_KANBAN_TRANSACTION_DIR
  unset UNIFIED_KANBAN_TRANSACTION_FD
  unset UNIFIED_KANBAN_TOKEN_LEDGER_FD
  exec 9<&-
  exec 10<&-
  "$@"
)

TRANSACTION_DIR="$(mktemp -d "${TMPDIR:-/tmp}/unified-kanban-uninstall.XXXXXX")"
chmod 700 "$TRANSACTION_DIR"
exec 9<"$TRANSACTION_DIR"
(umask 077 && : >"$TRANSACTION_DIR/token-ledger")
exec 10<>"$TRANSACTION_DIR/token-ledger"
export UNIFIED_KANBAN_TRANSACTION_DIR="$TRANSACTION_DIR"
export UNIFIED_KANBAN_TRANSACTION_FD=9
export UNIFIED_KANBAN_TOKEN_LEDGER_FD=10
TRANSACTION_RECEIPT="$TRANSACTION_DIR/receipt.json"
trap finish_uninstall_transaction EXIT
TRANSACTION_PATHS=(
  "$HOME/.local/bin/kanban-adapter"
  "$CLAUDE_HOOK_LINK"
  "$CODEX_HOOK_LINK"
  "$SESSION_VIEWER_LINK"
  "$CLAUDE_SETTINGS"
  "$CODEX_SETTINGS"
  "$HERMES_PLUGIN_TARGET"
  "$HERMES_CONFIG"
  "$HERMES_RELEASE_SELECTOR"
  "$HERMES_LAUNCHER"
  "$HERMES_LAUNCHER_BACKUP"
)
ROUTING_PATHS=()
ROUTING_FROZEN=false
if [[ -e "$PROJECTS_STATE" || -L "$PROJECTS_STATE" ]]; then
  ROUTING_PREFLIGHT="$TRANSACTION_DIR/routing-preflight.json"
  ROUTING_PATH_LIST="$TRANSACTION_DIR/routing-paths.bin"
  ROUTING_ASSIGNMENT="$(
    python3 "$REPO_ROOT/scripts/render-project-routing.py" preflight \
      "$PROJECTS_STATE" "$ROUTING_PREFLIGHT" "$ROUTING_PATH_LIST"
  )"
  eval "$ROUTING_ASSIGNMENT"
  TRANSACTION_PATHS+=("${ROUTING_PATHS[@]}")
  ROUTING_FROZEN=true
fi
TRANSACTION_TOKEN="$(python3 "$REPO_ROOT/scripts/path-transaction.py" begin \
  "$TRANSACTION_RECEIPT" "${TRANSACTION_PATHS[@]}")"
remove_managed_link() {
  local target="$1" expected="$2"
  next_stage_receipt
  python3 "$REPO_ROOT/scripts/manage_repo_link.py" uninstall "$expected" "$target" \
    --receipt "$STAGE_RECEIPT"
  checkpoint_stage
}

if [[ "$ROUTING_FROZEN" == true ]]; then
  STATE_BEFORE="$TRANSACTION_DIR/uninstall-state-before"
  python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
    "$TRANSACTION_RECEIPT" "$PROJECTS_STATE" "$STATE_BEFORE"
  python3 "$REPO_ROOT/scripts/render-project-routing.py" validate-state \
    "$STATE_BEFORE" "$ROUTING_PREFLIGHT"
  for ((ROUTING_INDEX = 1; ROUTING_INDEX < ${#ROUTING_PATHS[@]}; ROUTING_INDEX++)); do
    MANAGED_ENVRC="${ROUTING_PATHS[$ROUTING_INDEX]}"
    ENVRC_BEFORE="$TRANSACTION_DIR/uninstall-envrc-$ROUTING_INDEX-before"
    ENVRC_CANDIDATE="$TRANSACTION_DIR/uninstall-envrc-$ROUTING_INDEX-candidate"
    python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
      "$TRANSACTION_RECEIPT" "$MANAGED_ENVRC" "$ENVRC_BEFORE"
    python3 "$REPO_ROOT/scripts/render-project-routing.py" unregister \
      "$ENVRC_BEFORE" "$ENVRC_CANDIDATE"
    next_stage_receipt
    python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
      "$TRANSACTION_RECEIPT" "$ENVRC_CANDIDATE" "$MANAGED_ENVRC" "$STAGE_RECEIPT"
    checkpoint_stage
  done
  next_stage_receipt
  python3 "$REPO_ROOT/scripts/path-transaction.py" remove-file \
    "$TRANSACTION_RECEIPT" "$PROJECTS_STATE" "$STAGE_RECEIPT"
  checkpoint_stage
fi

next_stage_receipt
python3 "$REPO_ROOT/scripts/install-claude-hooks.py" uninstall \
  "$CLAUDE_SETTINGS" "$CLAUDE_HOOK_LINK" claude --receipt "$STAGE_RECEIPT"
checkpoint_stage
next_stage_receipt
python3 "$REPO_ROOT/scripts/install-claude-hooks.py" uninstall \
  "$CODEX_SETTINGS" "$CODEX_HOOK_LINK" codex --receipt "$STAGE_RECEIPT"
checkpoint_stage
remove_managed_link "$HOME/.local/bin/kanban-adapter" "$REPO_ROOT/bin/kanban-adapter"
remove_managed_link "$CLAUDE_HOOK_LINK" "$REPO_ROOT/bin/claude-kanban-hook"
remove_managed_link "$CODEX_HOOK_LINK" "$REPO_ROOT/bin/codex-kanban-hook"
remove_managed_link "$SESSION_VIEWER_LINK" "$REPO_ROOT/bin/ai-session-viewer"

if ((HERMES_PLUGIN_MANAGED)); then
  stage_hermes_plugin_config
  remove_managed_link "$HERMES_PLUGIN_TARGET" "$HERMES_PLUGIN_SOURCE"
fi

# Read the reviewed pin and carried manifest through the same trusted readers
# setup uses: nofollow descriptors, inode revalidation, and full 40-hex
# validation. A plain redirect here would accept a swapped or malformed pin that
# setup would have refused.
if ! REVIEWED="$(PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
  'from kanban_adapter.compatibility import read_supported_upstream, read_carried_commits
print(read_supported_upstream())
print(read_carried_commits()[-1])')"; then
  echo "Unable to trust the repository Hermes upstream pin or carried manifest" >&2
  exit 1
fi
EXPECTED_UPSTREAM="${REVIEWED%%$'\n'*}"
FINAL_CARRIED_COMMIT="${REVIEWED##*$'\n'}"
# Decide restore-versus-remove from the frozen transaction snapshot, never from
# a live re-read of the backup pathname, and re-derive the launcher the
# installer must have produced for exactly that snapshot. A foreign replacement,
# an injection where no original existed, and a deletion of the retained backup
# each change the binding, so none of them can be adopted into the launcher.
BACKUP_BEFORE="$TRANSACTION_DIR/hermes-launcher-backup-before"
python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
  "$TRANSACTION_RECEIPT" "$HERMES_LAUNCHER_BACKUP" "$BACKUP_BEFORE"
BACKUP_RETAINED=0
BASELINE_ARGS=(--baseline-absent)
if [[ -f "$BACKUP_BEFORE" ]]; then
  BACKUP_RETAINED=1
  BASELINE_ARGS=(--baseline-file "$BACKUP_BEFORE")
fi
SELECTOR_BEFORE_KIND="$(python3 "$REPO_ROOT/scripts/path-transaction.py" before-kind \
  "$TRANSACTION_RECEIPT" "$HERMES_RELEASE_SELECTOR")"
# With neither ownership artifact present at transaction opening, a prior
# uninstall already completed or ownership evidence was removed. Either way,
# the current launcher is outside this transaction's authority and must be
# preserved. If either artifact remains, retain the strict binding checks so a
# partial deletion cannot bypass fail-closed uninstall behavior.
if [[ "$SELECTOR_BEFORE_KIND" != "absent" || "$BACKUP_RETAINED" == 1 ]]; then
  EXPECTED_LAUNCHER="$TRANSACTION_DIR/expected-hermes-launcher"
  python3 "$REPO_ROOT/scripts/hermes-release-manager.py" render-launcher \
    "$AGENT_REPO" "$EXPECTED_UPSTREAM" "$FINAL_CARRIED_COMMIT" \
    "$EXPECTED_LAUNCHER" "${BASELINE_ARGS[@]}" >/dev/null
  LAUNCHER_BEFORE="$TRANSACTION_DIR/hermes-launcher-before"
  python3 "$REPO_ROOT/scripts/path-transaction.py" export-before \
    "$TRANSACTION_RECEIPT" "$HERMES_LAUNCHER" "$LAUNCHER_BEFORE"
  python3 - "$LAUNCHER_BEFORE" "$EXPECTED_LAUNCHER" <<'PY'
from pathlib import Path
import sys

actual, expected = map(Path, sys.argv[1:])
if not actual.is_file() or actual.read_bytes() != expected.read_bytes():
    raise SystemExit(
        "refusing to act on a Hermes launcher that is not the managed launcher "
        "issued for the retained original launcher backup"
    )
PY

  if ((BACKUP_RETAINED)); then
    next_stage_receipt
    python3 "$REPO_ROOT/scripts/path-transaction.py" replace-file \
      "$TRANSACTION_RECEIPT" "$BACKUP_BEFORE" "$HERMES_LAUNCHER" "$STAGE_RECEIPT"
    checkpoint_stage
  else
    next_stage_receipt
    python3 "$REPO_ROOT/scripts/path-transaction.py" remove-file \
      "$TRANSACTION_RECEIPT" "$HERMES_LAUNCHER" "$STAGE_RECEIPT"
    checkpoint_stage
  fi
  next_stage_receipt
  python3 "$REPO_ROOT/scripts/path-transaction.py" remove-file \
    "$TRANSACTION_RECEIPT" "$HERMES_RELEASE_SELECTOR" "$STAGE_RECEIPT"
  checkpoint_stage
  if ((BACKUP_RETAINED)); then
    next_stage_receipt
    python3 "$REPO_ROOT/scripts/path-transaction.py" remove-file \
      "$TRANSACTION_RECEIPT" "$HERMES_LAUNCHER_BACKUP" "$STAGE_RECEIPT"
    checkpoint_stage
  fi
fi

if ((NO_RESTART == 0)) && command -v hermes >/dev/null 2>&1; then
  GATEWAY_RESTARTED=1
  run_without_transaction_authority hermes gateway restart
fi

echo "Unified Kanban managed links and routing entries removed; Kanban data preserved."
echo "Retained for safe reinstall/recovery: ${XDG_CACHE_HOME:-$HOME/.cache}/kanban-adapter, ${XDG_CACHE_HOME:-$HOME/.cache}/unified-kanban/hermes-turns, ${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-*, and refs/unified-kanban/carried/* in the Hermes checkout."
