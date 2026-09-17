"""테스트 데이터만 사용하는 명시적 복구: 권한을 탐색하거나 외부 자원을 인수하지 않는다."""
import argparse
import json

import pytest
from test_conversation_grant_existing_blockers import existing, setup, run_cli
from test_conversation_grant_existing_durable import arguments
from kanban_adapter import conversation_transaction as tx
from kanban_adapter import conversation_runtime as runtime


def pending(state, monkeypatch, stop='policy.json'):
    args = arguments(state)
    original = tx.private.atomic_publish
    def publish(path, content, **kw):
        if path.name == stop:
            raise OSError('crash boundary')
        return original(path, content, **kw)
    with monkeypatch.context() as m:
        m.setattr(tx.private, 'atomic_publish', publish)
        with pytest.raises(OSError, match='crash boundary'):
            tx.grant_existing(args)
    return argparse.Namespace(runtime_config=str(state / 'runtime.json'),
        policy_file=str(state / 'policy.json'), secret_file=str(state / 'authority.key'))


def snapshot(state):
    return {p.name: (p.read_bytes(), p.stat().st_ino) for p in state.iterdir() if p.is_file()}


def test_recover_authenticated_old_old_via_cli(existing, monkeypatch):
    args = pending(existing, monkeypatch)
    fence = tx._fence_path(existing / 'policy.json')
    intent = json.loads(fence.read_bytes())['payload']
    result = run_cli(['recover-grant', '--runtime-config', args.runtime_config,
        '--policy-file', args.policy_file, '--secret-file', args.secret_file])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'committed'
    assert (existing / 'policy.json').read_bytes() == bytes.fromhex(intent['new_policy'])
    assert (existing / 'runtime.json').read_bytes() == bytes.fromhex(intent['new_runtime'])
    assert runtime._build(existing / 'runtime.json') is not None


def test_apply_failure_reports_observed_artifacts(existing, monkeypatch):
    args = arguments(existing)
    original = tx.private.atomic_publish
    def publish(path, content, **kw):
        if path.name == 'runtime.json':
            raise OSError('runtime boundary')
        return original(path, content, **kw)
    monkeypatch.setattr(tx.private, 'atomic_publish', publish)
    with pytest.raises(OSError) as raised:
        tx.grant_existing(args)
    notes = raised.value.__notes__
    assert any(note.startswith('grant artifacts: ') for note in notes)
    assert not any('retained authenticated fence' in note for note in notes)


@pytest.mark.parametrize('kind', ['partial-policy', 'new-runtime', 'foreign-old', 'same-inode', 'missing', 'bad-mac'])
def test_unprovable_state_denied_without_mutation(existing, monkeypatch, kind):
    args = pending(existing, monkeypatch, 'runtime.json' if kind == 'partial-policy' else 'policy.json')
    fence = tx._fence_path(existing / 'policy.json')
    record = json.loads(fence.read_bytes())['payload']
    target = existing / 'runtime.json'
    if kind == 'new-runtime':
        target.write_bytes(bytes.fromhex(record['new_runtime']))
    elif kind == 'foreign-old':
        other = existing / 'other'
        other.write_bytes(target.read_bytes())
        other.chmod(0o600)
        other.replace(target)
    elif kind == 'same-inode':
        target.write_bytes(target.read_bytes() + b' ')
    elif kind == 'missing':
        target.unlink()
    elif kind == 'bad-mac':
        record['nonce'] = 'f' * 64
        fence.write_bytes(tx._signed(b'wrong-key', record))
    before = snapshot(existing)
    with pytest.raises((PermissionError, FileNotFoundError)) as raised:
        tx.recover_grant(args)
    if kind in ('partial-policy', 'new-runtime'):
        assert 'no authenticated new inode' in str(raised.value)
    assert snapshot(existing) == before


@pytest.mark.parametrize('change', ['expiry', 'cutoff', 'scope', 'roots', 'kernel', 'generation'])
def test_signed_semantic_change_is_not_recovery_authority(existing, monkeypatch, change):
    args = pending(existing, monkeypatch)
    fence = tx._fence_path(existing / 'policy.json')
    record = json.loads(fence.read_bytes())['payload']
    secret = (existing / 'authority.key').read_bytes()
    if change in ('roots', 'kernel'):
        data = json.loads(bytes.fromhex(record['new_runtime']))
        if change == 'roots':
            data['provider_roots']['claude'] = '/foreign'
        else:
            data['kernel_secret_file'] = '/foreign'
        record['new_runtime'] = json.dumps(data).encode().hex()
    else:
        envelope = json.loads(bytes.fromhex(record['new_policy']))
        data = envelope['payload']
        if change == 'expiry':
            data['expires_at_ns'] += 1
        elif change == 'cutoff':
            data['pair_activated_at_ns']['test-board']['claude'] += 1
        elif change == 'scope':
            del data['enabled_boards']['test-board']
            del data['pair_activated_at_ns']['test-board']
        else:
            data['generation'] += 1
        from kanban_adapter.conversation_integration import OwnerPolicyFile
        envelope['mac'] = OwnerPolicyFile._mac(secret, data)
        record['new_policy'] = json.dumps(envelope).encode().hex()
    fence.write_bytes(tx._signed(secret, record))
    before = snapshot(existing)
    with pytest.raises(PermissionError):
        tx.recover_grant(args)
    assert snapshot(existing) == before


@pytest.mark.parametrize('boundary', ['policy', 'runtime', 'completion', 'readback'])
def test_recovery_crash_boundaries_remain_fenced_or_exact(existing, monkeypatch, boundary):
    args = pending(existing, monkeypatch)
    fence = tx._fence_path(existing / 'policy.json')
    original = tx.private.atomic_publish
    secret = (existing / 'authority.key').read_bytes()
    successor = None
    def publish(path, content, **kw):
        nonlocal successor
        if boundary == 'policy' and path.name == 'policy.json':
            raise OSError('injected boundary')
        if boundary == 'runtime' and path.name == 'runtime.json':
            raise OSError('injected boundary')
        if path == fence and boundary in ('completion', 'readback'):
            result = None
            if boundary == 'readback':
                result = original(path, content, **kw)
            data = json.loads(fence.read_bytes())['payload']
            data['nonce'] = 'c' * 64
            successor = tx._signed(secret, data)
            fence.write_bytes(successor)
            if boundary == 'readback':
                return result
        return original(path, content, **kw)
    with monkeypatch.context() as m:
        m.setattr(tx.private, 'atomic_publish', publish)
        with pytest.raises((PermissionError, RuntimeError, OSError)) as raised:
            tx.recover_grant(args)
    assert any(n.startswith('grant artifacts: ') for n in raised.value.__notes__)
    if successor is not None:
        assert fence.read_bytes() == successor
    if boundary == 'policy':
        assert tx.recover_grant(args)['status'] == 'committed'
    elif boundary == 'runtime':
        before = snapshot(existing)
        with pytest.raises(PermissionError, match='no authenticated new inode'):
            tx.recover_grant(args)
        assert snapshot(existing) == before
    if boundary != 'readback' and boundary != 'policy':
        with pytest.raises(PermissionError, match='pending'):
            runtime._build(existing / 'runtime.json')


def test_recovery_uses_common_exclusive_lease(existing, monkeypatch):
    args = pending(existing, monkeypatch)
    before = snapshot(existing)
    with tx.policy_lease(existing / 'policy.json'):
        with pytest.raises(PermissionError, match='upgrade'):
            tx.recover_grant(args)
    assert snapshot(existing) == before


@pytest.mark.parametrize('fail', [False, True])
def test_recovery_root_displacement_during_fence_fsync(existing, monkeypatch, fail):
    import os
    args = pending(existing, monkeypatch)
    before = snapshot(existing)
    root_inode = existing.stat().st_ino
    displaced = existing.with_name('displaced-owner')
    original = os.fsync
    def fsync(fd):
        if os.fstat(fd).st_ino == root_inode and existing.exists():
            existing.rename(displaced)
            existing.mkdir(mode=0o700)
            if fail:
                raise OSError('fsync displacement failure')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    with pytest.raises((PermissionError, RuntimeError, OSError)) as raised:
        tx.recover_grant(args)
    assert any(str(displaced) in note for note in raised.value.__notes__)
    assert snapshot(displaced) == before
    assert not list(existing.iterdir())
