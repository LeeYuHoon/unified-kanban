"""예외 추적 정보를 유지한 상태에서 열린 파일 서술자의 수명을 검증한다."""
import errno
import os
import sys

import pytest
from test_conversation_grant_existing_blockers import existing, setup, run_cli
from test_conversation_grant_existing_durable import arguments
from test_conversation_grant_recovery import pending, snapshot
from kanban_adapter import conversation_transaction as tx


@pytest.mark.parametrize('boundary', ['runtime-read', 'report-dup'])
def test_early_acquisition_closes_live_fds_with_traceback(existing, monkeypatch, boundary):
    args = arguments(existing)
    before = snapshot(existing)
    original_read, original_dup = tx.private.read_bytes, os.dup
    acquired, retained = [], []
    primary = OSError(errno.EMFILE, 'receipt exhaustion')
    def read(path, **kw):
        caller = sys._getframe(1).f_code.co_name
        if caller == '_apply_grant' and path.name == 'runtime.json' and boundary == 'runtime-read':
            raise primary
        result = original_read(path, **kw)
        if caller == '_apply_grant':
            acquired.append((result[1], result[1].file_fd, result[1].directory_fd))
        return result
    def dup(fd):
        if boundary == 'report-dup' and sys._getframe(1).f_code.co_name == '_apply_grant':
            raise primary
        return original_dup(fd)
    with monkeypatch.context() as m:
        m.setattr(tx.private, 'read_bytes', read)
        m.setattr(os, 'dup', dup)
        for _ in range(3):
            with pytest.raises(OSError) as raised:
                tx.grant_existing(args)
            retained.append(raised)
            assert raised.value is primary
            assert raised.value.__traceback__ is not None
            for receipt, *fds in acquired:
                assert receipt._closed
                for fd in fds:
                    with pytest.raises(OSError) as closed:
                        os.fstat(fd)
                    assert closed.value.errno == errno.EBADF
            with tx.policy_lease(existing / 'policy.json', exclusive=True):
                pass
            assert snapshot(existing) == before
    assert tx.grant_existing(args)['status'] == 'committed'


@pytest.mark.parametrize('recover', [False, True])
def test_primary_survives_diagnostic_and_cleanup_errors(existing, monkeypatch, recover):
    args = pending(existing, monkeypatch) if recover else arguments(existing)
    before = snapshot(existing)
    original_close = tx.private.Receipt.close
    original_publish = tx.private.atomic_publish
    primary = OSError('publication primary')
    closed = []
    def publish(path, *a, **kw):
        if path.name == 'policy.json':
            raise primary
        return original_publish(path, *a, **kw)
    def diagnostics(*a, **kw):
        raise RuntimeError('diagnostic secondary')
    def close(receipt):
        was_closed = receipt._closed
        original_close(receipt)
        if not was_closed:
            closed.append(receipt)
            if sys._getframe(1).f_code.co_name in ('_close_grant_resources', '_apply_grant', 'recover_grant'):
                raise OSError('cleanup secondary')
    with monkeypatch.context() as m:
        m.setattr(tx.private, 'atomic_publish', publish)
        m.setattr(tx, '_grant_failure_artifacts', diagnostics)
        m.setattr(tx.private.Receipt, 'close', close)
        with pytest.raises(OSError) as raised:
            (tx.recover_grant if recover else tx.grant_existing)(args)
        assert raised.value is primary
        assert any('diagnostic secondary' in n for n in raised.value.__notes__)
        assert any('cleanup secondary' in n for n in raised.value.__notes__)
        assert all(r._closed for r in closed)
    for leaf in ('policy.json', 'runtime.json'):
        assert snapshot(existing)[leaf] == before[leaf]
    with tx.policy_lease(existing / 'policy.json', exclusive=True):
        pass


def test_recovery_second_input_failure_closes_every_receipt(existing, monkeypatch):
    args = pending(existing, monkeypatch)
    before = snapshot(existing)
    original_read, original_fence = tx.private.read_bytes, tx._read_fence
    acquired = []
    primary = OSError(errno.EMFILE, 'recovery receipt exhaustion')
    def fence(*a, **kw):
        result = original_fence(*a, **kw)
        acquired.append((result[1], result[1].file_fd, result[1].directory_fd))
        return result
    def read(path, **kw):
        if sys._getframe(1).f_code.co_name == 'recover_grant' and path.name == 'runtime.json':
            raise primary
        result = original_read(path, **kw)
        acquired.append((result[1], result[1].file_fd, result[1].directory_fd))
        return result
    with monkeypatch.context() as m:
        m.setattr(tx.private, 'read_bytes', read)
        m.setattr(tx, '_read_fence', fence)
        with pytest.raises(OSError) as raised:
            tx.recover_grant(args)
        assert raised.value is primary
        for receipt, *fds in acquired:
            assert receipt._closed
            for fd in fds:
                with pytest.raises(OSError) as closed:
                    os.fstat(fd)
                assert closed.value.errno == errno.EBADF
    assert snapshot(existing) == before
    assert tx.recover_grant(args)['status'] == 'committed'


def test_prior_committed_fence_receipt_closed_after_regrant(existing, monkeypatch):
    tx.grant_existing(arguments(existing))
    original = tx._read_fence
    acquired = []
    def read(*a, **kw):
        result = original(*a, **kw)
        acquired.append(result[1])
        return result
    monkeypatch.setattr(tx, '_read_fence', read)
    assert tx.grant_existing(arguments(existing))['status'] == 'committed'
    assert acquired and all(receipt._closed for receipt in acquired)
