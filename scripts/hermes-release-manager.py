#!/usr/bin/env python3
"""Build immutable Hermes source releases without mutating the source checkout."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import importlib.util
import json
import marshal
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unicodedata
from pathlib import Path
from typing import NamedTuple

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kanban_adapter.release_layout import (  # noqa: E402
    COMPLETION_RECEIPT_NAME,
    release_directory,
    release_root,
    release_selector,
)

OFFICIAL_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"
TRUSTED_GIT = "/usr/bin/git"
SHA_RE = re.compile(r"[0-9a-f]{40}")
_RENAME_EXCL = 0x00000004
_COMPLETION_RECEIPT = COMPLETION_RECEIPT_NAME
_BYTECODE_FINGERPRINT = ".bytecode-fingerprint"
BASELINE_ABSENT = "absent"
_BASELINE_RE = re.compile(r"absent|sha256:[0-9a-f]{64}")
_BASELINE_MARKER = "# unified-kanban-hermes-baseline"

# The supported platform is macOS, whose default volume compares filenames after
# Unicode normalization *and* full case folding. Two reviewed source paths that
# fold together therefore cannot both exist in the release worktree: one silently
# aliases the other, the checkout looks dirty, and every build fails closed.
#
# Exactly one upstream namespace is permitted to fold, and only because it is
# pure release-time metadata: `contributors/emails/<commit-author-email>` maps a
# commit-author address to a GitHub login for upstream's own attribution CI. It
# is never imported, never executed, never read by the Hermes runtime, and never
# an input to dependency resolution or configuration. Nothing else may fold -
# an aliased module, lockfile, config, or executable would silently change what
# the release runs, so those stay a hard build failure.
METADATA_COLLISION_NAMESPACES = ("contributors/emails/",)
_PLAIN_FILE_MODE = "100644"
_DIRECTORY_MODE = "040000"
# Scoped, single-command permission for the only two file-transport reads a
# build may perform: the private bundle copy, and a test-only local source.
_FILE_TRANSPORT = ("-c", "protocol.file.allow=always")


class ForeignLauncher(RuntimeError):
    """Raised when a launcher is not exactly the managed launcher."""


class CaseCollision(NamedTuple):
    """One reviewed group of source paths a supported volume folds together."""

    key: str
    representative: str
    members: tuple[tuple[str, str, str], ...]


class ReleaseLayout(NamedTuple):
    root: Path
    release: Path
    selector: Path


def _sha(value: str, label: str) -> str:
    if SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a full lowercase SHA-1")
    return value


def release_layout(agent_repo: Path, upstream: str, carried: str) -> ReleaseLayout:
    _sha(upstream, "upstream")
    _sha(carried, "carried")
    # The shared normal form is what shell callers concatenate ".releases" onto,
    # so a trailing slash or a "/./" component can never place the release root
    # inside the checkout for one caller and beside it for another.
    return ReleaseLayout(
        release_root(agent_repo),
        release_directory(agent_repo, carried),
        release_selector(agent_repo),
    )


def _safe_git_env() -> dict[str, str]:
    # Release construction runs inside the setup transaction, so the ambient
    # environment still names the private transaction directory and its
    # descriptor numbers. Git is an external command and gets none of it.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
        and not key.startswith("UNIFIED_KANBAN_")
        and key not in {"SSH_ASKPASS", "GITHUB_TOKEN", "GH_TOKEN"}
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    return env


def _run_git_binary(repository: Path, *arguments: str) -> bytes:
    """Run Git for output that must stay bytes: blobs and NUL-delimited paths."""
    completed = subprocess.run(
        [TRUSTED_GIT, "-C", str(repository), *arguments],
        env=_safe_git_env(),
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = [
            line
            for line in completed.stderr.decode("utf-8", "replace").splitlines()
            if line.strip()
        ]
        raise RuntimeError(
            f"git {' '.join(arguments)} failed: {detail[-1] if detail else completed.returncode}"
        )
    return completed.stdout


def _run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [TRUSTED_GIT, "-C", str(repository), *arguments],
        env=_safe_git_env(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = [line for line in completed.stderr.splitlines() if line.strip()]
        raise RuntimeError(
            f"git {' '.join(arguments)} failed: {detail[-1] if detail else completed.returncode}"
        )
    return completed.stdout.strip()


def _porcelain_status(repository: Path) -> list[str]:
    """Return NUL-delimited status records, so no path is ever shell-quoted."""
    raw = _run_git_binary(
        repository, "-c", "core.fsmonitor=false", "status", "--porcelain", "-z"
    )
    return [os.fsdecode(record) for record in raw.split(b"\0") if record]


def _porcelain_status_all(repository: Path) -> list[str]:
    """Return every untracked file rather than collapsing owned output directories."""
    raw = _run_git_binary(
        repository,
        "-c",
        "core.fsmonitor=false",
        "status",
        "--porcelain",
        "--untracked-files=all",
        "-z",
    )
    return [os.fsdecode(record) for record in raw.split(b"\0") if record]


def _collision_key(path: str) -> str:
    """Return the normal form a supported volume compares two paths by."""
    return unicodedata.normalize("NFC", path).casefold()


def _checked_case_collision(key: str, members: tuple[tuple[str, str, str], ...]) -> CaseCollision:
    """Admit one folded group only under the reviewed metadata policy."""
    for path, mode, oid in members:
        namespace = next(
            (name for name in METADATA_COLLISION_NAMESPACES if path.startswith(name)), None
        )
        leaf = "" if namespace is None else path[len(namespace) :]
        if namespace is None or not leaf or "/" in leaf:
            raise RuntimeError(
                "case-fold collision outside the reviewed non-runtime metadata "
                f"namespace: {path}"
            )
        if mode != _PLAIN_FILE_MODE:
            raise RuntimeError(
                f"case-fold collision on a non-plain-file entry: mode {mode} at {path}"
            )
        if SHA_RE.fullmatch(oid) is None:
            raise RuntimeError(f"case-fold collision member has no reviewed blob: {path}")
    if len({path.rsplit("/", 1)[0] for path, _, _ in members}) != 1:
        raise RuntimeError(f"case-fold collision spans more than one directory: {key}")
    # Git checks the index out in path-byte order, so the byte-greatest member is
    # the one a supported volume is left holding. Selecting it explicitly makes
    # the survivor a reviewed decision instead of a checkout-order side effect.
    representative = max(members, key=lambda member: os.fsencode(member[0]))[0]
    return CaseCollision(key=key, representative=representative, members=members)


def case_collisions(repository: Path, treeish: str) -> tuple[CaseCollision, ...]:
    """Return the folded path groups of one exact Git tree, or fail closed.

    Detection reads the tree rather than the volume, so the same reviewed source
    yields the same verdict on every filesystem. Ancestor directories are folded
    too: a directory that aliases another merges its children on a supported
    volume just as surely as two aliased files do.
    """
    listing = _run_git_binary(repository, "ls-tree", "-r", "-z", "--full-tree", treeish)
    groups: dict[str, dict[str, tuple[str, str]]] = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        metadata, separator, encoded = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise RuntimeError("release source tree listing is malformed")
        try:
            mode, _kind, oid = (field.decode("ascii") for field in fields)
        except UnicodeDecodeError as exc:
            raise RuntimeError("release source tree listing is malformed") from exc
        path = os.fsdecode(encoded)
        components = path.split("/")
        for depth in range(1, len(components)):
            prefix = "/".join(components[:depth])
            groups.setdefault(_collision_key(prefix), {}).setdefault(
                prefix, (_DIRECTORY_MODE, "")
            )
        groups.setdefault(_collision_key(path), {})[path] = (mode, oid)
    collisions = []
    for key in sorted(groups):
        members = groups[key]
        if len(members) < 2:
            continue
        ordered = tuple(
            sorted(
                ((path, mode, oid) for path, (mode, oid) in members.items()),
                key=lambda member: os.fsencode(member[0]),
            )
        )
        collisions.append(_checked_case_collision(key, ordered))
    return tuple(collisions)


def _collision_slot(worktree: Path, directory: str, key: str) -> list[str]:
    """Return every on-disk name in one directory that folds onto ``key``."""
    occupied = []
    try:
        with os.scandir(worktree / directory) as entries:
            for entry in entries:
                candidate = f"{directory}/{entry.name}"
                if _collision_key(candidate) != key:
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise RuntimeError(
                        f"case-fold collision slot is not a plain file: {candidate}"
                    )
                occupied.append(candidate)
    except OSError as exc:
        raise RuntimeError(
            f"case-fold collision directory is unreadable: {directory}"
        ) from exc
    return sorted(occupied, key=os.fsencode)


def _rewrite_collision_representative(worktree: Path, occupant: str, representative: str, data: bytes) -> None:
    """Replace whatever the checkout left aliased with the reviewed survivor.

    Unlinking first is what makes the surviving *name* deterministic: writing
    through the alias would keep whichever spelling the checkout happened to
    create, so the release inventory would depend on Git's write order.
    """
    os.unlink(worktree / occupant)
    descriptor = os.open(
        worktree / representative,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o644,
    )
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(errno.EIO, "collision representative write made no progress")
            remaining = remaining[written:]
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory((worktree / representative).parent)


def _materialized_case_collisions(
    worktree: Path, collisions: tuple[CaseCollision, ...], *, rewrite: bool
) -> list[dict[str, object]]:
    """Prove what one exact folded group actually became on this volume.

    ``rewrite`` is the single build-time pass that pins the survivor. Every later
    call observes only, so a completed release re-derives the identical record
    and any drift - edited bytes, a swapped spelling, a resurrected member -
    fails the reuse verification instead of being repaired in place.
    """
    records: list[dict[str, object]] = []
    for collision in collisions:
        directory = collision.representative.rsplit("/", 1)[0]
        blobs = {
            path: _run_git_binary(worktree, "cat-file", "blob", oid)
            for path, _mode, oid in collision.members
        }
        occupied = _collision_slot(worktree, directory, collision.key)
        members = [path for path, _mode, _oid in collision.members]
        if len(occupied) == 1:
            if rewrite:
                _rewrite_collision_representative(
                    worktree,
                    occupied[0],
                    collision.representative,
                    blobs[collision.representative],
                )
                occupied = _collision_slot(worktree, directory, collision.key)
            if occupied != [collision.representative]:
                raise RuntimeError(
                    "case-fold collision did not materialize as the reviewed "
                    f"representative: {occupied}"
                )
            materialized = [collision.representative]
        elif occupied == sorted(members, key=os.fsencode):
            # A volume that keeps the members apart loses nothing, so every
            # member stays and the whole group is still bound to the receipt.
            materialized = occupied
        else:
            raise RuntimeError(
                f"unexpected case-fold collision materialization: {occupied}"
            )
        entries = []
        for path in materialized:
            data = _stable_regular_bytes(worktree / path)
            if data != blobs[path]:
                raise RuntimeError(
                    f"case-fold collision content is not the reviewed blob: {path}"
                )
            entries.append([path, hashlib.sha256(data).hexdigest()])
        records.append(
            {
                "key": collision.key,
                "members": [list(member) for member in collision.members],
                "representative": collision.representative,
                "materialized": entries,
            }
        )
    return records


def _expected_collision_status(
    collisions: tuple[CaseCollision, ...], records: list[dict[str, object]]
) -> set[str]:
    """Return the exact status records a normalized release is allowed to show.

    A member that lost its slot still has an index entry, so Git reports it as
    modified against the survivor's bytes. That single predicted record is what
    a build may see - never a blanket tolerance for a dirty worktree.
    """
    expected: set[str] = set()
    for collision, record in zip(collisions, records):
        if len(record["materialized"]) != 1:
            continue
        survivor = next(
            oid
            for path, _mode, oid in collision.members
            if path == collision.representative
        )
        expected.update(
            f" M {path}"
            for path, _mode, oid in collision.members
            if path != collision.representative and oid != survivor
        )
    return expected


def _ensure_private_root(root: Path) -> None:
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        info = os.lstat(root)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"release root is not a real directory: {root}")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError(f"release root must have mode 0700: {root}")


def _rename_exclusive(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(libc, "renamex_np", None)
    if renamex_np is None:
        raise RuntimeError("exclusive release publication requires macOS renamex_np")
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    if renamex_np(os.fsencode(source), os.fsencode(destination), _RENAME_EXCL) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(code, os.strerror(code), str(destination))
        raise OSError(code, os.strerror(code), str(destination))


def selector_payload(layout: ReleaseLayout) -> bytes:
    """Return the regular-file selector payload for path-transaction publication."""
    if not layout.release.is_dir() or layout.release.is_symlink():
        raise RuntimeError("release must be a real directory before selection")
    return f"{layout.release}\n".encode("utf-8")


def baseline_token(path: Path | None) -> str:
    """Return the producer-issued binding for the launcher this install replaces.

    The token is embedded in the managed launcher at install time, so uninstall
    can prove the retained backup it is about to restore is the exact byte
    sequence the installer displaced. A foreign replacement, an injection where
    no original existed, or a deletion of the retained backup all change the
    token and therefore stop matching the installed launcher.
    """
    if path is None:
        return BASELINE_ABSENT
    return "sha256:" + hashlib.sha256(_stable_regular_bytes(Path(path))).hexdigest()


def launcher_payload(layout: ReleaseLayout, baseline: str) -> bytes:
    """Render a managed launcher that validates the regular release selector."""
    if _BASELINE_RE.fullmatch(baseline) is None:
        raise ValueError("launcher baseline must be 'absent' or 'sha256:<64 hex>'")
    code = r'''import os,re,stat,sys
selector,root=sys.argv[1:3]
flags=os.O_RDONLY|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
try:
 before=os.lstat(selector)
 fd=os.open(selector,flags)
 opened=os.fstat(fd)
 data=os.read(fd,4097)
 after=os.lstat(selector)
 os.close(fd)
 sig=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
 if not stat.S_ISREG(opened.st_mode) or opened.st_nlink!=1 or sig(before)!=sig(opened) or sig(opened)!=sig(after) or len(data)>4096:
  raise ValueError
 release=data.decode("utf-8").removesuffix("\n")
 if "\n" in release or os.path.dirname(release)!=root or re.fullmatch(r"release-[0-9a-f]{40}",os.path.basename(release)) is None:
  raise ValueError
 release_info=os.lstat(release)
 if not stat.S_ISDIR(release_info.st_mode) or stat.S_ISLNK(release_info.st_mode):
  raise ValueError
 lazy_target=os.path.join(root,"lazy-packages")
 try:
  os.mkdir(lazy_target,0o700)
 except FileExistsError:
  pass
 lazy_info=os.lstat(lazy_target)
 if not stat.S_ISDIR(lazy_info.st_mode) or stat.S_ISLNK(lazy_info.st_mode) or stat.S_IMODE(lazy_info.st_mode)!=0o700 or lazy_info.st_uid!=os.getuid():
  raise ValueError
 executable=os.path.join(release,"venv","bin","hermes")
 executable_info=os.lstat(executable)
 if not stat.S_ISREG(executable_info.st_mode) or stat.S_ISLNK(executable_info.st_mode) or executable_info.st_mode & 0o111 == 0:
  raise ValueError
except (OSError,UnicodeError,ValueError):
 print("invalid Hermes release selector",file=sys.stderr)
 raise SystemExit(126)
os.environ.pop("PYTHONPATH",None)
os.environ.pop("PYTHONHOME",None)
os.environ["HERMES_DISABLE_LAZY_INSTALLS"]="1"
os.environ["HERMES_LAZY_INSTALL_TARGET"]=lazy_target
os.execv(executable,[executable,*sys.argv[3:]])'''
    command = " ".join(
        (
            "exec /usr/bin/python3 -I -c",
            shlex.quote(code),
            shlex.quote(str(layout.selector)),
            shlex.quote(str(layout.root)),
            '"$@"',
        )
    )
    return f"#!/bin/sh\n{_BASELINE_MARKER} {baseline}\n{command}\n".encode("utf-8")


def launcher_baseline(layout: ReleaseLayout, data: bytes) -> str:
    """Return the binding of an exactly-managed launcher, else raise.

    Only the producer can state which launcher it displaced, so the binding is
    trusted solely when re-rendering it reproduces the candidate byte for byte.
    That makes every foreign body, every truncation or extension, and every
    malformed token a rejection rather than an adopted claim.
    """
    lines = data.split(b"\n")
    if len(lines) < 2 or not lines[1].startswith(_BASELINE_MARKER.encode() + b" "):
        raise ForeignLauncher("launcher carries no managed baseline binding")
    try:
        token = lines[1][len(_BASELINE_MARKER) + 1 :].decode("utf-8")
    except UnicodeError as exc:
        raise ForeignLauncher("launcher baseline binding is not UTF-8") from exc
    if _BASELINE_RE.fullmatch(token) is None:
        raise ForeignLauncher("launcher baseline binding is malformed")
    if launcher_payload(layout, token) != data:
        raise ForeignLauncher("launcher is not the managed launcher for this checkout")
    return token


def sync_release_dependencies(
    release: Path,
    *,
    uv: str | Path = "uv",
    base_env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> Path:
    """Create and lock-sync a release-local venv at its stable final path."""
    release = Path(release)
    if not release.is_absolute() or not re.fullmatch(r"release-[0-9a-f]{40}", release.name):
        raise ValueError("release must be an absolute stable release path")
    if not release.is_dir() or release.is_symlink():
        raise RuntimeError("release must be a real directory")
    release_info = os.lstat(release)
    release_identity = (release_info.st_dev, release_info.st_ino)
    uv = Path(uv)
    if not uv.is_absolute():
        raise ValueError("dependency construction requires an absolute uv executable")
    uv_info = os.lstat(uv)
    if not stat.S_ISREG(uv_info.st_mode) or uv_info.st_nlink != 1 or uv_info.st_mode & 0o111 == 0:
        raise RuntimeError("uv executable must be a single-link executable regular file")
    uv_identity = (uv_info.st_dev, uv_info.st_ino)
    lockfile = release / "uv.lock"
    if not lockfile.is_file() or lockfile.is_symlink():
        raise RuntimeError("immutable release requires a regular uv.lock")
    ambient = os.environ if base_env is None else base_env
    env = {
        key: ambient[key]
        for key in ("HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if key in ambient
    }
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    if extra_env:
        env.update(extra_env)
    venv = release / "venv"
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)

    def run(*arguments: str) -> None:
        current_release = os.lstat(release)
        current_uv = os.lstat(uv)
        if (current_release.st_dev, current_release.st_ino) != release_identity:
            raise RuntimeError("release identity changed before dependency construction")
        if (current_uv.st_dev, current_uv.st_ino) != uv_identity:
            raise RuntimeError("uv executable identity changed before dependency construction")
        completed = subprocess.run(
            [str(uv), *arguments],
            cwd=release,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        after_release = os.lstat(release)
        after_uv = os.lstat(uv)
        if (after_release.st_dev, after_release.st_ino) != release_identity:
            raise RuntimeError("release identity changed during dependency construction")
        if (after_uv.st_dev, after_uv.st_ino) != uv_identity:
            raise RuntimeError("uv executable identity changed during dependency construction")
        if completed.returncode != 0:
            # uv opens with progress and advisory lines and puts the decisive
            # error last, so reporting the first line hid a locked-sync refusal
            # behind an informational note about resolution.
            detail = [line for line in completed.stderr.strip().splitlines() if line.strip()]
            raise RuntimeError(
                f"uv {' '.join(arguments)} failed: {detail[-1] if detail else completed.returncode}"
            )

    # `UV_NO_CONFIG` would also discard the *reviewed* project's own `[tool.uv]`
    # settings. Hermes locks under a `[tool.uv] exclude-newer` span, so dropping
    # that setting makes uv re-resolve and `--locked` refuse - every production
    # release build failed there. Point uv's user configuration directory at an
    # empty private one instead. The reviewed pyproject still applies, and the
    # invoking user's `~/.config/uv/uv.toml` cannot reach this build: project
    # configuration already takes precedence over it, and for an upstream that
    # carries no `[tool.uv]` of its own there is now nothing to inherit.
    with tempfile.TemporaryDirectory(prefix="unified-kanban-uv-config-") as isolated_config:
        os.chmod(isolated_config, 0o700)
        env["XDG_CONFIG_HOME"] = isolated_config
        run("venv", str(venv), "--python", "3.11")
        env["UV_PYTHON"] = str(venv / "bin" / "python")
        # The production gateway may enable Telegram before any interactive
        # process exists. Materialize the lockfile-pinned messaging set before
        # sealing so gateway startup never writes it into the immutable venv.
        run("sync", "--extra", "all", "--extra", "messaging", "--locked")
    launcher = venv / "bin" / "hermes"
    if not launcher.is_file() or launcher.is_symlink() or not os.access(launcher, os.X_OK):
        raise RuntimeError("locked dependency sync did not create an executable Hermes launcher")
    return launcher



def build_release_web_ui(
    release: Path,
    *,
    npm: str | Path,
    base_env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> Path:
    """Build the dashboard before the immutable release receipt is sealed."""
    release = Path(release)
    if not release.is_absolute() or not re.fullmatch(r"release-[0-9a-f]{40}", release.name):
        raise ValueError("release must be an absolute stable release path")
    if not release.is_dir() or release.is_symlink():
        raise RuntimeError("release must be a real directory")
    release_info = os.lstat(release)
    release_identity = (release_info.st_dev, release_info.st_ino)
    for relative in ("package.json", "package-lock.json", "web/package.json"):
        source = release / relative
        info = os.lstat(source)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(f"web build input must be a single-link regular file: {relative}")

    npm = Path(npm)
    if not npm.is_absolute():
        raise ValueError("web construction requires an absolute npm executable")
    npm_info = os.lstat(npm)
    npm_identity = (npm_info.st_dev, npm_info.st_ino)
    if stat.S_ISLNK(npm_info.st_mode):
        npm_target = npm.resolve(strict=True)
        target_info = os.lstat(npm_target)
        if npm_info.st_uid != os.getuid() or npm_info.st_nlink != 1:
            raise RuntimeError("npm launcher symlink is not privately owned")
    else:
        npm_target = npm
        target_info = npm_info
    if (
        not stat.S_ISREG(target_info.st_mode)
        or target_info.st_uid != os.getuid()
        or target_info.st_nlink != 1
        or target_info.st_mode & 0o022
        or target_info.st_mode & 0o111 == 0
    ):
        raise RuntimeError("npm target must be an owner-controlled single-link executable")
    npm_target_identity = (target_info.st_dev, target_info.st_ino)

    ambient = os.environ if base_env is None else base_env
    env = {
        key: ambient[key]
        for key in ("HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if key in ambient
    }
    env["PATH"] = f"{npm.parent}:/usr/bin:/bin:/usr/sbin:/sbin"
    env["NPM_CONFIG_FUND"] = "false"
    env["NPM_CONFIG_AUDIT"] = "false"
    if extra_env:
        env.update(extra_env)

    for arguments in (("ci", "--workspace", "web"), ("run", "build", "--workspace", "web")):
        current_release = os.lstat(release)
        current_npm = os.lstat(npm)
        current_target = os.lstat(npm_target)
        if (current_release.st_dev, current_release.st_ino) != release_identity:
            raise RuntimeError("release identity changed before web construction")
        if (current_npm.st_dev, current_npm.st_ino) != npm_identity:
            raise RuntimeError("npm launcher identity changed before web construction")
        if (current_target.st_dev, current_target.st_ino) != npm_target_identity:
            raise RuntimeError("npm target identity changed before web construction")
        completed = subprocess.run(
            [str(npm), *arguments], cwd=release, env=env, check=False,
            capture_output=True, text=True,
        )
        if completed.returncode != 0:
            detail = [line for line in completed.stderr.strip().splitlines() if line.strip()]
            raise RuntimeError(
                f"npm {' '.join(arguments)} failed: {detail[-1] if detail else completed.returncode}"
            )

    output = release / "hermes_cli" / "web_dist" / "index.html"
    output_info = os.lstat(output)
    if not stat.S_ISREG(output_info.st_mode) or output_info.st_nlink != 1:
        raise RuntimeError("web build did not create a stable dashboard index")
    # npm's content-addressed staging may use hard links. Runtime only needs the
    # reviewed web_dist output, so remove build dependencies before the release
    # inventory is sealed rather than weakening the single-link invariant.
    node_modules = release / "node_modules"
    if node_modules.exists() or node_modules.is_symlink():
        if not node_modules.is_dir() or node_modules.is_symlink():
            raise RuntimeError("npm dependency staging is not a real directory")
        shutil.rmtree(node_modules)
    current_release = os.lstat(release)
    if (current_release.st_dev, current_release.st_ino) != release_identity:
        raise RuntimeError("release identity changed during web construction")
    return output

_BYTECODE_VALIDATOR = r"""
import hashlib, importlib.util, json, marshal, os, stat, sys, types
root = os.path.abspath(sys.argv[1])
require_complete = sys.argv[2] == "1"
def stable(path):
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError("expected stable regular file: " + path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk: break
            chunks.append(chunk)
        completed = os.fstat(fd)
    finally:
        os.close(fd)
    after = os.lstat(path)
    signature = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if signature(before) != signature(opened) or signature(opened) != signature(completed) or signature(opened) != signature(after):
        raise RuntimeError("file changed while reading: " + path)
    return b"".join(chunks)
def filenames(code):
    result = [code.co_filename]
    for value in code.co_consts:
        if isinstance(value, types.CodeType): result.extend(filenames(value))
    return result
sources, bytecode = [], []
for directory, names, files in os.walk(root, topdown=True, followlinks=False):
    names.sort(); files.sort()
    for name in files:
        path = os.path.join(directory, name)
        if name.endswith(".py"): sources.append(path)
        elif name.endswith(".pyc"): bytecode.append(path)
source_set, claimed, mapping, unexpected = set(sources), set(), {}, []
for path in bytecode:
    try: source = importlib.util.source_from_cache(path)
    except ValueError:
        unexpected.append(os.path.relpath(path, root)); continue
    if source not in source_set or source in claimed:
        unexpected.append(os.path.relpath(path, root)); continue
    mapping[path] = source; claimed.add(source)
missing = sorted(os.path.relpath(path, root) for path in source_set - claimed)
if require_complete and missing: raise RuntimeError("release bytecode is incomplete: " + repr(missing[:3]))
if unexpected: raise RuntimeError("release contains bytecode without a unique reviewed source: " + repr(sorted(unexpected)[:3]))
digest = hashlib.sha256()
owned = []
for path in sorted(bytecode, key=lambda value: os.fsencode(os.path.relpath(value, root))):
    data, source = stable(path), mapping[path]
    if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER: raise RuntimeError("release bytecode header is invalid: " + path)
    if int.from_bytes(data[4:8], "little") != 3: raise RuntimeError("release bytecode is not checked-hash mode: " + path)
    if data[8:16] != importlib.util.source_hash(stable(source)): raise RuntimeError("release bytecode source hash does not match: " + path)
    try: code = marshal.loads(data[16:])
    except (EOFError, ValueError, TypeError) as exc: raise RuntimeError("release bytecode payload is invalid: " + path) from exc
    if not isinstance(code, types.CodeType): raise RuntimeError("release bytecode contains no code object: " + path)
    prefix = root + os.sep
    for filename in filenames(code):
        if not os.path.isabs(filename) or not filename.startswith(prefix): raise RuntimeError("release bytecode embeds a non-stable source path: %s: %s" % (path, filename))
    relative = os.fsencode(os.path.relpath(path, root))
    digest.update(len(relative).to_bytes(4, "big")); digest.update(relative); digest.update(hashlib.sha256(data).digest())
    owned.append(os.fsdecode(relative))
print(json.dumps({"version": 1, "count": len(bytecode), "sha256": digest.hexdigest(), "paths": owned}, sort_keys=True, separators=(",", ":")))
"""


def _release_bytecode_validation(release: Path, *, require_complete: bool) -> dict[str, object]:
    """Validate pyc with the release interpreter that defines its format."""
    release = Path(release)
    if not release.is_absolute() or not release.is_dir() or release.is_symlink():
        raise RuntimeError("bytecode inventory requires a real absolute release directory")
    python = release / "venv" / "bin" / "python"
    if not python.exists() or not os.access(python, os.X_OK):
        raise RuntimeError("bytecode inventory requires the release-local Python")
    env = {
        key: os.environ[key]
        for key in ("TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL")
        if key in os.environ
    }
    env.update({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONHASHSEED": "0", "PYTHONNOUSERSITE": "1"})
    completed = subprocess.run(
        [str(python), "-I", "-B", "-c", _BYTECODE_VALIDATOR, str(release), "1" if require_complete else "0"],
        cwd=release,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        close_fds=True,
    )
    if completed.returncode != 0:
        detail = [line for line in completed.stderr.splitlines() if line.strip()]
        raise RuntimeError("release bytecode validation failed: " + (detail[-1] if detail else str(completed.returncode)))
    try:
        result = json.loads(completed.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("release bytecode validator returned invalid output") from exc
    if (
        not isinstance(result, dict)
        or set(result) != {"version", "count", "sha256", "paths"}
        or result["version"] != 1
        or type(result["count"]) is not int
        or not isinstance(result["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", result["sha256"]) is None
        or not isinstance(result["paths"], list)
        or any(not isinstance(path, str) for path in result["paths"])
        or len(result["paths"]) != result["count"]
    ):
        raise RuntimeError("release bytecode validator returned malformed inventory")
    return result


def _partial_bytecode_paths(release: Path) -> set[str]:
    """Validate any pyc left before a receipt, allowing an interrupted compile."""
    if not any(release.rglob("*.pyc")):
        return set()
    return set(_release_bytecode_validation(release, require_complete=False)["paths"])


def _bytecode_inventory(release: Path) -> dict[str, object]:
    """Verify and summarize the release's complete checked-hash bytecode set."""
    result = _release_bytecode_validation(release, require_complete=True)
    return {key: result[key] for key in ("version", "count", "sha256")}


def _persist_bytecode(release: Path) -> None:
    directories = {release}
    for path in release.rglob("*.pyc"):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        current = path.parent
        while True:
            directories.add(current)
            if current == release:
                break
            current = current.parent
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)


def _precompile_release_bytecode(release: Path) -> dict[str, object]:
    """Materialize deterministic checked-hash pyc before sealing a release."""
    release = Path(release)
    if not release.is_absolute() or not release.is_dir() or release.is_symlink():
        raise RuntimeError("bytecode precompilation requires a real absolute release directory")
    python = release / "venv" / "bin" / "python"
    if not python.exists() or not os.access(python, os.X_OK):
        raise RuntimeError("bytecode precompilation requires the release-local Python")
    release_info = os.lstat(release)
    python_link = os.lstat(python)
    python_target = os.stat(python)
    env = {
        key: os.environ[key]
        for key in ("TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if key in os.environ
    }
    env.update(
        {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
        }
    )
    completed = subprocess.run(
        [
            str(python),
            "-I",
            "-m",
            "compileall",
            "-q",
            "-f",
            "--invalidation-mode",
            "checked-hash",
            "-s",
            str(release),
            "-p",
            str(release),
            str(release),
        ],
        cwd=release,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        close_fds=True,
    )
    after_release = os.lstat(release)
    after_link = os.lstat(python)
    after_target = os.stat(python)
    signature = lambda value: (value.st_dev, value.st_ino, value.st_mode)
    if signature(after_release) != signature(release_info):
        raise RuntimeError("release identity changed during bytecode precompilation")
    if signature(after_link) != signature(python_link) or signature(after_target) != signature(
        python_target
    ):
        raise RuntimeError("release Python identity changed during bytecode precompilation")
    if completed.returncode != 0:
        detail = [line for line in completed.stderr.splitlines() if line.strip()]
        raise RuntimeError(
            "bytecode precompilation failed: "
            + (detail[-1] if detail else str(completed.returncode))
        )
    inventory = _bytecode_inventory(release)
    _persist_bytecode(release)
    return inventory


def _expected_bytecode_fingerprint(release: Path, carried: str) -> str:
    """Return the exact checkout fingerprint Hermes' launch guard expects."""
    carried = _sha(carried, "carried")
    try:
        ref = _run_git(release, "symbolic-ref", "HEAD")
    except RuntimeError as exc:
        raise RuntimeError(
            f"release Git HEAD is not the required symbolic HEAD: {release}"
        ) from exc
    if ref != "refs/heads/main" or _run_git(release, "rev-parse", ref) != carried:
        raise RuntimeError(
            f"release Git HEAD cannot produce the Hermes bytecode fingerprint: {release}"
        )
    return f"git:{ref}:{carried}"


def _verify_bytecode_fingerprint(release: Path, carried: str) -> None:
    try:
        observed = _stable_regular_bytes(release / _BYTECODE_FINGERPRINT).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("release bytecode fingerprint is unavailable or invalid") from exc
    if observed != _expected_bytecode_fingerprint(release, carried):
        raise RuntimeError("release bytecode fingerprint does not match symbolic HEAD")


def _publish_bytecode_fingerprint(release: Path, carried: str) -> None:
    """Durably install Hermes' launch stamp before the release is receipted."""
    data = _expected_bytecode_fingerprint(release, carried).encode("utf-8")
    candidate = release / f"{_BYTECODE_FINGERPRINT}.{secrets.token_hex(16)}"
    stamp = release / _BYTECODE_FINGERPRINT
    _write_private_candidate(candidate, data, 0o600)
    candidate_info = os.lstat(candidate)
    expected_identity = (candidate_info.st_dev, candidate_info.st_ino)
    try:
        _rename_exclusive(candidate, stamp)
        try:
            installed = os.lstat(stamp)
            if (installed.st_dev, installed.st_ino) != expected_identity:
                raise RuntimeError("bytecode fingerprint identity changed during publication")
            _fsync_directory(release)
        except BaseException as original:
            retirement = release / f"{_BYTECODE_FINGERPRINT}.retired-{secrets.token_hex(16)}"
            try:
                current = os.lstat(stamp)
            except OSError:
                raise original from None
            if (current.st_dev, current.st_ino) == expected_identity:
                _rename_exclusive(stamp, retirement)
                retired = os.lstat(retirement)
                if (retired.st_dev, retired.st_ino) == expected_identity:
                    _fsync_directory(release)
                    retirement.unlink()
                    _fsync_directory(release)
                else:
                    _rename_exclusive(retirement, stamp)
                    _fsync_directory(release)
            raise
    finally:
        if candidate.exists() and not candidate.is_symlink():
            candidate.unlink()


def build_source_release(
    layout: ReleaseLayout,
    *,
    upstream: str,
    carried: str,
    bundle: Path,
    source_url: str = OFFICIAL_REPO_URL,
    allow_local_source: bool = False,
) -> Path:
    upstream = _sha(upstream, "upstream")
    carried = _sha(carried, "carried")
    bundle = Path(bundle)
    if layout.release.exists() or layout.release.is_symlink():
        raise FileExistsError(layout.release)
    if not allow_local_source and source_url != OFFICIAL_REPO_URL:
        raise ValueError("production releases must use the official HTTPS repository")
    if allow_local_source:
        source_url = str(Path(source_url).resolve())
    _ensure_private_root(layout.root)
    temporary = layout.root / f".building-{secrets.token_hex(16)}"
    temporary.mkdir(mode=0o700)
    try:
        _run_git(temporary, "init", "-q", "--initial-branch=main")
        for key, value in (
            ("core.hooksPath", "/dev/null"),
            ("core.attributesFile", "/dev/null"),
            ("core.fsmonitor", "false"),
            ("credential.helper", ""),
            ("protocol.allow", "never"),
            ("protocol.https.allow", "always"),
            # The persisted policy refuses the file transport even when the
            # source is local: the only file this build may ever read through a
            # transport is the private bundle copy below, and that one fetch
            # names the allowance on its own command line instead of leaving it
            # standing for every later Git invocation in this repository.
            ("protocol.file.allow", "never"),
        ):
            _run_git(temporary, "config", "--local", key, value)
        # A test-only local source is the one other file-transport read this
        # build may perform, and it is allowed for exactly that one command.
        source_transport = _FILE_TRANSPORT if allow_local_source else ()
        # Fetch the reviewed immutable snapshot directly. The moving main may
        # advance while this release is being validated; reachable exact object
        # IDs remain fetchable without turning the next update into a blocker.
        _run_git(
            temporary,
            *source_transport,
            "fetch",
            "--no-tags",
            source_url,
            f"+{upstream}:refs/upstream/frozen",
        )
        observed = _run_git(temporary, "rev-parse", "refs/upstream/frozen")
        if observed != upstream:
            raise RuntimeError(
                f"frozen upstream mismatch: expected {upstream}, fetched {observed}"
            )
        private_bundle = temporary / ".git" / f"unified-kanban-{secrets.token_hex(16)}.bundle"
        _write_private_candidate(private_bundle, _stable_regular_bytes(bundle.resolve()), 0o600)
        listed = _run_git(temporary, "bundle", "list-heads", str(private_bundle))
        bundle_refs: list[tuple[str, str]] = []
        for line in listed.splitlines():
            fields = line.split()
            if len(fields) != 2 or SHA_RE.fullmatch(fields[0]) is None:
                raise RuntimeError("reviewed bundle refs are malformed")
            match = re.fullmatch(r"refs/heads/carried-([0-9]{2})", fields[1])
            if match is None:
                raise RuntimeError("reviewed bundle refs are not carried-NN only")
            bundle_refs.append((fields[1], fields[0]))
        bundle_refs.sort()
        expected_names = [f"refs/heads/carried-{index:02d}" for index in range(1, len(bundle_refs) + 1)]
        if not bundle_refs or [name for name, _ in bundle_refs] != expected_names:
            raise RuntimeError("reviewed bundle refs are not a contiguous carried sequence")
        if bundle_refs[-1][1] != carried:
            raise RuntimeError("final carried bundle ref does not match requested carried SHA")
        # Git routes a bundle path through the file transport, so this fetch -
        # and only this fetch - carries the allowance. Its argument is the
        # private 128-bit-named copy this function just wrote inside the
        # temporary .git directory, never a caller-supplied path.
        _run_git(
            temporary,
            *_FILE_TRANSPORT,
            "fetch",
            "--no-tags",
            str(private_bundle),
            "+refs/heads/carried-*:refs/unified-kanban/carried/*",
        )
        if _run_git(temporary, "rev-parse", f"{carried}^{{commit}}") != carried:
            raise RuntimeError("carried tip is missing from the reviewed bundle")
        if _run_git(temporary, "merge-base", "--is-ancestor", upstream, carried):
            pass
        expected_parent = upstream
        for _, commit in bundle_refs:
            ancestry = _run_git(temporary, "rev-list", "--parents", "-n", "1", commit).split()
            if ancestry != [commit, expected_parent]:
                raise RuntimeError("reviewed bundle is not a sole-parent linear carried chain")
            expected_parent = commit
        _run_git(temporary, "checkout", "-q", "-B", "main", carried)
        collisions = case_collisions(temporary, carried)
        records = _materialized_case_collisions(temporary, collisions, rewrite=True)
        observed = set(_porcelain_status(temporary))
        expected = _expected_collision_status(collisions, records)
        if observed != expected:
            unexpected = sorted(observed - expected) or sorted(expected - observed)
            raise RuntimeError(
                f"constructed release worktree is not clean: {unexpected}"
            )
        if _run_git(temporary, "rev-parse", "HEAD") != carried:
            raise RuntimeError("constructed release HEAD is not the carried tip")
        os.chmod(temporary, 0o700)
        source_identity = os.lstat(temporary)
        _rename_exclusive(temporary, layout.release)
        installed = os.lstat(layout.release)
        expected_identity = (source_identity.st_dev, source_identity.st_ino)
        if (installed.st_dev, installed.st_ino) != expected_identity:
            raise RuntimeError("published release identity changed")
        directory_fd = os.open(layout.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                os.fsync(directory_fd)
            except BaseException:
                retirement = layout.root / f".retired-{secrets.token_hex(16)}"
                current = os.lstat(layout.release)
                if (current.st_dev, current.st_ino) == expected_identity:
                    _rename_exclusive(layout.release, retirement)
                    retired = os.lstat(retirement)
                    if (retired.st_dev, retired.st_ino) == expected_identity:
                        os.fsync(directory_fd)
                        shutil.rmtree(retirement)
                        os.fsync(directory_fd)
                    else:
                        _rename_exclusive(retirement, layout.release)
                        os.fsync(directory_fd)
                raise
        finally:
            os.close(directory_fd)
        return layout.release
    finally:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)


def selected_release(layout: ReleaseLayout) -> str | None:
    """Return the release the installed launcher would execute, if any."""
    try:
        info = os.lstat(layout.selector)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("release selector is not a regular file")
    if info.st_size > 4096:
        raise RuntimeError("release selector is too large")
    try:
        return _stable_regular_bytes(layout.selector).decode("utf-8").removesuffix("\n")
    except UnicodeError as exc:
        raise RuntimeError("release selector is not UTF-8") from exc


def _owned_incomplete_fingerprint_paths(release: Path) -> set[str]:
    """Return only exact producer stamp artifacts safe to retire after a crash."""
    carried = _run_git(release, "rev-parse", "HEAD")
    expected = _expected_bytecode_fingerprint(release, carried).encode("utf-8")
    owned: set[str] = set()
    candidate = re.compile(
        rf"{re.escape(_BYTECODE_FINGERPRINT)}(?:\.(?:retired-)?[0-9a-f]{{32}})?"
    )
    for path in release.iterdir():
        if candidate.fullmatch(path.name) is None:
            continue
        try:
            info = os.lstat(path)
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
                and _stable_regular_bytes(path) == expected
            ):
                owned.add(path.name)
        except OSError:
            continue
    return owned


def _retire_incomplete_release(layout: ReleaseLayout) -> None:
    """Durably retire a release only this project could have left unfinished.

    The release is published at its stable path before its dependencies are
    synced, because a venv records absolute paths and cannot be moved
    afterwards. A sync that fails, or a crash before the completion receipt is
    published, therefore leaves a release no later run could ever complete. It
    is retired only when it is provably ours - the exact carried tip, with no
    change beyond the venv this project builds - and provably unused, and the
    rename is made durable before anything is deleted, so an interruption
    leaves an inert private directory rather than a half-removed release.
    """
    collisions = case_collisions(layout.release, "HEAD")
    records = _materialized_case_collisions(layout.release, collisions, rewrite=False)
    normalized = _expected_collision_status(collisions, records)
    owned_bytecode = _partial_bytecode_paths(layout.release)
    owned_fingerprints = _owned_incomplete_fingerprint_paths(layout.release)
    ignored = {
        entry
        for entry in _run_git(
            layout.release,
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
        ).split("\0")
        if entry
    }
    for entry in ignored:
        if not (
            entry.rstrip("/") == "venv"
            or entry.startswith("venv/")
            or entry in owned_fingerprints
            or entry.startswith(f".{_COMPLETION_RECEIPT}.")
            or entry in owned_bytecode
        ):
            raise RuntimeError(
                f"incomplete release changed after construction; refusing recovery: !! {entry}"
            )
    for line in _porcelain_status_all(layout.release):
        if line in normalized:
            continue
        entry = line[3:]
        if not line.startswith("?? ") or not (
            entry.rstrip("/") == "venv"
            or entry.startswith("venv/")
            or entry in owned_fingerprints
            or entry.startswith(f".{_COMPLETION_RECEIPT}.")
            or entry in owned_bytecode
            ):
            raise RuntimeError(
                "existing release has no completion receipt and changed after "
                f"construction: {line}"
            )
    if selected_release(layout) == str(layout.release):
        raise RuntimeError(
            f"refusing to retire the release the selector still names: {layout.release}"
        )
    info = os.lstat(layout.release)
    identity = (info.st_dev, info.st_ino)
    retirement = layout.root / f".retired-{secrets.token_hex(16)}"
    descriptor = os.open(layout.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _rename_exclusive(layout.release, retirement)
        moved = os.lstat(retirement)
        if (moved.st_dev, moved.st_ino) != identity:
            _rename_exclusive(retirement, layout.release)
            raise RuntimeError("incomplete release identity changed during retirement")
        try:
            os.fsync(descriptor)
        except BaseException:
            current = os.lstat(retirement)
            if (current.st_dev, current.st_ino) == identity:
                _rename_exclusive(retirement, layout.release)
            raise
        shutil.rmtree(retirement)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_release(
    layout: ReleaseLayout,
    *,
    upstream: str,
    carried: str,
    bundle: Path,
    uv: str | Path = "uv",
    npm: str | Path | None = None,
    source_url: str = OFFICIAL_REPO_URL,
    allow_local_source: bool = False,
) -> Path:
    """Idempotently construct and dependency-sync one exact release."""
    if layout.release.exists() or layout.release.is_symlink():
        if not layout.release.is_dir() or layout.release.is_symlink():
            raise RuntimeError("existing release path is not a real directory")
        if _run_git(layout.release, "rev-parse", "HEAD") != carried:
            raise RuntimeError("existing release has the wrong carried HEAD")
        if _run_git(layout.release, "merge-base", "--is-ancestor", upstream, carried):
            pass
        receipt = layout.release / _COMPLETION_RECEIPT
        if receipt.exists() or receipt.is_symlink():
            _verify_completed_release(layout, upstream, carried)
            return layout.release
        _retire_incomplete_release(layout)
    build_source_release(
        layout,
        upstream=upstream,
        carried=carried,
        bundle=bundle,
        source_url=source_url,
        allow_local_source=allow_local_source,
    )
    launcher = layout.release / "venv" / "bin" / "hermes"
    if not launcher.is_file() or launcher.is_symlink() or not os.access(launcher, os.X_OK):
        sync_release_dependencies(layout.release, uv=uv)
    if (layout.release / "web/package.json").is_file():
        if npm is None:
            raise RuntimeError("immutable release web construction requires npm")
        build_release_web_ui(layout.release, npm=npm)
    _precompile_release_bytecode(layout.release)
    _publish_bytecode_fingerprint(layout.release, carried)
    _publish_completion_receipt(layout, upstream, carried)
    return layout.release


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_candidate(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    created = os.fstat(descriptor)
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(errno.EIO, "candidate write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            observed = os.lstat(path)
            if (observed.st_dev, observed.st_ino) == (created.st_dev, created.st_ino):
                path.unlink()
                _fsync_directory(path.parent)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def _stable_regular_bytes(path: Path) -> bytes:
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError(f"expected stable regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        data = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            data.extend(chunk)
        completed = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = os.lstat(path)
    signature = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if signature(before) != signature(opened) or signature(opened) != signature(completed) or signature(opened) != signature(after):
        raise RuntimeError(f"file changed while reading: {path}")
    return bytes(data)


def _tree_digest(root: Path, *, excluded_top_level: set[str] | None = None) -> str:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("release inventory root must be a real directory")
    excluded = excluded_top_level or set()
    digest = hashlib.sha256()
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names.sort()
        files.sort()
        base = Path(directory)
        if base == root:
            names[:] = [name for name in names if name not in excluded]
            files[:] = [name for name in files if name not in excluded]
        for name in [*names, *files]:
            path = base / name
            relative = os.fsencode(path.relative_to(root))
            info = os.lstat(path)
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(stat.S_IFMT(info.st_mode).to_bytes(4, "big"))
            digest.update(stat.S_IMODE(info.st_mode).to_bytes(4, "big"))
            if stat.S_ISREG(info.st_mode):
                data = _stable_regular_bytes(path)
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(hashlib.sha256(data).digest())
            elif stat.S_ISLNK(info.st_mode):
                target = os.fsencode(os.readlink(path))
                digest.update(len(target).to_bytes(4, "big"))
                digest.update(target)
            elif not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f"unsupported release venv entry: {path}")
    return digest.hexdigest()


def _completion_payload(layout: ReleaseLayout, upstream: str, carried: str) -> dict[str, object]:
    identity = os.lstat(layout.release)
    git_config = _stable_regular_bytes(layout.release / ".git" / "config")
    git_head = _stable_regular_bytes(layout.release / ".git" / "HEAD")
    git_refs = _run_git(
        layout.release,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    ).splitlines()
    # The folded groups are re-derived from this release's own tree and re-proven
    # against its own bytes, so the receipt binds the normalization decision as
    # tightly as it binds the rest of the inventory.
    collisions = case_collisions(layout.release, "HEAD")
    return {
        "version": 2,
        "release_identity": [identity.st_dev, identity.st_ino],
        "upstream": upstream,
        "carried": carried,
        "git_tree": _run_git(layout.release, "rev-parse", "HEAD^{tree}"),
        "git_config_sha256": hashlib.sha256(git_config).hexdigest(),
        "git_head_sha256": hashlib.sha256(git_head).hexdigest(),
        "git_refs": sorted(git_refs),
        "case_collisions": _materialized_case_collisions(
            layout.release, collisions, rewrite=False
        ),
        "bytecode": _bytecode_inventory(layout.release),
        "release_sha256": _tree_digest(
            layout.release,
            excluded_top_level={".git", _COMPLETION_RECEIPT},
        ),
    }


def _publish_completion_receipt(layout: ReleaseLayout, upstream: str, carried: str) -> None:
    _verify_bytecode_fingerprint(layout.release, carried)
    payload = _completion_payload(layout, upstream, carried)
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    candidate = layout.release / f".{_COMPLETION_RECEIPT}.{secrets.token_hex(16)}"
    _write_private_candidate(candidate, data, 0o600)
    candidate_info = os.lstat(candidate)
    expected_identity = (candidate_info.st_dev, candidate_info.st_ino)
    receipt = layout.release / _COMPLETION_RECEIPT
    try:
        _rename_exclusive(candidate, receipt)
        installed = os.lstat(receipt)
        if (installed.st_dev, installed.st_ino) != expected_identity:
            raise RuntimeError("completion receipt identity changed during publication")
        descriptor = os.open(layout.release, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                os.fsync(descriptor)
            except BaseException:
                retirement = layout.release / f".{_COMPLETION_RECEIPT}.retired-{secrets.token_hex(16)}"
                current = os.lstat(receipt)
                if (current.st_dev, current.st_ino) == expected_identity:
                    _rename_exclusive(receipt, retirement)
                    retired = os.lstat(retirement)
                    if (retired.st_dev, retired.st_ino) == expected_identity:
                        os.fsync(descriptor)
                        retirement.unlink()
                        os.fsync(descriptor)
                    else:
                        _rename_exclusive(retirement, receipt)
                        os.fsync(descriptor)
                raise
        finally:
            os.close(descriptor)
    finally:
        if candidate.exists() and not candidate.is_symlink():
            candidate.unlink()


def _verify_completed_release(layout: ReleaseLayout, upstream: str, carried: str) -> None:
    receipt = layout.release / _COMPLETION_RECEIPT
    if not receipt.exists() or receipt.is_symlink():
        raise RuntimeError("existing release has no completion receipt")
    try:
        observed = json.loads(_stable_regular_bytes(receipt))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("existing release completion receipt is invalid") from exc
    _verify_bytecode_fingerprint(layout.release, carried)
    expected = _completion_payload(layout, upstream, carried)
    if observed.get("git_config_sha256") != expected["git_config_sha256"]:
        raise RuntimeError("existing release Git config changed after completion")
    if observed.get("git_refs") != expected["git_refs"]:
        raise RuntimeError("existing release Git refs changed after completion")
    if observed.get("case_collisions") != expected["case_collisions"]:
        raise RuntimeError("existing release case-fold collision normalization changed")
    if observed != expected:
        raise RuntimeError("existing release completion receipt does not match content")


def _add_baseline_arguments(parser: argparse.ArgumentParser) -> None:
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--baseline-file", type=Path)
    baseline.add_argument("--baseline-absent", action="store_true")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("agent_repo", type=Path)
    prepare.add_argument("upstream")
    prepare.add_argument("carried")
    prepare.add_argument("bundle", type=Path)
    prepare.add_argument("--uv", type=Path, required=True)
    prepare.add_argument("--npm", type=Path)
    render = subparsers.add_parser("render")
    render.add_argument("agent_repo", type=Path)
    render.add_argument("upstream")
    render.add_argument("carried")
    render.add_argument("selector_candidate", type=Path)
    render.add_argument("launcher_candidate", type=Path)
    _add_baseline_arguments(render)
    # Rendering only the launcher must not depend on the release directory, so
    # uninstall can still prove launcher provenance after a release is gone.
    render_launcher = subparsers.add_parser("render-launcher")
    render_launcher.add_argument("agent_repo", type=Path)
    render_launcher.add_argument("upstream")
    render_launcher.add_argument("carried")
    render_launcher.add_argument("launcher_candidate", type=Path)
    _add_baseline_arguments(render_launcher)
    # Activation only swaps the selector. Emitting a launcher candidate there
    # would mean inventing a baseline, and an unused candidate carrying a wrong
    # binding is a live footgun, so the selector half stands alone.
    render_selector = subparsers.add_parser("render-selector")
    render_selector.add_argument("agent_repo", type=Path)
    render_selector.add_argument("upstream")
    render_selector.add_argument("carried")
    render_selector.add_argument("selector_candidate", type=Path)
    baseline = subparsers.add_parser("launcher-baseline")
    baseline.add_argument("agent_repo", type=Path)
    baseline.add_argument("upstream")
    baseline.add_argument("carried")
    baseline.add_argument("launcher", type=Path)
    args = parser.parse_args()
    layout = release_layout(args.agent_repo, args.upstream, args.carried)
    if args.action == "prepare":
        print(
            prepare_release(
                layout,
                upstream=args.upstream,
                carried=args.carried,
                bundle=args.bundle,
                uv=args.uv,
                npm=args.npm,
            )
        )
        return 0
    if args.action == "render-selector":
        _write_private_candidate(args.selector_candidate, selector_payload(layout), 0o600)
        print(layout.selector)
        return 0
    if args.action == "launcher-baseline":
        # Exit 3 means "provably not our launcher"; any other non-zero is an
        # internal fault, so callers never have to read a crash as a verdict.
        try:
            token = launcher_baseline(layout, _stable_regular_bytes(args.launcher))
        except ForeignLauncher as exc:
            print(f"foreign Hermes launcher: {exc}", file=sys.stderr)
            return 3
        print(token)
        return 0
    baseline = baseline_token(None if args.baseline_absent else args.baseline_file)
    if args.action == "render":
        _write_private_candidate(args.selector_candidate, selector_payload(layout), 0o600)
    _write_private_candidate(args.launcher_candidate, launcher_payload(layout, baseline), 0o755)
    print(layout.selector)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
