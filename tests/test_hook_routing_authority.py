import json
import os
import subprocess
import shutil
import sys
import venv
from pathlib import Path

import pytest
from kanban_adapter import claude_hook as hook
from kanban_adapter.backend import HermesCliBackend

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("source", ["claude-code", "codex"])
@pytest.mark.parametrize("receipt_kind", ["malformed", "wrong-board"])
def test_mapping_change_after_capture_failure_never_routes_other_board(tmp_path, monkeypatch, source, receipt_kind):
    mapping = ['original']
    monkeypatch.setattr(HermesCliBackend, 'resolve_board', lambda self, **kw: mapping[0])
    runtime = tmp_path / 'runtime.json'
    runtime.write_text('{}')
    runtime.chmod(0o600)
    monkeypatch.setenv('UNIFIED_KANBAN_CONVERSATION_CONFIG', str(runtime))
    calls = []
    def adapter(argv, cwd):
        calls.append(argv)
        if argv[0] == 'start':
            fd = int(next(a.split('=', 1)[1] for a in argv if a.startswith('--conversation-receipt-fd=')))
            os.write(fd, b'{' if receipt_kind == 'malformed' else json.dumps({'board': 'wrong', 'task': 't_same'}).encode())
            return 't_same'
        assert argv[argv.index('--board') + 1] == 'original'
        return ''
    payload = dict(session_id='session', cwd=str(tmp_path), prompt='hello')
    cache = tmp_path / 'cache'
    with pytest.raises(ValueError if receipt_kind == "malformed" else RuntimeError):
        hook.handle_event('prompt', payload, adapter=adapter, cache_dir=cache, source=source)
    assert any(a[0] == 'start' for a in calls)
    mapping[0] = 'wrong'
    hook.handle_event('stop', payload, adapter=adapter, cache_dir=cache, source=source)
    assert all(a[a.index('--board') + 1] == 'original' for a in calls)


def test_legacy_state_cannot_reroute(tmp_path):
    cache = tmp_path / 'cache'
    cache.mkdir(mode=0o700)
    state = hook._state_path(cache, 'session')
    state.write_text(json.dumps({'task_id': 't_same', 'cwd': str(tmp_path)}))
    state.chmod(0o600)
    calls = []
    with pytest.raises(RuntimeError):
        hook.handle_event('stop', {'session_id': 'session'}, adapter=lambda *args: calls.append(args), cache_dir=cache)
    assert not calls
    assert state.exists()


@pytest.mark.parametrize('args', [[], ['UNKNOWN_SECRET'], ['prompt', 'extra']])
def test_shell_usage_error_precedes_interpreter(tmp_path, args):
    """잘못된 호출은 인터프리터 실행 없이 사용법 오류 2를 반환한다."""
    marker = tmp_path / 'invoked'
    shim = tmp_path / 'python3'
    shim.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 42\n')
    shim.chmod(0o700)
    env = dict(os.environ, PATH=str(tmp_path) + ':' + os.environ['PATH'])
    result = subprocess.run([str(ROOT / 'bin/claude-kanban-hook'), *args], text=True, capture_output=True, env=env)
    assert result.returncode == 2
    assert not marker.exists()
    assert result.stdout == ''
    assert 'usage:' in result.stderr
    assert 'UNKNOWN_SECRET' not in result.stderr


@pytest.mark.parametrize('event', ['prompt', 'stop', 'session-end', 'post-tool-use', 'subagent-start'])
@pytest.mark.parametrize('damage', ['interpreter', 'import'])
def test_valid_event_failure_invokes_once_and_redacts(tmp_path, reviewed_release, event, damage):
    """유효한 이벤트의 시작 장애는 한 번만 실행하고 비밀 없이 계속한다."""
    repo = tmp_path / 'repo'
    (repo / 'bin').mkdir(parents=True)
    script = repo / 'bin/claude-kanban-hook'
    shutil.copy2(ROOT / 'bin/claude-kanban-hook', script)
    package = repo / 'src/kanban_adapter'
    shutil.copytree(ROOT / 'src/kanban_adapter', package)
    shutil.copytree(ROOT / 'patches', repo / 'patches', ignore=shutil.ignore_patterns('*.bundle'))
    from kanban_adapter.compatibility import read_supported_upstream, read_carried_commits
    agent = tmp_path / 'agent'
    agent.mkdir()
    layout = reviewed_release(agent, read_supported_upstream(), read_carried_commits()[-1])
    venv.EnvBuilder(with_pip=False).create(layout.release / 'venv')
    marker = tmp_path / 'calls'
    selected = layout.release / 'venv/bin/python'
    if damage == 'interpreter':
        # 버전 검사는 통과하고 실제 본문 진입만 한 번 실패하게 한다.
        selected.unlink()
        selected.write_text(
            '#!/bin/sh\n'
            f'if [ "$3" = "import sys; sys.exit(sys.version_info < (3, 11))" ]; then exec "{sys.executable}" "$@"; fi\n'
            f'printf x >> "{marker}"\nprintf SECRET_CANARY >&2\nexit 42\n'
        )
        selected.chmod(0o700)
    else:
        # 시스템 부트스트랩은 유지하고 선택된 런타임의 import만 손상시킨다.
        (package / '__init__.py').write_text(
            'import sys\nfrom pathlib import Path\n'
            f'if sys.executable == {str(selected)!r}:\n'
            f'    with Path({str(marker)!r}).open("a") as stream: stream.write("x")\n'
            '    raise ImportError("SECRET_CANARY")\n'
        )
    poison = tmp_path / 'python3'
    poison.write_text('#!/bin/sh\nprintf SECRET_PATH >&2\nexit 93\n')
    poison.chmod(0o700)
    env = dict(os.environ, HERMES_AGENT_REPO=str(agent), HOME=str(tmp_path),
               PATH=str(tmp_path) + ':' + os.environ['PATH'])
    result = subprocess.run([str(script), event], input='SECRET_INPUT', text=True, capture_output=True, env=env)
    assert result.returncode == 0
    assert marker.read_text() == 'x'
    assert 'interpreter-failed' in json.loads(result.stdout)['systemMessage']
    assert 'details redacted' in result.stderr
    assert 'SECRET' not in result.stdout + result.stderr


@pytest.mark.parametrize('damage', ['home', 'interpreter'])
def test_shell_startup_failure_is_redacted_nonblocking(tmp_path, damage):
    env = dict(os.environ, HOME=str(tmp_path), PYTHONHOME='/SECRET_CANARY', PYTHONPATH='/SECRET_CANARY')
    if damage == 'interpreter':
        shim = tmp_path / 'python3'
        shim.write_text('#!/bin/sh\nprintf SECRET_CANARY >&2\nexit 42\n')
        shim.chmod(0o700)
        env['PATH'] = str(tmp_path) + ':' + env['PATH']
    result = subprocess.run([str(ROOT / 'bin/claude-kanban-hook'), 'prompt'], input='SECRET_INPUT', text=True, capture_output=True, env=env)
    assert result.returncode == 0
    assert 'systemMessage' in result.stdout
    assert 'SECRET' not in result.stdout + result.stderr
