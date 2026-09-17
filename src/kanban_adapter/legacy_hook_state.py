"""소유권 없는 구형 hook 상태를 카드 변경 없이 private 증거로 격리한다."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from . import private_files as files


def quarantine_legacy_state(path: Path, receipt: files.Receipt, *, directory_fd: int) -> None:
    """호출자는 session lock을 보유한다. 성공 시에만 독립된 새 요청을 허용한다."""
    marker = None
    exchanged = False
    quarantine_name = None
    try:
        files.validate_directory(path.parent, directory_fd)
        if not files._same_directory(receipt.directory_fd, directory_fd):
            raise files.NamespaceAuthorityError("legacy receipt parent changed")
        info = files._validate_receipt(receipt, path.name)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("legacy state is not private")
        raw = os.pread(receipt.file_fd, info.st_size + 1, 0)
        if "lifecycle" in json.loads(raw):
            raise RuntimeError("legacy transition cannot replace lifecycle metadata")
        _, marker = files.create_private_text(
            path.parent, "", label="legacy-quarantine", directory_fd=directory_fd,
        )
        quarantine_name = marker.name
        os.fsync(receipt.file_fd)
        files._validate_receipt(receipt, path.name)
        files.validate_directory(path.parent, directory_fd)
        # 정식 이름의 후속 inode는 교환 후 검사하고 즉시 원위치로 되돌린다.
        files._swap_names(directory_fd, quarantine_name, path.name)
        exchanged = True
        marker.name = path.name
        try:
            files._validate_receipt(receipt, quarantine_name)
        except BaseException:
            exchanged = False  # 검증되지 않은 항목을 구형 receipt로 복구하지 않는다.
            try:
                files._swap_names(directory_fd, quarantine_name, path.name)
            except OSError:
                files._swap_names(directory_fd, quarantine_name, path.name)
            marker.name = quarantine_name
            os.fsync(directory_fd)
            raise
        receipt.name = quarantine_name
        os.fsync(directory_fd)
        files.validate_directory(path.parent, directory_fd)
        # 기존 detach 계약은 후속 항목을 덮어쓰지 않고 실패 시 복원한다.
        files.detach_expected(path, marker, directory_fd=directory_fd)
        files.discard_detached(marker)
        marker = None
        os.fsync(directory_fd)
        files._validate_receipt(receipt)
        files.validate_directory(path.parent, directory_fd)
        try:
            os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise files.NamespaceAuthorityError("legacy transition found a foreign successor")
    except BaseException:
        if exchanged:
            # 복구도 retained parent에만 적용한다. 후속 정식 항목은 건드리지 않는다.
            try:
                files.validate_directory(path.parent, directory_fd)
                entry = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    files.validate_directory(path.parent, directory_fd)
                    files._rename_exclusive(directory_fd, receipt.name, path.name)
                    receipt.name = path.name
                    os.fsync(directory_fd)
                except BaseException:
                    pass  # 증거는 격리 이름에 남기고 원래 오류를 유지한다.
            except BaseException:
                pass
            else:
                if marker is not None and not marker._closed and files._identity(entry) == marker.identity:
                    try:
                        files.detach_expected(path, marker, directory_fd=directory_fd)
                        files.validate_directory(path.parent, directory_fd)
                        files._rename_exclusive(directory_fd, receipt.name, path.name)
                        receipt.name = path.name
                        os.fsync(directory_fd)
                    except BaseException:
                        pass
        raise
    finally:
        if marker is not None:
            try:
                # 무작위 private 이름만 폐기한다. 정식 이름은 절대 unlink하지 않는다.
                if not marker._closed and marker.name != path.name:
                    files.discard_detached(marker)
            except BaseException:
                pass  # 정리 실패는 증거와 원래 오류를 보존한다.
            finally:
                marker.close()
        receipt.close()
