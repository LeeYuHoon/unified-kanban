"""일회용 데이터로 공개 쓰기 권한의 거부를 검증하며 설치된 파일이나 외부 공급 파일은 변경하지 않는다."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_conversation_launch import fixture_launch, invoke, BIN


@pytest.mark.parametrize('location', ['native-parent', 'native-upper', 'metadata-grandparent'])
def test_public_ancestor_refused(tmp_path, location):
    native, profile, selector, env = fixture_launch(tmp_path)
    if location.startswith('native'):
        upper = tmp_path / 'programs'
        parent = upper / 'bin'
        parent.mkdir(parents=True)
        moved = parent / 'native'
        native.rename(moved)
        data = json.loads(profile.read_text())
        data['executable'] = str(moved)
        profile.write_text(json.dumps(data))
        unsafe = parent if location == 'native-parent' else upper
    else:
        unsafe = selector.parent.parent
    unsafe.chmod(0o777)
    try:
        result = invoke(tmp_path, env)
        assert result.returncode == 2, result.stdout
        assert not (tmp_path / 'new-dir').exists()
        assert unsafe.stat().st_mode & 0o777 == 0o777
    finally:
        unsafe.chmod(0o700)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS ACL')
@pytest.mark.parametrize('location', ['native', 'profile', 'selector', 'parent'])
def test_acl_write_refused(tmp_path, location):
    native, profile, selector, env = fixture_launch(tmp_path)
    path = dict(native=native, profile=profile, selector=selector, parent=selector.parent)[location]
    subprocess.run(['/bin/chmod', '+a', 'everyone allow write', str(path)], check=True)
    result = invoke(tmp_path, env)
    assert result.returncode == 2, result.stdout
    assert not (tmp_path / 'new-dir').exists()


@pytest.mark.parametrize('location', ['checkout', 'interpreter', 'installed'])
def test_bootstrap_refuses_before_interpreter(tmp_path, location):
    import shutil
    repo = tmp_path / 'repo'
    (repo / 'bin').mkdir(parents=True)
    (repo / '.venv/bin').mkdir(parents=True)
    shutil.copy2(BIN, repo / 'bin/collected-native-agent')
    marker = tmp_path / 'executed'
    python = repo / '.venv/bin/python'
    python.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 23\n')
    python.chmod(0o700)
    entry = repo / 'bin/collected-native-agent'
    unsafe = repo if location == 'checkout' else repo / '.venv/bin'
    if location == 'installed':
        unsafe = tmp_path / 'installed'
        unsafe.mkdir()
        entry = unsafe / 'agent'
        entry.symlink_to(repo / 'bin/collected-native-agent')
    unsafe.chmod(0o777)
    result = invoke(tmp_path, {'PATH':'/usr/bin:/bin'}, entry=entry)
    assert result.returncode == 2
    assert not marker.exists()


def test_bootstrap_embeds_exact_namespace_policy():
    import shlex
    from test_conversation_launch import ROOT
    command = BIN.read_text().split('exec /usr/bin/python3 ', 1)[1]
    embedded = shlex.split(command)[4]
    assert embedded.startswith((ROOT / 'src/kanban_adapter/conversation_namespace.py').read_text())


def test_foreign_owner_refused_on_opened_fd(tmp_path, monkeypatch):
    from kanban_adapter import conversation_namespace as namespace
    from types import SimpleNamespace
    path = tmp_path / 'foreign'
    path.mkdir()
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    real = namespace.os.fstat
    monkeypatch.setattr(namespace.os, 'fstat', lambda value: SimpleNamespace(
        st_uid=os.getuid() + 1000, st_mode=real(value).st_mode))
    try:
        with pytest.raises(PermissionError, match='foreign'):
            namespace.validate_fd(fd)
    finally:
        os.close(fd)


def test_non_system_sticky_directory_refused(tmp_path):
    from kanban_adapter import conversation_namespace as namespace
    path = tmp_path / 'sticky'
    path.mkdir(mode=0o1777)
    path.chmod(0o1777)
    with pytest.raises(PermissionError):
        namespace.open_directory(path)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS ACL')
def test_acl_is_read_from_retained_inode(tmp_path):
    from kanban_adapter import conversation_namespace as namespace
    path = tmp_path / 'original'
    path.write_text('old')
    path.chmod(0o600)
    subprocess.run(['/bin/chmod', '+a', 'everyone allow write', str(path)], check=True)
    fd = os.open(path, os.O_RDONLY)
    path.rename(tmp_path / 'retained')
    path.write_text('successor')
    path.chmod(0o600)
    try:
        assert ':allow:' in namespace.acl_text(fd)
        with pytest.raises(PermissionError, match='ACL'):
            namespace.validate_fd(fd)
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS ACL')
def test_deny_only_system_style_acl_allowed(tmp_path):
    from kanban_adapter import conversation_namespace as namespace
    path = tmp_path / 'deny'
    path.write_text('data')
    path.chmod(0o600)
    subprocess.run(['/bin/chmod', '+a', 'everyone deny delete', str(path)], check=True)
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            namespace.validate_fd(fd)
            with pytest.raises(PermissionError):
                namespace.validate_fd(fd, private=True)
        finally:
            os.close(fd)
    finally:
        # 삭제 거부 ACL을 해제해 다음 pytest 실행의 basetemp 정리를 보장한다.
        subprocess.run(['/bin/chmod', '-a', 'everyone deny delete', str(path)], check=True)
        path.unlink()
