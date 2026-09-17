"""발행 직후 중단은 원래 준비 권한과 정확한 불변 범위로만 복구한다."""
import json
import os
from dataclasses import replace

import pytest

from kanban_adapter import claude_pending_final as queue
from kanban_adapter import claude_file_provenance as v2
from kanban_adapter import conversation as contract
from test_claude_file_final_readiness import _prepared
from test_claude_file_provenance_v2 import _final, _page


def _crashed(tmp_path, monkeypatch):
    source, service, kwargs = _prepared(tmp_path, monkeypatch)
    job = queue.enqueue(service, tmp_path / 'jobs', **kwargs)
    with source.open('ab') as stream:
        stream.write(_final())
    save = queue._save

    def crash(service, path, body, directory, expected=None):
        if body['status'] == 'ready':
            raise OSError('postpublication crash')
        return save(service, path, body, directory, expected)

    with monkeypatch.context() as patcher:
        patcher.setattr(queue, '_save', crash)
        with pytest.raises(OSError, match='postpublication crash'):
            queue.run_once(service, job)
    assert json.loads(job.read_bytes())['status'] == 'pending'
    return source, service, kwargs, job


def _stored(service):
    path = service.bindings._path('demo', 't_native')
    return path.read_bytes(), path.stat().st_ino


def test_reconciliation_uses_no_reseal_or_republication(tmp_path, monkeypatch):
    _, service, _, job = _crashed(tmp_path, monkeypatch)
    before = _stored(service)
    budget = json.loads(job.read_bytes())

    def forbidden(*a, **kw):
        pytest.fail('recovery attempted publication or resealing')

    monkeypatch.setattr(service.bindings, 'put', forbidden)
    monkeypatch.setattr(service.authority, 'begin_binding', forbidden)
    monkeypatch.setattr(service.authority, 'seal_binding', forbidden)
    assert queue.run_once(service, job) == 'ready'
    after = json.loads(job.read_bytes())
    assert after['attempts'] == budget['attempts'] + 1
    assert after['expires_ns'] == budget['expires_ns']
    assert _stored(service) == before
    assert [e['text'] for e in _page(service)['events']] == ['same request', 'current final']


@pytest.mark.parametrize('attack', ['prefix', 'final', 'inode', 'ancestor', 'append',
                                    'parent', 'prompt', 'no_final', 'policy', 'membership',
                                    'prepared_mac', 'receipt', 'wrong_turn', 'different_prompt'])
def test_recovery_revalidates_live_source_and_original_authority(tmp_path, monkeypatch, attack):
    source, service, kwargs, job = _crashed(tmp_path, monkeypatch)
    before = _stored(service)
    raw = source.read_bytes()
    if attack in {'prefix', 'final', 'parent', 'prompt', 'no_final'}:
        info = source.stat()
        if attack == 'prefix':
            raw = raw.replace(b'prior private final', b'other private final')
        elif attack == 'final':
            raw = raw.replace(b'current final', b'changed final')
        else:
            lines = raw.splitlines()
            row = json.loads(lines[-1])
            if attack == 'parent':
                row['parentUuid'] = 'old-a'
            elif attack == 'prompt':
                row['promptId'] = '650e8400-e29b-41d4-a716-446655440000'
            else:
                row['message']['stop_reason'] = 'tool_use'
            raw = b'\n'.join(lines[:-1] + [json.dumps(row).encode()]) + b'\n'
        source.write_bytes(raw)
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif attack == 'inode':
        source.rename(source.with_suffix('.old'))
        source.write_bytes(raw)
        source.chmod(0o600)
    elif attack == 'ancestor':
        source.parent.rename(source.parent.with_name(source.parent.name + '-old'))
        source.parent.mkdir(mode=0o700)
        source.write_bytes(raw)
        source.chmod(0o600)
    elif attack == 'append':
        with source.open('ab') as stream:
            stream.write(b'{"type":"progress"}\n')
    elif attack == 'policy':
        service.policies.replace(contract.CollectionPolicyV1.disabled(version=2, generation=3), expected_generation=2)
    elif attack == 'membership':
        monkeypatch.setattr(service, 'task_membership', lambda *a: False)
    else:
        body = json.loads(job.read_bytes())
        if attack == 'prepared_mac':
            body['kwargs']['prepared']['mac'] = '0' * 64
        elif attack == 'receipt':
            body['kwargs']['task_receipt']['mac'] = '0' * 64
        else:
            # 작업 MAC은 유효해도 원래 준비 UUID의 범위를 바꿀 수 없다.
            prepared = body['kwargs']['prepared']
            if attack == 'different_prompt':
                prepared['prompt_id'] = '750e8400-e29b-41d4-a716-446655440000'
                body['kwargs']['prompt_id'] = prepared['prompt_id']
                job = job.rename(job.parent / (queue._key(body['kwargs']) + '.json'))
            else:
                prepared['request_uuid'] = 'old-u'
            prepared['mac'] = v2._mac(service, {k: v for k, v in prepared.items() if k != 'mac'})
        body['mac'] = queue._mac(service, {k: v for k, v in body.items() if k != 'mac'})
        job.write_text(json.dumps(body))
    assert queue.run_once(service, job) == 'rejected'
    assert json.loads(job.read_bytes())['status'] == 'rejected'
    assert _stored(service) == before


@pytest.mark.parametrize('field', ['session', 'schema_pin', 'policy_version', 'producer_execution',
                                   'created_at_ns', 'generation', 'binding_version', 'turn_start',
                                   'turn_end', 'source_range_digest', 'locator_ref', 'auth_tag'])
def test_even_authenticated_different_stored_scope_is_not_reconciled(tmp_path, monkeypatch, field):
    _, service, _, job = _crashed(tmp_path, monkeypatch)
    binding, locator = service.bindings.get('demo', 't_native')
    value = getattr(binding, field)
    if field in {'turn_start', 'turn_end'}:
        value = replace(value, ordinal=value.ordinal + 1)
    elif isinstance(value, int):
        value += 1
    else:
        value += '-other'
    changed = replace(binding, **{field: value})
    if field != 'auth_tag':
        # fixture 서명으로 MAC만이 아닌 전체 재계산 범위 비교를 시험한다.
        changed = replace(changed, auth_tag=contract._mac(
            'bd', service.authority._secret, contract._canonical_json(changed.scope_dict())))
    path = service.bindings._path('demo', 't_native')
    path.rename(path.with_suffix('.original'))
    service.bindings.put(binding=changed, locator=locator)
    before = _stored(service)
    assert queue.run_once(service, job) == 'rejected'
    assert _stored(service) == before


def test_old_partial_plus_later_final_is_never_upgraded(tmp_path, monkeypatch):
    source, service, kwargs = _prepared(tmp_path, monkeypatch)
    queue.seal(service, **kwargs)
    job = queue.enqueue(service, tmp_path / 'jobs', **kwargs)
    with source.open('ab') as stream:
        stream.write(_final())
    before = _stored(service)
    assert queue.run_once(service, job) == 'rejected'
    assert _stored(service) == before
    assert [e['text'] for e in _page(service)['events']] == ['same request']
