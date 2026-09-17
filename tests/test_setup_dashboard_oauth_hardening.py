import os
import pytest
from test_setup_dashboard_oauth import load
from oauth_native_source import native_source

@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'scripts'))
    monkeypatch.syspath_prepend(str(native_source()))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    for key in list(os.environ):
        if key.startswith('HERMES_DASHBOARD_'):
            monkeypatch.delenv(key)
    return load('setup-dashboard-oauth'), tmp_path

@pytest.mark.parametrize('stored', ['', 'HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:fixture\n'])
def test_blank_default(settings, stored):
    m, home = settings
    (home/'config.yaml').write_text('dashboard: {oauth: {client_id: ""}}')
    (home/'.env').write_text(stored)
    if stored:
        assert m.validate_settings() == "agent:fixture"
    else:
        with pytest.raises(RuntimeError, match="hermes dashboard register"):
            m.validate_settings()

@pytest.mark.parametrize('kind', ['yaml', 'dotenv'])
@pytest.mark.parametrize('key', ['host', 'client_id', 'portal_url'])
def test_duplicate_keys(settings, kind, key):
    m, home = settings
    if kind == 'yaml':
        (home/'config.yaml').write_text('{}: x\n{}: y\n'.format(key, key))
    else:
        (home/'.env').write_text('{}=x\n{}=y\n'.format(key, key))
    with pytest.raises((ValueError, RuntimeError)):
        m.validate_settings()


@pytest.mark.parametrize('source', ['env', 'dotenv', 'yaml'])
def test_unapproved_portal(settings, monkeypatch, source):
    m, home = settings
    if source == 'env':
        monkeypatch.setenv('HERMES_DASHBOARD_PORTAL_URL', 'https://untrusted.invalid')
    elif source == 'dotenv':
        (home/'.env').write_text('HERMES_DASHBOARD_PORTAL_URL=https://untrusted.invalid')
    else:
        (home/'config.yaml').write_text('dashboard: {oauth: {portal_url: "https://untrusted.invalid"}}')
    with pytest.raises(RuntimeError):
        m.validate_settings()

@pytest.mark.parametrize('kind', ['symlink_parent', 'fifo', 'swap_fifo'])
def test_read_boundary(settings, monkeypatch, kind):
    import subprocess, sys
    m, home = settings
    target = home/'target'
    if kind == 'symlink_parent':
        outside = home/'outside'
        outside.mkdir()
        (outside/'config.yaml').write_text('{}')
        target.symlink_to(outside)
        with pytest.raises((RuntimeError, OSError)):
            m.read_optional(target/'config.yaml')
    else:
        # 격리된 자식 프로세스와 시간 제한으로 파일 열기가 멈추는 회귀도 제한 시간 안에 끝낸다.
        script = """import importlib.util, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
s=importlib.util.spec_from_file_location('m',sys.argv[1]+'/setup-dashboard-oauth.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
p=pathlib.Path(sys.argv[2])
if sys.argv[3]=='fifo': os.mkfifo(p)
else:
 p.write_text('x')
 original=os.open
 def swap(path, flags, *a, **k):
  if str(path) in (str(p),p.name):
   p.unlink();os.mkfifo(p)
  return original(path,flags,*a,**k)
 os.open=swap
try: m.read_optional(p)
except (RuntimeError,OSError): sys.exit(0)
sys.exit(2)
"""
        result = subprocess.run([sys.executable, '-B', '-c', script, str(__import__('pathlib').Path(m.__file__).parent), str(target), kind], timeout=2)
        assert result.returncode == 0

def test_symlink_home_refused(settings, monkeypatch):
    m, home = settings
    link = home / 'linked'
    link.symlink_to(home)
    monkeypatch.setenv('HERMES_HOME', str(link))
    with pytest.raises((RuntimeError, OSError)):
        m.validate_settings()
