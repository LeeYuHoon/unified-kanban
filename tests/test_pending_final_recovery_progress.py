"""복구 탐색의 호출 간 진행성과 원본 예산 보존 회귀 시험."""
import importlib
import json
import os

import pytest

from kanban_adapter import claude_pending_final, codex_pending_final, conversation_runtime
from kanban_adapter import pending_final_recovery as recovery
from test_claude_absent_pending_final import _job
from test_codex_pending_final import prepared_job


@pytest.fixture(params=['claude', 'codex'])
def crowded_queue(request, tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    provider = request.param
    queue = claude_pending_final if provider == 'claude' else codex_pending_final
    made = _job(tmp_path) if provider == 'claude' else prepared_job(tmp_path)
    _, service, _, job = made[:4]
    cache = tmp_path / 'cache'
    cache.mkdir(mode=0o700)
    root = cache / ('pending-final' if provider == 'claude' else 'codex-pending-final')
    job.parent.rename(root)
    original = root / job.name
    template = json.loads(original.read_bytes())
    for index in range(300):
        body = json.loads(json.dumps(template))
        body['kwargs']['prompt_id' if provider == 'claude' else 'turn_id'] = f'crowded-{index}'
        body['status'] = 'ready'
        body['mac'] = queue._mac(service, {k: v for k, v in body.items() if k != 'mac'})
        path = root / (queue._key(body['kwargs']) + '.json')
        path.write_text(json.dumps(body))
        path.chmod(0o600)
        (root / (f'{index:064x}.lock')).touch(mode=0o600)
    with os.scandir(root) as entries:
        names = [entry.name for entry in entries]
    target = next(root / name for name in reversed(names[256:]) if name.endswith('.json'))
    # 원본 작업도 terminal로 바꾸어 후반부 pending만 관찰한다.
    for path in {original, target}:
        body = json.loads(path.read_bytes())
        body['status'] = 'pending' if path == target else 'ready'
        body['mac'] = queue._mac(service, {k: v for k, v in body.items() if k != 'mac'})
        path.write_text(json.dumps(body))
    before = target.read_bytes()
    monkeypatch.setattr(conversation_runtime, 'get_conversation_service', lambda: service)
    launches = []
    monkeypatch.setattr(queue, 'launch', launches.append)
    return cache, 'claude-code' if provider == 'claude' else 'codex', queue, service, target, before, launches


def test_repeated_fresh_calls_reach_pending_after_terminal_and_locks(crowded_queue):
    cache, source, _, _, target, before, launches = crowded_queue
    for _ in range(20):
        # 프로세스 메모리의 iterator만 보관하는 수정은 통과하지 못한다.
        importlib.reload(recovery)
        recovery.resume(cache, source)
        if launches:
            break
    assert launches == [target]
    assert target.read_bytes() == before


def test_elapsed_pending_prefix_does_not_starve_live_job(crowded_queue):
    cache, source, queue, service, target, before, launches = crowded_queue
    for path in target.parent.glob('*.json'):
        if path == target:
            continue
        body = json.loads(path.read_bytes())
        body.pop('mac')
        body['status'] = 'pending'
        body['expires_ns'] = service.clock_ns() - 1
        path.write_bytes(queue._wire({**body, 'mac': queue._mac(service, body)}))
    for _ in range(20):
        importlib.reload(recovery)
        recovery.resume(cache, source)
        if launches:
            break
    assert launches == [target]
    assert target.read_bytes() == before
