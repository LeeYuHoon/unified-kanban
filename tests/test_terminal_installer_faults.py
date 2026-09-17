"""비공개 테스트 데이터만으로 호출자 수준의 영수증 게시와 충돌 종료 회귀를 검증한다."""
import os
from types import SimpleNamespace
import pytest
from test_terminal_launcher_installer import modules, run, RECEIPT
from test_conversation_launch import fixture_launch
from kanban_adapter import conversation_namespace as ns, conversation_launch as launch


def fixture(tmp_path, action='install'):
    _, _, _, env = fixture_launch(tmp_path)
    prefix = tmp_path / 'bin'
    prefix.mkdir()
    if action == 'uninstall':
        assert run(prefix, env).returncode == 0
    args = SimpleNamespace(prefix=str(prefix), profile_home=env['HERMES_HOME'], provider='codex', action=action, dry_run=False)
    return prefix, env, args


@pytest.mark.parametrize('nth', [1, 2])
def test_receipt_publication_fsync_no_stale_canonical(tmp_path, monkeypatch, nth):
    prefix, env, args = fixture(tmp_path)
    m, links, tx = modules()
    link, sync = os.link, os.fsync
    published = False
    calls = 0
    def publish(source, target, **kwargs):
        nonlocal published
        result = link(source, target, **kwargs)
        if target == RECEIPT:
            published = True
        return result
    def fail(fd):
        nonlocal calls
        if published:
            calls += 1
            if calls == nth:
                raise OSError('receipt-publication-fsync')
        return sync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'link', publish)
        patch.setattr(os, 'fsync', fail)
        with pytest.raises(OSError, match='receipt-publication-fsync'):
            m.operate(args, ns, launch, links, tx)
    assert published
    assert not (prefix / RECEIPT).exists(), sorted(p.name for p in prefix.iterdir())
    assert not (prefix / m.NAME).is_symlink()
    assert run(prefix, env).returncode == 0


@pytest.mark.parametrize('residue', ['..collected-native-agent.receipt.json.dead', '..collected-native-agent.receipt.json.retired.dead'])
@pytest.mark.parametrize('action', ['install', 'uninstall'])
def test_unknown_receipt_siblings_refuse_unchanged(tmp_path, residue, action):
    prefix, env, args = fixture(tmp_path)
    foreign = prefix / residue
    foreign.write_text('foreign evidence')
    before = foreign.stat().st_ino
    result = run(prefix, env, action)
    assert result.returncode == 2
    assert foreign.stat().st_ino == before
    assert foreign.read_text() == 'foreign evidence'


def test_public_partial_failure_diagnostics(tmp_path, monkeypatch, capsys):
    import json
    prefix, env, args = fixture(tmp_path, 'uninstall')
    m, links, tx = modules()
    original = os.fsync
    calls = 0
    def fail(fd):
        nonlocal calls
        calls += 1
        if calls == 8:
            raise OSError('last-unlink-fsync')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fail)
    monkeypatch.setattr(m, 'dependencies', lambda: (ns, launch, links, tx))
    assert m.main(['uninstall', '--prefix', str(prefix), '--profile-home', env['HERMES_HOME'], '--provider', 'codex']) == 2
    report = json.loads(capsys.readouterr().err)
    assert report['error']['message'] == 'last-unlink-fsync'
    assert report['partial'] is True
    assert report['artifacts']
    assert any(x['live'] == 'absent' and x['namespace_durability'] == 'uncertain' for x in report['artifacts'])


@pytest.mark.parametrize('action', ['install', 'uninstall'])
@pytest.mark.parametrize('nth', range(1, 9))
def test_real_fsync_matrix(tmp_path, monkeypatch, action, nth):
    prefix, env, args = fixture(tmp_path, action)
    m, links, tx = modules()
    original = os.fsync
    calls = 0
    def fail(fd):
        nonlocal calls
        calls += 1
        if calls == nth:
            raise OSError('matrix-fsync-' + str(nth))
        return original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', fail)
        with pytest.raises(OSError, match='matrix-fsync-' + str(nth)) as caught:
            m.operate(args, ns, launch, links, tx)
    assert calls >= nth
    report = caught.value.artifacts
    by_name = {os.path.basename(x['path']): x for x in report}
    for path in prefix.iterdir():
        assert by_name[path.name]['live'] == 'present'
        assert by_name[path.name]['identity'][1] == path.lstat().st_ino
    for artifact in report:
        assert artifact['live'] == ('present' if os.path.lexists(artifact['path']) else 'absent')
    if (prefix / RECEIPT).exists():
        assert (prefix / m.NAME).is_symlink()


@pytest.mark.parametrize('boundary', ['before-receipt', 'temp-receipt', 'published-receipt'])
def test_real_process_crash_residue(tmp_path, boundary):
    import subprocess, sys
    from test_terminal_launcher_installer import ROOT
    prefix, env, args = fixture(tmp_path)
    child = """
import sys, os
from types import SimpleNamespace
sys.path[:0] = [ROOT + '/tests', ROOT + '/src']
from test_terminal_launcher_installer import modules
from kanban_adapter import conversation_namespace as ns, conversation_launch as launch
m, links, tx = modules()
original = os.link
if BOUNDARY == 'before-receipt':
    m._write_receipt = lambda *a: os._exit(77)
else:
    def crash(source, target, **kw):
        if target == m.RECEIPT:
            if BOUNDARY == 'published-receipt':
                original(source, target, **kw)
            os._exit(78)
        return original(source, target, **kw)
    os.link = crash
m.operate(SimpleNamespace(**ARGS), ns, launch, links, tx)
"""
    code = 'ROOT=' + repr(str(ROOT)) + '\nBOUNDARY=' + repr(boundary) + '\nARGS=' + repr(vars(args)) + '\n' + child
    result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True)
    assert result.returncode == (77 if boundary == 'before-receipt' else 78), result.stderr
    before = {p.name: p.lstat().st_ino for p in prefix.iterdir()}
    assert run(prefix, env).returncode == (0 if boundary == 'before-receipt' else 2)
    assert run(prefix, env, 'uninstall').returncode == 2
    assert before == {p.name: p.lstat().st_ino for p in prefix.iterdir()}


def test_composed_fsync_preserves_primary_and_retained_state(tmp_path, monkeypatch):
    prefix, env, args = fixture(tmp_path, 'uninstall')
    m, links, tx = modules()
    original = os.fsync
    count = 0
    def fail(fd):
        nonlocal count
        count += 1
        if count >= 2:
            raise OSError('primary' if count == 2 else 'secondary')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError, match='primary') as caught:
        m.operate(args, ns, launch, links, tx)
    assert caught.value.cleanup_errors
    assert any(x['namespace_durability'] == 'uncertain' for x in caught.value.artifacts)


@pytest.mark.parametrize('boundary', ['receipt', 'link', 'uninstall'])
def test_non_fsync_primary_survives_cleanup_fsync(tmp_path, monkeypatch, capsys, boundary):
    import json
    prefix, env, args = fixture(tmp_path, 'uninstall' if boundary == 'uninstall' else 'install')
    m, links, tx = modules()
    original_link, original_sync = os.link, os.fsync
    primary = OSError('PRIMARY publication I/O')
    fired = False
    cleanup = []
    def publish(source, target, **kwargs):
        nonlocal fired
        if target == (RECEIPT if boundary == 'receipt' else m.NAME) and not fired:
            fired = True
            raise primary
        return original_link(source, target, **kwargs)
    def discard(*args):
        nonlocal fired
        fired = True
        raise primary
    def sync(fd):
        if fired:
            error = OSError('SECONDARY compensation fsync ' + str(len(cleanup)))
            cleanup.append(error)
            raise error
        return original_sync(fd)
    monkeypatch.setattr(os, 'link', publish)
    monkeypatch.setattr(os, 'fsync', sync)
    if boundary == 'uninstall':
        monkeypatch.setattr(tx, '_discard_operation_receipt', discard)
    monkeypatch.setattr(m, 'dependencies', lambda: (ns, launch, links, tx))
    assert m.main([args.action, '--prefix', str(prefix), '--profile-home', env['HERMES_HOME'], '--provider', 'codex']) == 2
    report = json.loads(capsys.readouterr().err)
    assert fired and cleanup
    assert report['error'] == {'type': 'OSError', 'message': str(primary)}
    assert report['cleanup_errors'] == ['OSError: ' + str(error) for error in cleanup]
    assert report['partial'] is True
    assert report['artifacts']
    for path in prefix.iterdir():
        assert any(item['path'] == str(path) and item['identity'][1] == path.lstat().st_ino for item in report['artifacts'])
    import traceback
    assert any(frame.name in {'publish', 'discard'} for frame in traceback.extract_tb(primary.__traceback__))


@pytest.mark.parametrize('operation_failure', [False, True])
def test_final_descriptor_cleanup_preserves_primary(tmp_path, monkeypatch, operation_failure):
    prefix, env, args = fixture(tmp_path)
    m, links, tx = modules()
    opened, close = ns.open_directory, os.close
    outer = None
    primary = OSError('operation failed')
    cleanup = OSError('descriptor cleanup failed')
    def open_directory(path):
        nonlocal outer
        fd = opened(path)
        if path == prefix:
            outer = fd
        return fd
    def close_directory(fd):
        close(fd)
        if fd == outer:
            raise cleanup
    def install(*args):
        raise primary
    monkeypatch.setattr(ns, 'open_directory', open_directory)
    monkeypatch.setattr(os, 'close', close_directory)
    if operation_failure:
        monkeypatch.setattr(m, '_install', install)
    with pytest.raises(OSError) as caught:
        m.operate(args, ns, launch, links, tx)
    assert caught.value is (primary if operation_failure else cleanup)
    if operation_failure:
        assert caught.value.cleanup_errors == ['OSError: descriptor cleanup failed']
        assert caught.value.artifacts


def test_fsync_failure_does_not_adopt_callers_handled_exception(tmp_path, monkeypatch):
    prefix, env, args = fixture(tmp_path)
    m, links, tx = modules()
    failure = OSError('operation fsync')
    def fail(fd):
        raise failure
    monkeypatch.setattr(os, 'fsync', fail)
    try:
        raise ValueError('unrelated handled caller error')
    except ValueError:
        with pytest.raises(OSError) as caught:
            m.operate(args, ns, launch, links, tx)
    assert caught.value is failure


def test_receipt_successor_during_failing_fsync_is_not_deleted(tmp_path, monkeypatch):
    prefix, env, args = fixture(tmp_path)
    m, links, tx = modules()
    original = os.fsync
    swapped = False
    identity = None
    def fail(fd):
        nonlocal swapped, identity
        receipt = prefix / RECEIPT
        if receipt.exists() and not swapped:
            swapped = True
            receipt.rename(prefix / 'foreign-retained-original')
            receipt.write_text('foreign receipt')
            identity = receipt.stat().st_ino
            raise OSError('receipt-successor-fsync')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError, match='receipt-successor-fsync'):
        m.operate(args, ns, launch, links, tx)
    assert swapped
    assert (prefix / RECEIPT).stat().st_ino == identity
    assert (prefix / RECEIPT).read_text() == 'foreign receipt'
    assert (prefix / m.NAME).is_symlink()
