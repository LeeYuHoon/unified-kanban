#!/usr/bin/env python3
"""기존 명시적 활성화에만 연결하는 단일 링크 설치기 후보."""
import argparse
import fcntl
import importlib.util
import json
import os
import secrets
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).absolute().parents[1]
NAME = 'collected-native-agent'
RECEIPT = '.collected-native-agent.receipt.json'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def dependencies():
    # 최초 인터프리터와 배포한 설치기/namespace 파일은 호출자가 신뢰해야 한다.
    if sys.version_info < (3, 10) or sys.platform != 'darwin':
        raise RuntimeError('requires trusted Python >=3.10 on macOS')
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError('invoke trusted Python with -I -S -B')
    ns = load('installer_namespace', ROOT / 'src/kanban_adapter/conversation_namespace.py')
    ns.trusted_file(Path(sys.executable), links=True)
    ns.trusted_file(ROOT / '.venv/bin/python', links=True)
    ns.trusted_file(ROOT / 'bin' / NAME)
    for directory, dirs, files in os.walk(ROOT / 'src/kanban_adapter', followlinks=False):
        for item in [Path(directory), *(Path(directory) / d for d in dirs)]:
            os.close(ns.open_directory(item))
        for item in files:
            ns.trusted_file(Path(directory) / item)
    for name in ('install-collected-launcher.py', 'manage_repo_link.py', 'path-transaction.py'):
        ns.trusted_file(ROOT / 'scripts' / name)
    sys.path.insert(0, str(ROOT / 'src'))
    from kanban_adapter import conversation_launch as launch
    return ns, launch, load('installer_links', ROOT / 'scripts/manage_repo_link.py'), load('installer_transaction', ROOT / 'scripts/path-transaction.py')


def validate_activation(launch, home, provider):
    selected = launch.activation.resolve_activation(home)
    if selected is None:
        raise ValueError('explicit activation required')
    path = home / 'unified-kanban-conversation/activation.json'
    selector = launch._read_private(path)
    if selector['enabled'] is not True or selector['runtime_config'] != str(selected):
        raise ValueError('activation changed')
    profile = launch._read_private(launch.activation._absolute(selector['launcher_profiles'][provider]))
    launch._validate_target(profile, provider)
    if launch._read_private(path) != selector:
        raise ValueError('activation changed')


def _secondary(primary, error):
    # 관찰자와 보상 가드가 같은 예외를 볼 수 있다.
    seen = getattr(primary, '_cleanup_exceptions', [])
    if error is primary or any(error is item for item in seen):
        return
    seen.append(error)
    primary._cleanup_exceptions = seen
    errors = getattr(primary, 'cleanup_errors', [])
    errors.append(type(error).__name__ + ': ' + str(error))
    primary.cleanup_errors = errors
    for nested in getattr(error, '_cleanup_exceptions', []):
        _secondary(primary, nested)


def _write_receipt(fd, links, result):
    """게시 전에 생성자 식별 정보를 추적한다. fsync는 게시가 아니다."""
    temp = '.' + RECEIPT + '.' + secrets.token_hex(16)
    owned = None
    published = False
    try:
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            links._write_all(out, (json.dumps({'entries': [result]}, sort_keys=True) + '\n').encode())
            owned = links._entry(fd, temp)
            os.fsync(out)
        finally:
            os.close(out)
        os.fsync(fd)
        # 이 작업이 부분적으로 실패하더라도 생성자 식별 정보는 이미 확보되어 있다.
        os.link(temp, RECEIPT, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        published = True
        os.fsync(fd)
        if links._entry(fd, RECEIPT) != owned:
            raise RuntimeError('receipt publication identity changed')
        if links._entry(fd, temp) != owned:
            raise RuntimeError('receipt staging identity changed')
        os.unlink(temp, dir_fd=fd)
        os.fsync(fd)
    except BaseException as primary:
        for name in ([RECEIPT] if published else []) + [temp]:
            try:
                if owned is not None and links._entry(fd, name) == owned:
                    links._discard_expected(fd, name, owned, 'collected-native-retired')
            except BaseException as error:
                _secondary(primary, error)
        raise


def _install(fd, source, target, links):
    result = links._install(source, target)
    if not result['changed']:
        return
    try:
        _write_receipt(fd, links, result)
    except BaseException as primary:
        try:
            # 영수증이 기록한 링크를 삭제해 정식 영수증만 남기지 않는다.
            # 알 수 없거나 외부 소유인 영수증 상태는 보존하며 인수하거나 삭제하지 않는다.
            expected = (tuple(result['identity']), source)
            if links._entry(fd, RECEIPT) is None and links._entry(fd, NAME) == expected:
                links._discard_expected(fd, NAME, expected, 'collected-native-retired')
        except BaseException as error:
            _secondary(primary, error)
        raise


class _ObservedOS:
    """호출별 보조 OS 인터페이스이며 프로세스의 os 모듈은 변경하지 않는다.

    여기서 내구성은 디렉터리 항목에만 해당하며 전원 손실 시 링크/영수증 쌍의
    원자성을 뜻하지 않는다. 사라진 이름도 보존해 재등장 가능성을 보고한다.
    """
    def __init__(self, base, fd, prefix, links):
        self.base, self.fd, self.prefix, self.links = base, fd, prefix, links
        self.known = {NAME, RECEIPT}
        self.durable = {}
        self.primary = None
        self.inherited_exception = sys.exc_info()[1]
        self.mutating = False

    def __getattr__(self, name):
        return getattr(self.base, name)

    def observe(self):
        self.known.update(self.base.listdir(self.fd))
        result = {}
        for name in sorted(self.known):
            try:
                entry = self.links._entry(self.fd, name)
                result[name] = None if entry is None else entry[0]
            except OSError:
                result[name] = 'unknown'
        return result

    def fsync(self, fd):
        directory = self.base.fstat(fd)
        pinned = self.base.fstat(self.fd)
        relevant = (directory.st_dev, directory.st_ino) == (pinned.st_dev, pinned.st_ino)
        observed = self.observe() if relevant else None
        # 예외 처리 중 호출된 시스템 호출은 보상 작업이며 새 주 오류가 아니다.
        # 자체 except 처리기에 들어가기 전에 활성 예외를 확보한다.
        active = sys.exc_info()[1]
        if active is self.inherited_exception:
            active = None
        try:
            self.base.fsync(fd)
        except BaseException as error:
            if self.primary is None:
                self.primary = active if active is not None else error
            _secondary(self.primary, error)
            raise
        if relevant:
            self.durable = observed

    def report(self):
        observed = self.observe()
        return [dict(path=str(self.prefix / name),
                     live='unknown' if value == 'unknown' else 'absent' if value is None else 'present',
                     identity=None if value in (None, 'unknown') else list(value),
                     namespace_durability=('confirmed' if name in self.durable and value != 'unknown' and value == self.durable[name]
                                           else 'uncertain' if self.mutating else 'not-established'))
                for name, value in observed.items()]


def operate(args, ns, launch, links, tx):
    prefix = ns.canonical(args.prefix)
    if str(prefix) in {'/', '//'}:
        raise ValueError('prefix must not be root')
    home = ns.canonical(args.profile_home)
    source = str(ROOT / 'bin' / NAME)
    target, receipt = prefix / NAME, prefix / RECEIPT
    fd = ns.open_directory(prefix)
    original_os = os
    observer = _ObservedOS(original_os, fd, prefix, links)
    old_link_os, old_tx_os = links.os, tx.os
    # 각 의존 모듈은 이번 호출에 속한다. 오류가 나더라도 복원한다.
    globals()['os'] = links.os = tx.os = observer
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise PermissionError('prefix must be owned by current user')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any(name.startswith(('.collected-native-recovery-', '.collected-native-retired.', '.unified-kanban-', '.' + RECEIPT + '.')) for name in os.listdir(fd)):
            raise RuntimeError('interrupted operation requires explicit reconciliation')
        validate_activation(launch, home, args.provider)
        current = links._entry(fd, NAME)
        saved = links._entry(fd, RECEIPT)
        entry = None
        receipt_snapshot = None
        if saved is not None:
            rfd = os.open(RECEIPT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = ns.validate_fd(rfd, private=True)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise ValueError('invalid ownership receipt')
            finally:
                os.close(rfd)
            receipt_snapshot = tx._snapshot(receipt)
            payload = tx._read_json_file(receipt, 'launcher receipt', receipt_snapshot)
            if not isinstance(payload, dict) or set(payload) != {'entries'} or len(payload['entries']) != 1:
                raise ValueError('invalid ownership receipt')
            entry = payload['entries'][0]
            if set(entry) != {'path', 'changed', 'identity', 'link_target'} or entry['path'] != str(target) or entry['changed'] is not True or entry['link_target'] != source:
                raise ValueError('receipt is not owned by this source')
            tx._verify_operation_entry(entry, tx._snapshot(target))
        if current is not None and current[1] != source:
            raise ValueError('foreign target refused')
        if args.action == 'uninstall' and current is not None and entry is None:
            raise ValueError('uninstall requires producer receipt')
        if args.dry_run:
            print(json.dumps({'action': args.action, 'target': str(target), 'source': source, 'dry_run': True}))
            return
        links._verify_parent(target, fd)
        ns.validate_fd(fd)
        observer.mutating = True
        if args.action == 'install':
            if current is None:
                _install(fd, source, target, links)
            # 기존 동일 소스 링크를 새로 소유했다고 주장하지 않는다.
        elif entry is not None:
            expected = (tuple(entry['identity']), source)
            backup = '.collected-native-recovery-' + secrets.token_hex(16)
            os.link(NAME, backup, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            try:
                os.fsync(fd)
                if links._entry(fd, backup) != expected:
                    raise RuntimeError('uninstall recovery identity changed')
                links._discard_expected(fd, NAME, expected, 'collected-native-retired')
                tx._discard_operation_receipt(receipt, receipt_snapshot)
                if links._entry(fd, RECEIPT) is not None:
                    raise RuntimeError('receipt cleanup incomplete')
            except BaseException as primary:
                try:
                    # 원래 생성자의 영수증이 남아 있는 동안에만 복원한다.
                    if (tx._equivalent(tx._snapshot(receipt), receipt_snapshot)
                            and links._entry(fd, backup) == expected):
                        if links._entry(fd, NAME) is None:
                            links._publish_exclusive(fd, backup, NAME)
                        elif links._entry(fd, NAME) == expected:
                            links._discard_expected(fd, backup, expected, 'collected-native-recovery-retired')
                except BaseException as error:
                    _secondary(primary, error)
                raise
            links._discard_expected(fd, backup, expected, 'collected-native-recovery-retired')
    except BaseException as error:
        primary = observer.primary or error
        if primary is not error:
            _secondary(primary, error)
        try:
            primary.artifacts = observer.report()
        except BaseException as diagnostic:
            _secondary(primary, diagnostic)
            primary.artifacts = [{'path': str(prefix), 'live': 'unknown', 'namespace_durability': 'uncertain'}]
        primary.partial = observer.mutating
        if primary is error:
            raise
        raise primary from error
    finally:
        globals()['os'] = original_os
        links.os, tx.os = old_link_os, old_tx_os
        active = sys.exc_info()[1]
        try:
            original_os.close(fd)
        except BaseException as cleanup:
            if active is None or active is observer.inherited_exception:
                raise
            _secondary(active, cleanup)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'uninstall'))
    parser.add_argument('--prefix', required=True)
    parser.add_argument('--profile-home', required=True)
    parser.add_argument('--provider', required=True, choices=('claude', 'codex'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if any(key.startswith('UNIFIED_KANBAN_TRANSACTION_') or key == 'UNIFIED_KANBAN_TOKEN_LEDGER_FD' for key in os.environ):
            raise ValueError('inherited transaction authority refused')
        operate(args, *dependencies())
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'refused', 'action': args.action,
            'error': {'type': type(error).__name__, 'message': str(error)},
            'partial': getattr(error, 'partial', False),
            'artifacts': getattr(error, 'artifacts', []),
            'cleanup_errors': getattr(error, 'cleanup_errors', [])}, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
