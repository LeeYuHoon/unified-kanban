from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from test_hermes_setup import SETUP, environment, run
from oauth_native_source import native_source, yaml_dependency_root

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def scripts_import_path(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native_fixture(env):
    import sys
    native = Path(env['HERMES_AGENT_REPO'])
    # 표준 라이브러리만 쓰는 공식 경로 해석을 검증하며 인증 모듈 가져오기는 금지한다.
    import shutil
    source = native_source()
    shutil.copyfile(source / 'hermes_constants.py', native / 'hermes_constants.py')
    package = native / 'hermes_cli'
    package.mkdir()
    (package / '__init__.py').write_text('')
    for name in ('auth', 'anon_auth', 'dashboard_register'):
        (package / f'{name}.py').write_text('raise AssertionError("credential/remote module forbidden")\n')
    shutil.copyfile(source / 'hermes_cli/config_defaults.py', package / 'config_defaults.py')
    (package / 'config.py').write_text('from hermes_cli.config_defaults import DEFAULT_CONFIG\n')
    binary = native / 'venv/bin/python'
    binary.parent.mkdir(parents=True)
    binary.symlink_to(sys.executable)
    return native


def test_missing_registration_no_mutations(tmp_path):
    env = environment(tmp_path)
    native_fixture(env)
    result = run(SETUP, env, '--dashboard-oauth', '--skip-smoke')
    assert result.returncode != 0
    assert 'hermes auth add nous' in result.stderr
    assert not Path(env['HERMES_AGENT_REPO'] + '.releases').exists()
    assert not (tmp_path / '.hermes').exists()
    assert not (tmp_path / '.local').exists()



def test_stage_oauth_uses_official_setter_and_preserves_config(tmp_path, monkeypatch):
    import yaml
    helper = load('stage-hermes-plugin-config')
    baseline = {'model': {'provider': 'unchanged'}, 'dashboard': {'theme': 'dark'},
                'plugins': {'enabled': ['other'], 'entries': {'other': {'x': 1}}}}
    calls = []
    def fake(command, *, env, check, close_fds, cwd):
        calls.append(command[1:])
        assert env['HERMES_HOME'] == '.'
        assert not (cwd / '.env').exists()
        assert not (cwd / 'auth.json').exists()
        config = yaml.safe_load((cwd / 'config.yaml').read_text())
        if command[1] == 'plugins':
            config['plugins']['enabled'].append('hermes-kanban')
        else:
            assert command[1:] == ['config', 'set', 'dashboard.require_auth', 'true']
            config['dashboard']['require_auth'] = True
        (cwd / 'config.yaml').write_text(yaml.safe_dump(config))
    monkeypatch.setattr(helper.subprocess, 'run', fake)
    stage = tmp_path / 'stage'
    stage.mkdir()
    rendered = helper._render_in_external_stage(stage, (yaml.safe_dump(baseline).encode(), 0o600), tmp_path, 'enable', tmp_path / 'hermes', dashboard_oauth=True)
    actual = yaml.safe_load(rendered)
    assert actual['dashboard'] == {'theme': 'dark', 'require_auth': True}
    assert actual['model'] == baseline['model']
    assert len(calls) == 2


@pytest.mark.parametrize('dashboard', ['null', 'false', '[]', 'text', '{require_auth: "true"}', '{require_auth: 1}'])
def test_stage_malformed_dashboard_refused_before_cli(tmp_path, monkeypatch, dashboard):
    helper = load('stage-hermes-plugin-config')
    monkeypatch.setattr(helper.subprocess, 'run', lambda *a, **k: pytest.fail('CLI before schema validation'))
    with pytest.raises((ValueError, RuntimeError)):
        helper._render_in_external_stage(tmp_path, (f'dashboard: {dashboard}\n'.encode(), 0o600), tmp_path, 'enable', tmp_path / 'hermes', dashboard_oauth=True)


@pytest.mark.parametrize('extra', ['model: changed', 'plugins: {enabled: [foreign]}'])
def test_stage_rejects_unrelated_cli_changes(tmp_path, monkeypatch, extra):
    helper = load('stage-hermes-plugin-config')
    def fake(command, *, cwd, **kwargs):
        (cwd / 'config.yaml').write_text('dashboard: {require_auth: true}\n' + extra + '\n')
    monkeypatch.setattr(helper.subprocess, 'run', fake)
    with pytest.raises(RuntimeError, match='unrelated'):
        helper._render_in_external_stage(tmp_path, None, tmp_path, 'enable', tmp_path / 'hermes', dashboard_oauth=True)


def oauth_environment(tmp_path):
    import shlex
    import sys
    env = environment(tmp_path)
    native = native_fixture(env)
    deps = yaml_dependency_root()
    binary = native / 'venv/bin/python'
    binary.unlink()
    binary.write_text(f'#!/bin/sh\nPYTHONPATH={shlex.quote(os.pathsep.join(deps))} exec {shlex.quote(sys.executable)} "$@"\n')
    binary.chmod(0o755)
    (native / 'hermes_cli/__init__.py').write_text(f'import sys\nsys.path[:0] = {deps!r}\n')
    shim = tmp_path / 'fake-bin/python3'
    text = shim.read_text().replace('  chmod +x "$release/venv/bin/hermes"',
        '  chmod +x "$release/venv/bin/hermes"\n'
        '  cp -R "$HERMES_AGENT_REPO/hermes_cli" "$release/hermes_cli"\n'
        '  cp "$HERMES_AGENT_REPO/hermes_constants.py" "$release/hermes_constants.py"\n'
        '  cp "$HERMES_AGENT_REPO/venv/bin/python" "$release/venv/bin/python"')
    shim.write_text(text)
    fake = Path(env['FAKE_HERMES_EXECUTABLE'])
    fake.write_text(f'''#!{sys.executable}
import sys, os
sys.path[:0] = {deps!r}
import yaml
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['HERMES_TEST_LOG'], 'a') as f: f.write(' '.join(args) + '\\n')
sys.path.insert(0, {str(native)!r})
from hermes_constants import get_hermes_home
home = get_hermes_home()
if args[:1] in (['dashboard'], ['auth']):
    raise AssertionError('credential/remote command forbidden')
elif '--help' in args:
    print({__import__('test_hermes_setup').HELP_TOKENS!r})
elif args[:1] in (['plugins'], ['config']):
    p = home / 'config.yaml'
    cfg = yaml.safe_load(p.read_text()) if p.exists() else {{}}
    if args[0] == 'plugins':
        cfg.setdefault('plugins', {{}})['enabled'] = ['hermes-kanban'] if args[1] == 'enable' else []
    else:
        cfg.setdefault('dashboard', {{}})['require_auth'] = True
    p.write_text(yaml.safe_dump(cfg))
''')
    home = Path(env['HERMES_HOME'])
    home.mkdir()
    (home / '.env').write_text('HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:fixture\n')
    # FIFO로 잘못된 인증 저장소 읽기를 실패시키거나 멈추게 하며 테스트 시간은 제한한다.
    os.mkfifo(home / 'auth.json')
    (home / 'config.yaml').write_text('model: {{provider: unchanged}}\ndashboard: {{theme: dark}}\n'.replace('{{', '{').replace('}}', '}'))
    return env


def test_reuse_across_runner_reentry_rerun_and_no_restart(tmp_path):
    from test_hermes_setup import hermes_calls, UNINSTALL
    env = oauth_environment(tmp_path)
    for args in [('--dashboard-oauth',), ('--dashboard-oauth',), ()]:
        result = run(SETUP, env, '--skip-smoke', *args)
        assert result.returncode == 0, result.stderr
        if args:
            assert 'activation pending' in result.stdout
    assert hermes_calls(env).count('dashboard register') == 0
    assert not any(c.startswith('gateway ') for c in hermes_calls(env))
    import yaml
    config = Path(env['HERMES_HOME']) / 'config.yaml'
    assert yaml.safe_load(config.read_text())['dashboard']['require_auth'] is True
    assert yaml.safe_load(config.read_text())['model'] == {'provider': 'unchanged'}
    result = run(UNINSTALL, env)
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load(config.read_text())['dashboard']['require_auth'] is True
    assert (config.parent / '.env').read_text() == 'HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:fixture\n'


@pytest.mark.parametrize('config,stored,inherited', [
    ('dashboard: null', '', {}),
    ('dashboard: {require_auth: "true"}', '', {}),
    ('dashboard: {host: 0.0.0.0}', '', {}),
    ('dashboard: {public_url: "https://public.example"}', '', {}),
    ('{}', 'HERMES_DASHBOARD_PUBLIC_URL=https://public.example\n', {}),
    ('{}', '', {'HERMES_DASHBOARD_HOST': '0.0.0.0'}),
    ('{}', '', {'HERMES_DASHBOARD_OAUTH_CLIENT_ID': 'agent:env-only'}),
    ('dashboard: {oauth: {client_id: "agent:yaml"}}', '', {}),
    ('{}', 'HERMES_DASHBOARD_OAUTH_CLIENT_ID=bad\n', {}),
    ('{}', 'HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:stored\n', {'HERMES_DASHBOARD_OAUTH_CLIENT_ID': 'agent:other'}),
    ('dashboard: {oauth: {client_id: "agent:yaml"}}', 'HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:stored\n', {}),
])
def test_preflight_conflicts_are_zero_mutation(tmp_path, config, stored, inherited):
    from test_hermes_setup import hermes_calls
    env = oauth_environment(tmp_path)
    home = Path(env['HERMES_HOME'])
    (home / 'config.yaml').write_text(config)
    (home / '.env').write_text(stored)
    env.update(inherited)
    result = run(SETUP, env, '--dashboard-oauth', '--skip-smoke')
    assert result.returncode != 0
    assert not Path(env['HERMES_AGENT_REPO'] + '.releases').exists()
    assert 'dashboard register' not in hermes_calls(env)
    assert (home / 'config.yaml').read_text() == config
    assert (home / '.env').read_text() == stored


@pytest.mark.parametrize('marker', ['FOREIGN', '{"state": "pending"}\n', '{"state": "pending"}\n{"state": "complete"}\n'])
def test_prior_marker_preserved_and_refused(tmp_path, marker):
    from test_hermes_setup import hermes_calls
    env = oauth_environment(tmp_path)
    path = Path(env['HERMES_HOME']) / '.dashboard-oauth-registration-pending'
    path.write_text(marker)
    for _ in range(2):
        result = run(SETUP, env, '--dashboard-oauth', '--skip-smoke')
        assert result.returncode != 0
        assert 'reconcile' in result.stderr
    assert path.read_text() == marker
    assert not Path(env['HERMES_AGENT_REPO'] + '.releases').exists()
    assert 'dashboard register' not in hermes_calls(env)


def test_oauth_dry_run_never_requires_runtime_or_writes(tmp_path):
    env = environment(tmp_path)
    Path(env["HERMES_AGENT_REPO"]).rmdir()
    result = run(SETUP, env, "--dashboard-oauth", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "not checked" in result.stdout
    assert not (tmp_path / ".hermes").exists()
    assert not (tmp_path / ".local").exists()
    assert not (tmp_path / "hermes.log").exists()


def test_oauth_missing_runtime_refuses_before_prepare(tmp_path):
    env = environment(tmp_path)
    result = run(SETUP, env, "--dashboard-oauth", "--skip-smoke")
    assert result.returncode != 0
    assert "hermes auth add nous" in result.stderr
    assert not Path(env["HERMES_AGENT_REPO"] + ".releases").exists()
    assert not (tmp_path / ".hermes").exists()
    assert not (tmp_path / ".local").exists()


def test_default_home_without_override_and_rollback_retains_registration(tmp_path):
    from test_hermes_setup import hermes_calls
    env = oauth_environment(tmp_path)
    home = Path(env.pop('HERMES_HOME'))
    original = (home / 'config.yaml').read_bytes()
    registration = (home / '.env').read_bytes()
    env['HERMES_TEST_ACTIVATION_FAIL_ONCE'] = str(tmp_path / 'activation-fail')
    failed = run(SETUP, env, '--dashboard-oauth', '--skip-smoke')
    assert failed.returncode != 0
    assert 'intentional activation failure' in failed.stderr
    assert (home / 'config.yaml').read_bytes() == original
    assert (home / '.env').read_bytes() == registration
    assert not (home / '.dashboard-oauth-registration-pending').exists()
    result = run(SETUP, env, '--dashboard-oauth', '--skip-smoke')
    assert result.returncode == 0, result.stderr
    import yaml
    assert yaml.safe_load((home / 'config.yaml').read_text())['dashboard']['require_auth'] is True
    assert (home / '.env').read_bytes() == registration
    assert not any(c.startswith(('dashboard ', 'auth ')) for c in hermes_calls(env))
