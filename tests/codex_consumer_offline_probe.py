"""검토한 Hermes 인터프리터와 PYTHONPATH의 소스로 실행하며 임시 DB만 사용한다."""
import json
import pytest
import os
import tempfile
from pathlib import Path
from kanban_adapter.backend import HermesCliBackend
from kanban_adapter.usage import usage_comment, usage_event_id
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from plugins.kanban.dashboard import plugin_api as api, daily_query as daily


def run():
    with tempfile.TemporaryDirectory() as root:
        os.environ['HERMES_HOME'] = root
        db = Path(root) / 'fixture.db'
        os.environ['HERMES_KANBAN_DB'] = str(db)
        os.environ['HERMES_KANBAN_BOARD'] = 'default'
        conn = connect(db)
        task = kb.create_task(conn, title='offline', observation=True, created_by='kanban-adapter', tenant='codex')
        def msg(rh=None, at=1789657199):
            return usage_comment(source='codex', model='fixture-model', usage={},
                tokens={'input':10,'output':2,'total':12,'requests':1},
                event_id=usage_event_id('codex', task, rh), request_hash=rh,
                usage_at=at, usage_timing='request' if rh else 'completion')
        failed = [False]
        def runner(argv):
            if 'show' in argv:
                return json.dumps({'task': {'id':task}, 'comments': [dict(r) for r in conn.execute('SELECT author,body FROM task_comments WHERE task_id=?',(task,))]})
            key = next(a.split('=',1)[1] for a in argv if a.startswith('--idempotency-key='))
            kb.add_comment(conn, task, 'kanban-adapter', argv[-1], idempotency_key=key)
            if key == usage_event_id('codex', task, 'a'*16) and not failed[0]:
                failed[0] = True
                raise RuntimeError('after remote commit')
            return '1'
        backend = HermesCliBackend(runner=runner)
        batch = [msg('a'*16), msg('b'*16,1789657200)]
        try:
            backend.publish_codex_usage(board='default',task_id=task,messages=batch)
        except RuntimeError as exc:
            assert str(exc) == 'after remote commit'
        else:
            raise AssertionError('fault not exercised')
        # 로컬 마커가 없어도 재전송된 배치가 다르면 영구 저장된 바이트를 바꿀 수 없다.
        HermesCliBackend(runner=runner).publish_codex_usage(board='default',task_id=task,messages=[msg('c'*16)])
        # 구형 생산자의 합계는 예약과 충돌해야 하며 토큰 이벤트로 저장되면 안 된다.
        kb.add_comment(conn, task, 'kanban-adapter', msg(), idempotency_key=usage_event_id('codex',task))
        totals, board = api._token_usage_rollup(conn,[task])
        assert totals[task]['tokens']['total'] == board['tokens']['total'] == 24
        result = daily.query(board='default',period='custom',start='2026-09-17',end='2026-09-18',timezone='Asia/Seoul',basis='execution',scope='current',q='',source='all',status='all',day=None,limit=100,offset=0,parse_event=api._parse_token_comment,reasoning_coverage=api._reasoning_coverage)
        assert {d['date']:d['token_usage']['tokens']['total'] for d in result['days']} == {'2026-09-17':12,'2026-09-18':12}, result
        assert result['summary']['token_usage']['tokens']['total'] == 24
        assert result['summary']['completion_attributed']['event_count'] == 0
        assert conn.execute('SELECT count(*) FROM task_comments').fetchone()[0] == 3
        task = kb.create_task(conn, title='legacy', observation=True, created_by='kanban-adapter', tenant='codex')
        backend.publish_codex_usage(board='default',task_id=task,messages=[msg()])
        backend.publish_codex_usage(board='default',task_id=task,messages=[msg('d'*16)])
        assert api._token_usage_rollup(conn,[task])[1]['tokens']['total'] == 12
        result = daily.query(board='default',period='custom',start='2026-09-17',end='2026-09-18',timezone='Asia/Seoul',basis='execution',scope='current',q='',source='all',status='all',day=None,limit=100,offset=0,parse_event=api._parse_token_comment,reasoning_coverage=api._reasoning_coverage)
        assert result['summary']['token_usage']['tokens']['total'] == 36
        assert result['summary']['completion_attributed']['tokens']['total'] == 12
        assert {d['date']:d['token_usage']['tokens']['total'] for d in result['days']} == {'2026-09-17':24,'2026-09-18':12}
        print('PASS legacy-first remains completion-only in actual cumulative and daily consumers')
        print('PASS real add_comment + cumulative rollup + daily.query/Reader: partial retry, remote-success failure, frozen batch, old completion exclusion, KST midnight 12/12, total 24')
        print('PRODUCER', __import__('kanban_adapter.backend',fromlist=['x']).__file__)
        print('CONSUMER', api.__file__)
        conn.close()

@pytest.mark.parametrize('lose_state', [False, True])
def test_public_native_late_final_stop_and_replay(tmp_path, monkeypatch, lose_state):
    """공개 네이티브 훅과 두 CLI 파서, 실제 DB와 소비자를 사용하되 공급자는 호출하지 않는다."""
    import argparse
    import contextlib
    import io
    from hermes_cli import kanban
    from kanban_adapter import claude_hook as hook, codex_hook, cli
    from test_codex_measured_usage import rows, write_rows

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'fixture.db'))
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'default')
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    monkeypatch.delenv('UNIFIED_KANBAN_CONVERSATION_CONFIG', raising=False)
    cache = tmp_path / 'cache'
    path = tmp_path / 'sessions' / 'rollout.jsonl'
    write_rows(path, rows()[:-1])
    conn = connect(tmp_path / 'fixture.db')
    calls = []
    fail_complete = [lose_state]

    def runner(argv):
        assert argv[:2] == ['hermes', 'kanban']
        if 'complete' in argv and fail_complete[0]:
            fail_complete[0] = False
            raise RuntimeError('injected failure before completion')
        parser = argparse.ArgumentParser()
        kanban.build_parser(parser.add_subparsers(dest='command'))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = kanban.kanban_command(parser.parse_args(argv[1:]))
        assert rc in (None, 0), output.getvalue()
        return output.getvalue()

    backend = HermesCliBackend(runner=runner)
    def adapter(argv, cwd):
        calls.append(argv[0])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli.main(argv, backend=backend)
        if rc:
            raise RuntimeError('offline adapter returned failure')
        return output.getvalue()

    # 오프라인 전송과 보드 탐색만 주입한다. 공개 핸들러, 수명 주기의 권위 있는 상태,
    # 게시, 실제 CLI 변경 작업과 소비자는 모두 실제 구현을 사용한다.
    monkeypatch.setattr(hook.HermesCliBackend, 'resolve_board', lambda self, **kw: 'default')
    monkeypatch.setattr(codex_hook, 'cache_dir_for', lambda source: cache)
    monkeypatch.setattr(codex_hook, 'handle_event',
        lambda event, payload, **kw: hook.handle_event(event, payload, adapter=adapter, **kw))
    payload = dict(session_id='session-a', turn_id='turn-a', cwd=str(tmp_path),
                   prompt='offline measured request', model='fixture-model',
                   transcript_path=str(path), last_assistant_message='fixture final')
    def emit(event, **changes):
        assert codex_hook.main([event], stdin=io.StringIO(json.dumps(dict(payload, **changes)))) == 0

    try:
        emit('prompt')
        state_path = hook._state_path(cache, 'session-a')
        state = json.loads(state_path.read_text())
        task = state['task_id']
        before = state_path.read_bytes()
        emit('stop', turn_id='foreign')
        assert state_path.read_bytes() == before
        emit('stop')
        assert state_path.exists(), 'late-final Stop must retain lifecycle'
        assert 'done' not in calls
        assert conn.execute('SELECT count(*) FROM task_comments').fetchone()[0] == 0
        write_rows(path, rows())
        emit('stop')
        if lose_state:
            assert state_path.exists()
            frozen = [tuple(row) for row in conn.execute('SELECT task_id,author,body FROM task_comments')]
            assert len(frozen) == 2
            state_path.unlink()  # 원격의 권위 있는 상태는 남기고 로컬 수명 주기 상태만 잃은 상황을 재현한다.
            emit('prompt')
            assert json.loads(state_path.read_text())['task_id'] == task
            emit('stop')
            assert [tuple(row) for row in conn.execute('SELECT task_id,author,body FROM task_comments')] == frozen
        assert not state_path.exists()
        assert conn.execute('SELECT status FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'done'
        comments = [tuple(row) for row in conn.execute('SELECT task_id,author,body FROM task_comments')]
        emit('prompt')
        # start에서 실행 중 관측의 권위를 검증하는 보호 로직은 이미 완료된 행을 거부한다.
        # 재전송으로 상태가 다시 생성되거나 해당 카드가 다시 열리면 안 된다.
        assert not state_path.exists()
        emit('stop')
        assert not state_path.exists()
        assert [tuple(row) for row in conn.execute('SELECT task_id,author,body FROM task_comments')] == comments
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
        assert api._token_usage_rollup(conn, [task])[1]['tokens']['total'] == 16092
        result = daily.query(board='default',period='custom',start='2026-09-17',end='2026-09-18',timezone='Asia/Seoul',basis='execution',scope='current',q='',source='all',status='all',day=None,limit=100,offset=0,parse_event=api._parse_token_comment,reasoning_coverage=api._reasoning_coverage)
        assert result['summary']['token_usage']['tokens']['total'] == 16092
        assert result['summary']['completion_attributed']['event_count'] == 0
        print('PASS public native prompt/foreign stop/early stop/late final/stop/state deletion/prompt replay/stop: actual CLI backend DB and daily consumer')
    finally:
        conn.close()


def test_actual_consumer():
    run()

if __name__ == '__main__':
    run()
