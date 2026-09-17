"""외부 대화 수집용 owner 파일만 준비하며 실행 환경은 활성화하지 않는다."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time

from .conversation import CollectionPolicyV1, SourceLocator, open_verified_root
from .conversation_integration import OwnerPolicyFile, ProductionConversationAuthority
from . import private_files as private
from .conversation_transaction import policy_lease


def _absolute(value: str) -> Path:
    if not value.startswith("/") or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("absolute path without dot components required")
    return Path(value)


def _owner_directory(path: Path) -> int:
    fd = private.open_directory(path)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("directory must be owner 0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _kernel(path: Path) -> bytes:
    parent = _owner_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        try:
            receipt_parent = os.dup(parent)
        except BaseException:
            os.close(fd)
            raise
        with private.Receipt(receipt_parent, fd, path.name) as receipt:
            info = private._validate_receipt(receipt)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or not 32 <= info.st_size <= 4096:
                raise PermissionError("kernel key must be owner 0600 single-link, 32..4096 bytes")
            value = os.read(fd, 4097)
            private._validate_receipt(receipt)
            private.validate_directory(path.parent, parent)
            if len(value) != info.st_size:
                raise OSError("incomplete kernel key read")
            return value
    finally:
        os.close(parent)


def initialize(args: argparse.Namespace) -> Path:
    state = _absolute(args.state_root)
    kernel_path = _absolute(args.kernel_secret_file)
    selected = os.environ.get("HERMES_KANBAN_CONVERSATION_KERNEL_SECRET_FILE")
    if not selected:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        selected = str(_absolute(home) / "conversation-journal" / "kernel-receipt.key")
    if kernel_path != _absolute(selected):
        raise ValueError("explicit kernel key must match native selected path")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.board):
        raise ValueError("invalid board")
    if len(args.principal) > 128:
        raise ValueError("at most 128 explicit principals allowed")
    principals = set(args.principal)
    # 네임스페이스 사용자 ID를 그대로 보존하고 native의 191자 제한을 따른다.
    if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.@:-]{0,190}", p) for p in principals):
        raise ValueError("invalid explicit principal")
    roots = {}
    for specification in args.provider_root:
        provider, separator, value = specification.partition("=")
        if not separator or provider not in {"claude", "codex"} or provider in roots:
            raise ValueError("invalid or duplicate provider root")
        root = _absolute(value)
        with open_verified_root(SourceLocator(provider, root, Path("."), os.getuid())):
            fd = _owner_directory(root)
            os.close(fd)
        roots[provider] = str(root)
    now = time.time_ns()
    if not now < args.expires_at_ns <= 2**63 - 1:
        raise ValueError("expiry must be a future signed 64-bit nanosecond timestamp")
    kernel_secret = _kernel(kernel_path)
    authority_secret = secrets.token_bytes(32)
    receipt = ProductionConversationAuthority.issue_kernel_receipt(
        kernel_secret=kernel_secret, issuer_id="hermes-kanban-kernel",
        key_id=hashlib.sha256(kernel_secret).hexdigest()[:24], now_ns=now,
    )
    ProductionConversationAuthority.bootstrap(authority_secret=authority_secret, kernel_secret=kernel_secret, receipt=receipt)
    parent = _owner_directory(state.parent)
    try:
        # 배타적 mkdir로 기존 state는 내용과 권한을 바꾸지 않고 거절한다.
        os.mkdir(state.name, 0o700, dir_fd=parent)
        root_fd = _owner_directory(state)
        try:
            with policy_lease(state / "policy.json", exclusive=True):
                private.validate_directory(state.parent, parent)
                os.mkdir("bindings", 0o700, dir_fd=root_fd)
                private.atomic_publish(state / "authority.key", authority_secret, directory_fd=root_fd).close()
                store = OwnerPolicyFile(state / "policy.json", secret=authority_secret)
                store.replace(CollectionPolicyV1(
                    version=1, generation=2, activated_at_ns=now, expires_at_ns=args.expires_at_ns,
                    enabled_boards={args.board: frozenset(roots)}, minimum_binding_version=1,
                ), expected_generation=1)
                private.validate_directory(state, root_fd)
                os.fsync(root_fd)
                os.fsync(parent)
                config = dict(schema_version=1, enabled=True, authority_secret_file=str(state / "authority.key"),
                              kernel_secret_file=str(kernel_path), kernel_receipt=asdict(receipt),
                              policy_file=str(state / "policy.json"), binding_root=str(state / "bindings"),
                              principal_board_grants={p: [args.board] for p in sorted(principals)}, provider_roots=roots)
                try:
                    private.atomic_publish(state / "runtime.json", json.dumps(config).encode() + b"\n", directory_fd=root_fd).close()
                except private.CommittedPublicationError as error:
                    # 이름 기반 unlink를 원자적 identity 삭제라고 주장하지 않는다.
                    # 보유한 새 config inode 자체를 무효화한다. I/O 장애는 그대로 보고한다.
                    try:
                        os.ftruncate(error.receipt.file_fd, 0)
                        os.fsync(error.receipt.file_fd)
                    except BaseException as cleanup_error:
                        error.add_note(f"runtime invalidation failed; state may remain usable: {cleanup_error}")
                    finally:
                        error.receipt.close()
                    raise
        finally:
            os.close(root_fd)
    finally:
        os.close(parent)
    return state / "runtime.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    for option in ("state-root", "kernel-secret-file", "board"):
        init.add_argument("--" + option, required=True)
    init.add_argument("--principal", required=True, action="append")
    init.add_argument("--provider-root", required=True, action="append")
    init.add_argument("--expires-at-ns", required=True, type=int)
    grant = commands.add_parser("grant-existing")
    grant.add_argument("--runtime-config", required=True)
    grant.add_argument("--board", required=True)
    grant.add_argument("--provider", required=True, action="append", choices=("claude", "codex"))
    grant.add_argument("--principal", required=True, action="append")
    grant.add_argument("--expected-policy-generation", required=True, type=int)
    grant.add_argument("--expected-config-sha256", required=True)
    mode = grant.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    recovery = commands.add_parser("recover-grant")
    for option in ("runtime-config", "policy-file", "secret-file"):
        recovery.add_argument("--" + option, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "recover-grant":
            from .conversation_transaction import recover_grant
            print(json.dumps(recover_grant(args), sort_keys=True))
            return 0
        if args.command == "grant-existing":
            from .conversation_transaction import grant_existing
            print(json.dumps(grant_existing(args), sort_keys=True))
            return 0
        initialize(args)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"conversation-owner: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(f"conversation-owner: {note}", file=sys.stderr)
        return 1
    print("Owner runtime prepared; collection environment remains unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
