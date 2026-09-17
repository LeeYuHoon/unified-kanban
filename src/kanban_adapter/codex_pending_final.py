"""Codex 네이티브 terminal 전용: 원문 없는 인증 작업과 유한 재시도."""
from __future__ import annotations

from .conversation_transaction import collection_operation

import fcntl
import hashlib
import hmac
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from . import private_files as private
from . import pending_final_archive as archive
import sys
from .codex_file_provenance import seal

TTL_NS = 60_000_000_000
MAX_ATTEMPTS = 12


def _wire(body):
    return json.dumps(body, sort_keys=True, separators=(',', ':')).encode()


def _mac(service, body):
    return hmac.new(service.bindings.secret, b'codex-pending-final-v1\0' + _wire(body), hashlib.sha256).hexdigest()


def _key(kwargs):
    task = hashlib.sha256(_wire([kwargs['board'], kwargs['task']])).hexdigest()
    turn = hashlib.sha256(_wire([kwargs['board'], kwargs['task'], kwargs['turn_id'], kwargs['task_receipt']['nonce']])).hexdigest()
    return task + '.' + turn


@contextmanager
def _locked(path):
    """대기하지 않는 task 잠금. 정식 디렉터리/잠금 inode를 함께 고정한다."""
    from .claude_hook import _ensure_cache
    directory = _ensure_cache(path.parent)
    lock = -1
    lock_name = path.name.split('.')[0] + '.lock'
    try:
        lock = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError('pending lock rejected')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = os.stat(lock_name, dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise PermissionError('pending lock changed')
        private.validate_directory(path.parent, directory)
        yield directory
    finally:
        if lock >= 0:
            os.close(lock)
        os.close(directory)


def _save(service, path, body, directory, expected=None):
    private.validate_directory(path.parent, directory)
    receipt = private.atomic_publish(path, _wire({**body, 'mac': _mac(service, body)}), directory_fd=directory, expected_identity=expected)
    private.validate_directory(path.parent, directory)
    return receipt


def _authenticate(service, path, raw, receipt):
    """기존 작업도 MAC/범위/형식을 검증한 뒤에만 수락한다."""
    info = os.fstat(receipt.file_fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError('pending job rejected')
    try:
        body = json.loads(raw)
        signature = body.pop('mac')
        if not isinstance(signature, str) or not hmac.compare_digest(signature, _mac(service, body)):
            raise ValueError('mac')
        if (body['schema'] != 1 or path.name != _key(body['kwargs']) + '.json'
                or body['status'] not in {'pending', 'ready', 'rejected', 'expired'}
                or type(body['attempts']) is not int or not 0 <= body['attempts'] <= MAX_ATTEMPTS
                or type(body['expires_ns']) is not int):
            raise ValueError('scope')
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise PermissionError('pending authentication rejected') from error
    return body


@collection_operation
def enqueue(service, root, **kwargs):
    """최초 인증 기한은 중복 Stop으로 연장하지 않는다."""
    path = Path(root) / (_key(kwargs) + '.json')
    with _locked(path) as directory:
        try:
            raw, receipt = archive.read(sys.modules[__name__], service, path, directory)
        except FileNotFoundError:
            body = dict(schema=1, kwargs=kwargs, expires_ns=service.clock_ns() + TTL_NS,
                        attempts=0, status='pending')
            receipt = _save(service, path, body, directory)
        else:
            try:
                body = _authenticate(service, path, raw, receipt)
                if body['kwargs'] != kwargs:
                    raise PermissionError('pending request mismatch')
            finally:
                receipt.close()
        receipt.close()
    return path


@collection_operation
def run_once(service, path):
    """매 시도 전에 인증된 예산을 CAS 소비하며 오류 본문을 저장하지 않는다."""
    path = Path(path)
    with _locked(path) as directory:
        raw, receipt = archive.read(sys.modules[__name__], service, path, directory)
        try:
            body = _authenticate(service, path, raw, receipt)
            if body['status'] != 'pending':
                return body['status']
            if service.clock_ns() >= body['expires_ns'] or body['attempts'] >= MAX_ATTEMPTS:
                body['status'] = 'expired'
            else:
                body['attempts'] += 1
                receipt = _save(service, path, body, directory, receipt)
                try:
                    result = seal(service, **body['kwargs'], reconcile_existing=True)
                    body['status'] = result['status']
                except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                    body['status'] = 'rejected'
            receipt = _save(service, path, body, directory, receipt)
            return body['status']
        finally:
            receipt.close()


def launch(path):
    """별도 프로세스는 표준 스트림과 hook의 생존 기간에 의존하지 않는다."""
    import subprocess
    import sys
    source = Path(__file__).resolve().parent.parent
    if source.name != 'src' or not (source.parent / 'bin/kanban-adapter').is_file():
        raise RuntimeError('repository worker source unavailable')
    # -I는 ambient Python import 설정만 무시한다. 실행 권한 표시는 그대로 상속한다.
    entry = ('import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); '
             'runpy.run_module("kanban_adapter.codex_pending_final",run_name="__main__")')
    return subprocess.Popen([sys.executable, '-I', '-c', entry, str(source), str(path)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)


def main():
    """동일 환경을 상속하고 런타임의 실제 서명 권한만 사용한다."""
    import signal
    import sys
    import time
    from .compatibility import check_hermes_compatibility, read_carried_commits
    from .release_layout import normalize_agent_repo, release_directory
    signal.alarm(65)
    try:
        if any(os.environ.get(name) for name in ('HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_KANBAN_TASK')):
            return 1
        selected_runtime = len(sys.argv) == 3 and sys.argv[2] == '--selected-runtime'
        if (len(sys.argv) != 2 and not selected_runtime) or not check_hermes_compatibility()[0]:
            return 1
        if not selected_runtime:
            # selector 문자열이나 ambient import 경로가 아니라 검증된 release 규약을 사용한다.
            repo = normalize_agent_repo(os.environ.get('HERMES_AGENT_REPO') or Path.home() / '.hermes/hermes-agent')
            prefix = release_directory(repo, read_carried_commits()[-1]) / 'venv'
            if not check_hermes_compatibility(agent_repo=repo, runtime_prefix=prefix)[0]:
                return 1
            source = Path(__file__).resolve().parent.parent
            if source.name != 'src' or not (source.parent / 'bin/kanban-adapter').is_file():
                return 1
            executable = str(prefix / 'bin/python')
            entry = ('import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); '
                     'runpy.run_module("kanban_adapter.codex_pending_final",run_name="__main__")')
            # execv는 위임/작업 권한 표시를 포함한 환경을 그대로 보존한다.
            os.execv(executable, [executable, '-I', '-c', entry, str(source), sys.argv[1], '--selected-runtime'])
            return 1
        if not check_hermes_compatibility(runtime_prefix=Path(sys.prefix))[0]:
            return 1
        from .conversation_runtime import get_conversation_service
        service = get_conversation_service()
        if service is None:
            return 1
        deadline = time.monotonic() + TTL_NS / 1_000_000_000
        for attempt in range(MAX_ATTEMPTS + 1):
            try:
                if run_once(service, Path(sys.argv[1])) != 'pending':
                    return 0
            except BlockingIOError:
                # 잠금 경합도 같은 유한 대기 예산을 소비하며 인증 기한은 바꾸지 않는다.
                pass
            remaining = deadline - time.monotonic()
            if attempt == MAX_ATTEMPTS or remaining <= 0:
                return 1
            time.sleep(min(5, remaining))
        return 1
    except Exception:
        return 1
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    raise SystemExit(main())
