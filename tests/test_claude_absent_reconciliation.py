"""부재 final 발행 뒤 복구는 최초 pin/저장 binding/CAS를 보존한다."""
import json
from dataclasses import replace

import pytest
from kanban_adapter import claude_pending_final as queue, conversation as contract
from test_claude_absent_pending_final import _job


def crashed(tmp_path, monkeypatch):
    source, service, kwargs, job, final = _job(tmp_path)
    with source.open('ab') as stream:
        stream.write(final)
    save = queue._save
    def failed(service, path, body, directory, expected=None):
        if body['status'] == 'ready':
            raise OSError('발행 직후 저장 실패')
        return save(service, path, body, directory, expected)
    with monkeypatch.context() as patcher:
        patcher.setattr(queue, '_save', failed)
        with pytest.raises(OSError):
            queue.run_once(service, job)
    return source, service, kwargs, job


@pytest.mark.parametrize('field', ['session', 'schema_pin', 'policy_version', 'producer_execution',
    'created_at_ns', 'generation', 'binding_version', 'turn_start', 'turn_end',
    'source_range_digest', 'locator_ref', 'auth_tag', 'source_identity', 'profile_root_identity'])
def test_authenticated_foreign_binding_is_not_reconciled(tmp_path, monkeypatch, field):
    _, service, _, job = crashed(tmp_path, monkeypatch)
    binding, locator = service.bindings.get('demo', 't_native')
    value = getattr(binding, field)
    if field in {'turn_start', 'turn_end'}:
        value = replace(value, ordinal=value.ordinal + 1)
    elif field in {'source_identity', 'profile_root_identity'}:
        value = replace(value, inode=value.inode + 1)
    elif isinstance(value, int):
        value += 1
    else:
        value += '-other'
    changed = replace(binding, **{field: value})
    if field != 'auth_tag':
        changed = replace(changed, auth_tag=contract._mac(
            'bd', service.authority._secret, contract._canonical_json(changed.scope_dict())))
    path = service.bindings._path('demo', 't_native')
    path.rename(path.with_suffix('.old'))
    service.bindings.put(binding=changed, locator=locator)
    before = path.read_bytes(), path.stat().st_ino
    assert queue.run_once(service, job) == 'rejected'
    assert (path.read_bytes(), path.stat().st_ino) == before


@pytest.mark.parametrize('attack', ['inode', 'prefix', 'append', 'pin', 'policy', 'membership'])
def test_recovery_revalidates_pin_source_and_authority(tmp_path, monkeypatch, attack):
    source, service, _, job = crashed(tmp_path, monkeypatch)
    path = service.bindings._path('demo', 't_native')
    before = path.read_bytes(), path.stat().st_ino
    if attack == 'inode':
        raw = source.read_bytes()
        source.rename(source.with_suffix('.old'))
        source.write_bytes(raw)
        source.chmod(0o600)
    elif attack == 'prefix':
        source.write_bytes(source.read_bytes().replace(b'public new turn', b'public OLD turn'))
    elif attack == 'append':
        # Claude는 정확한 snapshot identity만 복구하며 append로 범위를 확장하지 않는다.
        with source.open('ab') as stream:
            stream.write(b'{"type":"progress"}\n')
    elif attack == 'pin':
        body = json.loads(job.read_bytes())
        body['first_open']['mac'] = '0' * 64
        body['mac'] = queue._mac(service, {k: v for k, v in body.items() if k != 'mac'})
        job.write_text(json.dumps(body))
    elif attack == 'policy':
        service.policies.replace(contract.CollectionPolicyV1.disabled(version=2, generation=3), expected_generation=2)
    else:
        monkeypatch.setattr(service, 'task_membership', lambda *a: False)
    assert queue.run_once(service, job) == 'rejected'
    assert (path.read_bytes(), path.stat().st_ino) == before


def test_reconciliation_cas_never_overwrites_successor(tmp_path, monkeypatch):
    _, service, _, job = crashed(tmp_path, monkeypatch)
    before = service.bindings.get('demo', 't_native')
    save = queue._save
    foreign = b'{"foreign":true}'
    def replaced(service, path, body, directory, expected=None):
        if body['status'] == 'ready':
            path.rename(path.with_suffix('.old'))
            path.write_bytes(foreign)
            path.chmod(0o600)
        return save(service, path, body, directory, expected)
    monkeypatch.setattr(queue, '_save', replaced)
    with pytest.raises((OSError, RuntimeError)):
        queue.run_once(service, job)
    assert job.read_bytes() == foreign
    assert service.bindings.get('demo', 't_native') == before