"""테스트 데이터만으로 권한 부여 트랜잭션 경계를 결정론적으로 검증한다."""
import json
import os

import pytest
from test_conversation_grant_existing_blockers import existing, setup, run_cli
from test_conversation_grant_existing_durable import arguments
from kanban_adapter import conversation_transaction as transaction
from kanban_adapter import conversation_runtime as runtime


@pytest.mark.parametrize('leaf', ['runtime.json', 'policy.json'])
def test_same_inode_mutation_at_publish_preserved(existing, monkeypatch, leaf):
    args = arguments(existing)
    policy_path = runtime._build(existing / 'runtime.json').policies.path
    target = existing / leaf if leaf == 'runtime.json' else policy_path
    original = transaction.private.atomic_publish
    foreign = None
    inode = target.stat().st_ino
    def publish(path, content, **kw):
        nonlocal foreign
        if path == target:
            foreign = path.read_bytes() + b' '
            path.write_bytes(foreign)
        return original(path, content, **kw)
    monkeypatch.setattr(transaction.private, 'atomic_publish', publish)
    with pytest.raises((PermissionError, RuntimeError)):
        transaction.grant_existing(args)
    assert foreign is not None
    assert target.read_bytes() == foreign
    assert target.stat().st_ino == inode


@pytest.mark.parametrize('boundary', ['adoption', 'completion'])
def test_signed_fence_successor_is_not_adopted(existing, monkeypatch, boundary):
    transaction.grant_existing(arguments(existing))
    args = arguments(existing)
    service = runtime._build(existing / 'runtime.json')
    fence = transaction._fence_path(service.policies.path)
    original_check = transaction.check_grant_fence
    original_publish = transaction.private.atomic_publish
    foreign = None
    inode = None
    def replace_fence():
        nonlocal foreign, inode
        record = json.loads(fence.read_bytes())['payload']
        record['nonce'] = 'a' * 64
        foreign = transaction._signed(service.policies.secret, record)
        fence.write_bytes(foreign)
        inode = fence.stat().st_ino
    def check(*a, **kw):
        result = original_check(*a, **kw)
        import inspect
        if boundary == 'adoption' and inspect.currentframe().f_back.f_code.co_name == '_apply_grant':
            replace_fence()
        return result
    def publish(path, content, **kw):
        if boundary == 'completion' and path == fence and json.loads(content)['payload']['state'] == 'committed':
            replace_fence()
        return original_publish(path, content, **kw)
    monkeypatch.setattr(transaction, 'check_grant_fence', check)
    monkeypatch.setattr(transaction.private, 'atomic_publish', publish)
    with pytest.raises((PermissionError, RuntimeError)):
        transaction.grant_existing(args)
    assert foreign is not None
    assert fence.read_bytes() == foreign
    assert fence.stat().st_ino == inode


@pytest.mark.parametrize('raw', [b'null', b'[]', b'{}', b'{', b'{"payload":[]}'])
def test_malformed_fence_is_permission_denial(existing, raw):
    service = runtime._build(existing / 'runtime.json')
    fence = transaction._fence_path(service.policies.path)
    fence.write_bytes(raw)
    fence.chmod(0o600)
    with pytest.raises(PermissionError):
        transaction.check_grant_fence(service.policies.path, service.policies.secret)


def test_oversized_fence_rejected_before_content_read(existing, monkeypatch):
    service = runtime._build(existing / 'runtime.json')
    fence = transaction._fence_path(service.policies.path)
    fence.write_bytes(b' ' * 600_000)
    fence.chmod(0o600)
    original = os.read
    read_count = 0
    def read(fd, count):
        nonlocal read_count
        if os.fstat(fd).st_ino == fence.stat().st_ino:
            read_count += 1
        return original(fd, count)
    original_pread = os.pread
    def pread(fd, count, offset):
        nonlocal read_count
        if os.fstat(fd).st_ino == fence.stat().st_ino:
            read_count += 1
        return original_pread(fd, count, offset)
    monkeypatch.setattr(os, 'read', read)
    monkeypatch.setattr(os, 'pread', pread)
    with pytest.raises(PermissionError):
        transaction.check_grant_fence(service.policies.path, service.policies.secret)
    assert read_count == 0


def test_fence_reader_rejects_root_displacement(existing, monkeypatch):
    transaction.grant_existing(arguments(existing))
    service = runtime._build(existing / 'runtime.json')
    original = transaction._signed
    displaced = existing.with_name('displaced')
    def signed(*args):
        result = original(*args)
        existing.rename(displaced)
        existing.mkdir(mode=0o700)
        return result
    monkeypatch.setattr(transaction, '_signed', signed)
    with pytest.raises((PermissionError, RuntimeError)):
        transaction.check_grant_fence(service.policies.path, service.policies.secret)
    assert (displaced / transaction._fence_path(service.policies.path).name).is_file()


@pytest.mark.parametrize('mutation', ['unknown', 'missing', 'boolean-version', 'bad-hex', 'foreign-runtime'])
def test_authenticated_malformed_record_denied(existing, mutation):
    transaction.grant_existing(arguments(existing))
    service = runtime._build(existing / 'runtime.json')
    fence = transaction._fence_path(service.policies.path)
    record = json.loads(fence.read_bytes())['payload']
    if mutation == 'unknown':
        record['surprise'] = True
    elif mutation == 'missing':
        del record['old_policy']
    elif mutation == 'boolean-version':
        record['schema_version'] = True
    elif mutation == 'bad-hex':
        record['old_runtime'] = 'zz'
    else:
        record['runtime_path'] = '/foreign/runtime.json'
    fence.write_bytes(transaction._signed(service.policies.secret, record))
    with pytest.raises(PermissionError):
        transaction.check_grant_fence(service.policies.path, service.policies.secret)


def test_final_readback_rejects_signed_successor(existing, monkeypatch):
    args = arguments(existing)
    service = runtime._build(existing / 'runtime.json')
    fence = transaction._fence_path(service.policies.path)
    original = transaction.private.atomic_publish
    foreign = None
    def publish(path, content, **kw):
        nonlocal foreign
        result = original(path, content, **kw)
        if path == fence and json.loads(content)['payload']['state'] == 'committed':
            record = json.loads(content)['payload']
            record['nonce'] = 'b' * 64
            foreign = transaction._signed(service.policies.secret, record)
            path.write_bytes(foreign)
        return result
    monkeypatch.setattr(transaction.private, 'atomic_publish', publish)
    with pytest.raises(PermissionError):
        transaction.grant_existing(args)
    assert fence.read_bytes() == foreign
