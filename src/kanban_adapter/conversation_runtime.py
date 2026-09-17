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

from . import private_files as private
from .conversation_transaction import policy_lease, check_grant_fence

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


def _runtime_snapshot(path: Path):
    # 바이트가 같아도 교체된 inode를 이전 service의 권한으로 채택하지 않는다.
    parent = private.open_directory(path.parent)
    try:
        private.validate_directory(path.parent, parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or not 1 <= info.st_size <= 65_536):
                raise PermissionError("runtime input must be bounded owner 0600 single-link file")
            raw = os.read(fd, 65_537)
            after = os.fstat(fd)
            canonical = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            def identity(value):
                return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                        value.st_ctime_ns, value.st_mode, value.st_uid, value.st_nlink)
            if (len(raw) != info.st_size or identity(info) != identity(after)
                    or identity(info) != identity(canonical)):
                raise PermissionError("runtime identity changed during admission")
            private.validate_directory(path.parent, parent)
            root = os.fstat(parent)
            return raw, (info.st_dev, info.st_ino), (root.st_dev, root.st_ino)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _build(path: Path) -> ConversationService | None:
    snapshot = _runtime_snapshot(path)
    payload = json.loads(snapshot[0])
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("conversation runtime config schema is invalid")
    if payload.get("enabled") is not True:
        return None
    # 필드 누락을 내부 KeyError로 흘리거나 정책 잠금 파일을 만들기 전에 거부한다.
    _validate_runtime_fields(payload)
    policy_path = _absolute(payload["policy_file"], "policy file")
    with policy_lease(policy_path):
        if _runtime_snapshot(path) != snapshot:
            raise PermissionError("runtime changed before policy lease")
        service = _build_locked(path, snapshot[0])
        policy = service.policies.load()
        policy_snapshot = _runtime_snapshot(policy_path)

        def admission():
            check_grant_fence(policy_path, service.policies.secret)
            if _runtime_snapshot(path) != snapshot:
                raise PermissionError("runtime changed; retained service is stale")
            if service.policies.load() != policy or _runtime_snapshot(policy_path) != policy_snapshot:
                raise PermissionError("policy changed; retained runtime service is stale")

        service._runtime_admission = admission
        admission()
        return service


def _validate_runtime_fields(payload: dict[str, Any]) -> None:
    """활성 설정의 필드 집합을 부작용 없이 검사한다."""
    allowed = {
        "schema_version", "enabled", "authority_secret_file", "kernel_secret_file",
        "kernel_receipt", "policy_file", "binding_root", "principal_board_grants",
        "provider_roots",
    }
    if set(payload) != allowed:
        raise ValueError("conversation runtime config fields are invalid")


def _build_locked(path: Path, raw: bytes) -> ConversationService:
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("conversation runtime config schema is invalid")
    if payload.get("enabled") is not True:
        raise PermissionError("runtime must be enabled during construction")
    _validate_runtime_fields(payload)
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
    from .conversation_activation import resolve_runtime_config

    path = resolve_runtime_config()
    if path is None:
        return None
    value = str(path)
    with _lock:
        if _cached_path == value and _cached_service is not None:
            try:
                with _cached_service.collection_operation():
                    return _cached_service
            except (OSError, RuntimeError, TypeError, ValueError):
                # 같은 객체를 재인증 없이 반환하거나 정책 변경을 자동 채택하지 않는다.
                return None
        service = _build(path)
        _cached_path = value
        _cached_service = service
        return service


def capture_hook_start(**kwargs: Any) -> dict[str, object] | None:
    service = get_conversation_service()
    if service is None:
        return None
    prompt_id = kwargs.pop("prompt_id", None)
    if kwargs.get("provider") == "claude" and prompt_id is not None:
        from .claude_file_provenance import capture
        kwargs.pop("provider")
        return capture(service, prompt_id=prompt_id, **kwargs)
    return service.capture_hook_start(**kwargs)


def seal_hook_binding(**kwargs: Any) -> bool:
    service = get_conversation_service()
    if service is None:
        return False
    return service.seal_hook_binding(**kwargs) is not None
