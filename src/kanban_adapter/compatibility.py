"""Exact Hermes upstream compatibility checks shared by installed entry points."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from .release_layout import (
    COMPLETION_RECEIPT_NAME,
    normalize_agent_repo,
    release_directory,
    release_selector,
)


_SHA_RE = re.compile(r"[0-9A-Fa-f]{40}\Z")
_VERSION_UPSTREAM_RE = re.compile(r"\bupstream\s+([0-9A-Fa-f]{7,40})\b")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SUPPORTED_UPSTREAM_FILE = _REPO_ROOT / "patches" / "hermes-agent-supported-upstream"
_CARRIED_COMMITS_FILE = _REPO_ROOT / "patches" / "hermes-agent-carried-commits"
_MAX_SELECTOR_BYTES = 4096
_MAX_RECEIPT_BYTES = 64 * 1024


def _read_trusted_file(path: Path, *, max_bytes: int) -> str:
    """Read one repository policy file without following or racing a link."""
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"expected upstream file is not a trusted regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"expected upstream file is not regular: {path}")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"expected upstream file changed during validation: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise ValueError(f"repository policy file is unexpectedly large: {path}")
    return raw.decode("utf-8")


def read_supported_upstream(expected_file: Path | None = None) -> str:
    """Read the repository pin without following links or accepting a swapped file."""
    path = expected_file or _SUPPORTED_UPSTREAM_FILE
    expected = _read_trusted_file(path, max_bytes=256)
    if expected.endswith("\n"):
        expected = expected[:-1]
    if expected.endswith("\r"):
        expected = expected[:-1]
    if not _SHA_RE.fullmatch(expected):
        raise ValueError(
            f"invalid expected upstream in {path}: require one full 40-character commit SHA"
        )
    return expected


def read_carried_commits(carried_file: Path | None = None) -> tuple[str, ...]:
    """Return the reviewed carried commit order from a trusted repository file."""
    path = carried_file or _CARRIED_COMMITS_FILE
    commits: list[str] = []
    for line in _read_trusted_file(path, max_bytes=64 * 1024).splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if not _SHA_RE.fullmatch(value):
            raise ValueError(f"invalid carried commit in {path}: {value!r}")
        commits.append(value.lower())
    if not commits:
        raise ValueError(f"carried commit manifest is empty: {path}")
    return tuple(commits)


def _read_regular_bytes(path: Path, *, max_bytes: int, label: str) -> bytes:
    """Read one managed file without following or racing a link."""
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} is not a regular file: {path}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"{label} changed during validation: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise ValueError(f"{label} is unexpectedly large: {path}")
    return raw


def check_selected_release(agent_repo: Path, upstream: str, carried: str) -> str:
    """Return the refusal reason unless the selected release is the reviewed one.

    The Hermes checkout is a read-only input whose ``HEAD`` no flow ever moves,
    so what actually runs is the immutable release the selector names. This
    validates that artifact: the exact expected release path, a real release
    directory with an executable, and the producer's completion receipt bound
    to that directory's identity. The receipt's full content inventory is not
    recomputed here - hashing a whole venv on every Hermes turn is not viable -
    which is the same validation boundary the managed launcher accepts.
    """
    selector = release_selector(agent_repo)
    expected_release = release_directory(agent_repo, carried)
    try:
        raw_selector = _read_regular_bytes(
            selector, max_bytes=_MAX_SELECTOR_BYTES, label="Hermes release selector"
        )
        selected = raw_selector.decode("utf-8").removesuffix("\n")
    except FileNotFoundError:
        return f"no Hermes release is selected at {selector}"
    except (OSError, UnicodeError, ValueError) as exc:
        return f"unusable Hermes release selector: {exc}"
    if selected != str(expected_release):
        return (
            f"selected Hermes release {selected or '(empty)'}; "
            f"unified-kanban requires {expected_release}"
        )
    try:
        release_info = os.lstat(expected_release)
        if stat.S_ISLNK(release_info.st_mode) or not stat.S_ISDIR(release_info.st_mode):
            return f"selected Hermes release is not a real directory: {expected_release}"
        executable = expected_release / "venv" / "bin" / "hermes"
        executable_info = os.lstat(executable)
        if (
            stat.S_ISLNK(executable_info.st_mode)
            or not stat.S_ISREG(executable_info.st_mode)
            or not executable_info.st_mode & 0o111
        ):
            return f"selected Hermes release has no executable: {executable}"
        raw_receipt = _read_regular_bytes(
            expected_release / COMPLETION_RECEIPT_NAME,
            max_bytes=_MAX_RECEIPT_BYTES,
            label="Hermes release completion receipt",
        )
    except FileNotFoundError as exc:
        return f"incomplete Hermes release {expected_release}: {exc}"
    except (OSError, ValueError) as exc:
        return f"unusable Hermes release {expected_release}: {exc}"
    try:
        receipt = json.loads(raw_receipt.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        return f"invalid Hermes release completion receipt in {expected_release}: {exc}"
    if not isinstance(receipt, dict) or receipt.get("version") != 2:
        return f"invalid Hermes release completion receipt schema in {expected_release}"
    if receipt.get("upstream") != upstream or receipt.get("carried") != carried:
        return (
            "Hermes release completion receipt does not describe the reviewed "
            f"release: {expected_release}"
        )
    if receipt.get("release_identity") != [release_info.st_dev, release_info.st_ino]:
        return (
            "Hermes release completion receipt was issued for another directory: "
            f"{expected_release}"
        )
    return ""


def check_hermes_compatibility(
    *,
    agent_repo: Path | None = None,
    expected_file: Path | None = None,
    carried_commits_file: Path | None = None,
    hermes_version_output: str | None = None,
    runtime_prefix: Path | None = None,
) -> tuple[bool, str]:
    """Fail closed unless the selected release and optional host runtime agree."""
    if agent_repo is None:
        configured_repo = os.environ.get("HERMES_AGENT_REPO")
        agent_repo = (
            Path(configured_repo)
            if configured_repo
            else Path.home() / ".hermes" / "hermes-agent"
        )
    try:
        agent_repo = normalize_agent_repo(agent_repo)
    except ValueError as exc:
        return False, f"unsupported Hermes: {exc}"
    try:
        expected = read_supported_upstream(expected_file).lower()
        carried_commits = read_carried_commits(carried_commits_file)
    except (OSError, UnicodeError, ValueError) as exc:
        return False, f"unsupported Hermes: compatibility check failed: {exc}"
    try:
        release_reason = check_selected_release(
            agent_repo, expected, carried_commits[-1]
        )
    except (OSError, ValueError) as exc:
        return False, f"unsupported Hermes release: {exc}"
    if release_reason:
        return False, f"unsupported Hermes release: {release_reason}"
    if runtime_prefix is not None:
        expected_prefix = release_directory(
            agent_repo, carried_commits[-1]
        ) / "venv"
        if runtime_prefix != expected_prefix:
            return False, (
                f"unsupported Hermes host runtime {runtime_prefix}; "
                f"unified-kanban requires {expected_prefix}"
            )
    if hermes_version_output is None:
        return True, ""
    version_match = _VERSION_UPSTREAM_RE.search(hermes_version_output)
    if version_match is None:
        return False, "unsupported Hermes: active CLI did not report an upstream revision"
    reported_upstream = version_match.group(1).lower()
    if not expected.startswith(reported_upstream):
        return False, (
            f"unsupported active Hermes CLI upstream {reported_upstream}; "
            f"unified-kanban requires {expected}"
        )
    return True, ""


def main() -> int:
    """Return zero only when the active Hermes checkout matches the reviewed pin."""
    compatible, reason = check_hermes_compatibility()
    if compatible:
        return 0
    print(f"unified-kanban disabled: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
