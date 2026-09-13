"""명시적 private runtime config로만 conversation service를 조립한다."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .conversation_integration import (
    BindingStore,
    ConversationService,
    KernelAuthorityReceiptV1,
    OwnerPolicyFile,
    ProductionConversationAuthority,
)

_ENV = "UNIFIED_KANBAN_CONVERSATION_CONFIG"
_lock = threading.Lock()
_cached_path: str | None = None
_cached_service: ConversationService | None = None


def _private_bytes(path: Path, *, maximum: int = 65_536) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or not 1 <= info.st_size <= maximum
        ):
            raise PermissionError("conversation runtime input must be owner 0600 regular single-link file")
        value = os.read(fd, maximum + 1)
        if len(value) != info.st_size:
            raise OSError("conversation runtime input read was incomplete")
        return value
    finally:
        os.close(fd)


def _absolute(value: Any, name: str) -> Path:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def _task_membership(board: str, task: str) -> bool:
    """migration 없이 명시적 board DB에서 observation membership만 읽는다."""
    from hermes_cli import kanban_db

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", board):
        raise ValueError("invalid explicit board slug")
    path = (
        kanban_db.kanban_home() / "kanban.db"
        if board == "default"
        else kanban_db.board_dir(board) / "kanban.db"
    )
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT id, observation FROM tasks WHERE id = ? LIMIT 1", (task,)
        ).fetchone()
        return row is not None and row["id"] == task and row["observation"] == 1
    finally:
        connection.close()


def _build(path: Path) -> ConversationService | None:
    raw = _private_bytes(path)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("conversation runtime config schema is invalid")
    if payload.get("enabled") is not True:
        return None
    allowed = {
        "schema_version", "enabled", "authority_secret_file", "kernel_secret_file",
        "kernel_receipt", "policy_file", "binding_root", "principal_board_grants",
        "provider_roots",
    }
    if set(payload) != allowed:
        raise ValueError("conversation runtime config fields are invalid")
    authority_secret = _private_bytes(_absolute(payload["authority_secret_file"], "authority secret"), maximum=4096)
    kernel_secret = _private_bytes(_absolute(payload["kernel_secret_file"], "kernel secret"), maximum=4096)
    receipt_data = payload["kernel_receipt"]
    if not isinstance(receipt_data, dict):
        raise TypeError("kernel receipt is invalid")
    receipt = KernelAuthorityReceiptV1(**receipt_data)
    authority = ProductionConversationAuthority.bootstrap(
        authority_secret=authority_secret,
        kernel_secret=kernel_secret,
        receipt=receipt,
    )
    grants = payload["principal_board_grants"]
    if not isinstance(grants, dict):
        raise TypeError("principal board grants are invalid")
    normalized: dict[str, frozenset[str]] = {}
    for principal, boards in grants.items():
        if not isinstance(principal, str) or not principal or not isinstance(boards, list):
            raise ValueError("principal board grant is invalid")
        if any(not isinstance(board, str) or not board for board in boards):
            raise ValueError("principal board grant is invalid")
        normalized[principal] = frozenset(boards)
    roots = payload["provider_roots"]
    if not isinstance(roots, dict) or set(roots) - {"claude", "codex"}:
        raise ValueError("configured provider roots are invalid")
    normalized_roots = {
        provider: _absolute(root, f"{provider} provider root")
        for provider, root in roots.items()
    }
    return ConversationService(
        authority=authority,
        policies=OwnerPolicyFile(_absolute(payload["policy_file"], "policy file"), secret=authority_secret),
        bindings=BindingStore(_absolute(payload["binding_root"], "binding root"), secret=authority_secret),
        task_membership=_task_membership,
        principal_board_grants=normalized,
        kernel_secret=kernel_secret,
        provider_roots=normalized_roots,
    )


def get_conversation_service() -> ConversationService | None:
    """설정 미지정/disabled면 source나 board를 열지 않고 None을 반환한다."""
    global _cached_path, _cached_service
    value = os.environ.get(_ENV)
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("conversation runtime config path must be absolute")
    with _lock:
        if _cached_path == value:
            return _cached_service
        service = _build(path)
        _cached_path = value
        _cached_service = service
        return service


def capture_hook_start(**kwargs: Any) -> dict[str, object] | None:
    service = get_conversation_service()
    if service is None:
        return None
    return service.capture_hook_start(**kwargs)


def seal_hook_binding(**kwargs: Any) -> bool:
    service = get_conversation_service()
    if service is None:
        return False
    return service.seal_hook_binding(**kwargs) is not None
