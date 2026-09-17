"""작업자 런타임 선택은 격리된 fixture에서만 검증한다."""
import json
import os
from pathlib import Path
import subprocess
import sys
import venv
from types import SimpleNamespace

import pytest

from kanban_adapter import codex_pending_final as queue, compatibility, conversation_runtime
from kanban_adapter.release_layout import release_directory, release_selector, COMPLETION_RECEIPT_NAME


@pytest.fixture
def selected(tmp_path, monkeypatch):
    repo = tmp_path / 'agent'
    release = release_directory(repo, compatibility.read_carried_commits()[-1])
    release.mkdir(parents=True)
    venv.EnvBuilder(with_pip=False).create(release / 'venv')
    launcher = release / 'venv/bin/hermes'
    launcher.write_text('#!/bin/sh\nexit 1\n')
    launcher.chmod(0o700)
    info = release.stat()
    (release / COMPLETION_RECEIPT_NAME).write_text(json.dumps({
        'version': 2, 'upstream': compatibility.read_supported_upstream(),
        'carried': compatibility.read_carried_commits()[-1],
        'release_identity': [info.st_dev, info.st_ino],
    }))
    release_selector(repo).write_text(str(release) + '\n')
    monkeypatch.setenv('HERMES_AGENT_REPO', str(repo))
    return repo, release


def test_main_selects_verified_interpreter_before_runtime(selected, tmp_path, monkeypatch):
    repo, release = selected
    # 실제 위임 표시를 변경하지 않고 main의 환경 조회만 fixture로 격리한다.
    captured = {}
    def execv(executable, argv):
        captured.update(executable=executable, argv=argv)
        raise SystemExit(23)
    monkeypatch.setattr(queue, 'os', SimpleNamespace(environ={'HERMES_AGENT_REPO': str(repo)}, execv=execv))
    monkeypatch.setattr(sys, 'argv', ['worker', str(tmp_path / 'job.json')])
    monkeypatch.setattr(conversation_runtime, 'get_conversation_service', lambda: pytest.fail('runtime before verified interpreter'))
    with pytest.raises(SystemExit, match='23'):
        queue.main()
    assert captured['executable'] == str(release / 'venv/bin/python')
    assert captured['argv'][1:3] == ['-I', '-c']
    assert str(Path(queue.__file__).resolve().parent.parent) in captured['argv']
    assert captured['argv'][-1] == '--selected-runtime'

    # 재실행 명령의 진짜 인터프리터/import 경로만 probe한다. 작업자는 실행하지 않는다.
    site = next((release / 'venv/lib').glob('python*/site-packages'))
    package = site / 'hermes_cli'
    package.mkdir()
    (package / '__init__.py').write_text('')
    probe = 'import sys,os,json,hermes_cli; print(json.dumps([sys.prefix,hermes_cli.__file__,os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"),os.environ.get("HERMES_KANBAN_TASK")]))'
    argv = captured['argv'][:]
    argv[3] = probe
    poison = tmp_path / 'poison'
    poison.mkdir()
    (poison / 'hermes_cli.py').write_text('raise RuntimeError("ambient import")\n')
    monkeypatch.setenv('PYTHONPATH', str(poison))
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    prefix, origin, delegated, task = json.loads(result.stdout)
    assert prefix == str(release / 'venv')
    assert origin == str(package / '__init__.py')
    assert delegated == os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')
    assert task == os.environ.get('HERMES_KANBAN_TASK')


@pytest.mark.parametrize('guard', ['HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_KANBAN_TASK'])
@pytest.mark.parametrize('stage', [[], ['--selected-runtime']])
def test_guards_precede_compatibility_and_runtime(tmp_path, monkeypatch, guard, stage):
    facade = SimpleNamespace(**vars(os))
    facade.environ = {guard: 'fixture-denied'}
    monkeypatch.setattr(queue, 'os', facade)
    monkeypatch.setattr(sys, 'argv', ['worker', str(tmp_path / 'job')] + stage)
    monkeypatch.setattr(compatibility, 'check_hermes_compatibility', lambda **kw: pytest.fail('guard bypass'))
    monkeypatch.setattr(conversation_runtime, 'get_conversation_service', lambda: pytest.fail('runtime bypass'))
    assert queue.main() == 1
    assert facade.environ == {guard: 'fixture-denied'}


@pytest.mark.parametrize('invalid', ['selector', 'receipt', 'runtime'])
def test_unverified_authority_never_enters_runtime(selected, tmp_path, monkeypatch, invalid):
    repo, release = selected
    if invalid == 'selector':
        release_selector(repo).write_text(str(tmp_path / 'foreign'))
    elif invalid == 'receipt':
        (release / COMPLETION_RECEIPT_NAME).write_text('{}')
    facade = SimpleNamespace(**vars(os))
    facade.environ = {'HERMES_AGENT_REPO': str(repo)}
    facade.execv = lambda *a: pytest.fail('unverified exec')
    monkeypatch.setattr(queue, 'os', facade)
    stage = ['--selected-runtime'] if invalid == 'runtime' else []
    monkeypatch.setattr(sys, 'argv', ['worker', str(tmp_path / 'job')] + stage)
    monkeypatch.setattr(conversation_runtime, 'get_conversation_service', lambda: pytest.fail('unverified runtime'))
    assert queue.main() == 1


@pytest.mark.parametrize('persistent', [False, True])
def test_real_nonblocking_contention_retries_without_renewing_budget(tmp_path, monkeypatch, persistent):
    import time
    from test_codex_pending_final import prepared_job
    from test_codex_authenticated_prepare import message
    from test_codex_file_provenance import marker
    source, service, kwargs, job = prepared_job(tmp_path)
    initial = json.loads(job.read_bytes())
    with source.open('ab') as stream:
        stream.write(message('user', 43) + message('assistant', 44) + marker('task_complete', ordinal=45))
    # main만 격리한다. 실제 환경/production authority에는 손대지 않는다.
    facade = SimpleNamespace(**vars(os))
    facade.environ = {}
    monkeypatch.setattr(queue, 'os', facade)
    monkeypatch.setattr(sys, 'argv', ['worker', str(job), '--selected-runtime'])
    monkeypatch.setattr(compatibility, 'check_hermes_compatibility', lambda **kw: (True, 'fixture'))
    monkeypatch.setattr(conversation_runtime, 'get_conversation_service', lambda: service)
    elapsed = [0]
    sleeps = []
    monkeypatch.setattr(time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(service, 'clock_ns', lambda: initial['expires_ns'] - queue.TTL_NS)
    lock = queue._locked(job)
    lock.__enter__()
    held = [True]
    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds
        if not persistent and held[0]:
            lock.__exit__(None, None, None)
            held[0] = False
    monkeypatch.setattr(time, 'sleep', sleep)
    try:
        result = queue.main()
    finally:
        if held[0]:
            lock.__exit__(None, None, None)
    assert sleeps, 'contention must not discard the worker immediately'
    assert elapsed[0] <= queue.TTL_NS / 1e9
    assert len(sleeps) <= queue.MAX_ATTEMPTS
    body = json.loads(job.read_bytes())
    assert body['expires_ns'] == initial['expires_ns']
    if persistent:
        assert result == 1
        assert body == initial
    else:
        assert result == 0
        assert body['status'] == 'ready'
        assert body['attempts'] == 1
        page = service.get_parent_page(principal_id='owner', board='demo', task='task', cursor=None, limit=20)
        assert [e['text'] for e in page['events']] == ['public', 'public']
