"""범위를 제한한 v1 게시 결과 추적: 가시성은 소유권이나 내구성을 뜻하지 않는다."""
import json
import os

import pytest
from test_conversation_grant_existing_blockers import existing, setup, run_cli
from test_conversation_grant_existing_durable import arguments
from test_conversation_grant_recovery import pending, snapshot
from kanban_adapter import conversation_transaction as tx


@pytest.mark.parametrize('phase', ['pending', 'policy', 'runtime', 'committed'])
@pytest.mark.parametrize('fail', [False, True])
def test_root_movement_inside_publication_fsync(existing, monkeypatch, phase, fail):
    args = arguments(existing)
    root_inode = existing.stat().st_ino
    displaced = existing.with_name('displaced-accounting')
    original = os.fsync
    moved = []
    fence = tx._fence_path(existing / 'policy.json')
    def sync(fd):
        if os.fstat(fd).st_ino == root_inode and not moved and fence.exists():
            record = json.loads(fence.read_bytes())['payload']
            policy_new = (existing / 'policy.json').read_bytes() == bytes.fromhex(record['new_policy'])
            runtime_new = (existing / 'runtime.json').read_bytes() == bytes.fromhex(record['new_runtime'])
            current = ('committed' if record['state'] == 'committed' else
                       'runtime' if runtime_new else 'policy' if policy_new else 'pending')
            if current == phase:
                existing.rename(displaced)
                existing.mkdir(mode=0o700)
                marker = existing / 'foreign-marker'
                marker.write_bytes(b'foreign namespace survives')
                marker.chmod(0o600)
                moved.append(snapshot(existing))
                if fail:
                    raise OSError('composed fsync failure')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', sync)
    with pytest.raises((OSError, RuntimeError)) as raised:
        tx.grant_existing(args)
    assert len(moved) == 1
    assert snapshot(existing) == moved[0]
    artifacts = json.loads(next(n.removeprefix('grant artifacts: ') for n in raised.value.__notes__ if n.startswith('grant artifacts: ')))
    canonical = [a for a in artifacts if 'path' in a]
    assert len(canonical) == 3
    assert all(a['visible'] == 'absent' for a in canonical)
    retained = [a for a in artifacts if 'capability_path' in a]
    assert len(retained) == 3
    assert all(a['visible'] == 'present' and a['durability'] == 'not-inferred-from-visibility' for a in retained)
    assert {a['capability_path'] for a in retained} == {str(displaced / a['path'].split('/')[-1]) for a in canonical}
    if fail:
        assert any('pre-publication and installed names may survive crash' in a.get('durability', '') for a in artifacts)
    record = json.loads((displaced / fence.name).read_bytes())['payload']
    assert record['state'] == ('committed' if phase == 'committed' else 'pending')
    # 비공개 임시 저장 및 폐기용 이름이 없다고 암묵적으로 가정하지 않는다.
    known = {'runtime.json', 'policy.json', fence.name, 'authority.key', 'bindings'}
    private_names = {p.name for p in displaced.iterdir()} - known
    assert all(name.startswith('.') for name in private_names)


def test_new_new_foreign_inodes_remain_unadopted(existing, monkeypatch):
    args = pending(existing, monkeypatch)
    record = json.loads(tx._fence_path(existing / 'policy.json').read_bytes())['payload']
    for kind in ('policy', 'runtime'):
        foreign = existing / ('foreign-' + kind)
        foreign.write_bytes(bytes.fromhex(record['new_' + kind]))
        foreign.chmod(0o600)
        foreign.replace(existing / (kind + '.json'))
    before = snapshot(existing)
    with pytest.raises(PermissionError, match='no authenticated new inode'):
        tx.recover_grant(args)
    assert snapshot(existing) == before
