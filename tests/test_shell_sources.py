"""The shell syntax gate must cover every shell file this repository ships.

The gate used to name its files by hand, so shipped shell sources such as
``bin/ai-session-viewer`` and ``integrations/session-viewer/setup.sh`` were
never parsed by CI at all.
A hand-written list drifts silently every time a script is added, so discovery
now comes from the repository itself and these tests hold that property.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENUMERATOR = REPO / "scripts" / "list-shell-sources.py"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
SHELL_SUFFIXES = (".sh", ".bash")
# The exact sources the published gate was missing. Named explicitly so the
# reported defect stays covered even if discovery is rewritten again.
PREVIOUSLY_UNCOVERED = (
    "bin/ai-session-viewer",
    "integrations/session-viewer/setup.sh",
)


def load_enumerator():
    spec = importlib.util.spec_from_file_location("list_shell_sources", ENUMERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discovered() -> dict[str, str]:
    """Return the gate's own discovery, as ``path -> syntax checker``."""
    completed = subprocess.run(
        [sys.executable, str(ENUMERATOR)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    found = {}
    for line in completed.stdout.splitlines():
        interpreter, separator, path = line.partition("\t")
        assert separator, f"malformed enumerator record: {line!r}"
        assert path not in found, f"duplicate enumerator record: {path}"
        found[path] = interpreter
    return found


def walked() -> set[str]:
    """Discover shell sources by walking the tree, independent of ``git ls-files``.

    A file that exists, is not ignored, and reads as shell must be gated even if
    it never made it into the index - the missing wrapper was exactly that.
    """
    if not (REPO / ".git").exists():
        pytest.skip("the independent completeness oracle requires a Git checkout")
    ignored = subprocess.run(
        [
            "git",
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        ],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    skipped = {record.decode().rstrip("/") for record in ignored.split(b"\0") if record}
    skipped.add(".git")
    found = set()
    for directory, names, files in os.walk(REPO):
        base = Path(directory).relative_to(REPO)
        names[:] = [
            name
            for name in names
            if str(base / name) not in skipped and (base / name).parts[0] != ".git"
        ]
        for name in files:
            relative = base / name
            path = REPO / relative
            if str(relative) in skipped or path.is_symlink() or not path.is_file():
                continue
            if name.endswith(SHELL_SUFFIXES) or _shell_shebang(path):
                found.add(str(relative))
    return found


def _shell_shebang(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            first = handle.readline(256)
    except OSError:
        return False
    if not first.startswith(b"#!"):
        return False
    words = first[2:].decode("utf-8", "replace").strip().split()
    if not words:
        return False
    command = Path(words[0]).name
    if command == "env" and len(words) > 1:
        command = Path(words[1]).name
    return command in {"sh", "bash", "dash", "ksh", "zsh"}


def test_enumerator_finds_every_shell_file_present_in_the_tree() -> None:
    assert walked() == set(discovered())


def test_enumerator_covers_the_sources_the_published_gate_missed() -> None:
    found = discovered()
    for path in PREVIOUSLY_UNCOVERED:
        assert (REPO / path).is_file(), f"{path} is no longer shipped; drop it from this list"
        assert path in found


def test_every_discovered_shell_source_parses() -> None:
    """Run the gate locally, so CI drift cannot be the only thing that catches it."""
    for path, interpreter in sorted(discovered().items()):
        completed = subprocess.run(
            [interpreter, "-n", path], cwd=REPO, capture_output=True, text=True, check=False
        )
        assert completed.returncode == 0, f"{path}: {completed.stderr}"


def test_ci_shell_gate_is_driven_by_the_enumerator() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    step = workflow.partition("Validate shell syntax")[2].partition("- name:")[0]

    assert step, "CI has no shell syntax step"
    assert "scripts/list-shell-sources.py" in step
    hardcoded = [
        path
        for path in discovered()
        if path in step
    ]
    assert hardcoded == [], f"CI still names shell sources by hand: {hardcoded}"


@pytest.mark.parametrize(
    ("first_line", "name", "expected"),
    [
        ("#!/bin/sh\n", "setup.sh", "sh"),
        ("#!/usr/bin/env bash\n", "kanban-adapter", "bash"),
        ("#!/bin/bash\n", "hook", "bash"),
        ("", "plain.sh", "bash"),
        ("#!/usr/bin/env python3\n", "tool.py", None),
        ("not a script\n", "README.md", None),
    ],
)
def test_interpreter_classification(first_line: str, name: str, expected: str | None) -> None:
    enumerator = load_enumerator()

    assert enumerator.syntax_checker(name, first_line.encode()) == expected


def test_unsupported_shell_dialect_fails_closed() -> None:
    """A dialect with no configured checker must stop the gate, not slip past it."""
    enumerator = load_enumerator()

    with pytest.raises(SystemExit, match="zsh"):
        enumerator.syntax_checker("exotic", b"#!/bin/zsh\n")


def test_archive_discovery_excludes_generated_ignored_files(tmp_path: Path) -> None:
    enumerator = load_enumerator()
    setattr(enumerator, "ROOT", tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n*.log\n.env.*\n!.env.example\n", encoding="utf-8")
    (tmp_path / "script.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("#!/bin/bash\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("DOCUMENTATION=1\n", encoding="utf-8")
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/activate").write_text("#!/bin/sh\n", encoding="utf-8")

    assert enumerator.candidate_paths() == [".env.example", ".gitignore", "script.sh"]


def test_archive_discovery_rejects_unsupported_ignore_syntax(tmp_path: Path) -> None:
    enumerator = load_enumerator()
    setattr(enumerator, "ROOT", tmp_path)
    (tmp_path / ".gitignore").write_text("generated/**/*.sh\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="unsupported .gitignore rule"):
        enumerator.candidate_paths()


def test_checkout_does_not_fallback_when_git_inventory_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enumerator = load_enumerator()
    setattr(enumerator, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0])

    monkeypatch.setattr(enumerator.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        enumerator.candidate_paths()
