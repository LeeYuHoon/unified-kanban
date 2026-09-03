#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(/usr/bin/dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
MANIFEST="$REPO_ROOT/patches/hermes-agent-bootstrap-manifest"
UPSTREAM_FILE="$REPO_ROOT/patches/hermes-agent-supported-upstream"
MODE=install
case "${1:-}" in
  --status) MODE=status; shift ;;
  --validate-paths) MODE=validate-paths; shift ;;
esac
AGENT_REPO="${1:-}"
HERMES_HOME_ARG="${2:-}"

fail() {
  printf 'Hermes bootstrap refused: %s\n' "$1" >&2
  exit 1
}

normalize_absolute_path() {
  local path="$1" label="$2" remainder component normalized=""
  remainder="${path#/}"
  while [[ -n "$remainder" ]]; do
    if [[ "$remainder" == */* ]]; then
      component="${remainder%%/*}"
      remainder="${remainder#*/}"
    else
      component="$remainder"
      remainder=""
    fi
    case "$component" in
      ""|.) continue ;;
      ..) fail "$label must not contain traversal components: $path" ;;
      *) normalized="$normalized/$component" ;;
    esac
  done
  [[ -n "$normalized" ]] || fail "$label must not be the filesystem root: $path"
  printf '%s\n' "$normalized"
}

[[ "$(/usr/bin/uname -s)" == Darwin ]] || fail "automatic first-install is supported only on macOS"
[[ "$AGENT_REPO" == /* ]] || fail "HERMES_AGENT_REPO must be an absolute path"
[[ "$HERMES_HOME_ARG" == /* ]] || fail "HERMES_HOME must be an absolute path"
[[ "$HOME" == /* ]] || fail "HOME must be an absolute path"
case "$AGENT_REPO$HERMES_HOME_ARG$HOME${XDG_STATE_HOME:-}" in
  *$'\n'*) fail "Hermes paths must not contain newlines" ;;
esac
AGENT_REPO="$(normalize_absolute_path "$AGENT_REPO" HERMES_AGENT_REPO)"
HERMES_HOME_ARG="$(normalize_absolute_path "$HERMES_HOME_ARG" HERMES_HOME)"

umask 077
tmpdir=""
installer=""
marker_snapshot=""
receipt_tmp=""
receipt_staging=""
installer_started=0
cleanup() {
  if [[ -n "$installer" ]]; then
    /bin/rm -f "$installer" "$marker_snapshot"
  fi
  if [[ -n "$receipt_staging" && "$installer_started" == 0 ]]; then
    /bin/rm -f "$receipt_staging"
  fi
  if [[ -n "$receipt_tmp" && "$installer_started" == 0 ]]; then
    /bin/rm -f "$receipt_tmp"
  fi
  if [[ -n "$tmpdir" ]]; then
    /bin/rmdir "$tmpdir" 2>/dev/null || true
  fi
}
terminate() {
  local status="$1"
  trap - EXIT HUP INT TERM
  cleanup
  exit "$status"
}
trap cleanup EXIT
trap 'terminate 129' HUP
trap 'terminate 130' INT
trap 'terminate 143' TERM

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
assert_system_perl() {
  local component identity device inode owner mode links
  for component in / /usr /usr/bin; do
    [[ -d "$component" && ! -L "$component" ]] \
      || fail "macOS system Perl ancestry is unsafe: $component"
    identity="$(stat_identity "$component")" \
      || fail "cannot inspect macOS system Perl ancestry: $component"
    IFS=: read -r device inode owner mode links <<<"$identity"
    [[ "$owner" == 0 ]] || fail "macOS system Perl ancestry is not root-owned: $component"
    (( (8#$mode & 8#022) == 0 )) \
      || fail "macOS system Perl ancestry is writable: $component"
  done
  [[ -f /usr/bin/perl && ! -L /usr/bin/perl && -x /usr/bin/perl ]] \
    || fail "macOS system Perl is missing or unsafe"
  identity="$(stat_identity /usr/bin/perl)" || fail "cannot inspect macOS system Perl"
  IFS=: read -r device inode owner mode links <<<"$identity"
  [[ "$owner" == 0 ]] || fail "macOS system Perl is not root-owned"
  (( (8#$mode & 8#022) == 0 )) || fail "macOS system Perl is writable by group or other"
  [[ "$links" == 1 ]] || fail "macOS system Perl must have exactly one hard link"
}
system_sync_path() {
  local path="$1" label="$2" before after perl_before perl_after
  assert_system_perl
  before="$(stat_identity "$path")" || fail "cannot inspect $label before durability sync"
  perl_before="$(stat_identity /usr/bin/perl)" || fail "cannot bind macOS system Perl"
  /usr/bin/env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    /usr/bin/perl -MIO::Handle -MFcntl=:DEFAULT -e '
my ($path) = @ARGV;
my $flags = O_RDONLY;
$flags |= eval { O_NOFOLLOW() } || 0;
sysopen(my $fh, $path, $flags) or die "open failed: $!\n";
$fh->sync() or die "sync failed: $!\n";
' "$path" || fail "$label durability sync failed"
  after="$(stat_identity "$path")" || fail "cannot inspect $label after durability sync"
  perl_after="$(stat_identity /usr/bin/perl)" || fail "cannot revalidate macOS system Perl"
  [[ "$before" == "$after" ]] || fail "$label changed during durability sync"
  [[ "$perl_before" == "$perl_after" ]] || fail "macOS system Perl changed during durability sync"
}
mkdir_private_durable_preinstall() {
  local path="$1" label="$2" probe="" remainder component parent
  local identity device inode owner mode links created
  [[ "$path" == /* ]] || fail "$label must be absolute"
  remainder="${path#/}"
  while [[ -n "$remainder" ]]; do
    if [[ "$remainder" == */* ]]; then
      component="${remainder%%/*}"
      remainder="${remainder#*/}"
    else
      component="$remainder"
      remainder=""
    fi
    parent="${probe:-/}"
    probe="$probe/$component"
    created=0
    if [[ ! -e "$probe" && ! -L "$probe" ]]; then
      /bin/mkdir -m 700 "$probe" || fail "$label creation failed"
      created=1
    fi
    [[ -d "$probe" && ! -L "$probe" ]] || fail "$label ancestry is unsafe: $probe"
    identity="$(stat_identity "$probe")" || fail "cannot inspect $label ancestry: $probe"
    IFS=: read -r device inode owner mode links <<<"$identity"
    [[ "$owner" == 0 || "$owner" == "$owner_uid" ]] \
      || fail "$label ancestry has a foreign owner: $probe"
    (( (8#$mode & 8#022) == 0 )) || fail "$label ancestry is writable: $probe"
    if [[ "$created" == 1 ]]; then
      [[ "$owner" == "$owner_uid" && "$mode" == 700 ]] \
        || fail "$label new component is not private: $probe"
    fi
    if [[ "$owner" == "$owner_uid" && "$mode" == 700 ]]; then
      system_sync_path "$parent" "$label containing parent"
    fi
  done
  assert_safe_dir "$path" "$label"
  require_mode "$path" "$label" 700
}
assert_safe_ancestors "$HOME" "HOME"
assert_safe_ancestors "$AGENT_REPO" "Hermes Agent checkout"
assert_safe_ancestors "${AGENT_REPO}.releases" "Hermes release root"
assert_safe_ancestors "$HERMES_HOME_ARG" "Hermes data home"
if [[ "$MODE" == validate-paths ]]; then
  printf 'paths-safe\n'
  exit 0
fi
if [[ "$MODE" == install ]]; then
  assert_system_perl
fi
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

MANIFEST_SHA256="6459dfe6508ceefa5e8973ddf8e3a34674c743118a14bcff740ce787bb060416"
UPSTREAM_FILE_SHA256="ac0ca1f125898641447f190af1e2b7674ebec757ce5cc337ef1f55067c30d37f"
UPSTREAM="63279301bcbdc185c1b07b98a9312eb0c862f26d"
INSTALLER_SHA256="5854b15670b51a8daae8f59ddfa917062de9f74be261eb73b4b8d719710f8968"
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
readlink_exact() {
  local link="$1" captured
  assert_system_perl
  captured=$(
    /usr/bin/env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
      /usr/bin/perl -e \
      'my $target = readlink($ARGV[0]); exit 74 unless defined $target; print $target, chr(1);' \
      "$link"
  ) || fail "cannot inspect managed Python symlink target"
  [[ "$captured" == *$'\001' ]] \
    || fail "cannot delimit managed Python symlink target"
  managed_python_readlink="${captured%$'\001'}"
}

verify_managed_python_authority() {
  local link="$AGENT_REPO/venv/bin/python" runtime_root target
  local identity device inode owner mode links
  runtime_root="$AGENT_REPO/.hermes-runtime/python"
  assert_safe_ancestors "$AGENT_REPO/venv/bin" "Hermes virtual environment Python parent"
  [[ -L "$link" ]] || fail "Hermes virtual environment Python is not the installer-managed symlink"
  identity="$(stat_identity "$link")" || fail "cannot inspect Hermes virtual environment Python symlink"
  IFS=: read -r device inode owner mode links <<<"$identity"
  [[ "$owner" == "$owner_uid" ]] || fail "Hermes virtual environment Python symlink has a foreign owner"
  (( (8#$mode & 8#022) == 0 )) || fail "Hermes virtual environment Python symlink is writable by group or other"
  [[ "$links" == 1 ]] || fail "Hermes virtual environment Python symlink must have exactly one hard link"
  readlink_exact "$link"
  target="$managed_python_readlink"
  [[ "$target" == /* ]] || fail "Hermes virtual environment Python symlink is not absolute"
  case "$target" in
    *$'\n'*|*'//'*|*'/./'*|*'/../'*|*/.|*/..)
      fail "Hermes virtual environment Python target is not lexically normalized"
      ;;
  esac
  case "$target" in
    "$runtime_root"/*) ;;
    *) fail "Hermes virtual environment Python target escapes the managed runtime" ;;
  esac
  assert_safe_ancestors "$(/usr/bin/dirname "$target")" "Hermes managed Python target parent"
  assert_safe_regular "$target" "Hermes managed Python target"
  [[ -x "$target" ]] || fail "Hermes managed Python target is not executable"
  MANAGED_PYTHON_LINK_IDENTITY="$identity"
  MANAGED_PYTHON_TARGET="$target"
  MANAGED_PYTHON_TARGET_IDENTITY="$(stat_identity "$target")" \
    || fail "cannot bind Hermes managed Python target"
}
verify_managed_python_stable() {
  [[ "$(stat_identity "$AGENT_REPO/venv/bin/python")" == "$MANAGED_PYTHON_LINK_IDENTITY" ]] \
    || fail "Hermes virtual environment Python symlink changed during verification"
  readlink_exact "$AGENT_REPO/venv/bin/python"
  [[ "$managed_python_readlink" == "$MANAGED_PYTHON_TARGET" ]] \
    || fail "Hermes virtual environment Python target changed during verification"
  [[ "$(stat_identity "$MANAGED_PYTHON_TARGET")" == "$MANAGED_PYTHON_TARGET_IDENTITY" ]] \
    || fail "Hermes managed Python target changed during verification"
}
verify_installer_artifacts() {
  local head python_status node_major
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
  verify_managed_python_authority
  python_status=$(
    /usr/bin/env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
      "$AGENT_REPO/venv/bin/python" -c \
      'import sys; print("python-3.11-ok" if sys.version_info >= (3, 11) else "python-too-old")'
  ) || fail "Hermes virtual environment Python 3.11 requirement cannot be verified"
  verify_managed_python_stable
  [[ "$python_status" == python-3.11-ok ]] \
    || fail "Hermes virtual environment does not satisfy Python 3.11 or newer"
  assert_safe_ancestors "$HERMES_HOME_ARG/bin" "Hermes managed uv parent"
  assert_safe_regular "$HERMES_HOME_ARG/bin/uv" "Hermes managed uv"
  [[ -x "$HERMES_HOME_ARG/bin/uv" ]] || fail "Hermes managed uv is not executable"
  assert_safe_ancestors "$HERMES_HOME_ARG/node/bin" "Hermes managed Node 26 parent"
  assert_safe_regular "$HERMES_HOME_ARG/node/bin/node" "Hermes managed Node 26"
  [[ -x "$HERMES_HOME_ARG/node/bin/node" ]] || fail "Hermes managed Node 26 is not executable"
  node_major=$(
    /usr/bin/env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
      "$HERMES_HOME_ARG/node/bin/node" -p 'process.versions.node.split(".")[0]'
  ) || fail "Hermes managed Node 26 version cannot be verified"
  [[ "$node_major" == 26 ]] || fail "Hermes managed runtime is not Node 26"
  require_digest "$launcher" "Hermes public launcher" "$(launcher_digest)"
  [[ -x "$launcher" ]] || fail "Hermes public launcher is not executable"
}
fsync_regular() {
  local path="$1" label="$2"
  /usr/bin/env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    "$AGENT_REPO/venv/bin/python" -c '
import os, stat, sys
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(sys.argv[1], flags)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError("unsafe regular file")
    os.fsync(fd)
finally:
    os.close(fd)
' "$path" || fail "$label durability sync failed"
}
fsync_directory() {
  local path="$1" label="$2"
  /usr/bin/env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    "$AGENT_REPO/venv/bin/python" -c '
import os, stat, sys
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(sys.argv[1], flags)
try:
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        raise RuntimeError("unsafe directory")
    os.fsync(fd)
finally:
    os.close(fd)
' "$path" || fail "$label durability sync failed"
}

mkdir_private_durable() {
  local path="$1" label="$2"
  /usr/bin/env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    "$AGENT_REPO/venv/bin/python" -c '
import os, stat, sys

path = sys.argv[1]
if not path.startswith("/") or os.path.normpath(path) != path:
    raise RuntimeError("unsafe directory path")
current = "/"
for component in path[1:].split("/"):
    next_path = os.path.join(current, component)
    created = False
    try:
        info = os.lstat(next_path)
    except FileNotFoundError:
        os.mkdir(next_path, 0o700)
        info = os.lstat(next_path)
        created = True
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid not in (0, os.getuid())
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise RuntimeError("unsafe directory ancestry")
    if created:
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError("new directory is not private")
    if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(current, flags)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    current = next_path
parent = os.path.dirname(path)
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
parent_fd = os.open(parent, flags)
try:
    os.fsync(parent_fd)
finally:
    os.close(parent_fd)
' "$path" || fail "$label durable creation failed"
}

recover_published_receipt_candidate() {
  local identity candidate_identity device inode owner mode links output digest
  local candidate candidates=()
  [[ ! -L "$receipt" ]] || return 0
  identity="$(stat_identity "$receipt")" || return 0
  IFS=: read -r device inode owner mode links <<<"$identity"
  [[ "$links" == 2 ]] || return 0
  shopt -s nullglob
  candidates=("$state_dir"/.hermes-bootstrap.receipt.*)
  shopt -u nullglob
  [[ "${#candidates[@]}" == 1 ]] \
    || fail "published Hermes bootstrap receipt has ambiguous hard-link recovery state"
  candidate="${candidates[0]}"
  [[ ! -L "$candidate" && -f "$candidate" ]] \
    || fail "published Hermes bootstrap receipt candidate is unsafe"
  candidate_identity="$(stat_identity "$candidate")" \
    || fail "published Hermes bootstrap receipt candidate cannot be inspected"
  [[ "$candidate_identity" == "$identity" && "$owner" == "$owner_uid" && "$mode" == 600 ]] \
    || fail "published Hermes bootstrap receipt candidate does not match canonical authority"
  exec 7<"$receipt" || fail "cannot open published Hermes bootstrap receipt"
  output=$(/usr/bin/shasum -a 256 <&7) \
    || fail "cannot hash published Hermes bootstrap receipt"
  exec 7<&-
  [[ "$(stat_identity "$receipt")" == "$identity" ]] \
    || fail "published Hermes bootstrap receipt changed during recovery"
  digest="${output%% *}"
  [[ "$digest" == "$(expected_receipt_digest)" ]] \
    || fail "published Hermes bootstrap receipt exact-byte digest disagrees with reviewed policy"
  verify_installer_artifacts
  mkdir_private_durable "$state_dir" "Hermes bootstrap state"
  /bin/rm -f "$candidate" \
    || fail "published Hermes bootstrap receipt candidate cleanup failed"
  fsync_directory "$state_dir" "published Hermes bootstrap receipt candidate cleanup"
}

verify_receipt() {
  local receipt_label="existing or partial Hermes bootstrap receipt"
  assert_state_authority
  recover_published_receipt_candidate
  assert_safe_regular "$receipt" "$receipt_label"
  require_mode "$receipt" "$receipt_label" 600
  require_digest "$receipt" "$receipt_label" "$(expected_receipt_digest)"
  verify_installer_artifacts
  mkdir_private_durable "$state_dir" "Hermes bootstrap state"
  fsync_regular "$receipt" "$receipt_label"
  fsync_directory "$state_dir" "Hermes bootstrap state"
  fsync_directory "$(/usr/bin/dirname "$state_dir")" "Hermes bootstrap state parent"
}

cleanup_preinstaller_staging() {
  local staging artifacts=() removed=0
  [[ -e "$state_dir" || -L "$state_dir" ]] || return 0
  assert_state_authority
  shopt -s nullglob
  artifacts=("$state_dir"/.hermes-bootstrap.staging.*)
  shopt -u nullglob
  [[ "${#artifacts[@]}" != 0 ]] || return 0
  for staging in "${artifacts[@]}"; do
    assert_safe_regular "$staging" "pre-installer Hermes bootstrap staging artifact"
    require_mode "$staging" "pre-installer Hermes bootstrap staging artifact" 600
    /bin/rm -f "$staging" || fail "pre-installer Hermes bootstrap staging cleanup failed"
    removed=1
  done
  if [[ "$removed" == 1 ]]; then
    system_sync_path "$state_dir" "pre-installer Hermes bootstrap staging cleanup"
  fi
}
recover_unpublished_receipt_candidate() {
  local candidate candidates=()
  [[ ! -e "$receipt" && ! -L "$receipt" ]] || return 0
  [[ -e "$state_dir" || -L "$state_dir" ]] || return 0
  assert_state_authority
  shopt -s nullglob
  candidates=("$state_dir"/.hermes-bootstrap.receipt.*)
  shopt -u nullglob
  [[ "${#candidates[@]}" != 0 ]] || return 0
  [[ "${#candidates[@]}" == 1 ]] \
    || fail "unpublished Hermes bootstrap receipt recovery is ambiguous"
  candidate="${candidates[0]}"
  assert_safe_regular "$candidate" "unpublished Hermes bootstrap receipt candidate"
  require_mode "$candidate" "unpublished Hermes bootstrap receipt candidate" 600
  require_digest "$candidate" "unpublished Hermes bootstrap receipt candidate" \
    "$(expected_receipt_digest)"
  if [[ ! -e "$AGENT_REPO" && ! -L "$AGENT_REPO" ]]; then
    system_sync_path "$candidate" "pre-installer armed Hermes bootstrap candidate"
    /bin/rm -f "$candidate" \
      || fail "pre-installer armed Hermes bootstrap candidate cleanup failed"
    system_sync_path "$state_dir" "pre-installer armed Hermes bootstrap candidate cleanup"
    return 0
  fi
  verify_installer_artifacts
  mkdir_private_durable "$state_dir" "Hermes bootstrap state"
  fsync_regular "$candidate" "unpublished Hermes bootstrap receipt candidate durability"
  [[ ! -e "$receipt" && ! -L "$receipt" ]] \
    || fail "Hermes bootstrap receipt successor prevented recovery publication"
  /bin/ln "$candidate" "$receipt" \
    || fail "Hermes bootstrap receipt successor prevented recovery publication"
  fsync_directory "$state_dir" "Hermes bootstrap receipt recovery publication"
  /bin/rm -f "$candidate" \
    || fail "Hermes bootstrap receipt recovery candidate cleanup failed"
  fsync_directory "$state_dir" "Hermes bootstrap receipt recovery cleanup"
}

recover_unpublished_receipt_candidate
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

mkdir_private_durable_preinstall "$state_dir" "Hermes bootstrap state"
assert_state_authority
[[ ! -e "$receipt" && ! -L "$receipt" ]] \
  || fail "foreign Hermes bootstrap receipt appeared before installer execution"
cleanup_preinstaller_staging
receipt_staging=$(/usr/bin/mktemp "$state_dir/.hermes-bootstrap.staging.XXXXXX") \
  || fail "unable to stage bootstrap receipt"
expected_receipt >"$receipt_staging"
/bin/chmod 600 "$receipt_staging"
assert_safe_regular "$receipt_staging" "staged Hermes bootstrap receipt"
require_mode "$receipt_staging" "staged Hermes bootstrap receipt" 600
system_sync_path "$receipt_staging" "staged Hermes bootstrap receipt"
system_sync_path "$state_dir" "staged Hermes bootstrap receipt directory entry"
receipt_tmp="$state_dir/.hermes-bootstrap.receipt.${receipt_staging##*.hermes-bootstrap.staging.}"
[[ ! -e "$receipt_tmp" && ! -L "$receipt_tmp" ]] \
  || fail "Hermes bootstrap receipt candidate arm collision"
/bin/mv "$receipt_staging" "$receipt_tmp" \
  || fail "Hermes bootstrap receipt candidate arm failed"
receipt_staging=""
system_sync_path "$state_dir" "armed Hermes bootstrap receipt candidate"
state_opening_identity="$(stat_identity "$state_dir")"
installer_started=1

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
[[ "$(stat_identity "$state_dir")" == "$state_opening_identity" ]] \
  || fail "Hermes bootstrap state changed during installer execution"
assert_state_authority
verify_installer_artifacts

mkdir_private_durable "$state_dir" "Hermes bootstrap state"
assert_state_authority
[[ ! -e "$receipt" && ! -L "$receipt" ]] \
  || fail "foreign Hermes bootstrap receipt appeared before publication"
assert_safe_regular "$receipt_tmp" "staged Hermes bootstrap receipt"
require_mode "$receipt_tmp" "staged Hermes bootstrap receipt" 600
require_digest "$receipt_tmp" "staged Hermes bootstrap receipt" "$(expected_receipt_digest)"
fsync_regular "$receipt_tmp" "staged Hermes bootstrap receipt"
/bin/ln "$receipt_tmp" "$receipt" \
  || fail "Hermes bootstrap receipt successor prevented publication"
fsync_directory "$state_dir" "Hermes bootstrap receipt publication"
/bin/rm -f "$receipt_tmp"
fsync_directory "$state_dir" "Hermes bootstrap receipt staging cleanup"
receipt_tmp=""
verify_receipt
printf 'Hermes runtime bootstrap complete; provider setup and authenticated smoke may still be pending.\n'
