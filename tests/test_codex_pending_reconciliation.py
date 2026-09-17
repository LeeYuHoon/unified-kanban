"""발행 뒤 상태 저장 중단은 정확한 네이티브 terminal 범위로만 복구한다."""
import json
import os
from dataclasses import replace

import pytest

from kanban_adapter import codex_file_provenance as native, codex_pending_final as queue
from kanban_adapter import conversation as contract
from test_codex_pending_final import prepared_job
from test_codex_authenticated_prepare import append, message
from test_codex_file_provenance import marker


def crashed(tmp_path, monkeypatch):
    source, service, kwargs, job = prepared_job(tmp_path)
    append(source, message('user', 43) + message('assistant', 44) + marker('task_complete', ordinal=45))
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


def stored(service):
    path = service.bindings._path('demo', 'task')
    return path.read_bytes(), path.stat().st_ino


@pytest.mark.parametrize('later_turn', [False, True])
def test_retry_reconciles_exact_terminal_without_republication(tmp_path, monkeypatch, later_turn):
    source, service, _, job = crashed(tmp_path, monkeypatch)
    binding = service.bindings.get('demo', 'task')[0]
    before = stored(service)
    budget = json.loads(job.read_bytes())
    if later_turn:
        append(source, marker('task_started', 'next', 46) + message('user', 47, 'next'))

    def forbidden(*a, **kw):
        pytest.fail('recovery attempted publication or resealing')

    monkeypatch.setattr(service.bindings, 'put', forbidden)
    monkeypatch.setattr(service.authority, 'begin_binding', forbidden)
    monkeypatch.setattr(service.authority, 'seal_binding', forbidden)
    assert queue.run_once(service, job) == 'ready'
    after = json.loads(job.read_bytes())
    assert after['attempts'] == budget['attempts'] + 1
    assert after['expires_ns'] == budget['expires_ns']
    assert stored(service) == before
    assert service.bindings.get('demo', 'task')[0] == binding
    page = service.get_parent_page(principal_id='owner', board='demo', task='task', cursor=None, limit=20)
    assert [(e['kind'], e['text']) for e in page['events']] == [('user_message', 'public'), ('final_assistant', 'public')]

@pytest.mark.parametrize('attack', ['prefix', 'final', 'inode', 'ancestor', 'terminal',
                                    'ordinal', 'no_final', 'policy', 'membership',
                                    'prepared_mac', 'receipt', 'turn', 'session'])
def test_recovery_revalidates_original_authority_and_source(tmp_path, monkeypatch, attack):
    source, service, kwargs, job = crashed(tmp_path, monkeypatch)
    before = stored(service)
    raw = source.read_bytes()
    if attack in {'prefix', 'final', 'terminal', 'ordinal', 'no_final'}:
        info = source.stat()
        substitutions = {
            'prefix': (b'"old"', b'"bad"'),
            'final': (b'"public"', b'"secret"'),
            'terminal': (b'"task_complete", "turn_id": "target"', b'"task_complete", "turn_id": "wrong!"'),
            'ordinal': (b'"ordinal": 44', b'"ordinal": 74'),
            'no_final': (b'"final_answer"', b'"commentary"'),
        }
        old, new = substitutions[attack]
        assert old in raw
        source.write_bytes(raw.replace(old, new))
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        # 크기 증가가 원래 prefix/범위 digest 검증을 우회하면 안 된다.
        append(source, marker('task_started', 'next', 46))
    elif attack == 'inode':
        source.rename(source.with_suffix('.old'))
        source.write_bytes(raw)
        source.chmod(0o600)
    elif attack == 'ancestor':
        moved = source.parent.with_name('moved')
        source.parent.rename(moved)
        source.parent.mkdir(mode=0o700)
        (moved / source.name).rename(source)
    elif attack == 'policy':
        service.policies.replace(contract.CollectionPolicyV1.disabled(version=2, generation=3), expected_generation=2)
    elif attack == 'membership':
        monkeypatch.setattr(service, 'task_membership', lambda *a: False)
    else:
        body = json.loads(job.read_bytes())
        args = body['kwargs']
        if attack == 'prepared_mac':
            args['prepared']['mac'] = '0' * 64
        elif attack == 'receipt':
            args['task_receipt']['auth_tag'] = '0' * 64
        elif attack == 'turn':
            args['turn_id'] = args['prepared']['turn_id'] = 'old'
            args['prepared']['mac'] = native._mac(service, {k: v for k, v in args['prepared'].items() if k != 'mac'})
            job = job.rename(job.parent / (queue._key(args) + '.json'))
        else:
            args['prepared']['session'] = 'foreign'
            args['prepared']['mac'] = native._mac(service, {k: v for k, v in args['prepared'].items() if k != 'mac'})
        body['mac'] = queue._mac(service, {k: v for k, v in body.items() if k != 'mac'})
        job.write_text(json.dumps(body))
    assert queue.run_once(service, job) == 'rejected'
    assert stored(service) == before


def replace_stored(service, binding, locator):
    # 유효한 fixture 서명으로도 잘못된 범위는 복구할 수 없어야 한다.
    binding = replace(binding, auth_tag=contract._mac(
        'bd', service.authority._secret, contract._canonical_json(binding.scope_dict())))
    path = service.bindings._path('demo', 'task')
    path.rename(path.with_suffix('.original'))
    service.bindings.put(binding=binding, locator=locator)


@pytest.mark.parametrize('field', ['session', 'policy_version', 'producer_execution', 'created_at_ns',
                                  'generation', 'binding_version', 'turn_start', 'turn_end',
                                  'source_range_digest', 'locator_ref', 'codex_target_turn_id',
                                  'source_identity', 'profile_root_identity', 'auth_tag'])
def test_authenticated_foreign_binding_scope_is_not_ready(tmp_path, monkeypatch, field):
    _, service, _, job = crashed(tmp_path, monkeypatch)
    binding, locator = service.bindings.get('demo', 'task')
    value = getattr(binding, field)
    if field in {'turn_start', 'turn_end'}:
        value = replace(value, ordinal=value.ordinal + 1)
    elif field in {'source_identity', 'profile_root_identity'}:
        value = replace(value, inode=value.inode + 1)
    elif isinstance(value, int):
        value += 1
    elif field == 'source_range_digest':
        value = '1' * 64
    else:
        value += '-other'
    changed = replace(binding, **{field: value})
    if field == 'auth_tag':
        path = service.bindings._path('demo', 'task')
        path.rename(path.with_suffix('.original'))
        service.bindings.put(binding=changed, locator=locator)
    else:
        replace_stored(service, changed, locator)
    before = stored(service)
    assert queue.run_once(service, job) == 'rejected'
    assert stored(service) == before


def test_old_request_only_range_never_upgraded_by_later_final(tmp_path, monkeypatch):
    import hashlib
    source, service, _, job = crashed(tmp_path, monkeypatch)
    binding, locator = service.bindings.get('demo', 'task')
    end = source.read_bytes().index(message('assistant', 44))
    partial = replace(binding, turn_end=contract.SourceBoundaryV1(end, 44),
                      source_range_digest=hashlib.sha256(source.read_bytes()[binding.turn_start.byte_offset:end]).hexdigest())
    replace_stored(service, partial, locator)
    before = stored(service)
    assert queue.run_once(service, job) == 'rejected'
    assert stored(service) == before
    page = service.get_parent_page(principal_id='owner', board='demo', task='task', cursor=None, limit=20)
    assert [e['kind'] for e in page['events']] == ['user_message']


def test_retry_revalidates_membership_after_projection(tmp_path, monkeypatch):
    from kanban_adapter.transcript_projection import CodexTargetTurnProjector
    _, service, _, job = crashed(tmp_path, monkeypatch)
    project = CodexTargetTurnProjector.project
    before = stored(service)

    def revoke(self, *a, **kw):
        result = project(self, *a, **kw)
        monkeypatch.setattr(service, 'task_membership', lambda *a: False)
        return result

    monkeypatch.setattr(CodexTargetTurnProjector, 'project', revoke)
    assert queue.run_once(service, job) == 'rejected'
    assert stored(service) == before


@pytest.mark.parametrize('attack', ['source', 'binding'])
def test_recovery_rejects_replacement_during_validation(tmp_path, monkeypatch, attack):
    source, service, _, job = crashed(tmp_path, monkeypatch)
    before = stored(service)
    if attack == 'source':
        parse = native.parse_turn

        def replace_source(*a, **kw):
            selected = parse(*a, **kw)
            raw = source.read_bytes()
            source.rename(source.with_suffix('.old'))
            source.write_bytes(raw)
            source.chmod(0o600)
            return selected

        monkeypatch.setattr(native, 'parse_turn', replace_source)
    else:
        from kanban_adapter.transcript_projection import CodexTargetTurnProjector
        project = CodexTargetTurnProjector.project
        original = service.bindings.get('demo', 'task')

        def replace_binding(self, *a, **kw):
            outcome = project(self, *a, **kw)
            monkeypatch.setattr(service.bindings, 'get', lambda *a: (replace(original[0], session='foreign'), original[1]))
            return outcome

        monkeypatch.setattr(CodexTargetTurnProjector, 'project', replace_binding)
    assert queue.run_once(service, job) == 'rejected'
    assert stored(service) == before


def test_authenticated_but_unprojectable_stored_final_is_not_ready(tmp_path, monkeypatch):
    import hashlib
    source, service, _, job = crashed(tmp_path, monkeypatch)
    binding, locator = service.bindings.get('demo', 'task')
    raw = source.read_bytes()
    final = json.loads(message('assistant', 44))
    final['payload']['unknown_private'] = 'private'
    raw = raw.replace(message('assistant', 44), json.dumps(final).encode() + b'\n')
    source.write_bytes(raw)
    changed = replace(binding, turn_end=contract.SourceBoundaryV1(len(raw), 46),
                      source_identity=contract.SourceIdentityV1.from_stat(source.stat()),
                      source_range_digest=hashlib.sha256(raw[binding.turn_start.byte_offset:]).hexdigest())
    replace_stored(service, changed, locator)
    assert native.parse_turn(raw, 'target').ready
    before = stored(service)
    assert queue.run_once(service, job) == 'rejected'
    assert stored(service) == before
