"""새 hook 시작은 원래 인증 예산 내 pending만 재개한다."""
import json
from contextlib import nullcontext
import pytest
from kanban_adapter import claude_hook as hook
from kanban_adapter import claude_pending_final, codex_pending_final, conversation_runtime
from test_claude_absent_pending_final import _job
from test_codex_pending_final import prepared_job


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('status', ['pending', 'ready', 'rejected', 'expired', 'tamper', 'elapsed', 'exhausted',
                                  'lock', 'launch', 'symlink', 'public'])
def test_prompt_start_resumes_only_authenticated_live_queue(tmp_path, monkeypatch, provider, status):
    # 격리 fixture의 호출 경계만 시험하며 운영 위임 표시는 변경하지 않는다.
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    queue = claude_pending_final if provider == 'claude' else codex_pending_final
    made = _job(tmp_path) if provider == 'claude' else prepared_job(tmp_path)
    source_path, service, _, job = made[:4]
    cache = tmp_path / 'cache'
    cache.mkdir(mode=0o700)
    root = cache / ('pending-final' if provider == 'claude' else 'codex-pending-final')
    job.parent.rename(root)
    job = root / job.name
    body = json.loads(job.read_bytes())
    if status in {'ready', 'rejected', 'expired'}:
        body['status'] = status
    elif status == 'elapsed':
        body['expires_ns'] = service.clock_ns()
    elif status == 'exhausted':
        body['attempts'] = queue.MAX_ATTEMPTS
    body['mac'] = queue._mac(service, {k: v for k, v in body.items() if k != 'mac'})
    if status == 'tamper':
        body['mac'] = '0' * 64
    job.write_text(json.dumps(body))
    before = job.read_bytes()
    launches = []
    monkeypatch.setattr(queue, 'launch', launches.append)
    if status == 'launch':
        def failed(*a):
            raise OSError('격리된 실행 실패')
        monkeypatch.setattr(queue, 'launch', failed)
    elif status == 'symlink':
        target = job.with_suffix('.retained')
        job.rename(target)
        job.symlink_to(target)
    elif status == 'public':
        root.chmod(0o755)
    monkeypatch.setattr(conversation_runtime, 'get_conversation_service', lambda: service)
    monkeypatch.setattr(hook, '_handle_event_locked', lambda *a, **k: None)
    with queue._locked(job) if status == 'lock' else nullcontext():
        hook.handle_event('prompt', {'session_id': 'new'}, cache_dir=cache,
                          source='claude-code' if provider == 'claude' else 'codex')
    assert launches == ([job] if status == 'pending' else [])
    if status in {'ready', 'rejected', 'expired', 'elapsed', 'exhausted'}:
        assert not job.exists()
        saved = json.loads(job.with_suffix('.terminal').read_bytes())
        original = json.loads(before)
        if status in {'elapsed', 'exhausted'}:
            original['status'] = 'expired'
            original['mac'] = queue._mac(service, {k: v for k, v in original.items() if k != 'mac'})
        assert saved == original
    else:
        assert job.read_bytes() == before
    if status == 'pending':
        # 발견된 정확한 job이 실제 worker 재시도 경로에서도 수락되는지 확인한다.
        assert queue.run_once(service, launches[0]) == 'pending'
        assert json.loads(job.read_bytes())['expires_ns'] == body['expires_ns']
        if provider == 'claude':
            final = made[4]
        else:
            from test_codex_authenticated_prepare import message
            from test_codex_file_provenance import marker
            final = message('user', 43) + message('assistant', 44) + marker('task_complete', ordinal=45)
        with source_path.open('ab') as stream:
            stream.write(final)
        assert queue.run_once(service, launches[0]) == 'ready'
        task = 't_native' if provider == 'claude' else 'task'
        page = service.get_parent_page(principal_id='owner', board='demo', task=task, cursor=None, limit=20)
        assert [e['kind'] for e in page['events']] == ['user_message', 'final_assistant']
    elif status == 'launch':
        monkeypatch.setattr(queue, 'launch', launches.append)
        hook.handle_event('prompt', {'session_id': 'new'}, cache_dir=cache,
                          source='claude-code' if provider == 'claude' else 'codex')
        assert launches == [job]
        assert job.read_bytes() == before


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_startup_preserves_delegation_guard(tmp_path, monkeypatch, provider):
    monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', 'fixture')
    monkeypatch.setattr(conversation_runtime, 'get_conversation_service',
                        lambda: pytest.fail('위임된 hook은 복구 권한을 얻으면 안 됨'))
    monkeypatch.setattr(hook, '_handle_event_locked', lambda *a, **k: None)
    hook.handle_event('prompt', {'session_id': 'new'}, cache_dir=tmp_path / 'cache',
                      source='claude-code' if provider == 'claude' else 'codex')