"""Exact Hermes upstream compatibility checks shared by installed entry points."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path


_SHA_RE = re.compile(r"[0-9A-Fa-f]{40}\Z")
_VERSION_UPSTREAM_RE = re.compile(r"\bupstream\s+([0-9A-Fa-f]{7,40})\b")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SUPPORTED_UPSTREAM_FILE = _REPO_ROOT / "patches" / "hermes-agent-supported-upstream"
_CARRIED_COMMITS_FILE = _REPO_ROOT / "patches" / "hermes-agent-carried-commits"


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


def check_hermes_compatibility(
    *,
    agent_repo: Path | None = None,
    expected_file: Path | None = None,
    carried_commits_file: Path | None = None,
    hermes_version_output: str | None = None,
) -> tuple[bool, str]:
    """Fail closed unless the active CLI, checkout, stack, and pin agree."""
    agent_repo = agent_repo or Path.home() / ".hermes" / "hermes-agent"
    try:
        expected = read_supported_upstream(expected_file).lower()
        carried_commits = read_carried_commits(carried_commits_file)
        upstream_result = subprocess.run(
            ["git", "-C", str(agent_repo), "rev-parse", "origin/main"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        head_result = subprocess.run(
            ["git", "-C", str(agent_repo), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        return False, f"unsupported Hermes: compatibility check failed: {exc}"
    actual_upstream = upstream_result.stdout.strip().lower()
    if upstream_result.returncode != 0 or not _SHA_RE.fullmatch(actual_upstream):
        return False, f"unsupported Hermes: cannot resolve origin/main in {agent_repo}"
    if actual_upstream != expected:
        return False, (
            f"unsupported Hermes upstream {actual_upstream}; unified-kanban requires {expected}"
        )
    active_head = head_result.stdout.strip().lower()
    if head_result.returncode != 0 or not _SHA_RE.fullmatch(active_head):
        return False, f"unsupported Hermes: cannot resolve active HEAD in {agent_repo}"
    expected_head = carried_commits[-1]
    if active_head != expected_head:
        return False, (
            f"unsupported active Hermes checkout {active_head}; "
            f"unified-kanban requires carried head {expected_head}"
        )
    if hermes_version_output is None:
        try:
            version_result = subprocess.run(
                ["hermes", "version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"unsupported Hermes: cannot inspect active CLI version: {exc}"
        if version_result.returncode != 0:
            return False, "unsupported Hermes: 'hermes version' failed"
        hermes_version_output = version_result.stdout
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
