"""One shared normal form for the immutable Hermes release namespace.

Setup, uninstall, the updater, the release manager, and the installed runtime
gate must all name exactly the same release root, selector, and release
directory. Shell concatenation and :meth:`Path.with_name` disagree the moment
the configured checkout carries a trailing slash, a doubled separator, or a
``/./`` component: the shell would put ``.releases`` *inside* the Hermes
checkout while every Python caller keeps using the sibling, so setup would
install a launcher that reads a selector setup never wrote. Callers therefore
normalize through this module first, and the normal form is exactly the string
a shell may concatenate ``.releases`` onto.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

COMPLETION_RECEIPT_NAME = ".unified-kanban-release.json"
SELECTOR_NAME = "current"
RELEASE_ROOT_SUFFIX = ".releases"
_CARRIED_RE = re.compile(r"[0-9a-f]{40}\Z")


def normalize_agent_repo(value: str | os.PathLike[str]) -> Path:
    """Return the one absolute spelling every caller must derive paths from.

    ``pathlib`` already folds ``//``, ``/./``, and trailing separators into its
    normal form. ``..`` is not folded, and folding it here lexically would name
    a different directory than the kernel resolves whenever a component is a
    symlink, so a traversal component fails closed instead.
    """
    path = Path(os.fsdecode(value))
    if not path.is_absolute():
        raise ValueError(f"HERMES_AGENT_REPO must be an absolute path: {value!r}")
    if ".." in path.parts:
        raise ValueError(
            f"HERMES_AGENT_REPO must not contain a '..' component: {value!r}"
        )
    if not path.name:
        raise ValueError(f"HERMES_AGENT_REPO must be an absolute non-root path: {value!r}")
    return path


def release_root(agent_repo: str | os.PathLike[str]) -> Path:
    """Return the private release root beside - never inside - the checkout."""
    repo = normalize_agent_repo(agent_repo)
    return repo.with_name(f"{repo.name}{RELEASE_ROOT_SUFFIX}")


def release_selector(agent_repo: str | os.PathLike[str]) -> Path:
    """Return the one regular file that names the release in use."""
    return release_root(agent_repo) / SELECTOR_NAME


def release_directory(agent_repo: str | os.PathLike[str], carried: str) -> Path:
    """Return the immutable release directory for one reviewed carried tip."""
    if _CARRIED_RE.fullmatch(carried) is None:
        raise ValueError("carried commit must be a full lowercase SHA-1")
    return release_root(agent_repo) / f"release-{carried}"


def main() -> int:
    """Print the normal form so shell callers derive identical paths."""
    if len(sys.argv) != 2:
        print("usage: python3 -m kanban_adapter.release_layout AGENT_REPO", file=sys.stderr)
        return 2
    try:
        print(normalize_agent_repo(sys.argv[1]))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
