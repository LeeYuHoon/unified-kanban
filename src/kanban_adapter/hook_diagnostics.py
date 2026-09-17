"""명시적 동의와 만료 시간이 있는 고정 스키마 Claude 디스패치 증거.

본문, 경로, 환경 값, 명령, 예외 문자열은 저장하지 않는다.
소유자가 비공개 디렉터리와 설정을 준비해야 하며 여기서는 생성하지 않는다.
호환성 검사와 런타임 import 전에 시스템 Python 3.9에서도 동작한다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import time
import uuid

from .private_files import create_private_text, open_directory, validate_directory

EVENTS = {"prompt", "stop", "post-tool-use", "subagent-start", "session-end"}
STAGES = {"bootstrap", "bootstrap-failed", "entry", "compatibility-failed",
          "compatibility-rejected", "runtime-import", "runtime-import-failed",
          "session-matched", "session-mismatch", "input-failed", "handler-enter",
          "handler-returned", "handler-failed", "create-enter", "create-returned",
          "create-failed", "collection-failed"}


def diagnostic_directory() -> Path:
    return Path.home() / ".cache/kanban-adapter/claude-diagnostics"


def _private(info: os.stat_result) -> bool:
    return info.st_uid == os.geteuid() and not (stat.S_IMODE(info.st_mode) & 0o077)


def _ancestor(target: int) -> bool:
    pid = os.getppid()
    for _ in range(12):
        if pid == target:
            return True
        if pid <= 1:
            return False
        result = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=1, check=False)
        if result.returncode or not result.stdout.strip().isdigit():
            return False
        pid = int(result.stdout.strip())
    return False


def category(error: BaseException | None) -> str:
    # 고정 범주만 사용하며 사용자 정의 클래스와 traceback 이름은 기록하지 않는다.
    for cls, label in ((ImportError, "import"), (PermissionError, "permission"),
                       (FileNotFoundError, "missing-file"), (TimeoutError, "timeout"),
                       (ValueError, "invalid-value"), (OSError, "os-error"),
                       (RuntimeError, "runtime")):
        if isinstance(error, cls):
            return label
    return "none" if error is None else "other"


def failure_site(error: BaseException | None) -> str:
    sites = {("claude_hook.py", "_ensure_cache"): "private-cache",
             ("claude_hook.py", "_open_session_lock"): "session-lock",
             ("backend.py", "resolve_board"): "board-routing",
             ("claude_hook.py", "run_adapter"): "adapter",
             ("claude_hook.py", "_write_state"): "state-publication"}
    selected = "none" if error is None else "other"
    tb = error.__traceback__ if error is not None else None
    while tb is not None:
        code = tb.tb_frame.f_code
        selected = sites.get((os.path.basename(code.co_filename), code.co_name), selected)
        tb = tb.tb_next
    return selected


def _config_stamp(info: os.stat_result) -> tuple:
    """설정 교체·내용 변경·권한 변경을 재검사할 메타데이터를 반환한다."""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _close(fd: int) -> None:
    """정리 실패로 정상 훅 실행이나 기존 예외를 덮어쓰지 않는다."""
    try:
        os.close(fd)
    except OSError:
        pass


class Recorder:
    def __init__(self, directory: Path, fd: int, session_id: str, expires_at: int, config_stamp: tuple):
        self.directory, self.fd = directory, fd
        self.session_id, self.expires_at = session_id, expires_at
        self.config_stamp = config_stamp
        self.matched = False

    @classmethod
    def open(cls, directory: Path | None = None):
        fd = config_fd = -1
        try:
            directory = directory if directory is not None else diagnostic_directory()
            fd = open_directory(directory)
            if not _private(os.fstat(fd)):
                return None
            config_fd = os.open("config.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=fd)
            info = os.fstat(config_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not _private(info) or info.st_size > 1024:
                return None
            config = json.loads(os.read(config_fd, 1025))
            if _config_stamp(os.fstat(config_fd)) != _config_stamp(info):
                return None
            if set(config) != {"session_id", "ancestor_pid", "expires_at"}:
                return None
            sid, pid, expiry = config["session_id"], config["ancestor_pid"], config["expires_at"]
            if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid:
                return None
            if type(pid) is not int or pid <= 1 or type(expiry) is not int:
                return None
            if not time.time() < expiry <= time.time() + 3600 or not _ancestor(pid):
                return None
            validate_directory(directory, fd)
            current = os.stat("config.json", dir_fd=fd, follow_symlinks=False)
            if _config_stamp(current) != _config_stamp(info):
                return None
            recorder = cls(directory, fd, sid, expiry, _config_stamp(info))
            fd = -1
            return recorder
        except Exception:
            return None
        finally:
            for opened in (config_fd, fd):
                if opened >= 0:
                    _close(opened)

    def emit(self, event: str, stage: str, error: BaseException | None = None) -> None:
        try:
            if stage not in STAGES or self.fd < 0 or time.time() >= self.expires_at:
                return
            validate_directory(self.directory, self.fd)
            if not _private(os.fstat(self.fd)):
                return
            current = os.stat("config.json", dir_fd=self.fd, follow_symlinks=False)
            if _config_stamp(current) != self.config_stamp:
                return
            record = {"schema": 1, "time_ns": time.time_ns(), "pid": os.getpid(),
                      "event": event if event in EVENTS else "unknown", "stage": stage,
                      "scope": "session" if self.matched else "ancestor",
                      "category": category(error), "site": failure_site(error)}
            # 임의 배타적 파일명과 고정 디렉터리 FD, 소유자 전용 권한을 사용한다.
            # 기존 경로에는 추가하지 않아 symlink/hardlink/FIFO로 대상을 지정할 수 없다.
            _, receipt = create_private_text(self.directory, json.dumps(record) + "\n",
                                              label="event", directory_fd=self.fd)
            receipt.close()
        except Exception:
            pass

    def close(self) -> None:
        if self.fd >= 0:
            fd = self.fd
            self.fd = -1
            _close(fd)


def bootstrap() -> None:
    """Claude 래퍼에서 release 런타임 선택기를 가져오기 전에 호출한다."""
    import runpy
    import sys
    recorder = Recorder.open()
    event = sys.argv[2] if len(sys.argv) > 2 else "unknown"
    if recorder:
        recorder.emit(event, "bootstrap")
    try:
        runpy.run_module("kanban_adapter.wrapper_runtime", run_name="__main__")
    except Exception as error:
        if recorder:
            recorder.emit(event, "bootstrap-failed", error)
        raise
    finally:
        if recorder:
            recorder.close()
