#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADAPTER="${KANBAN_ADAPTER:-$REPO_ROOT/bin/kanban-adapter}"
BOARD="${KANBAN_SMOKE_BOARD:-unified-kanban-smoke}"
TASK_ID=""

fail() {
  echo "SMOKE FAIL: $*" >&2
  exit 1
}

cleanup_best_effort() {
  local exit_code=$?
  if [[ -n "$TASK_ID" ]]; then
    hermes kanban --board "$BOARD" archive "$TASK_ID" >/dev/null 2>&1 || true
  fi
  return "$exit_code"
}
trap cleanup_best_effort EXIT

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
    fail "board does not exist: $BOARD (create it before running smoke)"
  else
    fail "Hermes board data is invalid"
  fi
fi

TASK_ID="$("$ADAPTER" start --board "$BOARD" --title "Unified Kanban smoke" --source manual)"
[[ "$TASK_ID" == t_* ]] || fail "adapter start returned invalid task id: $TASK_ID"

"$ADAPTER" update --board "$BOARD" --task "$TASK_ID" --message "smoke-update-ok"
SHOW_AFTER_UPDATE="$(hermes kanban --board "$BOARD" show "$TASK_ID")"
[[ "$SHOW_AFTER_UPDATE" == *"smoke-update-ok"* ]] || fail "comment missing from show"

"$ADAPTER" done --board "$BOARD" --task "$TASK_ID" --summary "smoke-done-ok"
SHOW_AFTER_DONE="$(hermes kanban --board "$BOARD" show "$TASK_ID")"
[[ "$SHOW_AFTER_DONE" == *"status:    done"* ]] || fail "task did not reach done"
[[ "$SHOW_AFTER_DONE" == *"smoke-done-ok"* ]] || fail "summary missing from show"

hermes kanban --board "$BOARD" archive "$TASK_ID" >/dev/null
SHOW_AFTER_ARCHIVE="$(hermes kanban --board "$BOARD" show "$TASK_ID")"
[[ "$SHOW_AFTER_ARCHIVE" == *"status:    archived"* ]] || fail "task cleanup did not reach archived"
TASK_ID=""

echo "SMOKE PASS: archived card on board $BOARD"
