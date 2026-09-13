"""owner가 collection policy를 명시적으로 enable/disable하는 전용 CLI."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .conversation import CollectionPolicyV1
from .conversation_integration import OwnerPolicyFile


def _secret(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size < 32
            or info.st_size > 4096
        ):
            raise PermissionError("policy secret must be an owner 0600 regular single-link file")
        value = os.read(fd, 4097)
        if len(value) != info.st_size:
            raise OSError("policy secret read was incomplete")
        return value
    finally:
        os.close(fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kanban-conversation-policy")
    parser.add_argument("--policy-file", required=True, type=Path)
    parser.add_argument("--secret-file", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    enable = commands.add_parser("enable")
    enable.add_argument("--board", required=True)
    enable.add_argument(
        "--provider", required=True, action="append",
        choices=("claude", "claude-code", "codex", "hermes"),
        help="claude-code는 claude의 CLI 별칭이며 새 정책에는 claude로 서명됩니다",
    )
    enable.add_argument("--expires-at-ns", required=True, type=int)
    enable.add_argument("--now-ns", type=int)
    disable = commands.add_parser("disable")
    disable.add_argument("--board", required=True)
    disable.add_argument("--now-ns", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = OwnerPolicyFile(args.policy_file.resolve(), secret=_secret(args.secret_file.resolve()))
        current = store.load()
        now_ns = args.now_ns if args.now_ns is not None else time.time_ns()
        enabled = dict(current.enabled_boards)
        if args.command == "enable":
            # CLI 입력의 별칭만 서명 전에 정규화하고 기존 서명 정책은 그대로 검증한다.
            enabled[args.board] = frozenset(
                "claude" if provider == "claude-code" else provider
                for provider in args.provider
            )
            expires_at_ns = args.expires_at_ns
        else:
            enabled.pop(args.board, None)
            expires_at_ns = max(current.expires_at_ns, now_ns + 1)
        replacement = CollectionPolicyV1(
            version=current.version,
            generation=current.generation + 1,
            activated_at_ns=now_ns,
            expires_at_ns=expires_at_ns,
            enabled_boards=enabled,
            minimum_binding_version=current.minimum_binding_version,
        )
        store.replace(replacement, expected_generation=current.generation)
    except (OSError, PermissionError, RuntimeError, TypeError, ValueError) as error:
        print(f"kanban-conversation-policy: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
