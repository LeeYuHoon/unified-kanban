"""Codex model resolution for hook events that omit ``model``.

Codex 0.145's hook input schemas require ``model`` on UserPromptSubmit,
PostToolUse, SubagentStart and Stop, but SessionEnd carries none and older or
future builds may drop it. The one recovered value is the model recorded for
*this exact session* in Codex's state database.

There is deliberately no second source. A machine-wide default -- such as the
top-level ``model`` in ``config.toml`` -- is not this session's model; reporting
it would put a plausible but unverified identifier on a card, which is worse
than an honest gap. When the session row cannot be read, the model is simply
left unrecorded.

Only the model identifier is ever read: the SQL projection names the single
``model`` column, so no prompt, cwd, or title can be returned by accident. The
database is opened read-only, and its path is checked to be a regular
non-symlink file both before and after the connection is opened, so a file
swapped in mid-call is refused rather than read.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote

from .usage import sanitize_model

_STATE_DB_RE = re.compile(r"state_(\d+)\.sqlite\Z")
_QUERY_TIMEOUT_SECONDS = 2.0


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def default_state_db(codex_home: Path | None = None) -> Path | None:
    """Newest versioned Codex state database, or None when there is none."""
    home = codex_home or default_codex_home()
    best: tuple[int, Path] | None = None
    try:
        entries = list(home.iterdir())
    except OSError:
        return None
    for entry in entries:
        match = _STATE_DB_RE.fullmatch(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        if best is None or version > best[0]:
            best = (version, entry)
    return None if best is None else best[1]


def _regular_file_stat(path: Path | None) -> os.stat_result | None:
    """``lstat`` of a plain regular file, or None for anything else.

    ``lstat`` rather than ``stat`` so a symlink is seen as a symlink and
    refused instead of being followed to whatever it points at.
    """
    if path is None:
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return info


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    """Whether two stats describe the same file with unchanged permissions."""
    return (before.st_dev, before.st_ino, before.st_mode) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
    )


def _read_only_uri(path: Path) -> str:
    """SQLite read-only URI for ``path``.

    The path is percent-encoded because SQLite parses ``?`` and ``#`` in a URI
    as query and fragment delimiters; a Codex home containing either would
    otherwise silently open a different database.
    """
    return f"file:{quote(str(path))}?mode=ro"


def _model_from_state_db(session_id: str, state_db: Path | None) -> str | None:
    if not session_id or state_db is None:
        return None
    before = _regular_file_stat(state_db)
    if before is None:
        return None
    connection = None
    try:
        connection = sqlite3.connect(
            _read_only_uri(state_db),
            uri=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
        # Between the first lstat and the open, the path could have been
        # replaced -- with a symlink, a FIFO, or another user's database.
        # Re-check before running any statement and fail closed on any
        # difference, so at worst the model goes unrecorded.
        after = _regular_file_stat(state_db)
        if after is None or not _same_file(before, after):
            return None
        row = connection.execute(
            "SELECT model FROM threads WHERE id = ? LIMIT 1", (session_id,)
        ).fetchone()
    except Exception:
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    if not row:
        return None
    return sanitize_model(row[0])


def resolve_model(session_id: str, *, state_db: Path | None) -> str | None:
    """This session's Codex model, or None when it cannot be read exactly."""
    return _model_from_state_db(session_id, state_db)


def resolve_rollout_path(session_id: str, *, state_db: Path | None) -> str | None:
    """Return this session's rollout path from Codex state, read-only and fail-closed."""
    if not session_id or state_db is None:
        return None
    before = _regular_file_stat(state_db)
    if before is None:
        return None
    connection = None
    try:
        connection = sqlite3.connect(
            _read_only_uri(state_db),
            uri=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
        after = _regular_file_stat(state_db)
        if after is None or not _same_file(before, after):
            return None
        row = connection.execute(
            "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1", (session_id,)
        ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
    if not row or not isinstance(row[0], str) or not row[0] or "\x00" in row[0]:
        return None
    path = Path(row[0])
    if not path.is_absolute() or len(row[0]) > 4096:
        return None
    return row[0]
