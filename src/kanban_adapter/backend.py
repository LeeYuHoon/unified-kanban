"""Validated Hermes Kanban CLI boundary used by every observation adapter."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


Runner = Callable[[list[str]], str]
_FD_FILE_RE = re.compile(r"--(?:title|result)-file=/dev/fd/([0-9]+)\Z")


class BoardNotMappedError(RuntimeError):
    """No dashboard board project directory contains the current directory."""


def run_command(argv: list[str]) -> str:
    """Run a Hermes command and return stdout, raising with sanitized diagnostics."""
    pass_fds = tuple(sorted({
        int(match.group(1))
        for argument in argv
        if (match := _FD_FILE_RE.fullmatch(argument)) is not None
    }))
    completed = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
        pass_fds=pass_fds,
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

    def start(
        self,
        *,
        board: str,
        source: str,
        title: str | None = None,
        title_file: Path | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Create a running observation card and return its validated task id."""
        if source not in _SOURCE:
            raise ValueError(f"unsupported source: {source}")
        if (title is None) == (title_file is None):
            raise ValueError("title and title_file are mutually exclusive")
        owned_title_file: Path | None = None
        if title_file is None:
            fd, name = tempfile.mkstemp(prefix="unified-kanban-title-")
            owned_title_file = Path(name)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(title or "")
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                owned_title_file.unlink(missing_ok=True)
                raise
            title_file = owned_title_file
        assignee, tenant = _SOURCE[source]
        argv = ["hermes", "kanban", "--board", board, "create"]
        if idempotency_key is not None:
            if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
                raise ValueError("invalid idempotency key")
            argv.append(f"--idempotency-key={idempotency_key}")
        if assignee:
            argv.extend(["--assignee", assignee])
        argv.extend([
            "--tenant", tenant,
            "--created-by", "kanban-adapter",
            "--observation",
            "--json",
            f"--title-file={title_file}",
        ])
        try:
            raw = self.runner(argv)
        finally:
            if owned_title_file is not None:
                owned_title_file.unlink(missing_ok=True)
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
        """Append a comment, atomically at most once per ``idempotency_key``."""
        argv = [
            "hermes", "kanban", "--board", board, "comment",
            "--author", "kanban-adapter",
        ]
        if idempotency_key is not None:
            if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
                raise ValueError("invalid idempotency key")
            argv.append(f"--idempotency-key={idempotency_key}")
        argv.extend(["--", task_id, message])
        self.runner(argv)

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
