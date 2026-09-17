"""종료 archive의 중단/중복/후속 inode 보존 계약."""
import json

import pytest

from kanban_adapter import pending_final_archive as archive
from kanban_adapter import private_files as private
from test_pending_final_recovery_progress import crowded_queue


def terminal_job(fixture):
    _, _, queue, service, path, _, _ = fixture
    body = json.loads(path.read_bytes())
    body.pop('mac')
    body['status'] = 'ready'
    with queue._locked(path) as directory:
        _, receipt = private.read_bytes(path, directory_fd=directory)
        queue._save(service, path, body, directory, receipt).close()
    return queue, service, path, body


def migrate(queue, service, path):
    with queue._locked(path) as directory:
        raw, receipt = private.read_bytes(path, directory_fd=directory)
        with receipt:
            archive.retire(queue, service, path, queue._authenticate(service, path, raw, receipt), receipt, directory)


@pytest.mark.parametrize('crash', ['before_publish', 'before_retire', 'after_retire', None])
def test_duplicate_terminal_survives_interruption(crowded_queue, monkeypatch, crash):
    queue, service, path, body = terminal_job(crowded_queue)
    with monkeypatch.context() as patch:
        if crash:
            name = 'atomic_publish' if crash == 'before_publish' else 'retire_expected'
            original = getattr(private, name)
            def interrupted(*a, **kw):
                if crash == 'after_retire':
                    original(*a, **kw)
                raise RuntimeError('중단')
            patch.setattr(private, name, interrupted)
        if crash:
            with pytest.raises(RuntimeError):
                migrate(queue, service, path)
        else:
            migrate(queue, service, path)
    assert queue.enqueue(service, path.parent, **body['kwargs']) == path
    assert queue.run_once(service, path) == 'ready'
    if path.exists():
        migrate(queue, service, path)
    assert not path.exists()
    saved = json.loads(archive.archived(path).read_bytes())
    assert {k: v for k, v in saved.items() if k != 'mac'} == body
    with pytest.raises(PermissionError):
        queue.enqueue(service, path.parent, **{**body['kwargs'], 'extra': 'changed'})


@pytest.mark.parametrize('kind', ['mismatch', 'symlink', 'hardlink', 'public'])
def test_foreign_archive_preserved(crowded_queue, kind):
    queue, service, path, body = terminal_job(crowded_queue)
    terminal = archive.archived(path)
    altered = {**body, 'attempts': 5}
    terminal.write_bytes(queue._wire({**altered, 'mac': queue._mac(service, altered)}))
    terminal.chmod(0o600)
    if kind in {'symlink', 'hardlink'}:
        other = terminal.with_suffix('.foreign')
        terminal.rename(other)
        if kind == 'symlink':
            terminal.symlink_to(other)
        else:
            terminal.hardlink_to(other)
    elif kind == 'public':
        terminal.chmod(0o644)
    before = terminal.read_bytes()
    with pytest.raises((OSError, RuntimeError)):
        migrate(queue, service, path)
    with pytest.raises((OSError, RuntimeError)):
        queue.enqueue(service, path.parent, **body['kwargs'])
    assert path.exists()
    assert terminal.read_bytes() == before


def test_active_replacement_preserved(crowded_queue, monkeypatch):
    queue, service, path, body = terminal_job(crowded_queue)
    original = private.retire_expected
    def replace(*a, **kw):
        path.rename(path.with_suffix('.old'))
        path.write_bytes(b'foreign successor')
        path.chmod(0o600)
        return original(*a, **kw)
    monkeypatch.setattr(private, 'retire_expected', replace)
    with pytest.raises(RuntimeError):
        migrate(queue, service, path)
    assert path.read_bytes() == b'foreign successor'
    with pytest.raises(PermissionError):
        queue.run_once(service, path)


@pytest.mark.parametrize('during_read', [False, True])
def test_archive_disappearance_after_read_does_not_renew(crowded_queue, monkeypatch, during_read):
    queue, service, path, body = terminal_job(crowded_queue)
    migrate(queue, service, path)
    original = private.read_bytes
    def disappear(p, **kw):
        result = original(p, **kw)
        if p == archive.archived(path):
            p.rename(p.with_suffix('.displaced'))
            if during_read:
                result[1].close()
                raise FileNotFoundError('읽기 중 이름 소실')
        return result
    monkeypatch.setattr(private, 'read_bytes', disappear)
    with pytest.raises((PermissionError, RuntimeError)):
        queue.enqueue(service, path.parent, **body['kwargs'])
    assert not path.exists()


@pytest.mark.parametrize('kind', ['archive', 'parent'])
def test_replacement_before_retirement_preserves_active(crowded_queue, monkeypatch, kind):
    queue, service, path, body = terminal_job(crowded_queue)
    original = private.atomic_publish
    def replace(p, *a, **kw):
        result = original(p, *a, **kw)
        if p == archive.archived(path):
            if kind == 'archive':
                p.rename(p.with_suffix('.old'))
                p.write_bytes(b'foreign archive')
                p.chmod(0o600)
            else:
                path.parent.rename(path.parent.with_name('displaced'))
                path.parent.mkdir(mode=0o700)
                path.write_bytes(b'foreign parent job')
                path.chmod(0o600)
        return result
    monkeypatch.setattr(private, 'atomic_publish', replace)
    with pytest.raises(RuntimeError):
        migrate(queue, service, path)
    assert path.exists()
    if kind == 'parent':
        assert path.read_bytes() == b'foreign parent job'
    else:
        assert archive.archived(path).read_bytes() == b'foreign archive'


def test_existing_archive_must_be_synced_before_retirement(crowded_queue, monkeypatch):
    queue, service, path, body = terminal_job(crowded_queue)
    terminal = archive.archived(path)
    terminal.write_bytes(path.read_bytes())
    terminal.chmod(0o600)
    def failed(fd):
        raise OSError('내구 저장 실패')
    monkeypatch.setattr(private.os, 'fsync', failed)
    with pytest.raises(OSError):
        migrate(queue, service, path)
    assert path.exists()
    assert terminal.exists()
