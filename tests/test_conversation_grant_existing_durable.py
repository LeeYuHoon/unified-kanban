"""실제 권한을 사용하지 않고 테스트 데이터만으로 권한 부여의 내구성 기준을 검증한다."""
import argparse
import hashlib
import json
import time

import pytest
from test_conversation_grant_existing_blockers import existing, setup, run_cli
from kanban_adapter import conversation_runtime as runtime
from kanban_adapter import conversation_transaction as transaction


def arguments(state):
    path = state / 'runtime.json'
    return argparse.Namespace(runtime_config=str(path), board='project-two',
        provider=['claude'], principal=['github:new-user'], dry_run=False,
        expected_config_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        expected_policy_generation=runtime._build(path).policies.load().generation)


@pytest.mark.parametrize('foreign', [False, True])
def test_partial_runtime_failure_fences_fresh_cached_and_writer(existing, monkeypatch, foreign):
    args = arguments(existing)
    path = existing / 'runtime.json'
    cached = runtime._build(path)
    old_runtime = path.read_bytes()
    original = transaction.private.atomic_publish
    successor = None
    def publish(target, content, **kw):
        nonlocal successor
        if target == path:
            if foreign:
                other = path.with_name('foreign.json')
                other.write_bytes(old_runtime)
                other.chmod(0o600)
                other.replace(path)
                successor = path.stat().st_ino
                return original(target, content, **kw)
            raise OSError('runtime injection')
        return original(target, content, **kw)
    monkeypatch.setattr(transaction.private, 'atomic_publish', publish)
    with pytest.raises((OSError, RuntimeError)):
        transaction.grant_existing(args)
    assert path.read_bytes() == old_runtime
    if foreign:
        assert path.stat().st_ino == successor
    with pytest.raises(PermissionError, match='pending'):
        runtime._build(path)
    standalone = runtime._build_locked(path, old_runtime)
    with pytest.raises(PermissionError, match='pending'):
        with standalone.collection_operation():
            pass
    with pytest.raises(PermissionError):
        with cached.collection_operation():
            pass
    policy = cached.policies.load()
    with pytest.raises(PermissionError, match='pending'):
        cached.policies.replace(policy, expected_generation=policy.generation)


def test_apply_preserves_authority_and_old_cutoff(existing):
    args = arguments(existing)
    path = existing / 'runtime.json'
    before = json.loads(path.read_bytes())
    old = runtime._build(path).policies.load()
    start = time.time_ns()
    result = transaction.grant_existing(args)
    assert result['status'] == 'committed'
    service = runtime._build(path)
    new = service.policies.load()
    assert start <= new.activation_for('project-two', 'claude') <= time.time_ns()
    assert new.activation_for('test-board', 'claude') == old.activation_for('test-board', 'claude')
    assert (new.version, new.expires_at_ns, new.activated_at_ns) == (old.version, old.expires_at_ns, old.activated_at_ns)
    after = json.loads(path.read_bytes())
    assert {k:v for k,v in before.items() if k != 'principal_board_grants'} == {k:v for k,v in after.items() if k != 'principal_board_grants'}
    assert after['principal_board_grants']['github:new-user'] == ['project-two']
    assert after['principal_board_grants']['github:fixture-user'] == before['principal_board_grants']['github:fixture-user']
    assert result['atomic_two_file_commit'] is False
