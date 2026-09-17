"""종료 증거를 먼저 내구 저장한 뒤 활성 이름만 receipt로 퇴역시킨다."""
from __future__ import annotations

import os

from . import private_files as private


def archived(path):
    return path.with_suffix('.terminal')


def read(queue, service, path, directory):
    """종료 증거가 있으면 누락된 활성 이름을 새 작업으로 해석하지 않는다."""
    terminal = archived(path)
    try:
        os.stat(terminal.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return private.read_bytes(path, directory_fd=directory)
    try:
        raw, receipt = private.read_bytes(terminal, directory_fd=directory)
    except FileNotFoundError as error:
        raise PermissionError('pending archive disappeared') from error
    try:
        body = queue._authenticate(service, path, raw, receipt)
        if body['status'] == 'pending':
            raise PermissionError('pending archive rejected')
        try:
            active, active_receipt = private.read_bytes(path, directory_fd=directory)
        except FileNotFoundError:
            pass
        else:
            with active_receipt:
                if queue._authenticate(service, path, active, active_receipt) != body:
                    raise PermissionError('pending archive mismatch')
        private.validate_directory(path.parent, directory)
        private._validate_receipt(receipt)
        return raw, receipt
    except FileNotFoundError as error:
        receipt.close()
        raise PermissionError('pending archive disappeared') from error
    except BaseException:
        receipt.close()
        raise


def retire(queue, service, path, body, receipt, directory):
    """복사 저장/fsync → 정확한 증거 비교 → receipt 퇴역. 중단 시 양쪽을 보존한다."""
    if body['status'] == 'pending':
        raise PermissionError('pending archive rejected')
    terminal = archived(path)
    private.validate_directory(path.parent, directory)
    try:
        raw, evidence = private.read_bytes(terminal, directory_fd=directory)
    except FileNotFoundError:
        evidence = private.atomic_publish(
            terminal, queue._wire({**body, 'mac': queue._mac(service, body)}),
            directory_fd=directory)
    else:
        try:
            if queue._authenticate(service, path, raw, evidence) != body:
                raise PermissionError('pending archive mismatch')
        except BaseException:
            evidence.close()
            raise
    with evidence:
        # 이전 publish 직후 중단된 증거도 삭제 전에 다시 내구 저장한다.
        os.fsync(evidence.file_fd)
        os.fsync(directory)
        private.validate_directory(path.parent, directory)
        private._validate_receipt(evidence)
        private.retire_expected(path, receipt, directory_fd=directory)
