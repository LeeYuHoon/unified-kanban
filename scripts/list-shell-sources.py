#!/usr/bin/env python3
"""List every shell source this repository ships, with its syntax checker.

The CI shell gate used to name its files by hand, which silently stopped
covering three shipped scripts. Discovery therefore comes from the repository
itself: everything Git tracks plus everything present and not ignored, so a new
script is gated the moment it exists rather than the moment somebody remembers
to add it to a list.

Output is one ``<checker>\\t<path>`` record per line, sorted, so a plain
``while IFS=$'\\t' read -r`` loop in CI bash consumes it without arrays.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL_SUFFIXES = (".sh", ".bash")
# Only dialects with a checker available on the supported platform. Anything
# else has to fail loudly: a script nobody can parse is exactly the gap this
# enumerator exists to close.
SYNTAX_CHECKERS = {"sh": "sh", "bash": "bash"}
UNCHECKABLE_SHELLS = ("dash", "ksh", "zsh", "csh", "tcsh", "fish")


def syntax_checker(name: str, head: bytes) -> str | None:
    """Return the checker for one candidate file, or ``None`` if it is not shell."""
    command = ""
    if head.startswith(b"#!"):
        words = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split()
        if words:
            command = Path(words[0]).name
            if command == "env" and len(words) > 1:
                command = Path(words[1]).name
    if command in UNCHECKABLE_SHELLS:
        raise SystemExit(f"no shell syntax checker is configured for {command}: {name}")
    if command in SYNTAX_CHECKERS:
        return SYNTAX_CHECKERS[command]
    if command:
        return None
    return "bash" if name.endswith(SHELL_SUFFIXES) else None


def candidate_paths() -> list[str]:
    """Return every shipped file from a checkout or normalized source archive."""
    if not (ROOT / ".git").exists():
        return _archive_candidate_paths()
    paths: set[str] = set()
    for arguments in (
        ["ls-files", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        listed = subprocess.run(
            ["git", "-C", str(ROOT), *arguments],
            capture_output=True,
            check=True,
        ).stdout
        paths.update(record.decode("utf-8", "surrogateescape") for record in listed.split(b"\0") if record)
    return sorted(paths)


def _archive_ignore_rules() -> list[tuple[bool, bool, str]]:
    """Parse the deliberately small root ``.gitignore`` subset used by releases."""
    ignore = ROOT / ".gitignore"
    try:
        lines = ignore.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"source archive has no readable .gitignore: {exc}") from exc
    rules = []
    for number, raw in enumerate(lines, 1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        negated = value.startswith("!")
        if negated:
            value = value[1:]
        directory_only = value.endswith("/")
        if directory_only:
            value = value[:-1]
        if not value or "/" in value or "\\" in value or "**" in value:
            raise SystemExit(
                f"unsupported .gitignore rule for archive discovery at line {number}: {raw!r}"
            )
        rules.append((negated, directory_only, value))
    return rules


def _archive_ignored(relative: Path, *, directory: bool, rules: list[tuple[bool, bool, str]]) -> bool:
    ignored = False
    components = relative.parts
    for negated, directory_only, pattern in rules:
        if directory_only:
            candidates = components if directory else components[:-1]
            matched = any(fnmatch.fnmatchcase(component, pattern) for component in candidates)
        else:
            matched = not directory and fnmatch.fnmatchcase(relative.name, pattern)
        if matched:
            ignored = not negated
    return ignored


def _archive_candidate_paths() -> list[str]:
    """Walk a normalized archive while excluding generated ignored outputs."""
    rules = _archive_ignore_rules()
    paths = []
    for directory, names, files in os.walk(ROOT):
        base = Path(directory).relative_to(ROOT)
        kept = []
        for name in names:
            relative = base / name
            if name == ".git" or _archive_ignored(relative, directory=True, rules=rules):
                continue
            kept.append(name)
        names[:] = kept
        for name in files:
            relative = base / name
            if not _archive_ignored(relative, directory=False, rules=rules):
                paths.append(str(relative))
    return sorted(paths)


def shell_sources() -> list[tuple[str, str]]:
    discovered = []
    for relative in candidate_paths():
        path = ROOT / relative
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                head = handle.read(256)
        except OSError as exc:
            raise SystemExit(f"could not read candidate shell source {relative}: {exc}") from exc
        checker = syntax_checker(relative, head)
        if checker is not None:
            discovered.append((checker, relative))
    return discovered


def main() -> int:
    discovered = shell_sources()
    if not discovered:
        print("no shell sources discovered; the gate would check nothing", file=sys.stderr)
        return 1
    for checker, relative in discovered:
        print(f"{checker}\t{relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
