"""공식 경로로 오프라인 재사용을 검증하며 인증 정보나 네트워크 권한은 쓰지 않는다."""
import builtins
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest
from test_setup_dashboard_oauth_hardening import settings
from oauth_native_source import native_source

NATIVE = native_source()


def native_constants():
    module = types.ModuleType('hermes_constants')
    source = NATIVE / 'hermes_constants.py'
    module.__file__ = str(source)
    exec(compile(source.read_text(), str(source), 'exec'), module.__dict__)
    return module


@pytest.mark.parametrize('kind', ['default', 'profile', 'custom'])
def test_actual_native_root_reuse_without_credentials(settings, monkeypatch, kind):
    m, temporary = settings
    constants = native_constants()
    home = temporary / '.hermes'
    if kind == 'profile':
        home = home / 'profiles' / 'fixture'
    elif kind == 'custom':
        home = temporary / 'custom'
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    assert constants.get_hermes_home() == home
    assert constants.get_default_hermes_root() == (home if kind == 'custom' else temporary / '.hermes')
    (home / '.env').write_text('HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:fixture\n')
    (home / 'config.yaml').write_text('dashboard: {host: 127.0.0.1, require_auth: false}\n')
    credential = home / 'auth.json'
    credential.write_bytes(b'fixture-not-a-token')
    baseline = {p.name: p.read_bytes() for p in home.iterdir()}
    real_open, real_import = os.open, builtins.__import__
    def guard_open(path, flags, *a, **kw):
        assert Path(path).name != 'auth.json', 'credential read forbidden'
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT), 'write forbidden'
        return real_open(path, flags, *a, **kw)
    def guard_import(name, *a, **kw):
        assert name not in ('hermes_cli.auth', 'hermes_cli.anon_auth', 'hermes_cli.dashboard_register')
        return real_import(name, *a, **kw)
    def forbidden(*a, **kw):
        pytest.fail('remote/child invocation forbidden')
    monkeypatch.setattr(os, 'open', guard_open)
    monkeypatch.setattr(builtins, '__import__', guard_import)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(sys, 'argv', [m.__file__, str(NATIVE)])
    for _ in range(2):
        assert m.main() == 0
    assert {p.name: p.read_bytes() for p in home.iterdir()} == baseline
