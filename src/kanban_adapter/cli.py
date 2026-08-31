"""명시적인 Unified Kanban 카드 변경을 위한 명령줄 인터페이스."""

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
    "--title", "--title-file", "--message", "--result", "--result-file",
    "--summary", "--reason",
})
_BOARD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _bind_user_data_options(argv: Sequence[str]) -> list[str]:
    """다음 토큰이 하이픈으로 시작하더라도 데이터로 바인딩한다."""
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
    """프로세스 전역 인자를 읽지 않고 어댑터 파서를 구성한다."""
    parser = argparse.ArgumentParser(prog="kanban-adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--board")
    title_source = start.add_mutually_exclusive_group(required=True)
    title_source.add_argument("--title")
    title_source.add_argument("--title-file", type=Path)
    start.add_argument("--source", required=True, choices=("claude-code", "codex", "manual"))
    start.add_argument("--idempotency-key", dest="idempotency_key")

    update = sub.add_parser("update")
    update.add_argument("--board")
    update.add_argument("--task", required=True)
    update.add_argument("--message", required=True)
    # 선택 사항: 지정되면 카드가 이 마커를 아직 가지고 있지 않을 때에만
    # 댓글이 추가되므로, 재시도된 게시가 댓글을 중복시킬 수 없다.
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
    """어댑터 명령 하나를 실행하고 안정적인 프로세스 종료 상태를 반환한다."""
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
            options = {"board": board, "source": args.source}
            if args.idempotency_key is not None:
                options["idempotency_key"] = args.idempotency_key
            if args.title is not None:
                options["title"] = args.title
            else:
                options["title_file"] = args.title_file
            task_id = service.start(**options)
            print(task_id)
        elif args.command == "update":
            options: dict[str, Any] = {
                "board": board, "task_id": args.task, "message": args.message,
            }
            if args.idempotency_key is not None:
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
