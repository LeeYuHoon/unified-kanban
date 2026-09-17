"""영구 선택자를 사용하는 저장소 소유 네이티브 실행 후보. 설치는 수행하지 않는다."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
import sys

from . import conversation_activation as activation
from . import private_files
from . import conversation_namespace as namespace


def _read_private(path: Path) -> dict:
    """선택자와 실행 참조를 권한 수정 없이 FD로 읽는다."""
    parent = namespace.open_directory(path.parent)
    try:
        namespace.validate_fd(parent, private=True)
        activation._validate_private(os.fstat(parent), directory=True)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        with private_files.Receipt(os.dup(parent), fd, path.name) as receipt:
            before = private_files._validate_receipt(receipt)
            activation._validate_private(before)
            namespace.validate_fd(fd, private=True)
            raw = os.read(fd, activation._MAXIMUM + 1)
            after = private_files._validate_receipt(receipt)
            activation._validate_private(after)
            private_files.validate_directory(path.parent, parent)
            activation._validate_private(os.fstat(parent), directory=True)
            if (len(raw) != before.st_size or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns):
                raise RuntimeError("launch metadata changed")
    finally:
        os.close(parent)
    value = json.loads(raw, object_pairs_hook=activation._unique_object)
    if not isinstance(value, dict):
        raise ValueError("launch metadata must be an object")
    return value


def _validate_target(profile: dict, provider: str) -> Path:
    """원본의 절대 경로와 승인된 바이트를 확인하고 자체 실행을 거부한다."""
    if (set(profile) != {"schema_version", "provider", "executable", "sha256"}
            or type(profile["schema_version"]) is not int or profile["schema_version"] != 1
            or profile["provider"] != provider
            or not isinstance(profile["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", profile["sha256"])):
        raise ValueError("invalid native profile")
    target = activation._absolute(profile["executable"])
    wrapper = Path(__file__).resolve().parents[2] / "bin" / "collected-native-agent"
    parent = namespace.open_directory(target.parent)
    try:
        fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        try:
            before = namespace.validate_fd(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid not in {0, os.getuid()}
                    or before.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
                    or not before.st_mode & 0o111
                    or (before.st_dev, before.st_ino) == (wrapper.stat().st_dev, wrapper.stat().st_ino)):
                raise ValueError("unsafe native executable")
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(fd)
            named = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
            private_files.validate_directory(target.parent, parent)
            if (identity(before) != identity(after) or identity(after) != identity(named)
                    or digest.hexdigest() != profile["sha256"] or not os.access(target, os.X_OK)):
                raise ValueError("native executable changed")
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    return target


def main(argv: list[str] | None = None) -> int:
    """현재 실행 프로세스를 교체하며 부모 환경과 umask는 변경하지 않는다."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if any(key in os.environ for key in (
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK",
                "UNIFIED_KANBAN_NATIVE_LAUNCH_ACTIVE")):
            raise ValueError("guarded native launch")
        if not args or args[0] not in {"claude", "codex"}:
            raise ValueError("provider required")
        provider = args.pop(0)
        home = activation._absolute(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        selector_path = home / "unified-kanban-conversation" / "activation.json"
        selector = _read_private(selector_path)
        selected = activation.resolve_activation(home)
        if selected is None:
            raise ValueError("explicit activation required")
        if (selector.get("enabled") is not True
                or selector.get("runtime_config") != str(selected)
                or _read_private(selector_path) != selector):
            raise ValueError("activation changed")
        profile = _read_private(activation._absolute(selector["launcher_profiles"][provider]))
        target = _validate_target(profile, provider)
        env = dict(os.environ)
        # 검증한 선택자를 재사용하여 세 번째 해석에서 다른 런타임을 섞지 않는다.
        env[activation.CONFIG_ENV] = str(
            activation._absolute(env[activation.CONFIG_ENV])
            if activation.CONFIG_ENV in env else selected)
        if _read_private(selector_path) != selector:
            raise ValueError("activation changed")
        env["UNIFIED_KANBAN_NATIVE_LAUNCH_ACTIVE"] = "1"
        os.umask(0o077)
        os.execve(target, [str(target), *args], env)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        print("Native launch refused: invalid activation or executable.", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
