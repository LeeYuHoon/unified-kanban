"""합성 네이티브 hook부터 실제 공개 projection까지 검증한다."""
import json
import os
import pytest

from kanban_adapter import claude_hook as hook, conversation_runtime as runtime
from kanban_adapter.codex_hook import normalize_payload
from test_codex_authenticated_prepare import setup, append, message
from test_codex_file_provenance import marker
from test_conversation_integration_security_fixes import _receipt


def harness(tmp_path, monkeypatch):
    from kanban_adapter import token_usage

    source, service, args, _ = setup(tmp_path)
    # 합성 원본의 승인 루트를 실제 CODEX_HOME/sessions 경계 대신 명시한다.
    monkeypatch.setattr(token_usage, '_default_root', lambda provider: source.parent)
    monkeypatch.setattr(hook.HermesCliBackend, 'resolve_board', lambda self, **kw: 'demo')
    monkeypatch.setenv('UNIFIED_KANBAN_CONVERSATION_CONFIG', str(tmp_path / 'config'))
    monkeypatch.setattr(runtime, 'get_conversation_service', lambda: service)
    commands = []
    tasks = {}
    def adapter(argv, cwd):
        commands.append(argv)
        if argv[0] == 'start':
            key = argv[argv.index('--idempotency-key') + 1]
            task = tasks.setdefault(key, 't_' + str(len(tasks) + 1))
            receipt = _receipt(b'k' * 32, board='demo', task=task, created_at=2)
            fd = int(next(a.split('=', 1)[1] for a in argv if a.startswith('--conversation-receipt-fd=')))
            os.write(fd, json.dumps(receipt).encode())
            return task
        return ''
    cache = tmp_path / 'cache'
    def send(event, turn='target'):
        payload = normalize_payload(dict(session_id='native', turn_id=turn, cwd=str(tmp_path),
            prompt='public', transcript_path=str(source), last_assistant_message='hook text not authority'))
        hook.handle_event(event, payload, adapter=adapter, cache_dir=cache, source='codex')
    return source, service, commands, cache, send


def test_native_stop_done_before_terminal_then_exact_public_projection(tmp_path, monkeypatch):
    source, service, commands, cache, send = harness(tmp_path, monkeypatch)
    send('prompt')
    state = json.loads(hook._state_path(cache, 'native').read_bytes())
    assert state['conversation_prepared']['mode'] == 'codex-file-provenance-v1'
    from kanban_adapter import codex_pending_final as queue
    launches = []
    monkeypatch.setattr(queue, 'launch', lambda path: launches.append(path))
    append(source, message('user', 43) + message('assistant', 44))
    send('stop')
    assert not hook._state_path(cache, 'native').exists()
    assert any(c[0] == 'done' for c in commands)
    job, = (cache / 'codex-pending-final').glob('*.json')
    assert launches == [job]
    assert b'hook text not authority' not in job.read_bytes()
    assert queue.run_once(service, job) == 'pending'
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 't_1')
    before = job.read_bytes()
    send('stop')
    assert job.read_bytes() == before
    append(source, marker('task_complete', ordinal=45) + marker('task_started', 'next', 46))
    send('prompt', 'next')
    current = hook._state_path(cache, 'native').read_bytes()
    send('stop')
    assert hook._state_path(cache, 'native').read_bytes() == current
    assert queue.run_once(service, job) == 'ready'
    assert queue.run_once(service, job) == 'ready'
    page = service.get_parent_page(principal_id='owner', board='demo', task='t_1', cursor=None, limit=20)
    assert [(e['kind'], e['text']) for e in page['events']] == [('user_message', 'public'), ('final_assistant', 'public')]
    assert service.bindings.get('demo', 't_1')[0].codex_target_turn_id == 'target'
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 't_2')


def prepared_job(tmp_path):
    from kanban_adapter import codex_file_provenance as native, codex_pending_final as queue
    source, service, args, _ = setup(tmp_path)
    prepared = native.prepare(service, session='native', source_path=source, **args)
    kwargs = dict(prepared=prepared, **args)
    return source, service, kwargs, queue.enqueue(service, tmp_path / 'jobs', **kwargs)


@pytest.mark.parametrize('attack', ['attempts', 'expires_ns', 'turn_id', 'nonce', 'provider'])
def test_job_authentication_rejects_mutation_before_source(tmp_path, monkeypatch, attack):
    from kanban_adapter import codex_pending_final as queue
    _, service, _, job = prepared_job(tmp_path)
    body = json.loads(job.read_bytes())
    if attack in {'attempts', 'expires_ns'}:
        body[attack] += 1
    elif attack == 'nonce':
        body['kwargs']['task_receipt']['nonce'] = 'foreign'
    elif attack == 'provider':
        body['kwargs']['prepared']['provider'] = 'claude'
    else:
        body['kwargs'][attack] = 'foreign'
    job.write_text(json.dumps(body))
    before = job.read_bytes(), job.stat().st_ino
    monkeypatch.setattr(queue, 'seal', lambda *a, **kw: pytest.fail('untrusted source access'))
    with pytest.raises(PermissionError):
        queue.run_once(service, job)
    assert (job.read_bytes(), job.stat().st_ino) == before


def test_duplicate_enqueue_preserves_budget_and_crash_never_guesses_ready(tmp_path, monkeypatch):
    from kanban_adapter import codex_pending_final as queue
    source, service, kwargs, job = prepared_job(tmp_path)
    assert queue.run_once(service, job) == 'pending'
    before = job.read_bytes(), job.stat().st_ino
    assert queue.enqueue(service, job.parent, **kwargs) == job
    assert (job.read_bytes(), job.stat().st_ino) == before
    append(source, message('user', 43) + message('assistant', 44) + marker('task_complete', ordinal=45))
    original = queue._save
    def fail_status(service, path, body, directory, expected=None):
        if body['status'] == 'ready':
            raise OSError('fixture status crash')
        return original(service, path, body, directory, expected)
    with monkeypatch.context() as patcher:
        patcher.setattr(queue, '_save', fail_status)
        with pytest.raises(OSError, match='fixture status crash'):
            queue.run_once(service, job)
    binding = service.bindings.get('demo', 'task')
    assert json.loads(job.read_bytes())['status'] == 'pending'
    assert queue.run_once(service, job) == 'ready'
    assert service.bindings.get('demo', 'task') == binding


@pytest.mark.parametrize('budget', ['attempts', 'deadline'])
def test_bounded_pending_never_fabricates_final(tmp_path, monkeypatch, budget):
    from kanban_adapter import codex_pending_final as queue
    _, service, _, job = prepared_job(tmp_path)
    if budget == 'attempts':
        for _ in range(queue.MAX_ATTEMPTS):
            assert queue.run_once(service, job) == 'pending'
    else:
        monkeypatch.setattr(service, 'clock_ns', lambda: json.loads(job.read_bytes())['expires_ns'])
    assert queue.run_once(service, job) == 'expired'
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 'task')


@pytest.mark.parametrize('obstacle', ['launch', 'lock', 'mac', 'mode', 'missing-id'])
def test_stop_failure_retains_card_without_publication(tmp_path, monkeypatch, obstacle):
    from kanban_adapter import codex_pending_final as queue
    from contextlib import nullcontext
    source, service, commands, cache, send = harness(tmp_path, monkeypatch)
    send('prompt')
    state_path = hook._state_path(cache, 'native')
    state = json.loads(state_path.read_bytes())
    kwargs = dict(board='demo', task='t_1', prepared=state['conversation_prepared'],
                  task_receipt=state['observation_receipt'], turn_id='target')
    job = queue.enqueue(service, cache / 'codex-pending-final', **kwargs)
    if obstacle == 'mac':
        body = json.loads(job.read_bytes())
        body['mac'] = '0' * 64
        job.write_text(json.dumps(body))
    if obstacle == 'mode':
        state['conversation_prepared']['mode'] = 'legacy'
        state_path.write_text(json.dumps(state))
    def failed_launch(path):
        raise OSError('fixture launch failure')
    monkeypatch.setattr(queue, 'launch', failed_launch)
    with queue._locked(job) if obstacle == 'lock' else nullcontext():
        send('stop', None if obstacle == 'missing-id' else 'target')
    assert state_path.exists()
    assert not any(c[0] == 'done' for c in commands)
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 't_1')


def test_absent_prompt_source_never_promotes_later_source(tmp_path, monkeypatch):
    source, service, commands, cache, send = harness(tmp_path, monkeypatch)
    source.unlink()
    send('prompt')
    state = json.loads(hook._state_path(cache, 'native').read_bytes())
    assert state['conversation_status'] == 'unavailable'
    assert 'conversation_prepared' not in state
    source.write_bytes(marker('task_started', ordinal=42) + message('user', 43) + message('assistant', 44) + marker('task_complete', ordinal=45))
    source.chmod(0o600)
    send('stop')
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 't_1')
    assert not (cache / 'codex-pending-final').exists()


def test_actual_worker_inherits_guard_and_ignores_poisoned_import(tmp_path, monkeypatch):
    from kanban_adapter import codex_pending_final as queue
    package = tmp_path / 'poison' / 'kanban_adapter'
    package.mkdir(parents=True)
    marker_path = tmp_path / 'imported'
    (package / '__init__.py').write_text('from pathlib import Path\nPath(' + repr(str(marker_path)) + ').touch()\n')
    monkeypatch.setenv('PYTHONPATH', str(package.parent))
    monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', 'true')
    process = queue.launch(tmp_path / 'no-job.json')
    assert process.wait(timeout=5) == 1
    assert not marker_path.exists()
    assert not (tmp_path / 'no-job.json').exists()


def test_job_receipt_cas_preserves_foreign_successor(tmp_path, monkeypatch):
    from kanban_adapter import codex_pending_final as queue
    _, service, _, job = prepared_job(tmp_path)
    original = queue._save
    foreign = b'{"foreign":true}'
    def replace_before_save(service, path, body, directory, expected=None):
        path.rename(path.with_suffix('.retained'))
        path.write_bytes(foreign)
        path.chmod(0o600)
        return original(service, path, body, directory, expected)
    monkeypatch.setattr(queue, '_save', replace_before_save)
    monkeypatch.setattr(queue, 'seal', lambda *a, **kw: pytest.fail('foreign CAS reached source'))
    with pytest.raises(RuntimeError):
        queue.run_once(service, job)
    assert job.read_bytes() == foreign


def test_queue_mac_is_provider_domain_separated(tmp_path):
    from kanban_adapter import codex_pending_final as queue, claude_pending_final as claude
    _, service, _, job = prepared_job(tmp_path)
    body = json.loads(job.read_bytes())
    body.pop('mac')
    assert queue._mac(service, body) != claude._mac(service, body)
