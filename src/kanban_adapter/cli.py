"""Command-line interface for explicit Unified Kanban card mutations."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .backend import BoardNotMappedError, HermesCliBackend
from .compatibility import check_hermes_compatibility


_USER_DATA_OPTIONS = frozenset({
    "--title", "--message", "--result", "--result-file", "--summary", "--reason",
})
_BOARD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _bind_user_data_options(argv: Sequence[str]) -> list[str]:
    """Bind the next token as data even when it starts with a hyphen."""
    normalized: list[str] = []
    index = 0
    raw = list(argv)
    while index < len(raw):
        token = raw[index]
        if token in _USER_DATA_OPTIONS and index + 1 < len(raw):
            normalized.append(f"{token}={raw[index + 1]}")
            index += 2
            continue
        normalized.append(token)
        index += 1
    return normalized


def build_parser() -> argparse.ArgumentParser:
    """Build the adapter parser without reading process-global arguments."""
    parser = argparse.ArgumentParser(prog="kanban-adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--board")
    start.add_argument("--title", required=True)
    start.add_argument("--source", required=True, choices=("claude-code", "codex", "manual"))

    update = sub.add_parser("update")
    update.add_argument("--board")
    update.add_argument("--task", required=True)
    update.add_argument("--message", required=True)
    # Optional: when given, the comment is appended only if the card does not
    # already carry this marker, so a retried post cannot duplicate it.
    update.add_argument("--idempotency-key", dest="idempotency_key")

    done = sub.add_parser("done")
    done.add_argument("--board")
    done.add_argument("--task", required=True)
    result_source = done.add_mutually_exclusive_group()
    result_source.add_argument("--result")
    result_source.add_argument("--result-file", type=Path)
    done.add_argument("--summary")

    block = sub.add_parser("block")
    block.add_argument("--board")
    block.add_argument("--task", required=True)
    block.add_argument("--reason", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: Any | None = None,
    compatibility_check: Callable[[], tuple[bool, str]] = check_hermes_compatibility,
) -> int:
    """Execute one adapter command and return a stable process exit status."""
    raw_argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(_bind_user_data_options(raw_argv))
    if backend is None:
        compatible, reason = compatibility_check()
        if not compatible:
            print(f"kanban-adapter: {reason}", file=sys.stderr)
            return 1
    service = backend if backend is not None else HermesCliBackend()
    board = args.board
    if not board:
        try:
            board = service.resolve_board(cwd=Path.cwd().resolve())
        except BoardNotMappedError as exc:
            board = os.environ.get("HERMES_KANBAN_BOARD")
            if not board:
                print(f"kanban-adapter: {exc}", file=sys.stderr)
                return 1
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"kanban-adapter: {exc}", file=sys.stderr)
            return 1
    if not _BOARD_RE.fullmatch(board):
        print("kanban-adapter: invalid board slug", file=sys.stderr)
        return 2

    try:
        if args.command == "start":
            task_id = service.start(board=board, title=args.title, source=args.source)
            print(task_id)
        elif args.command == "update":
            options: dict[str, Any] = {
                "board": board, "task_id": args.task, "message": args.message,
            }
            if args.idempotency_key:
                options["idempotency_key"] = args.idempotency_key
            service.update(**options)
        elif args.command == "done":
            service.done(
                board=board,
                task_id=args.task,
                result=args.result,
                result_file=args.result_file,
                summary=args.summary,
            )
        elif args.command == "block":
            service.block(board=board, task_id=args.task, reason=args.reason)
        else:  # pragma: no cover - argparse enforces this
            raise AssertionError(f"unexpected command: {args.command}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"kanban-adapter: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
