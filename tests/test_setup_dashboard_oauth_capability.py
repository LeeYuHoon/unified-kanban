"""준비된 기능은 소스 데이터로만 검사하며 인증 정보를 다루는 모듈은 가져오지 않는다."""
import sys
from pathlib import Path
import pytest
from test_setup_dashboard_oauth import load, oauth_environment, ROOT
from oauth_native_source import native_source
from test_hermes_setup import SETUP, run, hermes_calls


def test_nonexistent_runtime_refused(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / '.env').write_text('HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:fixture\n')
    m = load('setup-dashboard-oauth')
    monkeypatch.setattr(sys, 'argv', [m.__file__, str(tmp_path / 'missing')])
    assert m.main() == 1


@pytest.mark.parametrize('prepared_source', [
    'DEFAULT_CONFIG = {}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False}}\nDEFAULT_CONFIG: dict = {}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False}, **{"dashboard": {}}}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False, **{"require_auth": True}}}',
])
@pytest.mark.parametrize('installed_support', [False, True])
def test_unsupported_prepared_refuses_before_application(tmp_path, installed_support, prepared_source):
    import shlex
    env = oauth_environment(tmp_path)
    native = Path(env['HERMES_AGENT_REPO'])
    if not installed_support:
        (native / 'hermes_cli/config.py').write_text('DEFAULT_CONFIG = {}\n')
        (native / 'hermes_cli/config_defaults.py').write_text('DEFAULT_CONFIG = {}\n')
    shim = tmp_path / 'fake-bin/python3'
    text = shim.read_text().replace(
        '  cp -R "$HERMES_AGENT_REPO/hermes_cli" "$release/hermes_cli"',
        '  cp -R "$HERMES_AGENT_REPO/hermes_cli" "$release/hermes_cli"\n'
        f'  printf "%s\\n" {shlex.quote(prepared_source)} > "$release/hermes_cli/config_defaults.py"')
    shim.write_text(text)
    home = Path(env['HERMES_HOME'])
    before = {name: (home / name).read_bytes() for name in ('.env', 'config.yaml')}
    result = run(SETUP, env, '--dashboard-oauth', '--skip-smoke')
    assert result.returncode != 0, result.stdout
    assert Path(env['HERMES_AGENT_REPO'] + '.releases').exists()
    assert not (tmp_path / '.local/bin/kanban-adapter').exists()
    assert not (tmp_path / '.local/bin/hermes').exists()
    assert not (Path(env['HERMES_AGENT_REPO'] + '.releases') / 'current').exists()
    assert {name: (home / name).read_bytes() for name in before} == before
    assert not any(c.startswith(('config ', 'plugins ', 'gateway ')) for c in hermes_calls(env))


def test_old_installed_new_prepared_upgrade(tmp_path):
    import shlex
    env = oauth_environment(tmp_path)
    native = Path(env['HERMES_AGENT_REPO'])
    (native / 'hermes_cli/config.py').write_text('DEFAULT_CONFIG = {}\n')
    (native / 'hermes_cli/config_defaults.py').write_text('DEFAULT_CONFIG = {}\n')
    shim = tmp_path / 'fake-bin/python3'
    approved = native_source() / 'hermes_cli/config_defaults.py'
    shim.write_text(shim.read_text().replace(
        '  cp -R "$HERMES_AGENT_REPO/hermes_cli" "$release/hermes_cli"',
        '  cp -R "$HERMES_AGENT_REPO/hermes_cli" "$release/hermes_cli"\n'
        f'  cp {shlex.quote(str(approved))} "$release/hermes_cli/config_defaults.py"'))
    result = run(SETUP, env, '--dashboard-oauth', '--skip-smoke')
    assert result.returncode == 0, result.stderr
    import yaml
    assert yaml.safe_load((Path(env['HERMES_HOME']) / 'config.yaml').read_text())['dashboard']['require_auth'] is True
    assert (tmp_path / '.local/bin/kanban-adapter').is_symlink()
    assert (native / 'hermes_cli/config_defaults.py').read_text() == 'DEFAULT_CONFIG = {}\n'


@pytest.mark.parametrize('source', [
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False}}\nDEFAULT_CONFIG: dict = {}',
    'DEFAULT_CONFIG: dict = {"dashboard": {"require_auth": False}}',
    'DEFAULT_CONFIG = alias = {"dashboard": {"require_auth": False}}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False}}\nDEFAULT_CONFIG, other = {}, {}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False}}\nDEFAULT_CONFIG = {}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False}, **{"dashboard": {}}}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False, **{"require_auth": True}}}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False}, key(): {}}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False, key(): True}}',
    '# require_auth False\nDEFAULT_CONFIG = {}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": 0}}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": "false"}}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": True}}',
    'DEFAULT_CONFIG = {"dashboard": {"require_auth": False, "require_auth": True}}',
])
def test_non_native_default_contract_refused(tmp_path, source):
    native = tmp_path / 'hermes_cli'
    native.mkdir()
    (native / 'config_defaults.py').write_text(source)
    with pytest.raises(RuntimeError):
        load('setup-dashboard-oauth').validate_capability(tmp_path)
