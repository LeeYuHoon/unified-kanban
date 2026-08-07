#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
fi

for cmd in python3 hermes; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "Missing required command: $cmd" >&2; exit 1; }
done
hermes version >/dev/null
has_help_token() {
  local text="$1" expected="$2"
  printf '%s\n' "$text" | python3 -c '
import re, sys
expected = sys.argv[1]
tokens = set(re.findall(r"--[A-Za-z0-9][A-Za-z0-9-]*|[A-Za-z_][A-Za-z0-9_-]*", sys.stdin.read()))
raise SystemExit(0 if expected in tokens else 1)
' "$expected"
}
KANBAN_HELP="$(hermes kanban --help)"
CREATE_HELP="$(hermes kanban create --help)"
BOARDS_LIST_HELP="$(hermes kanban boards list --help)"
BOARDS_CREATE_HELP="$(hermes kanban boards create --help)"
COMMENT_HELP="$(hermes kanban comment --help)"
COMPLETE_HELP="$(hermes kanban complete --help)"
BLOCK_HELP="$(hermes kanban block --help)"
SHOW_HELP="$(hermes kanban show --help)"
ARCHIVE_HELP="$(hermes kanban archive --help)"
has_help_token "$KANBAN_HELP" "--board" \
  && has_help_token "$CREATE_HELP" "--assignee" \
  && has_help_token "$CREATE_HELP" "--tenant" \
  && has_help_token "$CREATE_HELP" "--created-by" \
  && has_help_token "$CREATE_HELP" "--initial-status" \
  && has_help_token "$CREATE_HELP" "--observation" \
  && has_help_token "$CREATE_HELP" "--json" \
  && has_help_token "$BOARDS_LIST_HELP" "--json" \
  && has_help_token "$BOARDS_CREATE_HELP" "--name" \
  && has_help_token "$COMMENT_HELP" "--author" \
  && has_help_token "$COMPLETE_HELP" "--summary" \
  && has_help_token "$BLOCK_HELP" "--kind" \
  && has_help_token "$SHOW_HELP" "task_id" \
  && has_help_token "$ARCHIVE_HELP" "task_ids" || {
  echo "Installed Hermes Kanban CLI is incompatible with unified-kanban" >&2
  exit 1
}

HERMES_PLUGIN_SOURCE="$REPO_ROOT/integrations/hermes/hermes-kanban"
HERMES_PLUGIN_TARGET="$HOME/.hermes/plugins/hermes-kanban"
LEGACY_HERMES_PLUGIN_SOURCE="${UNIFIED_KANBAN_LEGACY_HERMES_PLUGIN_SOURCE:-$(dirname "$REPO_ROOT")/hermes-kanban/plugin/hermes-kanban}"
AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
CARRIED_COMMITS_FILE="${HERMES_CARRIED_COMMITS_FILE:-$REPO_ROOT/patches/hermes-agent-carried-commits}"
CARRIED_BUNDLE="${HERMES_CARRIED_BUNDLE:-$REPO_ROOT/patches/hermes-agent-carried.bundle}"
EXPECTED_UPSTREAM_FILE="$REPO_ROOT/patches/hermes-agent-supported-upstream"
HERMES_PLUGIN_ENABLED=1
CARRIED_OBJECTS_UNAVAILABLE=0
HERMES_REPAIR_ATTEMPTED=0

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
        echo "Ambiguous carried commit status for $commit: $output" >&2
        return 1
      }
      matched="$sign"
    fi
  done <<< "$output"
  [[ "$matched" == "+" || "$matched" == "-" ]] || {
    echo "Unable to verify required Hermes carried commit $commit: $output" >&2
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
  if ((DRY_RUN)); then
    echo "DRY RUN: would import repository-contained Hermes carried commits from $CARRIED_BUNDLE"
    CARRIED_OBJECTS_UNAVAILABLE=1
    return 0
  fi
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

if [[ ! -d "$AGENT_REPO" ]]; then
  echo "Hermes Agent checkout not found at $AGENT_REPO; setup cannot verify compatibility." >&2
  exit 1
else
  command -v git >/dev/null 2>&1 || { echo "Missing required command: git" >&2; exit 1; }
  [[ -f "$HERMES_PLUGIN_SOURCE/plugin.yaml" && -f "$HERMES_PLUGIN_SOURCE/__init__.py" ]] || {
    echo "repository Hermes plugin files are missing: $HERMES_PLUGIN_SOURCE" >&2
    exit 1
  }
  if ! SUPPORTED_UPSTREAM="$(PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
    'from kanban_adapter.compatibility import read_supported_upstream; print(read_supported_upstream())')"; then
    echo "Unable to trust the repository Hermes upstream pin: $EXPECTED_UPSTREAM_FILE" >&2
    exit 1
  fi
  HERMES_VERSION_OUTPUT="$(hermes version)"
  if [[ "$HERMES_VERSION_OUTPUT" =~ upstream[[:space:]]+([0-9A-Fa-f]{7,40}) ]]; then
    HERMES_UPSTREAM="${BASH_REMATCH[1]}"
  else
    echo "Unable to read the Hermes upstream version from 'hermes version'" >&2
    exit 1
  fi
  CHECKOUT_UPSTREAM="$(git -C "$AGENT_REPO" rev-parse origin/main)"
  if [[ "$CHECKOUT_UPSTREAM" != "$HERMES_UPSTREAM"* ]]; then
    echo "Hermes version mismatch: CLI reports upstream $HERMES_UPSTREAM, but the checkout expects $CHECKOUT_UPSTREAM." >&2
    echo "Run $REPO_ROOT/scripts/update-hermes-if-needed.sh before setup." >&2
    exit 1
  fi
  if [[ "$CHECKOUT_UPSTREAM" != "$SUPPORTED_UPSTREAM" ]]; then
    echo "Setup blocked: unified-kanban supports Hermes upstream $SUPPORTED_UPSTREAM, but the installed checkout is $CHECKOUT_UPSTREAM." >&2
    echo "Update and verify unified-kanban's compatibility pin before running setup." >&2
    exit 1
  fi
  ensure_carried_commit_objects
  if [[ -f "$CARRIED_COMMITS_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      commit="${line%%#*}"
      commit="${commit//[[:space:]]/}"
      [[ -n "$commit" ]] || continue
      if ((CARRIED_OBJECTS_UNAVAILABLE)); then
        echo "DRY RUN: would verify required Hermes carried commit $commit"
        continue
      fi
      CHERRY_STATUS="$(carried_commit_status "$commit")" || exit 1
      case "$CHERRY_STATUS" in
        "+")
          if ((DRY_RUN)); then
            echo "DRY RUN: would prepare missing Hermes carried commit $commit"
            continue
          fi
          if ((HERMES_REPAIR_ATTEMPTED == 0)); then
            echo "Preparing Hermes checkout for unified-kanban"
            "$REPO_ROOT/scripts/update-hermes-if-needed.sh" --prepare-only
            HERMES_REPAIR_ATTEMPTED=1
          fi
          CHERRY_STATUS="$(carried_commit_status "$commit")" || exit 1
          if [[ "$CHERRY_STATUS" == "+" ]]; then
            echo "Setup blocked: required Hermes carried commit is still missing: $commit" >&2
            exit 1
          fi
          ;;
        "-") ;;
      esac
    done < "$CARRIED_COMMITS_FILE"
  fi
fi

run() {
  if ((DRY_RUN)); then
    printf 'DRY RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
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
  run mkdir -p "$(dirname "$target")"
  run python3 "$REPO_ROOT/scripts/manage_repo_link.py" install "$source" "$target"
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
      run hermes kanban boards create "$BOARD" --name "$BOARD"
    else
      echo "Refusing setup because Hermes board data is invalid" >&2
      exit 1
    fi
  fi
fi

run mkdir -p "$STATE_DIR"
run mkdir -p "${XDG_CACHE_HOME:-$HOME/.cache}/unified-kanban"

if [[ -n "$PROJECT_DIR" ]]; then
  LINE="export HERMES_KANBAN_BOARD=$BOARD # unified-kanban"
  if ((DRY_RUN)); then
    echo "DRY RUN: ensure $ENVRC contains: $LINE"
    echo "DRY RUN: register $PROJECT_DIR in $PROJECTS_STATE"
  else
    python3 - "$ENVRC" "$LINE" "$PROJECTS_STATE" "$PROJECT_DIR" <<'PY'
from pathlib import Path
import fcntl, json, os, stat, sys, tempfile

envrc = Path(sys.argv[1])
line = sys.argv[2]
state_file = Path(sys.argv[3])
project = Path(sys.argv[4])

lock_fd = os.open(
    state_file.parent / ".routing.lock", os.O_CREAT | os.O_RDWR, 0o600
)
fcntl.flock(lock_fd, fcntl.LOCK_EX)


def snapshot(path, label):
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return {"exists": False, "bytes": b"", "mode": None, "identity": None}
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino)
        if identity != (before.st_dev, before.st_ino):
            raise RuntimeError(f"{label} changed during validation")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return {
        "exists": True,
        "bytes": b"".join(chunks),
        "mode": stat.S_IMODE(opened.st_mode),
        "identity": identity,
    }


def current_identity(path):
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode):
        return None
    return (current.st_dev, current.st_ino)


def assert_unchanged(path, original, label):
    expected = original["identity"] if original["exists"] else None
    if current_identity(path) != expected:
        raise RuntimeError(f"{label} changed after validation")


def prepare_temp(path, content, mode):
    fd, name = tempfile.mkstemp(dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        return temp
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def restore_file(path, content, mode):
    temp = prepare_temp(path, content, mode)
    try:
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


# Revalidate after board lookup so a symlink/object swap cannot pass the earlier preflight.
state_original = snapshot(state_file, "managed-projects state")
envrc_original = snapshot(envrc, "project .envrc")
projects = (
    json.loads(state_original["bytes"].decode("utf-8"))
    if state_original["exists"] else []
)
if not isinstance(projects, list):
    raise ValueError("managed-projects state must be a JSON list")
if any(
    not isinstance(item, str) or not item or not Path(item).is_absolute()
    for item in projects
):
    raise ValueError("managed-projects entries must be non-empty absolute path strings")
if len(set(projects)) != len(projects):
    raise ValueError("managed-projects entries must be unique")
project_text = str(project)
if project_text not in projects:
    projects.append(project_text)

old_lines = (
    envrc_original["bytes"].decode("utf-8").splitlines()
    if envrc_original["exists"] else []
)
new_lines = [item for item in old_lines if not item.endswith(" # unified-kanban")]
new_lines.append(line)
state_content = (json.dumps(projects, indent=2) + "\n").encode("utf-8")
envrc_content = ("\n".join(new_lines) + "\n").encode("utf-8")
state_mode = state_original["mode"] if state_original["exists"] else 0o600
envrc_mode = envrc_original["mode"] if envrc_original["exists"] else 0o644

state_temp = prepare_temp(state_file, state_content, state_mode)
envrc_temp = None
state_committed = False
state_commit_identity = None
try:
    envrc_temp = prepare_temp(envrc, envrc_content, envrc_mode)
    assert_unchanged(state_file, state_original, "managed-projects state")
    assert_unchanged(envrc, envrc_original, "project .envrc")
    state_commit_identity = current_identity(state_temp)
    if state_commit_identity is None:
        raise RuntimeError("managed-projects temp identity is invalid")
    os.replace(state_temp, state_file)
    state_temp = None
    state_committed = True
    if current_identity(state_file) != state_commit_identity:
        raise RuntimeError("managed-projects state changed immediately after commit")

    # Check again immediately before replacing .envrc; this catches swaps during setup.
    assert_unchanged(envrc, envrc_original, "project .envrc")
    envrc_commit_identity = current_identity(envrc_temp)
    if envrc_commit_identity is None:
        raise RuntimeError("project .envrc temp identity is invalid")
    os.replace(envrc_temp, envrc)
    envrc_temp = None
    if current_identity(envrc) != envrc_commit_identity:
        raise RuntimeError("project .envrc changed immediately after commit")
except BaseException as original_error:
    rollback_error = None
    if state_committed:
        try:
            if current_identity(state_file) != state_commit_identity:
                raise RuntimeError("managed-projects state changed before rollback")
            if state_original["exists"]:
                restore_file(
                    state_file, state_original["bytes"], state_original["mode"]
                )
            else:
                state_file.unlink()
        except BaseException as exc:
            rollback_error = exc
    if rollback_error is not None:
        raise RuntimeError(f"setup rollback failed: {rollback_error}") from original_error
    raise
finally:
    if state_temp is not None:
        state_temp.unlink(missing_ok=True)
    if envrc_temp is not None:
        envrc_temp.unlink(missing_ok=True)
PY
  fi
fi

run python3 "$REPO_ROOT/scripts/install-claude-hooks.py" batch-install \
  "$CLAUDE_SETTINGS" "$CLAUDE_HOOK_LINK" \
  "$CODEX_SETTINGS" "$CODEX_HOOK_LINK"
link_repo_file "$REPO_ROOT/bin/kanban-adapter" "$HOME/.local/bin/kanban-adapter"
link_repo_file "$REPO_ROOT/bin/claude-kanban-hook" "$CLAUDE_HOOK_LINK"
link_repo_file "$REPO_ROOT/bin/codex-kanban-hook" "$CODEX_HOOK_LINK"
link_repo_file "$REPO_ROOT/bin/ai-session-viewer" "$SESSION_VIEWER_LINK"

if ((HERMES_PLUGIN_ENABLED)); then
  assert_hermes_plugin_installable
  if [[ -L "$HERMES_PLUGIN_TARGET" && "$(readlink "$HERMES_PLUGIN_TARGET")" != "$HERMES_PLUGIN_SOURCE" ]]; then
    echo "Migrating legacy hermes-kanban plugin symlink: $(readlink "$HERMES_PLUGIN_TARGET") -> $HERMES_PLUGIN_SOURCE"
    run python3 "$REPO_ROOT/scripts/manage_repo_link.py" uninstall \
      "$LEGACY_HERMES_PLUGIN_SOURCE" "$HERMES_PLUGIN_TARGET"
    if ((DRY_RUN)); then
      run mkdir -p "$(dirname "$HERMES_PLUGIN_TARGET")"
      run python3 "$REPO_ROOT/scripts/manage_repo_link.py" install \
        "$HERMES_PLUGIN_SOURCE" "$HERMES_PLUGIN_TARGET"
    else
      link_repo_file "$HERMES_PLUGIN_SOURCE" "$HERMES_PLUGIN_TARGET"
    fi
  else
    link_repo_file "$HERMES_PLUGIN_SOURCE" "$HERMES_PLUGIN_TARGET"
  fi
  run hermes plugins enable --no-allow-tool-override hermes-kanban
fi

if ((NO_RESTART == 0)); then
  run hermes gateway restart
fi
if ((SKIP_SMOKE == 0)); then
  run "$REPO_ROOT/scripts/kanban-smoke.sh"
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
