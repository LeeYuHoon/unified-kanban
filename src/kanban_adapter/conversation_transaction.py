"""정책 부모 capability에 결합한 작업 수명 reader/writer lease.

경합은 기다리지 않고 거부한다. 호출자는 작업 전체를 다시 시도해야 한다.
이 lease는 협력하는 게시자를 직렬화하며 다중 파일의 원자성을 주장하지 않는다.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import fcntl
import os
from pathlib import Path
import stat
import threading

from . import private_files as private

_local = threading.local()


@contextmanager
def policy_lease(path: Path, *, exclusive: bool = False, create: bool = False):
    """같은 부모의 정책은 보수적으로 하나의 잠금 공간을 공유한다."""
    path = Path(path)
    fd = private.open_directory(path.parent, create=create)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("policy lease parent must be owner 0700")
        private.validate_directory(path.parent, fd)
        key = (os.getpid(), info.st_dev, info.st_ino)
        held = getattr(_local, "held", None)
        if held is None:
            held = _local.held = {}
        prior = held.get(key)
        if prior is not None:
            if exclusive and not prior:
                raise PermissionError("policy lease cannot upgrade a reader")
            yield fd
            private.validate_directory(path.parent, fd)
            return
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PermissionError("policy transaction lease is busy; retry whole operation") from error
        held[key] = exclusive
        try:
            private.validate_directory(path.parent, fd)
            yield fd
            private.validate_directory(path.parent, fd)
        finally:
            del held[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def collection_operation(function):
    """직접 provenance와 service 메서드가 동일한 lexical lease를 사용한다."""
    @wraps(function)
    def wrapped(service, *args, **kwargs):
        with service.collection_operation():
            return function(service, *args, **kwargs)
    return wrapped


def grant_existing(args):
    """권한을 발견하거나 갱신하지 않고 명시적 추가만 계획한다."""
    import hashlib
    import json
    import re
    import time
    from .conversation_owner_cli import _absolute
    from .conversation_runtime import _runtime_snapshot, _build
    from .conversation import CollectionPolicyV1

    path = _absolute(args.runtime_config)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.board):
        raise ValueError("invalid board")
    if not args.principal or len(args.principal) > 128 or any(
        not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.@:-]{0,190}", p)
        for p in args.principal
    ):
        raise ValueError("invalid explicit principal")
    if not args.provider or set(args.provider) - {"claude", "codex"}:
        raise ValueError("invalid explicit provider")
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_config_sha256):
        raise ValueError("expected config digest must be lowercase SHA256")
    opening = _runtime_snapshot(path)
    config = json.loads(opening[0])
    policy_path = _absolute(config["policy_file"])
    if policy_path.parent != path.parent or policy_path == path:
        raise ValueError("grant requires distinct runtime/policy in the same private state root")
    with policy_lease(policy_path, exclusive=True):
        if _runtime_snapshot(path) != opening:
            raise PermissionError("runtime changed before grant lease")
        service = _build(path)
        if service is None:
            raise PermissionError("grant requires enabled authenticated runtime")
        current = service.policies.load()
        if (hashlib.sha256(opening[0]).hexdigest() != args.expected_config_sha256
                or current.generation != args.expected_policy_generation):
            raise RuntimeError("grant CAS failed")
        if set(args.provider) - set(config["provider_roots"]):
            raise ValueError("provider must already have an explicit installed root")
        now = time.time_ns()
        if now >= current.expires_at_ns:
            raise ValueError("expired policy requires separate renewal")
        enabled = dict(current.enabled_boards)
        enabled[args.board] = enabled.get(args.board, frozenset()) | frozenset(args.provider)
        replacement = CollectionPolicyV1(
            version=current.version, generation=current.generation + 1,
            activated_at_ns=current.activated_at_ns, expires_at_ns=current.expires_at_ns,
            enabled_boards=enabled, minimum_binding_version=current.minimum_binding_version,
            schema_version=2, pair_activated_at_ns={
                board: {provider: (cutoff if (cutoff := current.activation_for(board, provider)) is not None else now)
                        for provider in providers} for board, providers in enabled.items()},
        )
        grants = config["principal_board_grants"]
        for principal in args.principal:
            grants[principal] = sorted(set(grants.get(principal, [])) | {args.board})
        result = {"status": "dry-run", "board": args.board,
                  "providers": sorted(set(args.provider)), "principals": sorted(set(args.principal)),
                  "expected_generation": current.generation, "new_generation": replacement.generation,
                  "expires_at_ns": replacement.expires_at_ns,
                  "new_pair_cutoff_ns": now, "atomic_two_file_commit": False}
        if not args.dry_run:
            _apply_grant(path, policy_path, opening, service.policies, replacement, config)
            result['status'] = 'committed'
        return result


def _fence_path(policy_path):
    return policy_path.with_name('.' + policy_path.name + '.grant-transaction.json')


def _signed(secret, payload):
    import hashlib
    import hmac
    import json
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    mac = hmac.new(secret, b'grant-existing-v1\0' + raw, hashlib.sha256).hexdigest()
    return json.dumps({'payload': payload, 'mac': mac}, sort_keys=True, separators=(',', ':')).encode() + b'\n'


_MAX_FENCE_BYTES = 540_672  # 크기가 제한된 64-KiB 입력 네 개의 16진수 인코딩과 메타데이터


def _read_fence(path):
    """할당 크기를 제한하고 정확한 디렉터리/파일 접근 권한을 유지한다."""
    parent = private.open_directory(path.parent)
    fd = -1
    receipt = None
    try:
        private.validate_directory(path.parent, parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        receipt = private.Receipt(parent, fd, path.name)
        parent = fd = -1
        info = private._validate_receipt(receipt)
        if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                or not 1 <= info.st_size <= _MAX_FENCE_BYTES):
            raise PermissionError('invalid or oversized grant fence metadata')
        raw = os.pread(receipt.file_fd, _MAX_FENCE_BYTES + 1, 0)
        after = private._validate_receipt(receipt)
        if (len(raw) != info.st_size or info.st_size != after.st_size
                or info.st_mtime_ns != after.st_mtime_ns or info.st_ctime_ns != after.st_ctime_ns):
            raise PermissionError('grant fence changed during read')
        private.validate_directory(path.parent, receipt.directory_fd)
        return raw, receipt
    except BaseException:
        if receipt is not None:
            receipt.close()
        else:
            if fd >= 0:
                os.close(fd)
            if parent >= 0:
                os.close(parent)
        raise


def check_grant_fence(policy_path, secret, *, allow_pending=False):
    """알 수 없거나 형식이 잘못되었거나 대기 중인 권한으로는 수집 읽기 작업을 허용하지 않는다."""
    import json
    import hmac
    path = _fence_path(policy_path)
    try:
        raw, receipt = _read_fence(path)
    except FileNotFoundError:
        return
    with receipt:
        info = private._validate_receipt(receipt)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('invalid grant fence metadata')
        try:
            envelope = json.loads(raw)
            payload = envelope['payload']
            if not isinstance(payload, dict):
                raise ValueError('payload must be an object')
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise PermissionError('malformed grant fence') from error
        import re
        fields = {'schema_version', 'nonce', 'state', 'root_identity', 'policy_path',
                  'runtime_path', 'old_policy', 'new_policy', 'old_runtime', 'new_runtime',
                  'old_policy_identity', 'old_runtime_identity'}
        identities = ('root_identity', 'old_policy_identity', 'old_runtime_identity')
        contents = ('old_policy', 'new_policy', 'old_runtime', 'new_runtime')
        if (set(payload) != fields or type(payload.get('schema_version')) is not int
                or payload.get('schema_version') != 1
                or not isinstance(payload.get('nonce'), str)
                or re.fullmatch('[0-9a-f]{64}', payload['nonce']) is None
                or payload.get('state') not in ('pending', 'committed')
                or any(not isinstance(payload.get(k), list) or len(payload[k]) != 2
                       or any(type(v) is not int or v < 0 for v in payload[k]) for k in identities)
                or any(not isinstance(payload.get(k), str)
                       or not 2 <= len(payload[k]) <= 131_072 or len(payload[k]) % 2
                       or re.fullmatch('[0-9a-f]+', payload[k]) is None for k in contents)
                or not isinstance(payload.get('runtime_path'), str)
                or Path(payload['runtime_path']).parent != policy_path.parent
                or Path(payload['runtime_path']) in (policy_path, path)):
            raise PermissionError('malformed grant fence schema')
        if not hmac.compare_digest(raw, _signed(secret, payload)):
            raise PermissionError('invalid grant fence authentication')
        root = os.fstat(receipt.directory_fd)
        if (payload.get('schema_version') != 1 or payload.get('policy_path') != str(policy_path)
                or payload.get('root_identity') != [root.st_dev, root.st_ino]):
            raise PermissionError('grant fence authority mismatch')
        if payload.get('state') != 'committed' and not allow_pending:
            raise PermissionError('grant transaction pending; authenticated recovery required')
        private.validate_directory(path.parent, receipt.directory_fd)
        private._validate_receipt(receipt)
        if os.pread(receipt.file_fd, len(raw) + 1, 0) != raw:
            raise PermissionError('grant fence changed during authentication')
        return raw, receipt.identity, (root.st_dev, root.st_ino)


def _close_grant_resources(receipts, root_fd=None):
    """활성 작업 예외를 가리지 않으면서 모든 해제를 시도한다."""
    import sys
    primary = sys.exception()
    failure = None
    for resource in [*receipts, *([root_fd] if root_fd is not None else [])]:
        try:
            if isinstance(resource, int):
                os.close(resource)
            else:
                resource.close()
        except BaseException as error:
            if primary is not None:
                primary.add_note(f'grant cleanup secondary: {error}')
            elif failure is None:
                failure = error
            else:
                failure.add_note(f'grant cleanup secondary: {error}')
    if primary is None and failure is not None:
        raise failure


def _report_grant_failure(error, *args):
    try:
        _grant_failure_artifacts(error, *args)
    except BaseException as diagnostic:
        error.add_note(f'grant artifact diagnostic failed: {diagnostic}')


def _apply_grant(path, policy_path, opening, store, replacement, config):
    """의도를 내구성 있게 기록한 뒤 두 CAS 게시를 각각 내구성 있게 수행한다.

    경로명만으로 되돌리거나 삭제하지 않는다. 대기 중 실패는 명시적 복구를 위해
    서명된 이전/새 바이트를 그대로 보존하며 새 inode 인수를 추론하지 않는다.
    """
    import json
    import secrets
    from .conversation_runtime import _runtime_snapshot
    policy_opening = _runtime_snapshot(policy_path)
    receipts = []
    fence_receipt = None
    report_root = None
    published = []
    try:
        raw_policy, policy_receipt = private.read_bytes(policy_path)
        receipts.append(policy_receipt)
        raw_runtime, runtime_receipt = private.read_bytes(path)
        receipts.append(runtime_receipt)
        report_root = os.dup(policy_receipt.directory_fd)
        if (raw_runtime != opening[0] or runtime_receipt.identity != opening[1]
                or raw_policy != policy_opening[0] or policy_receipt.identity != policy_opening[1]):
            raise PermissionError('grant input identity CAS failed')
        if store.load().generation != replacement.generation - 1:
            raise RuntimeError('grant policy generation changed')
        payload = store._payload(replacement)
        new_policy = json.dumps({'payload': payload, 'mac': store._mac(store.secret, payload)},
                                sort_keys=True, separators=(',', ':')).encode() + b'\n'
        new_runtime = json.dumps(config, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        fence = _fence_path(policy_path)
        fence_opening = check_grant_fence(policy_path, store.secret)
        old_fence = None
        if fence_opening is not None:
            old_fence, fence_receipt = _read_fence(fence)
            receipts.append(fence_receipt)
            root = os.fstat(fence_receipt.directory_fd)
            if (old_fence, fence_receipt.identity, (root.st_dev, root.st_ino)) != fence_opening:
                raise PermissionError('grant fence changed after authentication')
        record = dict(schema_version=1, nonce=secrets.token_hex(32), state='pending',
                      root_identity=list(opening[2]), policy_path=str(policy_path), runtime_path=str(path),
                      old_policy=raw_policy.hex(), new_policy=new_policy.hex(),
                      old_runtime=raw_runtime.hex(), new_runtime=new_runtime.hex(),
                      old_policy_identity=list(policy_receipt.identity),
                      old_runtime_identity=list(runtime_receipt.identity))
        pending_bytes = _signed(store.secret, record)
        fence_receipt = private.atomic_publish(fence, pending_bytes, expected_identity=fence_receipt,
                                               expected_content=old_fence)
        receipts.append(fence_receipt)
        if _runtime_snapshot(path) != opening or _runtime_snapshot(policy_path) != policy_opening:
            raise PermissionError('grant inputs changed after durable fence')
        for target, content, expected, old_content in (
                (policy_path, new_policy, policy_receipt, raw_policy),
                (path, new_runtime, runtime_receipt, raw_runtime)):
            receipt = private.atomic_publish(target, content, expected_identity=expected,
                                             expected_content=old_content)
            receipts.append(receipt)
            published.append((target, content, receipt))
        for target, content, receipt in published:
            actual = _runtime_snapshot(target)
            if actual[0] != content or actual[1] != receipt.identity or actual[2] != opening[2]:
                raise PermissionError('grant publication readback mismatch')
        record['state'] = 'committed'
        committed_bytes = _signed(store.secret, record)
        receipt = private.atomic_publish(fence, committed_bytes, expected_identity=fence_receipt,
                                         expected_content=pending_bytes)
        receipts.append(receipt)
        expected_fence = (committed_bytes, receipt.identity, opening[2])
        if check_grant_fence(policy_path, store.secret) != expected_fence:
            raise PermissionError('committed grant fence readback mismatch')
    except BaseException as error:
        _report_grant_failure(error, (policy_path, path, _fence_path(policy_path)),
                                 opening[2], [p for p, _, _ in published], report_root)
        if isinstance(error, private.CommittedPublicationError):
            receipts.append(error.receipt)
        raise
    finally:
        _close_grant_resources(receipts, report_root)


def _reconcile_intent(record, store, path, secret_path):
    """유효한 MAC이 무관한 권한을 갱신하거나 변경할 허가를 뜻하지는 않는다."""
    import json
    import re
    import time
    from .conversation_runtime import _build_locked
    old = store.decode(bytes.fromhex(record['old_policy']))
    new = store.decode(bytes.fromhex(record['new_policy']))
    before = json.loads(bytes.fromhex(record['old_runtime']))
    after = json.loads(bytes.fromhex(record['new_runtime']))
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or before.get('authority_secret_file') != str(secret_path)
            or before.get('policy_file') != str(store.path)
            or {k: v for k, v in before.items() if k != 'principal_board_grants'}
            != {k: v for k, v in after.items() if k != 'principal_board_grants'}):
        raise PermissionError('recovery runtime authority changed')
    if (new.generation != old.generation + 1 or new.schema_version != 2
            or (old.version, old.activated_at_ns, old.expires_at_ns, old.minimum_binding_version)
            != (new.version, new.activated_at_ns, new.expires_at_ns, new.minimum_binding_version)):
        raise PermissionError('recovery policy renewal or authority change prohibited')
    if time.time_ns() >= old.expires_at_ns:
        raise PermissionError('recovery policy expired; renewal is not recovery')
    changed_boards, cutoffs = set(), set()
    for board, providers in old.enabled_boards.items():
        if not providers <= new.enabled_boards.get(board, frozenset()):
            raise PermissionError('recovery removes existing policy scope')
    for board, providers in new.enabled_boards.items():
        for provider in providers:
            previous = old.activation_for(board, provider)
            cutoff = new.activation_for(board, provider)
            if previous is not None:
                if cutoff != previous:
                    raise PermissionError('recovery changes existing pair cutoff')
            else:
                changed_boards.add(board)
                cutoffs.add(cutoff)
                if provider not in before['provider_roots'] or not old.activated_at_ns <= cutoff <= time.time_ns():
                    raise PermissionError('recovery invalid new pair cutoff or provider')
    grants, updated = before['principal_board_grants'], after['principal_board_grants']
    if not isinstance(grants, dict) or not isinstance(updated, dict) or not grants.keys() <= updated.keys():
        raise PermissionError('recovery removes principal grants')
    for principal, boards in updated.items():
        if (not isinstance(principal, str) or re.fullmatch(r'[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.@:-]{0,190}', principal) is None
                or not isinstance(boards, list) or any(not isinstance(b, str) for b in boards)):
            raise PermissionError('recovery malformed principal grants')
        previous = set(grants.get(principal, []))
        if not previous <= set(boards):
            raise PermissionError('recovery removes principal scope')
        additions = set(boards) - previous
        if additions and (boards != sorted(set(boards)) or not additions <= new.enabled_boards.keys()):
            raise PermissionError('recovery invalid principal additions')
        changed_boards.update(additions)
    if len(changed_boards) > 1 or len(cutoffs) > 1:
        raise PermissionError('recovery intent is not a single-board grant')
    for raw in (bytes.fromhex(record['old_runtime']), bytes.fromhex(record['new_runtime'])):
        service = _build_locked(path, raw)
        if service.policies.secret != store.secret:
            raise PermissionError('recovery authority secret changed')


def _grant_failure_artifacts(error, paths, root_identity, returned, root_fd=None):
    """차단 장치를 소유한다는 포괄적 주장 대신 관찰 결과를 보고한다."""
    import hashlib
    import json
    from .conversation_runtime import _runtime_snapshot
    artifacts = []
    for target in paths:
        item: dict[str, object] = {'path': str(target), 'visible': 'unknown',
                'durability': 'publication-returned' if target in returned else 'not-established-by-this-invocation'}
        try:
            if target.name.endswith('.grant-transaction.json'):
                raw, receipt = _read_fence(target)
                with receipt:
                    root = os.fstat(receipt.directory_fd)
                    identity, parent = receipt.identity, (root.st_dev, root.st_ino)
            else:
                raw, identity, parent = _runtime_snapshot(target)
            item.update(visible='present', identity=list(identity), sha256=hashlib.sha256(raw).hexdigest(),
                        opening_root_matches=parent == root_identity)
        except FileNotFoundError:
            item['visible'] = 'absent'
        except BaseException as diagnostic:
            item['probe_error'] = str(diagnostic)
        artifacts.append(item)
    if isinstance(error, private.CommittedPublicationError):
        artifacts.append({'receipt_name': error.receipt.name, 'identity': list(error.receipt.identity),
                          'visible': 'installed-before-error; canonical authority must be revalidated',
                          'durability': 'indeterminate; pre-publication and installed names may survive crash'})
    if root_fd is not None:
        try:
            parent = Path(os.fsdecode(fcntl.fcntl(root_fd, 50, bytes(1024)).split(b'\0', 1)[0]))
            for target in paths:
                item = {'capability_path': str(parent / target.name), 'visible': 'unknown',
                        'root_identity': list(root_identity), 'durability': 'not-inferred-from-visibility'}
                try:
                    entry = os.stat(target.name, dir_fd=root_fd, follow_symlinks=False)
                    item.update(visible='present', identity=[entry.st_dev, entry.st_ino])
                except FileNotFoundError:
                    item['visible'] = 'absent'
                except BaseException as diagnostic:
                    item['probe_error'] = str(diagnostic)
                artifacts.append(item)
        except BaseException as diagnostic:
            artifacts.append({'root_identity': list(root_identity), 'capability_path': 'unknown',
                              'probe_error': str(diagnostic)})
    error.add_note('grant artifacts: ' + json.dumps(artifacts, sort_keys=True))
    error.add_note('No rollback attempted; no unverified canonical artifact is claimed owned.')


def recover_grant(args):
    """인증된 이전/이전 식별 정보에서만 명시적 v1 복구를 수행한다.

    V1 의도에는 새 inode 영수증이 없다. 따라서 바이트가 동일한 새 말단 항목도
    충돌 후 안전하게 인수할 수 없으므로 그대로 두고 차단 사유를 남긴다.
    """
    import json
    from .conversation_owner_cli import _absolute
    from .conversation_runtime import _runtime_snapshot
    from .conversation_integration import OwnerPolicyFile
    path, policy, secret_path = map(_absolute, (args.runtime_config, args.policy_file, args.secret_file))
    if path.parent != policy.parent or path in (policy, _fence_path(policy)):
        raise PermissionError('recovery requires distinct runtime/policy in the same root')
    with policy_lease(policy, exclusive=True) as root_fd:
        root = os.fstat(root_fd)
        root_identity = (root.st_dev, root.st_ino)
        secret_opening = _runtime_snapshot(secret_path)
        secret = secret_opening[0]
        if not 32 <= len(secret) <= 4096:
            raise PermissionError('recovery secret length invalid')
        fence = _fence_path(policy)
        receipts, returned = [], []
        try:
            authenticated = check_grant_fence(policy, secret, allow_pending=True)
            if authenticated is None:
                raise PermissionError('recovery requires authenticated pending intent; fence absent')
            raw, fence_receipt = _read_fence(fence)
            receipts.append(fence_receipt)
            if (raw, fence_receipt.identity, root_identity) != authenticated:
                raise PermissionError('recovery fence changed after authentication')
            record = json.loads(raw)['payload']
            if record['state'] != 'pending' or record['runtime_path'] != str(path):
                raise PermissionError('recovery requires matching pending intent')
            store = OwnerPolicyFile(policy, secret=secret)
            _reconcile_intent(record, store, path, secret_path)
            inputs = []
            for target, kind in ((policy, 'policy'), (path, 'runtime')):
                snapshot = _runtime_snapshot(target)
                old, new = bytes.fromhex(record['old_' + kind]), bytes.fromhex(record['new_' + kind])
                if snapshot != (old, tuple(record['old_' + kind + '_identity']), root_identity):
                    if snapshot[0] == new:
                        raise PermissionError(f'recovery blocker: {kind} has new bytes but v1 intent has no authenticated new inode; no adoption')
                    raise PermissionError(f'recovery blocker: {kind} old bytes/inode/root mismatch; foreign or unknown state')
                content, receipt = private.read_bytes(target)
                receipts.append(receipt)
                if content != old or receipt.identity != snapshot[1]:
                    raise PermissionError(f'recovery {kind} changed during receipt acquisition')
                inputs.append((target, old, new, receipt, snapshot))
            # 모든 입력과 의미 검증을 통과하기 전에는 아무것도 변경하지 않는다.
            if _runtime_snapshot(secret_path) != secret_opening:
                raise PermissionError('recovery secret changed before publication')
            private.validate_directory(policy.parent, root_fd)
            private._validate_receipt(fence_receipt)
            if os.pread(fence_receipt.file_fd, len(raw) + 1, 0) != raw:
                raise PermissionError('recovery intent changed before publication')
            os.fsync(fence_receipt.file_fd)
            os.fsync(root_fd)
            private.validate_directory(policy.parent, root_fd)
            publications = []
            for target, old, new, receipt, snapshot in inputs:
                if _runtime_snapshot(target) != snapshot:
                    raise PermissionError('recovery input changed before publication')
                result = private.atomic_publish(target, new, expected_identity=receipt, expected_content=old)
                receipts.append(result)
                returned.append(target)
                publications.append((target, new, result.identity))
                private.validate_directory(policy.parent, root_fd)
            for target, new, identity in publications:
                if _runtime_snapshot(target) != (new, identity, root_identity):
                    raise PermissionError('recovery publication readback mismatch')
            record['state'] = 'committed'
            committed = _signed(secret, record)
            result = private.atomic_publish(fence, committed, expected_identity=fence_receipt, expected_content=raw)
            receipts.append(result)
            returned.append(fence)
            if check_grant_fence(policy, secret) != (committed, result.identity, root_identity):
                raise PermissionError('recovery committed fence readback mismatch')
            return {'status': 'committed', 'atomic_two_file_commit': False, 'recovered_nonce': record['nonce'],
                    'durably_returned_publications': [str(p) for p in returned]}
        except BaseException as error:
            _report_grant_failure(error, (policy, path, fence), root_identity, returned, root_fd)
            if isinstance(error, private.CommittedPublicationError):
                receipts.append(error.receipt)
            raise
        finally:
            _close_grant_resources(receipts)


def policy_writer(function):
    @wraps(function)
    def wrapped(store, *args, **kwargs):
        with policy_lease(store.path, exclusive=True, create=True):
            check_grant_fence(store.path, store.secret)
            return function(store, *args, **kwargs)
    return wrapped
