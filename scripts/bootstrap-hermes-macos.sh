#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(/usr/bin/dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
MANIFEST="$REPO_ROOT/patches/hermes-agent-bootstrap-manifest"
UPSTREAM_FILE="$REPO_ROOT/patches/hermes-agent-supported-upstream"
MODE=install
if [[ "${1:-}" == --status ]]; then
  MODE=status
  shift
fi
AGENT_REPO="${1:-}"
HERMES_HOME_ARG="${2:-}"

fail() {
  printf 'Hermes bootstrap refused: %s\n' "$1" >&2
  exit 1
}

[[ "$(/usr/bin/uname -s)" == Darwin ]] || fail "automatic first-install is supported only on macOS"
[[ "$AGENT_REPO" == /* ]] || fail "HERMES_AGENT_REPO must be an absolute path"
[[ "$HERMES_HOME_ARG" == /* ]] || fail "HERMES_HOME must be an absolute path"
[[ "$HOME" == /* ]] || fail "HOME must be an absolute path"
case "$AGENT_REPO$HERMES_HOME_ARG$HOME${XDG_STATE_HOME:-}" in
  *$'\n'*) fail "Hermes paths must not contain newlines" ;;
esac

umask 077
tmpdir=""
installer=""
marker_snapshot=""
receipt_tmp=""
cleanup() {
  if [[ -n "$installer" ]]; then
    /bin/rm -f "$installer" "$marker_snapshot"
  fi
  if [[ -n "$receipt_tmp" ]]; then
    /bin/rm -f "$receipt_tmp"
  fi
  if [[ -n "$tmpdir" ]]; then
    /bin/rmdir "$tmpdir" 2>/dev/null || true
  fi
}
trap cleanup EXIT HUP INT TERM

owner_uid="$(/usr/bin/id -u)"
stat_identity() { /usr/bin/stat -f '%d:%i:%u:%Lp:%l' "$1" 2>/dev/null; }
assert_safe_regular() {
  local path="$1" label="$2" identity device inode owner mode links
  [[ -f "$path" && ! -L "$path" ]] || fail "$label is missing or unsafe"
  identity="$(stat_identity "$path")" || fail "cannot inspect $label"
  IFS=: read -r device inode owner mode links <<<"$identity"
  [[ "$owner" == "$owner_uid" ]] || fail "$label has a foreign owner"
  (( (8#$mode & 8#022) == 0 )) || fail "$label is writable by group or other"
  [[ "$links" == 1 ]] || fail "$label must have exactly one hard link"
}
assert_safe_dir() {
  local path="$1" label="$2" identity device inode owner mode links
  [[ -d "$path" && ! -L "$path" ]] || fail "$label is missing or unsafe"
  identity="$(stat_identity "$path")" || fail "cannot inspect $label"
  IFS=: read -r device inode owner mode links <<<"$identity"
  [[ "$owner" == "$owner_uid" ]] || fail "$label has a foreign owner"
  (( (8#$mode & 8#022) == 0 )) || fail "$label is writable by group or other"
}
require_mode() {
  local path="$1" label="$2" expected="$3" identity device inode owner mode links
  identity="$(stat_identity "$path")" || fail "cannot inspect $label mode"
  IFS=: read -r device inode owner mode links <<<"$identity"
  [[ "$mode" == "$expected" ]] || fail "$label mode must be 0$expected"
}
assert_safe_ancestors() {
  local path="$1" label="$2" probe="" remainder component last_existing="/"
  local identity device inode owner mode links
  [[ "$path" == /* ]] || fail "$label must be an absolute path"
  case "$path/" in
    *'//'*) fail "$label is not lexically normalized" ;;
    *'/./'*|*'/../'*) fail "$label contains a relative path component" ;;
  esac
  identity="$(stat_identity /)" || fail "cannot inspect $label ancestor: /"
  IFS=: read -r device inode owner mode links <<<"$identity"
  [[ ! -L / && -d / ]] || fail "$label root ancestor is unsafe"
  [[ "$owner" == 0 || "$owner" == "$owner_uid" ]] \
    || fail "$label traverses a foreign ancestor: /"
  (( (8#$mode & 8#022) == 0 )) \
    || fail "$label traverses a writable ancestor: /"
  remainder="${path#/}"
  while [[ -n "$remainder" ]]; do
    if [[ "$remainder" == */* ]]; then
      component="${remainder%%/*}"
      remainder="${remainder#*/}"
    else
      component="$remainder"
      remainder=""
    fi
    [[ -n "$component" ]] || fail "$label is not lexically normalized"
    probe="$probe/$component"
    [[ ! -L "$probe" ]] || fail "$label traverses a symlink: $probe"
    if [[ -e "$probe" ]]; then
      [[ -d "$probe" ]] || fail "$label traverses a non-directory: $probe"
      identity="$(stat_identity "$probe")" || fail "cannot inspect $label ancestor: $probe"
      IFS=: read -r device inode owner mode links <<<"$identity"
      [[ "$owner" == 0 || "$owner" == "$owner_uid" ]] \
        || fail "$label traverses a foreign ancestor: $probe"
      (( (8#$mode & 8#022) == 0 )) \
        || fail "$label traverses a writable ancestor: $probe"
      last_existing="$probe"
    else
      break
    fi
  done
  assert_safe_dir "$last_existing" "$label ancestor"
}
assert_safe_ancestors "$HOME" "HOME"
tmpdir=$(/usr/bin/mktemp -d "$HOME/.unified-kanban-hermes-bootstrap.XXXXXX") \
  || fail "unable to create private temporary directory"
assert_safe_dir "$tmpdir" "Hermes bootstrap temporary directory"
require_mode "$tmpdir" "Hermes bootstrap temporary directory" 700
installer="$tmpdir/install.sh"
marker_snapshot="$tmpdir/bootstrap-marker"
sha256_stream() {
  local output digest
  output=$(/usr/bin/shasum -a 256) || fail "unable to calculate SHA-256"
  digest="${output%% *}"
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || fail "invalid SHA-256 output"
  printf '%s' "$digest"
}
digest_stable() {
  local path="$1" label="$2" before after output digest
  assert_safe_regular "$path" "$label"
  before="$(stat_identity "$path")"
  exec 7<"$path" || fail "cannot open $label"
  output=$(/usr/bin/shasum -a 256 <&7) || fail "cannot hash $label"
  exec 7<&-
  after="$(stat_identity "$path")" || fail "cannot revalidate $label"
  [[ "$before" == "$after" ]] || fail "$label changed during validation"
  digest="${output%% *}"
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || fail "invalid SHA-256 for $label"
  printf '%s' "$digest"
}
require_digest() {
  local path="$1" label="$2" expected="$3" actual
  actual="$(digest_stable "$path" "$label")"
  [[ "$actual" == "$expected" ]] || fail "$label exact-byte digest disagrees with reviewed policy"
}
copy_stable() {
  local path="$1" label="$2" destination="$3" before after
  assert_safe_regular "$path" "$label"
  [[ ! -e "$destination" && ! -L "$destination" ]] || fail "private snapshot already exists"
  before="$(stat_identity "$path")"
  exec 7<"$path" || fail "cannot open $label"
  /bin/cat <&7 >"$destination" || fail "cannot snapshot $label"
  exec 7<&-
  after="$(stat_identity "$path")" || fail "cannot revalidate $label"
  [[ "$before" == "$after" ]] || fail "$label changed during validation"
}

MANIFEST_SHA256="b07704912af4aabc0c72ed6ddc6e1e9b2c9e286374e6bbdfda5ea35248c941de"
UPSTREAM_FILE_SHA256="030c9e318f9e5dbb0554aa599ecb415915799b900d93835667d9fc9470861998"
UPSTREAM="10b388300a63d83857fac3ca4f8b05b64e01bc50"
INSTALLER_SHA256="c0380bc1f78d3d662a77663ce20cc17e14cbc4bec35e61ab7a33bac5f3afed2d"
INSTALLER_URL="https://raw.githubusercontent.com/NousResearch/hermes-agent/$UPSTREAM/scripts/install.sh"
require_digest "$MANIFEST" "bootstrap manifest" "$MANIFEST_SHA256"
require_digest "$UPSTREAM_FILE" "supported upstream pin" "$UPSTREAM_FILE_SHA256"

state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/unified-kanban"
receipt="$state_dir/hermes-bootstrap.receipt"
launcher="$HOME/.local/bin/hermes"
release_root="${AGENT_REPO}.releases"

expected_receipt() {
  printf '%s\n' \
    'format=unified-kanban-hermes-bootstrap-receipt-v1' \
    "upstream=$UPSTREAM" \
    "agent_repo=$AGENT_REPO" \
    "hermes_home=$HERMES_HOME_ARG" \
    'status=bootstrap-complete' \
    'python_requirement=3.11' \
    'node_major=26' \
    'toolchain_resolution=moving-patch-and-tool-versions'
}
expected_receipt_digest() { expected_receipt | sha256_stream; }
line_digest() { printf '%s\n' "$1" | sha256_stream; }
launcher_digest() {
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'unset PYTHONPATH' \
    'unset PYTHONHOME' \
    "exec \"$AGENT_REPO/venv/bin/python\" \"$AGENT_REPO/hermes\" \"\$@\"" \
    | sha256_stream
}
assert_state_authority() {
  assert_safe_ancestors "$state_dir" "Hermes bootstrap state"
  assert_safe_dir "$state_dir" "Hermes bootstrap state"
  require_mode "$state_dir" "Hermes bootstrap state" 700
}
git_checked() {
  /usr/bin/env -i \
    HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    /usr/bin/git -C "$AGENT_REPO" "$@"
}
verify_marker() {
  local marker="$AGENT_REPO/.hermes-bootstrap-complete" count line1 line2 line3 line4 line5 line6
  copy_stable "$marker" "Hermes bootstrap marker" "$marker_snapshot"
  count=$(/usr/bin/wc -l <"$marker_snapshot" | /usr/bin/tr -d '[:space:]')
  [[ "$count" == 6 ]] || fail "Hermes bootstrap marker has an unexpected shape"
  line1=$(/usr/bin/sed -n '1p' "$marker_snapshot")
  line2=$(/usr/bin/sed -n '2p' "$marker_snapshot")
  line3=$(/usr/bin/sed -n '3p' "$marker_snapshot")
  line4=$(/usr/bin/sed -n '4p' "$marker_snapshot")
  line5=$(/usr/bin/sed -n '5p' "$marker_snapshot")
  line6=$(/usr/bin/sed -n '6p' "$marker_snapshot")
  [[ "$line1" == '{' ]] || fail "Hermes bootstrap marker opening is invalid"
  [[ "$line2" == '  "schemaVersion": 1,' ]] || fail "Hermes bootstrap marker schema is invalid"
  [[ "$line3" == "  \"pinnedCommit\": \"$UPSTREAM\"," ]] || fail "Hermes bootstrap marker commit is invalid"
  [[ "$line4" == '  "pinnedBranch": "main",' ]] || fail "Hermes bootstrap marker branch is invalid"
  [[ "$line5" =~ ^\ \ \"completedAt\":\ \"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.000Z\"$ ]] \
    || fail "Hermes bootstrap marker completion time is invalid"
  [[ "$line6" == '}' ]] || fail "Hermes bootstrap marker closing is invalid"
  /bin/rm -f "$marker_snapshot"
}
verify_installer_artifacts() {
  local head
  assert_safe_ancestors "$AGENT_REPO" "Hermes Agent checkout"
  assert_safe_ancestors "$HERMES_HOME_ARG" "Hermes data home"
  assert_safe_ancestors "$(/usr/bin/dirname "$launcher")" "Hermes launcher parent"
  assert_safe_dir "$AGENT_REPO" "Hermes Agent checkout"
  assert_safe_dir "$AGENT_REPO/.git" "Hermes Git metadata"
  require_digest "$AGENT_REPO/.git/HEAD" "Hermes Git HEAD" "$(line_digest "$UPSTREAM")"
  head="$(git_checked rev-parse --verify HEAD 2>/dev/null)" \
    || fail "Hermes checkout HEAD cannot be verified"
  [[ "$head" == "$UPSTREAM" ]] || fail "Hermes checkout is not the pinned installer commit"
  if git_checked symbolic-ref -q HEAD >/dev/null 2>&1; then
    fail "Hermes checkout is not detached at the pinned commit"
  fi
  git_checked diff-index --quiet "$UPSTREAM" -- \
    || fail "Hermes tracked checkout differs from the pinned commit"
  git_checked ls-files --error-unmatch -- hermes >/dev/null 2>&1 \
    || fail "Hermes checked-in launcher is not tracked"
  verify_marker
  require_digest "$AGENT_REPO/.install_method" "Hermes install method" "$(line_digest git)"
  assert_safe_regular "$AGENT_REPO/hermes" "Hermes checked-in launcher"
  [[ -x "$AGENT_REPO/venv/bin/python" ]] || fail "Hermes virtual environment is incomplete"
  assert_safe_regular "$HERMES_HOME_ARG/bin/uv" "Hermes managed uv"
  [[ -x "$HERMES_HOME_ARG/bin/uv" ]] || fail "Hermes managed uv is not executable"
  require_digest "$launcher" "Hermes public launcher" "$(launcher_digest)"
  [[ -x "$launcher" ]] || fail "Hermes public launcher is not executable"
}
verify_receipt() {
  local receipt_label="existing or partial Hermes bootstrap receipt"
  assert_state_authority
  assert_safe_regular "$receipt" "$receipt_label"
  require_mode "$receipt" "$receipt_label" 600
  require_digest "$receipt" "$receipt_label" "$(expected_receipt_digest)"
  verify_installer_artifacts
}

if [[ -e "$receipt" || -L "$receipt" ]]; then
  verify_receipt
  printf 'bootstrap-complete\n'
  exit 0
fi
if [[ "$MODE" == status ]]; then
  assert_safe_ancestors "$state_dir" "Hermes bootstrap state"
  printf 'bootstrap-absent\n'
  exit 0
fi

for marker in \
  "$AGENT_REPO" "$release_root" "$launcher" "$HOME/.local/bin/hermes-agent" \
  "$HOME/.local/bin/hermes-acp" "$HERMES_HOME_ARG/bin/uv" "$HERMES_HOME_ARG/node" \
  "$HERMES_HOME_ARG/config.yaml" "$HERMES_HOME_ARG/.env" "$HERMES_HOME_ARG/auth.json" \
  "$HERMES_HOME_ARG/state.db" "$HOME/Applications/Hermes.app" "/Applications/Hermes.app" \
  "$receipt"
do
  [[ ! -e "$marker" && ! -L "$marker" ]] || fail "existing or partial Hermes state: $marker"
done
assert_safe_ancestors "$AGENT_REPO" "Hermes Agent checkout"
assert_safe_ancestors "$HERMES_HOME_ARG" "Hermes data home"
assert_safe_ancestors "$state_dir" "Hermes bootstrap state"
assert_safe_ancestors "$(/usr/bin/dirname "$launcher")" "Hermes launcher parent"
state_opening_identity="absent"
if [[ -e "$state_dir" || -L "$state_dir" ]]; then
  assert_state_authority
  state_opening_identity="$(stat_identity "$state_dir")"
fi

/usr/bin/curl --proto '=https' --proto-redir '=https' --tlsv1.2 \
  --fail --silent --show-error --location --output "$installer" "$INSTALLER_URL" \
  || fail "unable to fetch frozen official installer"
require_digest "$installer" "official installer" "$INSTALLER_SHA256"

/usr/bin/env -i \
  HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" TMPDIR="$tmpdir" \
  HERMES_AGENT_REPO="$AGENT_REPO" HERMES_HOME="$HERMES_HOME_ARG" \
  /bin/bash "$installer" \
    --commit "$UPSTREAM" --dir "$AGENT_REPO" --hermes-home "$HERMES_HOME_ARG" \
    --skip-setup --non-interactive

assert_safe_ancestors "$AGENT_REPO" "Hermes Agent checkout"
assert_safe_ancestors "$HERMES_HOME_ARG" "Hermes data home"
assert_safe_ancestors "$state_dir" "Hermes bootstrap state"
assert_safe_ancestors "$(/usr/bin/dirname "$launcher")" "Hermes launcher parent"
[[ ! -e "$receipt" && ! -L "$receipt" ]] \
  || fail "foreign Hermes bootstrap receipt appeared during installer execution"
[[ ! -e "$release_root" && ! -L "$release_root" ]] \
  || fail "foreign Hermes release root appeared during installer execution"
for marker in \
  "$HERMES_HOME_ARG/config.yaml" "$HERMES_HOME_ARG/.env" "$HERMES_HOME_ARG/auth.json" \
  "$HERMES_HOME_ARG/state.db" "$HOME/Applications/Hermes.app" "/Applications/Hermes.app"
do
  [[ ! -e "$marker" && ! -L "$marker" ]] \
    || fail "unexpected Hermes state appeared during installer execution: $marker"
done
if [[ "$state_opening_identity" == absent ]]; then
  [[ ! -e "$state_dir" && ! -L "$state_dir" ]] \
    || fail "foreign Hermes bootstrap state appeared during installer execution"
else
  [[ "$(stat_identity "$state_dir")" == "$state_opening_identity" ]] \
    || fail "Hermes bootstrap state changed during installer execution"
  assert_state_authority
fi
verify_installer_artifacts

if [[ "$state_opening_identity" == absent ]]; then
  /bin/mkdir -p "$state_dir"
  /bin/chmod 700 "$state_dir"
fi
assert_state_authority
[[ ! -e "$receipt" && ! -L "$receipt" ]] \
  || fail "foreign Hermes bootstrap receipt appeared before publication"
receipt_tmp=$(/usr/bin/mktemp "$state_dir/.hermes-bootstrap.receipt.XXXXXX") \
  || fail "unable to stage bootstrap receipt"
expected_receipt >"$receipt_tmp"
/bin/chmod 600 "$receipt_tmp"
assert_safe_regular "$receipt_tmp" "staged Hermes bootstrap receipt"
require_mode "$receipt_tmp" "staged Hermes bootstrap receipt" 600
/bin/ln "$receipt_tmp" "$receipt" \
  || fail "Hermes bootstrap receipt successor prevented publication"
/bin/rm -f "$receipt_tmp"
receipt_tmp=""
verify_receipt
printf 'Hermes runtime bootstrap complete; provider setup and authenticated smoke may still be pending.\n'
