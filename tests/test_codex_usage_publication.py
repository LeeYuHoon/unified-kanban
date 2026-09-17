"""실제 백엔드 전송 계약을 오프라인으로 검증하며 설치된 서비스는 호출하지 않는다."""
import json

import pytest

from kanban_adapter.backend import HermesCliBackend
from kanban_adapter.usage import usage_comment, usage_event_id

TASK = 't_offline'

def message(request=None, at=1789660799):
    return usage_comment(source='codex', model='fixture-model', usage={},
        tokens={'input': 10, 'output': 2, 'total': 12, 'requests': 1},
        event_id=usage_event_id('codex', TASK, request), request_hash=request,
        usage_at=at, usage_timing='request' if request else 'completion')

class Transport:
    def __init__(self):
        self.comments = {}
        self.fail = None
    def __call__(self, argv):
        if 'show' in argv:
            return json.dumps({'task': {'id': TASK}, 'comments': list(self.comments.values())})
        assert 'comment' in argv
        key = next(a.split('=', 1)[1] for a in argv if a.startswith('--idempotency-key='))
        self.comments.setdefault(key, {'author': 'kanban-adapter', 'body': argv[-1]})
        if self.fail == key:
            self.fail = None
            raise RuntimeError('remote success local failure')
        return '1'

def test_request_mode_is_durable_and_completion_cannot_double_count():
    transport = Transport()
    backend = HermesCliBackend(runner=transport)
    request = message('a'*16)
    backend.publish_codex_usage(board='default', task_id=TASK, messages=[request])
    # 새 백엔드 인스턴스로 훅의 로컬 상태를 모두 잃은 상황을 재현한다.
    HermesCliBackend(runner=transport).publish_codex_usage(
        board='default', task_id=TASK, messages=[message()])
    usage = [c['body'] for c in transport.comments.values() if c['body'].startswith('Codex tool usage\n')]
    assert usage == [request]

@pytest.mark.parametrize('failure', ['slot', 'first', 'second'])
def test_remote_success_failure_retry_freezes_batch(failure):
    transport = Transport()
    original = [message('a'*16), message('b'*16, 1789660800)]
    transport.fail = usage_event_id('codex', TASK, {'slot': None, 'first': 'a'*16, 'second': 'b'*16}[failure])
    with pytest.raises(RuntimeError, match='remote success'):
        HermesCliBackend(runner=transport).publish_codex_usage(board='default', task_id=TASK, messages=original)
    HermesCliBackend(runner=transport).publish_codex_usage(board='default', task_id=TASK, messages=[message('c'*16)])
    usage = [c['body'] for c in transport.comments.values() if c['body'].startswith('Codex tool usage\n')]
    assert usage == original

@pytest.mark.parametrize('candidate', [['x' * 48001], ['invalid'], [None] * 129])
def test_frozen_retry_ignores_invalid_later_candidate(candidate):
    transport = Transport()
    original = [message('a' * 16)]
    transport.fail = usage_event_id('codex', TASK)
    with pytest.raises(RuntimeError, match='remote success'):
        HermesCliBackend(runner=transport).publish_codex_usage(
            board='default', task_id=TASK, messages=original)
    HermesCliBackend(runner=transport).publish_codex_usage(
        board='default', task_id=TASK, messages=candidate)
    assert [c['body'] for c in transport.comments.values()
            if c['body'].startswith('Codex tool usage\n')] == original


def test_empty_unreserved_batch_is_retryable_not_success():
    transport = Transport()
    with pytest.raises(RuntimeError, match='not ready'):
        HermesCliBackend(runner=transport).publish_codex_usage(
            board='default', task_id=TASK, messages=[])
    assert not transport.comments


def test_existing_completion_stays_legacy():
    transport = Transport()
    backend = HermesCliBackend(runner=transport)
    backend.publish_codex_usage(board='default', task_id=TASK, messages=[message()])
    backend.publish_codex_usage(board='default', task_id=TASK, messages=[message('a'*16)])
    assert [c['body'] for c in transport.comments.values()] == [message()]

@pytest.mark.parametrize('pending', ['truncated-first', 'no-record-yet', None])
def test_native_hook_publishes_request_batch(tmp_path, monkeypatch, pending):
    from test_codex_measured_usage import rows, write_rows

    from kanban_adapter import claude_hook as hook
    path = tmp_path / 'sessions' / 'rollout.jsonl'
    write_rows(path, rows())
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    state_path = hook._state_path(tmp_path, 'session-a')
    state = {'task_id': TASK, 'cwd': str(tmp_path), 'transcript_path': str(path),
             'lifecycle': {'session': 'session-a', 'source': 'codex', 'board': 'default',
                           'prompt_id': 'turn-a', 'idempotency_key': 'a'*64}}
    state_path.write_text(json.dumps(state)); state_path.chmod(0o600)
    calls = []
    transport = Transport()
    def adapter(argv, cwd):
        calls.append(argv)
        if argv[0] == 'publish-codex-usage':
            from kanban_adapter.cli import main
            if main(argv, backend=HermesCliBackend(runner=transport)) != 0:
                raise RuntimeError('adapter command failed')
        return ''
    import os
    if pending:
        write_rows(path, rows()[:3] + [rows()[4]])
        if pending == 'truncated-first':
            with path.open('a') as stream:
                stream.write(json.dumps(rows()[3])[:100])
        fd = os.open(tmp_path, os.O_RDONLY)
        try:
            hook._complete(state_path, 'done', adapter=adapter, source='codex', directory_fd=fd)
        finally:
            os.close(fd)
        assert not transport.comments
        assert not any(call[0] == 'publish-codex-usage' for call in calls)
        assert not state_path.exists()
        return
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        hook._complete(state_path, 'done', adapter=adapter, source='codex', directory_fd=fd)
    finally:
        os.close(fd)
    publish = [a for a in calls if a[0] == 'publish-codex-usage']
    assert len(publish) == 1
    batch = json.loads(publish[0][publish[0].index('--message') + 1])
    assert len(batch) == 1
    assert json.loads(batch[0].partition('\n')[2])['usage_timing'] == 'request'
    assert not any(a[0] == 'update' for a in calls)
    assert not state_path.exists()
    before = dict(transport.comments)
    # 삭제가 성공한 뒤 재전송된 네이티브 start가 수명 주기 상태를 다시 생성했다.
    state_path.write_text(json.dumps(state)); state_path.chmod(0o600)
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        hook._complete(state_path, 'done', adapter=adapter, source='codex', directory_fd=fd)
    finally:
        os.close(fd)
    assert transport.comments == before
    assert not state_path.exists()

def test_tool_only_legacy_completion_is_supported():
    body = usage_comment(source='codex', model=None, usage={}, tokens={},
                         event_id=usage_event_id('codex', TASK), usage_timing='completion', usage_at=1)
    transport = Transport()
    HermesCliBackend(runner=transport).publish_codex_usage(board='default',task_id=TASK,messages=[body])
    assert len(transport.comments) == 1


@pytest.mark.parametrize("kind", [
    "boundary-legacy", "empty", "earlier-native-empty", "earlier-native-legacy", "legacy-next-turn",
])
def test_native_hook_completes_legacy_and_empty_turns(tmp_path, monkeypatch, kind):
    import os

    from test_codex_measured_usage import boundary, prior_native_turn, rows, write_rows

    from kanban_adapter import claude_hook as hook

    data = rows()
    target = data[:3] + (data[4:] if "legacy" in kind else data[-1:])
    selected = ([data[0], *prior_native_turn(), *target[1:]]
                if kind.startswith("earlier-native") else target)
    if kind == "legacy-next-turn":
        next_total = rows()[4]
        next_total["payload"]["info"]["total_token_usage"]["total_tokens"] = 999999
        selected.extend([
            boundary("task_started", "turn-next", "2026-09-17T01:46:00Z"),
            next_total,
            boundary("task_complete", "turn-next", "2026-09-17T01:46:02Z"),
        ])
    path = tmp_path / "sessions" / "rollout.jsonl"
    write_rows(path, selected)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    state_path = hook._state_path(tmp_path, "session-a")
    state = {"task_id": TASK, "cwd": str(tmp_path), "transcript_path": str(path),
             "lifecycle": {"session": "session-a", "source": "codex", "board": "default",
                           "prompt_id": "turn-a", "idempotency_key": "a" * 64}}
    state_path.write_text(json.dumps(state)); state_path.chmod(0o600)
    transport = Transport()
    calls = []

    def adapter(argv, cwd):
        calls.append(argv)
        if argv[0] == "publish-codex-usage":
            from kanban_adapter.cli import main
            if main(argv, backend=HermesCliBackend(runner=transport)) != 0:
                raise RuntimeError("adapter command failed")
        return ""

    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        hook._complete(state_path, "done", adapter=adapter, source="codex", directory_fd=fd)
    finally:
        os.close(fd)

    if "empty" in kind:
        assert not transport.comments
        assert not any(argv[0] == "publish-codex-usage" for argv in calls)
    else:
        [body] = [comment["body"] for comment in transport.comments.values()]
        payload = json.loads(body.partition("\n")[2])
        assert payload["usage_timing"] == "completion"
        assert payload["tokens"]["total"] == 16092
    assert any(argv[0] == "done" for argv in calls)
    assert not state_path.exists()


@pytest.mark.parametrize("malformed", ["legacy", "native"])
def test_native_hook_rejects_malformed_target_usage_and_retains_state(
    tmp_path, monkeypatch, malformed,
):
    import os

    from test_codex_measured_usage import rows, write_rows
    from kanban_adapter import claude_hook as hook

    data = rows()
    if malformed == "legacy":
        data.pop(3)
        data[3]["payload"]["info"] = "bad"
    else:
        data[3]["payload"]["usage"]["input_tokens"] = True
    path = tmp_path / "sessions" / "rollout.jsonl"
    write_rows(path, data)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    state_path = hook._state_path(tmp_path, "session-a")
    state = {"task_id": TASK, "cwd": str(tmp_path), "transcript_path": str(path),
             "lifecycle": {"session": "session-a", "source": "codex", "board": "default",
                           "prompt_id": "turn-a", "idempotency_key": "a" * 64}}
    state_path.write_text(json.dumps(state)); state_path.chmod(0o600)
    calls = []
    def adapter(argv, cwd):
        calls.append(argv)
        return ""
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(RuntimeError, match="evidence"):
            hook._complete(state_path, "done", adapter=adapter,
                           source="codex", directory_fd=fd)
    finally:
        os.close(fd)

    assert state_path.exists()
    assert not any(argv[0] in {"publish-codex-usage", "done"} for argv in calls)


def test_native_hook_interruption_retains_state(tmp_path, monkeypatch):
    import os

    from test_codex_measured_usage import rows, write_rows

    from kanban_adapter import claude_hook as hook
    from kanban_adapter import token_usage

    path = tmp_path / 'sessions' / 'rollout.jsonl'
    write_rows(path, rows()[:-1])
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    state_path = hook._state_path(tmp_path, 'session-a')
    state = {'task_id': TASK, 'cwd': str(tmp_path), 'transcript_path': str(path),
             'lifecycle': {'session': 'session-a', 'source': 'codex', 'board': 'default',
                           'prompt_id': 'turn-a', 'idempotency_key': 'a' * 64}}
    state_path.write_text(json.dumps(state)); state_path.chmod(0o600)
    monkeypatch.setattr(token_usage, 'wait_for_codex_token_evidence',
                        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    calls = []
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(KeyboardInterrupt):
            hook._complete(state_path, 'done', adapter=lambda argv, cwd: calls.append(argv),
                           source='codex', directory_fd=fd)
    finally:
        os.close(fd)
    assert state_path.exists()
    assert json.loads(state_path.read_text())['result'] == 'done'
    assert calls == []


def test_native_hook_snapshot_interruption_closes_state_identity_once(tmp_path, monkeypatch):
    import os

    from kanban_adapter import claude_hook as hook
    from kanban_adapter import token_usage

    state_path = hook._state_path(tmp_path, "session-a")
    state = {"task_id": TASK, "cwd": str(tmp_path), "result": "done",
             "transcript_path": str(tmp_path / "sessions" / "rollout.jsonl"),
             "lifecycle": {"session": "session-a", "source": "codex", "board": "default",
                           "prompt_id": "turn-a", "idempotency_key": "a" * 64}}

    class Identity:
        closes = 0

        def close(self):
            self.closes += 1

    identity = Identity()
    monkeypatch.setattr(hook, "_read_state", lambda *args, **kwargs: (state, identity))
    monkeypatch.setattr(token_usage, "wait_for_codex_token_evidence",
                        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(KeyboardInterrupt):
            hook._complete(state_path, "done", adapter=lambda *_: "",
                           source="codex", directory_fd=fd)
    finally:
        os.close(fd)

    assert identity.closes == 1


def test_unreserved_historical_requests_fail_closed():
    transport = Transport()
    transport.comments['historical'] = {'author':'kanban-adapter','body':message('a'*16)}
    with pytest.raises(RuntimeError, match='unreserved'):
        HermesCliBackend(runner=transport).publish_codex_usage(board='default',task_id=TASK,messages=[message()])
    assert len(transport.comments) == 1
