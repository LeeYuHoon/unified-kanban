"""지연 final은 카드 수명주기 밖에서 정확한 receipt로 수집한다."""
import importlib.util
import json
import pytest


def test_worker_refuses_delegated_before_runtime(tmp_path, monkeypatch):
    from kanban_adapter import claude_pending_final as queue
    from kanban_adapter import conversation_runtime, compatibility
    import sys
    monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', 'true')
    monkeypatch.setattr(sys, 'argv', ['worker', str(tmp_path / 'job.json')])
    monkeypatch.setattr(compatibility, 'check_hermes_compatibility', lambda: (True, 'fixture'))
    def forbidden():
        pytest.fail('delegated worker read runtime')
    monkeypatch.setattr(conversation_runtime, 'get_conversation_service', forbidden)
    assert queue.main() == 1

from kanban_adapter import claude_hook as hook
from test_claude_file_provenance_v2 import _setup, _history, _request, _final, _page


@pytest.mark.parametrize('attack', ['mac', 'kwargs'])
def test_enqueue_authenticates_existing_job_without_mutation(tmp_path, monkeypatch, attack):
    from kanban_adapter import claude_pending_final as queue
    from test_claude_file_final_readiness import _prepared
    _, service, kwargs = _prepared(tmp_path, monkeypatch)
    path = queue.enqueue(service, tmp_path / 'jobs', **kwargs)
    body = json.loads(path.read_bytes())
    if attack == 'mac':
        body['mac'] = '0' * 64
    else:
        body['kwargs']['prepared']['session'] = 'foreign'
        body['mac'] = queue._mac(service, {k: v for k, v in body.items() if k != 'mac'})
    path.write_text(json.dumps(body))
    before = path.read_bytes(), path.stat().st_ino
    with pytest.raises(PermissionError):
        queue.enqueue(service, tmp_path / 'jobs', **kwargs)
    assert (path.read_bytes(), path.stat().st_ino) == before


def test_task_lock_scope_does_not_overwrite_native_jobs():
    from kanban_adapter import claude_pending_final as queue
    kwargs = dict(board='b', task='t_one', prompt_id='one', task_receipt={'nonce': 'receipt'})
    first = queue._key(kwargs)
    second = queue._key({**kwargs, 'prompt_id': 'two'})
    other = queue._key({**kwargs, 'task': 't_other'})
    assert first != second
    assert first.split('.')[0] == second.split('.')[0]
    assert first.split('.')[0] != other.split('.')[0]


def test_actual_worker_inherits_delegation_refusal(tmp_path, monkeypatch):
    from kanban_adapter import claude_pending_final as queue
    monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', 'true')
    process = queue.launch(tmp_path / 'must-not-be-created.json')
    assert process.wait(timeout=5) == 1
    assert not list(tmp_path.iterdir())


def test_worker_does_not_import_ambient_pythonpath(tmp_path, monkeypatch):
    from kanban_adapter import claude_pending_final as queue
    poison = tmp_path / 'poison'
    package = poison / 'kanban_adapter'
    package.mkdir(parents=True)
    marker = tmp_path / 'untrusted-import'
    (package / '__init__.py').write_text(
        'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n'
    )
    (package / 'claude_pending_final.py').write_text('raise SystemExit(0)\n')
    monkeypatch.setenv('PYTHONPATH', str(poison))
    monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', 'true')
    process = queue.launch(tmp_path / 'missing-job.json')
    assert process.wait(timeout=5) == 1
    assert not marker.exists()


@pytest.mark.parametrize('attack', ['attempts', 'expires_ns', 'prompt_id', 'nonce'])
def test_job_tampering_never_reads_source(tmp_path, monkeypatch, attack):
    from kanban_adapter import claude_pending_final as queue
    from test_claude_file_final_readiness import _prepared
    source, service, kwargs = _prepared(tmp_path, monkeypatch)
    path = queue.enqueue(service, tmp_path / 'jobs', **kwargs)
    body = json.loads(path.read_bytes())
    if attack in {'attempts', 'expires_ns'}:
        body[attack] += 1
    elif attack == 'prompt_id':
        body['kwargs']['prompt_id'] = 'foreign'
    else:
        body['kwargs']['task_receipt']['nonce'] = 'foreign'
    path.write_text(json.dumps(body))
    monkeypatch.setattr(queue, 'seal', lambda *a, **k: pytest.fail('untrusted job reached source'))
    with pytest.raises(PermissionError):
        queue.run_once(service, path)


@pytest.mark.parametrize('budget', ['deadline', 'attempts'])
def test_retry_budget_expires_without_fabricated_final(tmp_path, monkeypatch, budget):
    from kanban_adapter import claude_pending_final as queue
    from test_claude_file_final_readiness import _prepared
    _, service, kwargs = _prepared(tmp_path, monkeypatch)
    path = queue.enqueue(service, tmp_path / 'jobs', **kwargs)
    if budget == 'deadline':
        monkeypatch.setattr(service, 'clock_ns', lambda: json.loads(path.read_bytes())['expires_ns'])
    else:
        for _ in range(queue.MAX_ATTEMPTS):
            assert queue.run_once(service, path) == 'pending'
    assert queue.run_once(service, path) == 'expired'
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 't_native')


@pytest.mark.parametrize('obstacle', ['locked', 'forged'])
def test_stop_ready_cannot_bypass_queue_authority(tmp_path, monkeypatch, obstacle):
    from contextlib import nullcontext
    from kanban_adapter import claude_pending_final as queue
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    hook.handle_event('prompt', payload, adapter=adapter, cache_dir=cache)
    state = json.loads(hook._state_path(cache, 'native').read_bytes())
    kwargs = dict(board='demo', task='t_native', prepared=state['conversation_prepared'],
                  task_receipt=state['observation_receipt'], prompt_id=payload['prompt_id'])
    job = queue.enqueue(service, cache / 'pending-final', **kwargs)
    if obstacle == 'forged':
        body = json.loads(job.read_bytes())
        body['mac'] = '0' * 64
        job.write_text(json.dumps(body))
    before = job.read_bytes(), job.stat().st_ino
    with source.open('ab') as stream:
        stream.write(_final())
    monkeypatch.setattr(queue, 'launch', lambda p: pytest.fail('unauthorized worker launch'))
    with queue._locked(job) if obstacle == 'locked' else nullcontext():
        hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache)
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 't_native')
    assert hook._state_path(cache, 'native').exists()
    assert not any('done' in command for command in commands)
    assert (job.read_bytes(), job.stat().st_ino) == before


def test_concurrent_worker_blocks_stop_then_stop_reuses_ready(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from kanban_adapter import claude_pending_final as queue
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    hook.handle_event('prompt', payload, adapter=adapter, cache_dir=cache)
    state = json.loads(hook._state_path(cache, 'native').read_bytes())
    kwargs = dict(board='demo', task='t_native', prepared=state['conversation_prepared'],
                  task_receipt=state['observation_receipt'], prompt_id=payload['prompt_id'])
    job = queue.enqueue(service, cache / 'pending-final', **kwargs)
    with source.open('ab') as stream:
        stream.write(_final())
    entered, release = Event(), Event()
    original = queue.seal
    calls = []
    def paused(*args, **kw):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args, **kw)
    monkeypatch.setattr(queue, 'seal', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(queue.run_once, service, job)
        try:
            assert entered.wait(5)
            hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache)
            assert hook._state_path(cache, 'native').exists()
            assert not any('done' in command for command in commands)
        finally:
            release.set()
        assert future.result(timeout=5) == 'ready'
    before = job.read_bytes(), job.stat().st_ino
    hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache)
    assert not hook._state_path(cache, 'native').exists()
    assert len(calls) == 1
    assert (job.read_bytes(), job.stat().st_ino) == before
    assert [e['text'] for e in _page(service)['events']] == ['same request', 'current final']


def test_launch_failure_preserves_stop_for_explicit_retry(tmp_path, monkeypatch):
    from kanban_adapter import claude_pending_final as queue
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    hook.handle_event('prompt', payload, adapter=adapter, cache_dir=cache)
    def failed_launch(path):
        raise OSError('injected launch failure')
    monkeypatch.setattr(queue, 'launch', failed_launch)
    hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache)
    assert hook._state_path(cache, 'native').exists()
    assert not any('done' in command for command in commands)
    job, = (cache / 'pending-final').glob('*.json')
    deadline = json.loads(job.read_bytes())['expires_ns']
    with source.open('ab') as stream:
        stream.write(_final())
    hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache)
    assert not hook._state_path(cache, 'native').exists()
    body = json.loads(job.read_bytes())
    assert body['status'] == 'ready'
    assert body['expires_ns'] == deadline


def test_retry_enqueue_does_not_reset_budget(tmp_path, monkeypatch):
    from kanban_adapter import claude_pending_final as queue
    from test_claude_file_final_readiness import _prepared
    _, service, kwargs = _prepared(tmp_path, monkeypatch)
    job = queue.enqueue(service, tmp_path / 'jobs', **kwargs)
    assert queue.run_once(service, job) == 'pending'
    before = job.read_bytes(), job.stat().st_ino
    assert queue.enqueue(service, tmp_path / 'jobs', **kwargs) == job
    assert (job.read_bytes(), job.stat().st_ino) == before


@pytest.mark.parametrize('published', ['final', 'partial'])
def test_existing_binding_never_guessed_ready_after_status_failure(tmp_path, monkeypatch, published):
    from kanban_adapter import claude_pending_final as queue
    from test_claude_file_final_readiness import _prepared
    source, service, kwargs = _prepared(tmp_path, monkeypatch)
    job = queue.enqueue(service, tmp_path / 'jobs', **kwargs)
    if published == 'partial':
        queue.seal(service, **kwargs)
    else:
        with source.open('ab') as stream:
            stream.write(_final())
        original = queue._save
        def fail_status(service, path, body, directory, expected=None):
            if body['status'] == 'ready':
                raise OSError('injected status failure')
            return original(service, path, body, directory, expected)
        with monkeypatch.context() as patcher:
            patcher.setattr(queue, '_save', fail_status)
            with pytest.raises(OSError, match='injected status failure'):
                queue.run_once(service, job)
        assert json.loads(job.read_bytes())['status'] == 'pending'
    before = service.bindings.get('demo', 't_native')
    assert queue.run_once(service, job) == ('ready' if published == 'final' else 'rejected')
    assert service.bindings.get('demo', 't_native') == before
    if published == 'final':
        assert [e['text'] for e in _page(service)['events']] == ['same request', 'current final']


def test_foreign_successor_preserved_when_budget_cas_fails(tmp_path, monkeypatch):
    from kanban_adapter import claude_pending_final as queue
    from test_claude_file_final_readiness import _prepared
    _, service, kwargs = _prepared(tmp_path, monkeypatch)
    job = queue.enqueue(service, tmp_path / 'jobs', **kwargs)
    original = queue._save
    foreign = b'{"foreign":true}'
    identities = []
    def replace_before_save(service, path, body, directory, expected=None):
        path.rename(path.with_suffix('.retained'))
        path.write_bytes(foreign)
        path.chmod(0o600)
        identities.append(path.stat().st_ino)
        return original(service, path, body, directory, expected)
    monkeypatch.setattr(queue, '_save', replace_before_save)
    monkeypatch.setattr(queue, 'seal', lambda *a, **kw: pytest.fail('foreign CAS reached source'))
    with pytest.raises(RuntimeError):
        queue.run_once(service, job)
    assert job.read_bytes() == foreign
    assert job.stat().st_ino == identities[0]


def test_stop_done_then_late_final(tmp_path, monkeypatch):
    assert importlib.util.find_spec('kanban_adapter.claude_pending_final') is not None
    from kanban_adapter import claude_pending_final as queue
    source, service, commands, adapter, payload, cache = _setup(tmp_path, monkeypatch, _history() + _request())
    launches = []
    monkeypatch.setattr(queue, 'launch', lambda path: launches.append(path))
    hook.handle_event('prompt', payload, adapter=adapter, cache_dir=cache)
    hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache)
    assert not hook._state_path(cache, 'native').exists()
    assert any('done' in command for command in commands)
    jobs = list((cache / 'pending-final').glob('*.json'))
    assert len(jobs) == 1
    assert launches == jobs
    assert b'same request' not in jobs[0].read_bytes()
    assert queue.run_once(service, jobs[0]) == 'pending'
    with source.open('ab') as stream:
        stream.write(_final())
    assert queue.run_once(service, jobs[0]) == 'ready'
    assert queue.run_once(service, jobs[0]) == 'ready'
    assert [e['text'] for e in _page(service)['events']] == ['same request', 'current final']
