"""hook 시작 시 기존 인증 queue를 유한 탐색하며 재시작한다.

호출당 JSON 후보 256개/실행 16개/항목 사이 1초 예산이다. 잠금과 종료
증거는 후보 수에 포함하지 않는다. 인증된 종료/예산 소진 작업은 증거를
보존한 채 활성 이름을 제거하므로 모듈/프로세스 재시작에도 진행한다.
손상·경합 작업은 임의 삭제하지 않는다. 그러한 후보가 예산을 모두 차지하거나
비후보 열거 자체가 시간 예산을 소모하는 규모에서는 진행을 보장하지 않는다.
파일 I/O 한 번의 지연까지 제한하는 실시간 타이머나 idle daemon은 아니다.
"""
from __future__ import annotations

import os
import re
import stat
import time

from . import private_files as private
from . import pending_final_archive as archive


def resume(cache, source):
    """종료 증거는 보존하며 활성 후보를 줄인다. worker가 권한을 다시 확인한다."""
    if source not in {'claude-code', 'codex'} or any(os.environ.get(name) for name in (
            'HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_KANBAN_TASK')):
        return
    from . import claude_pending_final, codex_pending_final
    from .conversation_runtime import get_conversation_service
    queue = codex_pending_final if source == 'codex' else claude_pending_final
    root = cache / ('codex-pending-final' if source == 'codex' else 'pending-final')
    directory = -1
    try:
        # 없는 queue를 만들지 않으며 symlink/공개 디렉터리를 복구 권한으로 삼지 않는다.
        directory = private.open_directory(root)
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            return
        service = get_conversation_service()
        if service is None:
            return
        deadline = time.monotonic() + 1
        launched = 0
        candidates = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                if candidates >= 256 or launched >= 16 or time.monotonic() >= deadline:
                    break
                if not re.fullmatch(r'[0-9a-f]{64}\.[0-9a-f]{64}\.json', entry.name):
                    continue
                candidates += 1
                path = root / entry.name
                try:
                    private.validate_directory(root, directory)
                    with queue._locked(path) as locked:
                        private.validate_directory(root, directory)
                        raw, receipt = private.read_bytes(path, directory_fd=locked)
                        try:
                            body = queue._authenticate(service, path, raw, receipt)
                            if (body['status'] == 'pending' and (body['attempts'] >= queue.MAX_ATTEMPTS
                                    or service.clock_ns() >= body['expires_ns'])):
                                body['status'] = 'expired'
                                receipt = queue._save(service, path, body, locked, receipt)
                            if body['status'] != 'pending':
                                archive.retire(queue, service, path, body, receipt, locked)
                            elif (body['attempts'] < queue.MAX_ATTEMPTS
                                    and service.clock_ns() < body['expires_ns']):
                                queue.launch(path)
                                launched += 1
                        finally:
                            receipt.close()
                except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                    # 한 작업의 손상/경합/실행 실패로 나머지 작업을 잃지 않는다.
                    continue
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return
    finally:
        if directory >= 0:
            os.close(directory)