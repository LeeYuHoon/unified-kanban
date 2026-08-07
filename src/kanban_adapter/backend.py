"""Validated Hermes Kanban CLI boundary used by every observation adapter."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


Runner = Callable[[list[str]], str]


class BoardNotMappedError(RuntimeError):
    """No dashboard board project directory contains the current directory."""


def run_command(argv: list[str]) -> str:
    """Run a Hermes command and return stdout, raising with sanitized diagnostics."""
    completed = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {detail}")
    return completed.stdout


_SOURCE = {
    "claude-code": ("claude-code-external", "claude"),
    "codex": ("codex-external", "codex"),
    "manual": (None, "manual"),
}
_BOARD_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_TASK_RE = re.compile(r"t_[A-Za-z0-9]+\Z")
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")
_MARKER_SCAN_DEPTH = 12


def _marker_pattern(marker: str) -> re.Pattern[str]:
    """Matches ``marker`` only as a whole token.

    The event id is matched exactly: a longer id that merely starts with this
    one, or a token that embeds it, is a different event and must not suppress
    a comment that was never posted.
    """
    return re.compile(
        rf"(?<![A-Za-z0-9._-]){re.escape(marker)}(?![A-Za-z0-9._-])"
    )


def _contains_marker(value: object, pattern: re.Pattern[str], depth: int = 0) -> bool:
    """True when ``pattern`` matches any string inside a decoded card.

    The scan is deliberately schema-agnostic: it walks whatever ``show --json``
    returns instead of assuming where comments live, so a Hermes schema change
    can never silently turn duplicate detection off.
    """
    if depth > _MARKER_SCAN_DEPTH:
        return False
    if isinstance(value, str):
        return pattern.search(value) is not None
    if isinstance(value, dict):
        return any(
            _contains_marker(item, pattern, depth + 1) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_marker(item, pattern, depth + 1) for item in value)
    return False


def comment_marker_present(
    runner: Runner, *, board: str, task_id: str, marker: str
) -> bool:
    """Whether this card already carries a comment bearing ``marker``.

    Raises when the card cannot be inspected: without an answer the only safe
    move is to not append, because a duplicate usage comment is worse than a
    late-reported failure.
    """
    if not _IDEMPOTENCY_KEY_RE.fullmatch(marker or ""):
        raise ValueError("invalid idempotency key")
    raw = runner([
        "hermes", "kanban", "--board", board, "show", "--json", "--", task_id,
    ])
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Hermes show returned non-JSON output") from exc
    if not isinstance(payload, (dict, list)):
        raise RuntimeError("Hermes show did not return a card")
    return _contains_marker(payload, _marker_pattern(marker))


@dataclass
class HermesCliBackend:
    """Create and mutate validated observation cards through the Hermes CLI."""

    runner: Runner = run_command

    def resolve_board(self, *, cwd: Path) -> str:
        """Select the uniquely deepest Dashboard project mapping containing ``cwd``."""
        raw = self.runner(["hermes", "kanban", "boards", "list", "--json"])
        try:
            boards = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Hermes board list returned non-JSON output") from exc
        if not isinstance(boards, list):
            raise RuntimeError("Hermes board list was not a JSON array")

        current = cwd.resolve()
        matches: list[tuple[int, str]] = []
        for board in boards:
            if not isinstance(board, dict):
                raise RuntimeError("Hermes board list contained a non-object entry")
            if "default_workdir" not in board:
                raise RuntimeError(
                    "Hermes board list entry was missing default_workdir"
                )
            slug = board.get("slug")
            workdir = board["default_workdir"]
            if not isinstance(slug, str) or not _BOARD_RE.fullmatch(slug):
                raise RuntimeError("Hermes board list contained an invalid slug")
            if workdir is None:
                continue
            if not isinstance(workdir, str) or not workdir:
                raise RuntimeError("Hermes board list contained an invalid project directory")
            requested = Path(workdir).expanduser()
            if not requested.is_absolute():
                raise RuntimeError(
                    "Hermes board list contained a non-absolute project directory"
                )
            root = requested.resolve()
            if current == root or root in current.parents:
                matches.append((len(root.parts), slug))

        if not matches:
            raise BoardNotMappedError(
                "no Kanban board is mapped to this directory; set Project directory "
                "when creating the board in Hermes Dashboard"
            )
        matches.sort(reverse=True)
        longest = matches[0][0]
        winners = sorted(slug for depth, slug in matches if depth == longest)
        if len(winners) > 1:
            raise RuntimeError(
                "multiple Kanban boards map to this directory: " + ", ".join(winners)
            )
        return winners[0]

    def start(self, *, board: str, title: str, source: str) -> str:
        """Create a running observation card and return its validated task id."""
        if source not in _SOURCE:
            raise ValueError(f"unsupported source: {source}")
        assignee, tenant = _SOURCE[source]
        argv = ["hermes", "kanban", "--board", board, "create"]
        if assignee:
            argv.extend(["--assignee", assignee])
        argv.extend([
            "--tenant", tenant,
            "--created-by", "kanban-adapter",
            "--observation",
            "--json",
            "--", title,
        ])
        raw = self.runner(argv)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            preview = raw.strip().replace("\n", " ")[:200]
            raise RuntimeError(f"Hermes returned non-JSON output: {preview}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Hermes create response was not a JSON object")
        task_id = payload.get("id")
        if not isinstance(task_id, str) or not _TASK_RE.fullmatch(task_id):
            raise RuntimeError("Hermes create response did not contain a valid task id")
        if payload.get("status") != "running" or payload.get("observation") is not True:
            # Idempotent create may return a pre-existing row. Without a
            # creation token we cannot prove this call owns the returned task,
            # so compensating with ``complete`` could terminate unrelated work.
            raise RuntimeError(
                "Hermes create did not return a running observation card"
            )
        return task_id

    def update(
        self,
        *,
        board: str,
        task_id: str,
        message: str,
        idempotency_key: str | None = None,
    ) -> None:
        """Append a comment, at most once per ``idempotency_key``.

        The key is a marker carried inside ``message``; when the card already
        shows it, the append is skipped. That makes a retry after a crash --
        or after the caller failed to record that it had posted -- harmless.
        """
        if idempotency_key is not None:
            if comment_marker_present(
                self.runner, board=board, task_id=task_id, marker=idempotency_key
            ):
                return
        self.runner([
            "hermes", "kanban", "--board", board, "comment",
            "--author", "kanban-adapter", "--", task_id, message,
        ])

    def done(
        self,
        *,
        board: str,
        task_id: str,
        result: str | None = None,
        result_file: Path | None = None,
        summary: str | None = None,
    ) -> None:
        """Complete a card while keeping full result and concise summary separate."""
        argv = ["hermes", "kanban", "--board", board, "complete"]
        if result is not None and result_file is not None:
            raise ValueError("result and result_file are mutually exclusive")
        if result is None:
            if result_file is not None:
                argv.append(f"--result-file={result_file}")
            else:
                result = summary
        if result is not None:
            argv.append(f"--result={result}")
        if summary:
            # Keep the concise handoff separate from the complete assistant
            # result. `--opt=<text>` binds leading-hyphen user data safely.
            argv.append(f"--summary={summary}")
        argv.extend(["--", task_id])
        self.runner(argv)

    def block(self, *, board: str, task_id: str, reason: str) -> None:
        """Block an observation card when explicit user input is required."""
        self.runner([
            "hermes", "kanban", "--board", board, "block",
            "--kind", "needs_input", "--", task_id, reason,
        ])
