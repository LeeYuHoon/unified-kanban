#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
python3 "$REPO_ROOT/scripts/install-claude-hooks.py" batch-validate \
  "$CLAUDE_SETTINGS" "$CLAUDE_HOOK_LINK" \
  "$CODEX_SETTINGS" "$CODEX_HOOK_LINK"

HERMES_PLUGIN_SOURCE="$REPO_ROOT/integrations/hermes/hermes-kanban"
HERMES_PLUGIN_TARGET="$HOME/.hermes/plugins/hermes-kanban"
if [[ -L "$HERMES_PLUGIN_TARGET" ]]; then
  if [[ "$(readlink "$HERMES_PLUGIN_TARGET")" != "$HERMES_PLUGIN_SOURCE" ]]; then
    echo "Refusing foreign Hermes plugin symlink: $HERMES_PLUGIN_TARGET" >&2
    exit 1
  fi
elif [[ -e "$HERMES_PLUGIN_TARGET" ]]; then
  echo "Refusing existing non-managed Hermes plugin path: $HERMES_PLUGIN_TARGET" >&2
  exit 1
fi

remove_managed_link() {
  local target="$1" expected="$2"
  python3 "$REPO_ROOT/scripts/manage_repo_link.py" uninstall "$expected" "$target"
}

if [[ -e "$PROJECTS_STATE" || -L "$PROJECTS_STATE" ]]; then
  python3 - "$PROJECTS_STATE" <<'PY'
from pathlib import Path
import fcntl, json, os, stat, sys, tempfile

state_file = Path(sys.argv[1])
lock_fd = os.open(
    state_file.parent / ".routing.lock", os.O_CREAT | os.O_RDWR, 0o600
)
fcntl.flock(lock_fd, fcntl.LOCK_EX)


def path_identity(path):
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return None
    return (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode))


def snapshot(path, label, skip_symlink=False):
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode):
        if skip_symlink:
            return None
        raise ValueError(f"{label} must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
        expected = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
        if identity != expected:
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
        "path": path,
        "bytes": b"".join(chunks),
        "mode": stat.S_IMODE(opened.st_mode),
        "identity": identity,
    }


def assert_unchanged(item, label):
    if path_identity(item["path"]) != item["identity"]:
        raise RuntimeError(f"{label} changed after validation")


state = snapshot(state_file, "managed-projects state")
if state is None:
    raise FileNotFoundError(state_file)
projects = json.loads(state["bytes"].decode("utf-8"))
if not isinstance(projects, list):
    raise ValueError("managed-projects state must be a JSON list")
if any(not isinstance(item, str) or not item or not Path(item).is_absolute() for item in projects):
    raise ValueError("managed-projects entries must be non-empty absolute path strings")
if len(set(projects)) != len(projects):
    raise ValueError("managed-projects entries must be unique")

plans = []
for project in projects:
    envrc = Path(project) / ".envrc"
    original = snapshot(envrc, f"managed .envrc {envrc}", skip_symlink=True)
    if original is None:
        continue
    original_text = original["bytes"].decode("utf-8")
    original_lines = original_text.splitlines()
    filtered = [line for line in original_lines if not line.endswith(" # unified-kanban")]
    if filtered == original_lines:
        continue
    original["replacement"] = (
        (("\n".join(filtered) + "\n") if filtered else "").encode("utf-8")
    )
    original["temp"] = None
    original["installed_identity"] = None
    plans.append(original)


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


# Prepare every replacement before mutating any managed file.
try:
    for plan in plans:
        plan["temp"] = prepare_temp(plan["path"], plan["replacement"], plan["mode"])
except BaseException:
    for plan in plans:
        temp = plan["temp"]
        if temp is not None:
            temp.unlink(missing_ok=True)
    raise

replaced = []
state_removed = False
try:
    # Recheck every target after temp preparation and before the first mutation.
    assert_unchanged(state, "managed-projects state")
    for plan in plans:
        assert_unchanged(plan, f"managed .envrc {plan['path']}")

    for plan in plans:
        assert_unchanged(plan, f"managed .envrc {plan['path']}")
        plan["installed_identity"] = path_identity(plan["temp"])
        if plan["installed_identity"] is None:
            raise RuntimeError(f"invalid .envrc temp identity: {plan['path']}")
        os.replace(plan["temp"], plan["path"])
        plan["temp"] = None
        replaced.append(plan)
        if path_identity(plan["path"]) != plan["installed_identity"]:
            raise RuntimeError(f"managed .envrc changed immediately after commit: {plan['path']}")

    assert_unchanged(state, "managed-projects state")
    state_file.unlink()
    state_removed = True
except BaseException as original_error:
    rollback_errors = []
    if state_removed:
        try:
            if path_identity(state_file) is not None:
                raise RuntimeError("managed-projects state was recreated before rollback")
            restore_file(state_file, state["bytes"], state["mode"])
        except BaseException as exc:
            rollback_errors.append(f"state rollback failed: {exc}")
    for plan in reversed(replaced):
        try:
            if path_identity(plan["path"]) != plan["installed_identity"]:
                raise RuntimeError("file changed after uninstall replacement")
            restore_file(plan["path"], plan["bytes"], plan["mode"])
        except BaseException as exc:
            rollback_errors.append(f"{plan['path']} rollback failed: {exc}")
    if rollback_errors:
        raise RuntimeError("; ".join(rollback_errors)) from original_error
    raise
finally:
    for plan in plans:
        temp = plan["temp"]
        if temp is not None:
            temp.unlink(missing_ok=True)
PY
fi

python3 "$REPO_ROOT/scripts/install-claude-hooks.py" batch-uninstall \
  "$CLAUDE_SETTINGS" "$CLAUDE_HOOK_LINK" \
  "$CODEX_SETTINGS" "$CODEX_HOOK_LINK"
remove_managed_link "$HOME/.local/bin/kanban-adapter" "$REPO_ROOT/bin/kanban-adapter"
remove_managed_link "$CLAUDE_HOOK_LINK" "$REPO_ROOT/bin/claude-kanban-hook"
remove_managed_link "$CODEX_HOOK_LINK" "$REPO_ROOT/bin/codex-kanban-hook"
remove_managed_link "$SESSION_VIEWER_LINK" "$REPO_ROOT/bin/ai-session-viewer"

if [[ -L "$HERMES_PLUGIN_TARGET" && "$(readlink "$HERMES_PLUGIN_TARGET")" == "$HERMES_PLUGIN_SOURCE" ]]; then
  if command -v hermes >/dev/null 2>&1; then
    hermes plugins disable hermes-kanban
  fi
  python3 "$REPO_ROOT/scripts/manage_repo_link.py" uninstall \
    "$HERMES_PLUGIN_SOURCE" "$HERMES_PLUGIN_TARGET"
fi

if ((NO_RESTART == 0)) && command -v hermes >/dev/null 2>&1; then
  hermes gateway restart
fi

echo "Unified Kanban managed links and routing entries removed; Kanban data preserved."
