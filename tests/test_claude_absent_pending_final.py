"""부재 시작 hook과 영속 worker의 final 경계를 검증한다."""
import json
import pytest
from kanban_adapter import claude_hook as hook, claude_pending_final as queue
from test_claude_missing_transcript import _harness, _native_turn, PROMPT_ID


@pytest.mark.parametrize('missing', [False, True])
def test_absent_stop_done_then_worker_public_final(tmp_path, monkeypatch, missing):
    source, service, commands, adapter, payload, cache = _harness(tmp_path, monkeypatch)
    payload['prompt_id'] = PROMPT_ID
    launches = []
    monkeypatch.setattr(queue, 'launch', launches.append)
    hook.handle_event('prompt', payload, adapter=adapter, cache_dir=cache)
    if not missing:
        _native_turn(source)
        request, final = source.read_bytes().splitlines(keepends=True)
        source.write_bytes(request)
    hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache)
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 't_native')
    assert not hook._state_path(cache, 'native').exists()
    assert any(c[0] == 'done' for c in commands)
    job, = (cache / 'pending-final').glob('*.json')
    assert launches == [job]
    assert queue.run_once(service, job) == 'pending'
    if missing:
        _native_turn(source)
    else:
        with source.open('ab') as stream:
            stream.write(final)
    assert queue.run_once(service, job) == 'ready'
    page = service.get_parent_page(principal_id='owner', board='demo', task='t_native', cursor=None, limit=20)
    assert page['completeness'] == 'partial'
    assert [e['text'] for e in page['events']] == ['public new turn', 'public final answer']


def _job(tmp_path):
    from test_claude_absent_final_preparation import setup_source
    source, service, kwargs, proof, final = setup_source(tmp_path)
    kwargs['prepared'] = proof
    job = queue.enqueue(service, tmp_path / 'jobs', **kwargs)
    return source, service, kwargs, job, final


def test_pin_is_durable_before_seal_and_duplicate_stop_keeps_it(tmp_path, monkeypatch):
    source, service, kwargs, job, final = _job(tmp_path)
    original = queue.absent.seal_final
    def checked(service, **kw):
        body = json.loads(job.read_bytes())
        assert body['first_open'] == kw['prepared']
        assert body['status'] == 'pending'
        assert body['mac'] == queue._mac(service, {k: v for k, v in body.items() if k != 'mac'})
        return original(service, **kw)
    monkeypatch.setattr(queue.absent, 'seal_final', checked)
    assert queue.run_once(service, job) == 'pending'
    before = job.read_bytes()
    assert queue.enqueue(service, job.parent, **kwargs) == job
    assert job.read_bytes() == before
    monkeypatch.setattr(queue.absent, 'prepare_final', lambda *a, **k: pytest.fail('repinned'))
    assert queue.run_once(service, job) == 'pending'
    with source.open('ab') as stream:
        stream.write(final)
    assert queue.run_once(service, job) == 'ready'


@pytest.mark.parametrize('failure', ['inode', 'parent', 'prefix'])
def test_first_open_never_accepts_replacement(tmp_path, failure):
    source, service, kwargs, job, final = _job(tmp_path)
    assert queue.run_once(service, job) == 'pending'
    pin = json.loads(job.read_bytes())['first_open']
    if failure == 'inode':
        source.rename(source.with_suffix('.old'))
        _native_turn(source)
    elif failure == 'parent':
        source.rename(source.with_suffix('.old'))
        _native_turn(source)
        source.write_bytes(source.read_bytes().replace(b'"parentUuid": null', b'"parentUuid": "prior"'))
    else:
        source.write_bytes(source.read_bytes().replace(b'public new turn', b'public OLD turn') + final)
    assert queue.run_once(service, job) == 'rejected'
    assert json.loads(job.read_bytes())['first_open'] == pin
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 't_native')


@pytest.mark.parametrize('failure', ['error', 'crash', 'successor'])
def test_pin_save_failure_cannot_retry_absent_proof(tmp_path, monkeypatch, failure):
    source, service, kwargs, job, final = _job(tmp_path)
    original = queue._save
    foreign = b'{"foreign":true}'
    foreign_inode = []
    def failed(service, path, body, directory, expected=None):
        if 'first_open' in body:
            if failure == 'successor':
                if not foreign_inode:
                    path.rename(path.with_suffix('.retained'))
                    path.write_bytes(foreign)
                    path.chmod(0o600)
                    foreign_inode.append(path.stat().st_ino)
                return original(service, path, body, directory, expected)
            if failure == 'crash':
                raise KeyboardInterrupt()
            raise OSError('pin save failed')
        return original(service, path, body, directory, expected)
    monkeypatch.setattr(queue.absent, 'seal_final', lambda *a, **k: pytest.fail('unpersisted pin sealed'))
    with monkeypatch.context() as patcher:
        patcher.setattr(queue, '_save', failed)
        with pytest.raises((OSError, RuntimeError, KeyboardInterrupt)):
            queue.run_once(service, job)
    monkeypatch.setattr(queue.absent, 'prepare_final', lambda *a, **k: pytest.fail('original proof reused'))
    if failure == 'successor':
        assert job.read_bytes() == foreign
        assert job.stat().st_ino == foreign_inode[0]
        with pytest.raises(PermissionError):
            queue.run_once(service, job)
    else:
        assert queue.run_once(service, job) == 'rejected'
        assert queue.enqueue(service, job.parent, **kwargs) == job
        assert queue.run_once(service, job) == 'rejected'
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 't_native')


@pytest.mark.parametrize('obstacle', ['launch', 'tamper', 'lock', 'service', 'enqueue', 'rejected', 'expired', 'unknown'])
def test_absent_stop_preserves_state_on_queue_failure(tmp_path, monkeypatch, obstacle):
    from contextlib import nullcontext
    source, service, commands, adapter, payload, cache = _harness(tmp_path, monkeypatch)
    payload['prompt_id'] = PROMPT_ID
    hook.handle_event('prompt', payload, adapter=adapter, cache_dir=cache)
    state_path = hook._state_path(cache, 'native')
    state = json.loads(state_path.read_bytes())
    kwargs = dict(board='demo', task='t_native', prepared=state['conversation_prepared'],
                  task_receipt=state['observation_receipt'], prompt_id=PROMPT_ID)
    job = queue.enqueue(service, cache / 'pending-final', **kwargs)
    def failed(*a, **k):
        raise OSError('fixture failure')
    monkeypatch.setattr(queue, 'launch', failed)
    if obstacle == 'tamper':
        body = json.loads(job.read_bytes())
        body['kwargs']['prompt_id'] = 'foreign'
        job.write_text(json.dumps(body))
    elif obstacle == 'service':
        from kanban_adapter import conversation_runtime
        monkeypatch.setattr(conversation_runtime, 'get_conversation_service', lambda: None)
    elif obstacle == 'enqueue':
        monkeypatch.setattr(queue, 'enqueue', failed)
    elif obstacle in {'rejected', 'expired', 'unknown'}:
        monkeypatch.setattr(queue, 'run_once', lambda *a: obstacle)
    with queue._locked(job) if obstacle == 'lock' else nullcontext():
        hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache)
    assert state_path.exists()
    assert not any(c[0] == 'done' for c in commands)
    with pytest.raises(FileNotFoundError):
        service.bindings.get('demo', 't_native')


def test_absent_launch_retry_keeps_original_pin(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _harness(tmp_path, monkeypatch)
    payload['prompt_id'] = PROMPT_ID
    hook.handle_event('prompt', payload, adapter=adapter, cache_dir=cache)
    _native_turn(source)
    request, final = source.read_bytes().splitlines(keepends=True)
    source.write_bytes(request)
    def failed(*a):
        raise OSError('launch failed')
    monkeypatch.setattr(queue, 'launch', failed)
    hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache)
    job, = (cache / 'pending-final').glob('*.json')
    pin = json.loads(job.read_bytes())['first_open']
    with source.open('ab') as stream:
        stream.write(final)
    hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache)
    assert not hook._state_path(cache, 'native').exists()
    assert json.loads(job.read_bytes())['first_open'] == pin
    assert json.loads(job.read_bytes())['status'] == 'ready'


@pytest.mark.parametrize('published', ['legacy-partial', 'final-status-failure'])
def test_absent_never_guesses_existing_binding_ready(tmp_path, monkeypatch, published):
    source, service, kwargs, job, final = _job(tmp_path)
    if published == 'legacy-partial':
        queue.absent.seal(service, **kwargs)
    else:
        with source.open('ab') as stream:
            stream.write(final)
        original = queue._save
        def failed(service, path, body, directory, expected=None):
            if body['status'] == 'ready':
                raise OSError('status failed')
            return original(service, path, body, directory, expected)
        with monkeypatch.context() as patcher:
            patcher.setattr(queue, '_save', failed)
            with pytest.raises(OSError):
                queue.run_once(service, job)
    before = service.bindings.get('demo', 't_native')
    if published == 'final-status-failure':
        pin = json.loads(job.read_bytes())['first_open']
        def forbidden(*a, **k):
            pytest.fail('복구 중 재고정/재발행 금지')
        monkeypatch.setattr(queue.absent, 'prepare_final', forbidden)
        monkeypatch.setattr(service.bindings, 'put', forbidden)
        monkeypatch.setattr(service.authority, 'begin_binding', forbidden)
        monkeypatch.setattr(service.authority, 'seal_binding', forbidden)
        assert queue.run_once(service, job) == 'ready'
        assert json.loads(job.read_bytes())['first_open'] == pin
    else:
        assert queue.run_once(service, job) == 'rejected'
    assert service.bindings.get('demo', 't_native') == before


def test_first_open_pin_tamper_rejected_before_source(tmp_path, monkeypatch):
    _, service, kwargs, job, final = _job(tmp_path)
    assert queue.run_once(service, job) == 'pending'
    body = json.loads(job.read_bytes())
    del body['first_open']
    job.write_text(json.dumps(body))
    monkeypatch.setattr(queue.absent, 'prepare_final', lambda *a, **k: pytest.fail('tampered job opened'))
    with pytest.raises(PermissionError):
        queue.run_once(service, job)


def test_absent_queue_written_before_done_without_raw_content(tmp_path, monkeypatch):
    source, service, commands, adapter, payload, cache = _harness(tmp_path, monkeypatch)
    payload['prompt_id'] = PROMPT_ID
    monkeypatch.setattr(queue, 'launch', lambda path: None)
    def checked(argv, cwd):
        if argv[0] == 'done':
            job, = (cache / 'pending-final').glob('*.json')
            body = json.loads(job.read_bytes())
            assert body['status'] == 'pending'
            assert body['mac'] == queue._mac(service, {k: v for k, v in body.items() if k != 'mac'})
            assert b'public new turn' not in job.read_bytes()
            with pytest.raises(FileNotFoundError):
                service.bindings.get('demo', 't_native')
        return adapter(argv, cwd)
    hook.handle_event('prompt', payload, adapter=checked, cache_dir=cache)
    hook.handle_event('stop', payload, adapter=checked, cache_dir=cache)
    assert any(c[0] == 'done' for c in commands)
