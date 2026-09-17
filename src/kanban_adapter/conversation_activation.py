"""현재 프로필의 명시적 선택자만 읽고 준비된 런타임을 자동 활성화하지 않는다."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from . import private_files
from . import conversation_namespace as namespace

CONFIG_ENV = "UNIFIED_KANBAN_CONVERSATION_CONFIG"
_MAXIMUM = 65_536


def _absolute(value: Any) -> Path:
    """환경 및 선택자 경로의 잘못된 값을 보정하지 않고 거부한다."""
    if (not isinstance(value, str) or not value or "\0" in value
            or not Path(value).is_absolute() or ".." in value.split("/")):
        raise ValueError("conversation activation path must be absolute and safe")
    return Path(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """중복 키가 동의 플래그를 덮어쓰지 못하게 한다."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("conversation activation has duplicate fields")
        result[key] = value
    return result


def _validate_private(info: os.stat_result, *, directory: bool = False) -> None:
    """권한을 고치지 않고 기존 소유자 전용 inode만 허용한다."""
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and (info.st_nlink != 1 or not 1 <= info.st_size <= _MAXIMUM))):
        raise PermissionError("conversation activation requires private owner-only state")


def resolve_activation(profile_home: Path) -> Path | None:
    """선택자 부재 또는 명시적 비활성만 None으로 취급한다."""
    path = _absolute(str(profile_home)) / "unified-kanban-conversation" / "activation.json"
    try:
        parent = namespace.open_directory(path.parent)
    except FileNotFoundError:
        return None
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            private_files.validate_directory(path.parent, parent)
            return None
        with private_files.Receipt(os.dup(parent), fd, path.name) as receipt:
            _validate_private(os.fstat(parent), directory=True)
            namespace.validate_fd(parent, private=True)
            before = private_files._validate_receipt(receipt)
            _validate_private(before)
            namespace.validate_fd(fd, private=True)
            raw = os.read(fd, _MAXIMUM + 1)
            after = private_files._validate_receipt(receipt)
            _validate_private(after)
            private_files.validate_directory(path.parent, parent)
            _validate_private(os.fstat(parent), directory=True)
            namespace.validate_fd(parent, private=True)
            if (len(raw) != before.st_size or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns):
                raise RuntimeError("conversation activation changed during read")
    finally:
        os.close(parent)
    payload = json.loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(payload, dict)
            or set(payload) != {"schema_version", "enabled", "runtime_config", "launcher_profiles"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or type(payload["enabled"]) is not bool):
        raise ValueError("conversation activation schema is invalid")
    selected = _absolute(payload["runtime_config"])
    profiles = payload["launcher_profiles"]
    if not isinstance(profiles, dict) or set(profiles) - {"claude", "codex"}:
        raise ValueError("conversation activation launcher profiles are invalid")
    for reference in profiles.values():
        _absolute(reference)
    # 참조는 검증만 한다. 네이티브 실행 및 해당 파일 읽기는 별도 승인 범위다.
    return selected if payload["enabled"] else None


def resolve_runtime_config() -> Path | None:
    """명시적 환경 재정의를 우선하고 현재 프로필 이외는 탐색하지 않는다."""
    if CONFIG_ENV in os.environ:
        return _absolute(os.environ[CONFIG_ENV])
    home = _absolute(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    return resolve_activation(home)
