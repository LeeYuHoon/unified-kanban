"""생산자 시작 경계를 검증하며 픽스처는 실제 설치나 운영 상태를 변경하지 않는다."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from test_hermes_release_manager import load_helper
from tui_completed_fixture import completed_layout, complete_tui


def fixture(h, tmp_path):
    checkout = tmp_path/'agent'
    checkout.mkdir()
    layout = completed_layout(h, checkout, tmp_path/'source')
    complete_tui(h, layout)
    layout.selector.write_bytes(h.selector_payload(layout))
    return layout


@pytest.mark.parametrize('phase', ['validation', 'acl', 'body', 'read', 'postvalidation', 'normal'])
def test_checked_fd_cleanup_preserves_primary(tmp_path, monkeypatch, phase):
    h = load_helper()
    path = tmp_path/'input'
    path.write_bytes(b'payload')
    primary = RuntimeError('operation primary')
    secondary = OSError('secondary close')
    original_close, original_fstat = h.os.close, h.os.fstat
    closed = []
    trace = []
    calls = 0
    def fail():
        try:
            raise primary
        except BaseException as error:
            trace.append(error.__traceback__)
            raise
    def fstat(fd):
        nonlocal calls
        calls += 1
        if phase == 'validation' or (phase == 'postvalidation' and calls == 2):
            fail()
        return original_fstat(fd)
    def acl(fd):
        if phase == 'acl':
            fail()
        return ''
    def close(fd):
        closed.append(fd)
        original_close(fd)  # close 이후 오류이므로 재시도는 안전하지 않다.
        raise secondary
    monkeypatch.setattr(h.os, 'fstat', fstat)
    monkeypatch.setattr(h, '_tui_acl_text', acl)
    monkeypatch.setattr(h.os, 'close', close)
    if phase == 'read':
        class Stream:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, *args): fail()
        monkeypatch.setattr(h.os, 'fdopen', lambda *a, **k: Stream())
    with pytest.raises(BaseException) as raised:
        if phase == 'read':
            h._read_tui_bytes(path)
        else:
            with h._checked_tui_fd(path):
                if phase == 'body': fail()
    assert len(closed) == 1
    assert raised.value is (secondary if phase == 'normal' else primary)
    if phase != 'normal':
        assert any('secondary close' in n for n in primary.__notes__)
        tb = primary.__traceback__
        frames = []
        while tb:
            frames.append(tb)
            tb = tb.tb_next
        assert trace[0] in frames
        assert sum(t.tb_frame.f_code.co_name == '_checked_tui_fd' for t in frames) <= 1


@pytest.mark.parametrize('body_failure', [True, False])
def test_nested_checked_fds_attempt_every_close(tmp_path, monkeypatch, body_failure):
    h = load_helper()
    path = tmp_path/'input'
    path.write_bytes(b'payload')
    primary = RuntimeError('body primary')
    original_close = h.os.close
    entered, closed, errors = [], [], []
    def close(fd):
        closed.append(fd)
        original_close(fd)
        error = OSError('close ' + str(fd))
        errors.append(error)
        raise error
    monkeypatch.setattr(h.os, 'close', close)
    with pytest.raises(BaseException) as raised:
        with h._checked_tui_fd(path) as outer, h._checked_tui_fd(path) as inner:
            entered.extend([outer, inner])
            if body_failure:
                raise primary
    assert closed == list(reversed(entered))
    assert len(set(closed)) == 2
    assert raised.value is (primary if body_failure else errors[0])
    assert len(raised.value.__notes__) == (2 if body_failure else 1)


def test_embedded_fd_helpers_are_exact(tmp_path):
    import ast
    import inspect
    import shlex
    h = load_helper()
    layout = fixture(h, tmp_path)
    payload = h.launcher_payload(layout, 'absent').decode()
    words = shlex.split(payload)
    code = words[words.index('-c') + 1]
    tree = ast.parse(code)
    for name in ('_tui_acl_text', '_checked_tui_fd', '_read_tui_bytes', '_validate_real_directory_ancestry'):
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        source = inspect.getsource(getattr(h, name)).strip()
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        assert '\n'.join(code.splitlines()[start-1:node.end_lineno]) == source


def test_embedded_cleanup_on_system_python(tmp_path):
    import inspect
    h = load_helper()
    path = tmp_path/'input'
    path.write_bytes(b'payload')
    code = 'import os, stat, contextlib\nfrom pathlib import Path\n'
    code += inspect.getsource(h._checked_tui_fd)
    code += '''
_tui_acl_text = lambda fd: ''
primary = RuntimeError('primary')
original_close = os.close
closed = []
def close(fd):
    closed.append(fd)
    original_close(fd)
    raise OSError('secondary')
os.close = close
try:
    with _checked_tui_fd(Path(__import__('sys').argv[1])):
        raise primary
except BaseException as error:
    assert error is primary, repr(error)
    assert any('secondary' in note for note in error.__notes__)
assert len(closed) == 1
'''
    result = subprocess.run(['/usr/bin/python3', '-I', '-B', '-c', code, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_first_python_exec_environment(tmp_path):
    h = load_helper()
    layout = fixture(h, tmp_path)
    launch = tmp_path/'launcher'
    launch.write_bytes(h.launcher_payload(layout, 'absent'))
    observer = tmp_path/'observer'
    # macOS 보호 셸 진입 후 DYLD를 설정하고 내장 명령으로 관찰한다.
    # /usr/bin/env 자체도 SIP에 의해 DYLD를 잃을 수 있다.
    observer.write_text('export DYLD_BOUNDARY_TEST=bad; exec() { export -p; printf "first=%s\\n" "$1"; }; . "$1"\n')
    injected = dict(LD_LIBRARY_PATH='/untrusted/loader', LD_PRELOAD='absent', NODE_OPTIONS='bad', NODE_PATH='bad', PYTHONPATH='bad', PYTHONHOME='bad', PYTHONSTARTUP='bad')
    result = subprocess.run(['/bin/bash', str(observer), str(launch)], env={**os.environ, **injected}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'first=/usr/bin/python3' in result.stdout
    assert 'DYLD_BOUNDARY_TEST=' not in result.stdout
    assert not any(name + '=' in result.stdout for name in injected)


@pytest.mark.skipif(sys.platform != 'darwin', reason='real Darwin ACLs')
@pytest.mark.parametrize('target', ['authority', 'authority_parent', 'release_parent'])
@pytest.mark.parametrize('grant', [True, False])
def test_acl_producer_boundaries(tmp_path, target, grant):
    h = load_helper()
    # 별도 권한 파일을 사용하여 검사 중인 소스의 ACL 변경을 방지한다.
    authority = tmp_path/'producer'/'hermes-release-manager.py'
    authority.parent.mkdir()
    shutil.copytree(Path(h.__file__).resolve().parents[1]/'src', tmp_path/'src')
    shutil.copy2(h.__file__, authority)
    h.__file__ = str(authority)
    layout = fixture(h, tmp_path)
    launch = tmp_path/'launcher'
    launch.write_bytes(h.launcher_payload(layout, 'absent'))
    launch.chmod(0o700)
    subject = {'authority': authority, 'authority_parent': authority.parent, 'release_parent': layout.root}[target]
    ace = 'everyone allow write' if grant else 'everyone deny delete'
    subprocess.run(['/bin/chmod', '+a', ace, str(subject)], check=True)
    try:
        if target == 'release_parent':
            if grant:
                with pytest.raises((RuntimeError, PermissionError), match='ACL'):
                    h.managed_tui_environment(layout.release)
            else:
                assert h.managed_tui_environment(layout.release)
        result = subprocess.run([str(launch), 'chat'], env={'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
        assert result.returncode == (126 if grant else 0), result.stderr
    finally:
        subprocess.run(['/bin/chmod', '-N', str(subject)], check=True)
