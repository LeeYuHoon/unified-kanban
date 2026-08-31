from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/path-transaction.py"
_RECEIPT_TOKENS: dict[str, str] = {}


def load_helper():
    spec = importlib.util.spec_from_file_location("path_transaction", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*args: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(HELPER), *args]
    action = args[0] if args else ""
    receipt = args[1] if len(args) > 1 else ""
    if action in {"checkpoint", "rollback", "commit"} and receipt in _RECEIPT_TOKENS:
        command.append(_RECEIPT_TOKENS[receipt])
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0 and action in {"begin", "checkpoint"}:
        _RECEIPT_TOKENS[receipt] = completed.stdout.strip()
    elif completed.returncode == 0 and action in {"rollback", "commit"}:
        _RECEIPT_TOKENS.pop(receipt, None)
    return completed


def operation_entry(path: Path, *, changed: bool = True) -> dict[str, object]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"path": str(path), "changed": changed, "identity": None}
    identity = [
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_mtime_ns,
        info.st_size,
    ]
    result: dict[str, object] = {
        "path": str(path),
        "changed": changed,
        "identity": identity,
    }
    if stat.S_ISLNK(info.st_mode):
        result["link_target"] = os.readlink(path)
    else:
        result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def write_operation_receipt(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    path.chmod(0o600)


def test_create_temp_stays_private_during_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    observed_modes: list[int] = []
    real_write_all = helper._write_all

    def observe_mode(fd: int, data: bytes) -> None:
        observed_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_write_all(fd, data)

    monkeypatch.setattr(helper, "_write_all", observe_mode)
    directory_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        name, _identity = helper._create_temp(
            directory_fd, "private-candidate", b"secret", 0o644
        )
        os.unlink(name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)

    assert observed_modes == [0o600]


def test_replace_absent_target_stays_private_until_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    target = tmp_path / "installed"
    candidate.write_text("secret", encoding="utf-8")
    candidate.chmod(0o644)
    helper.begin(receipt, [target])
    observed_modes: list[int] = []
    real_publish = helper._publish_temp_exclusive

    def observe_publish(
        directory_fd: int, source: str, destination: str, expected
    ) -> None:
        info = os.stat(source, dir_fd=directory_fd, follow_symlinks=False)
        observed_modes.append(stat.S_IMODE(info.st_mode))
        real_publish(directory_fd, source, destination, expected)

    monkeypatch.setattr(helper, "_publish_temp_exclusive", observe_publish)
    helper.replace_file(receipt, candidate, target, operation, absent_mode=0o644)

    assert observed_modes
    assert set(observed_modes) == {0o600}
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_replace_file_compensates_when_publication_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    target = tmp_path / "installed"
    target.write_text("original", encoding="utf-8")
    candidate.write_text("replacement", encoding="utf-8")
    baseline = target.stat()
    helper.begin(receipt, [target])
    real_link = helper.os.link
    real_fsync = helper.os.fsync
    publication_linked = False
    failed = False

    def observe_link(*args, **kwargs):
        nonlocal publication_linked
        result = real_link(*args, **kwargs)
        publication_linked = True
        return result

    def fail_after_publication(fd: int) -> None:
        nonlocal failed
        if publication_linked and not failed:
            failed = True
            raise OSError("injected publication fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(helper.os, "link", observe_link)
    monkeypatch.setattr(helper.os, "fsync", fail_after_publication)

    with pytest.raises(OSError, match="publication fsync failure"):
        helper.replace_file(receipt, candidate, target, operation)

    restored = target.stat()
    assert (restored.st_dev, restored.st_ino) == (baseline.st_dev, baseline.st_ino)
    assert target.read_text(encoding="utf-8") == "original"
    assert not operation.exists()
    assert not list(tmp_path.glob(".installed.original.*"))


def test_receipt_retirement_fsync_failure_restores_canonical_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"receipt": true}\n', encoding="utf-8")
    receipt.chmod(0o600)
    expected = helper._snapshot(receipt)
    baseline = receipt.stat()
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_rename = helper.os.rename
    real_fsync = helper.os.fsync
    renamed = False
    failed = False

    def observe_rename(*args, **kwargs):
        nonlocal renamed
        result = real_rename(*args, **kwargs)
        renamed = True
        return result

    def fail_after_rename(fd: int) -> None:
        nonlocal failed
        if renamed and not failed:
            failed = True
            raise OSError("injected retirement fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(helper.os, "rename", observe_rename)
    monkeypatch.setattr(helper.os, "fsync", fail_after_rename)
    try:
        with pytest.raises(OSError, match="retirement fsync failure"):
            helper._unlink_exact_receipt(
                directory_fd,
                receipt.name,
                receipt,
                expected,
                "test receipt",
            )
    finally:
        os.close(directory_fd)

    restored = receipt.stat()
    assert (restored.st_dev, restored.st_ino) == (baseline.st_dev, baseline.st_ino)
    assert receipt.read_text(encoding="utf-8") == '{"receipt": true}\n'
    assert not list(tmp_path.glob(".receipt.json.quarantine.*"))


def test_temp_publication_substitution_preserves_foreign_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    temp = tmp_path / "temp"
    destination = tmp_path / "published"
    retained = tmp_path / "owned-retained"
    temp.write_text("owned", encoding="utf-8")
    expected = helper._snapshot(temp)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_link = helper.os.link

    def substitute_before_link(source, target, **kwargs):
        os.rename(temp, retained)
        temp.write_text("foreign", encoding="utf-8")
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(helper.os, "link", substitute_before_link)
    try:
        with pytest.raises(RuntimeError, match="foreign inode retained"):
            helper._publish_temp_exclusive(
                directory_fd, temp.name, destination.name, expected
            )
    finally:
        os.close(directory_fd)

    assert temp.read_text(encoding="utf-8") == "foreign"
    assert retained.read_text(encoding="utf-8") == "owned"
    assert destination.read_text(encoding="utf-8") == "foreign"


def test_temp_destination_substitution_preserves_foreign_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    temp = tmp_path / "temp"
    destination = tmp_path / "published"
    replacement = tmp_path / "replacement"
    temp.write_text("owned", encoding="utf-8")
    replacement.write_text("foreign", encoding="utf-8")
    expected = helper._snapshot(temp)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_snapshot_at = helper._snapshot_at
    injected = False

    def substitute_before_destination_snapshot(fd, name, display):
        nonlocal injected
        if not injected and name == destination.name and destination.exists():
            injected = True
            os.replace(replacement, destination)
        return real_snapshot_at(fd, name, display)

    monkeypatch.setattr(helper, "_snapshot_at", substitute_before_destination_snapshot)
    try:
        with pytest.raises(RuntimeError, match="foreign inode retained"):
            helper._publish_temp_exclusive(
                directory_fd, temp.name, destination.name, expected
            )
    finally:
        os.close(directory_fd)

    assert injected
    assert temp.read_text(encoding="utf-8") == "owned"
    assert destination.read_text(encoding="utf-8") == "foreign"


def test_temp_cleanup_substitution_preserves_foreign_inode(tmp_path: Path) -> None:
    helper = load_helper()
    temp = tmp_path / "temp"
    retained = tmp_path / "owned-retained"
    temp.write_text("owned", encoding="utf-8")
    expected = helper._snapshot(temp)
    os.rename(temp, retained)
    temp.write_text("foreign", encoding="utf-8")
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="changed before cleanup"):
            helper._remove_at(directory_fd, temp.name, temp, expected)
    finally:
        os.close(directory_fd)

    assert temp.read_text(encoding="utf-8") == "foreign"
    assert retained.read_text(encoding="utf-8") == "owned"


def test_replace_preserves_foreign_successor_before_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    target = tmp_path / "managed"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o644)
    candidate.write_text("installed", encoding="utf-8")
    helper.begin(receipt, [target])
    real_apply = helper._apply_snapshot_metadata_at
    raced = False

    def replace_before_metadata(
        directory_fd: int,
        name: str,
        snapshot: dict[str, object],
        expected_identity: list[int],
        *,
        private_mode: bool = False,
    ) -> None:
        nonlocal raced
        if name == target.name and not private_mode and not raced:
            os.unlink(name, dir_fd=directory_fd)
            foreign_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
                dir_fd=directory_fd,
            )
            try:
                os.write(foreign_fd, b"foreign")
                os.fchmod(foreign_fd, 0o644)
            finally:
                os.close(foreign_fd)
            raced = True
        real_apply(
            directory_fd,
            name,
            snapshot,
            expected_identity,
            private_mode=private_mode,
        )

    monkeypatch.setattr(helper, "_apply_snapshot_metadata_at", replace_before_metadata)
    with pytest.raises(RuntimeError, match="metadata target identity changed"):
        helper.replace_file(receipt, candidate, target, operation)

    assert raced
    assert target.read_text(encoding="utf-8") == "foreign"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    quarantines = list(tmp_path.glob(".managed.original.*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_text(encoding="utf-8") == "before"
    assert stat.S_IMODE(quarantines[0].stat().st_mode) == 0o600


def test_replace_metadata_failure_restores_original_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    target = tmp_path / "managed"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o640)
    original_identity = (target.stat().st_dev, target.stat().st_ino)
    candidate.write_text("installed", encoding="utf-8")
    helper.begin(receipt, [target])
    real_apply = helper._apply_snapshot_metadata_at
    failed = False

    def fail_published_metadata(
        directory_fd: int,
        name: str,
        snapshot: dict[str, object],
        expected_identity: list[int],
        *,
        private_mode: bool = False,
    ) -> None:
        nonlocal failed
        if name == target.name and not private_mode and not failed:
            failed = True
            raise OSError("injected published metadata failure")
        real_apply(
            directory_fd,
            name,
            snapshot,
            expected_identity,
            private_mode=private_mode,
        )

    monkeypatch.setattr(helper, "_apply_snapshot_metadata_at", fail_published_metadata)
    with pytest.raises(OSError, match="injected published metadata failure"):
        helper.replace_file(receipt, candidate, target, operation)

    assert failed
    assert target.read_text(encoding="utf-8") == "before"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert (target.stat().st_dev, target.stat().st_ino) == original_identity
    assert not operation.exists()
    assert list(tmp_path.glob(".managed.original.*")) == []
    assert list(tmp_path.glob(".managed.failed-replace.*")) == []


def test_replace_quarantine_is_private_before_first_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    target = tmp_path / "managed"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o644)
    candidate.write_text("after", encoding="utf-8")
    helper.begin(receipt, [target])
    real_snapshot = helper._snapshot_at
    observed_modes: list[int] = []

    def observe_snapshot(directory_fd: int, name: str, path: Path):
        if ".original." in name:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            observed_modes.append(stat.S_IMODE(info.st_mode))
        return real_snapshot(directory_fd, name, path)

    monkeypatch.setattr(helper, "_snapshot_at", observe_snapshot)
    helper.replace_file(receipt, candidate, target, operation)

    assert observed_modes
    assert set(observed_modes) == {0o600}


def test_checkpoint_persistence_failure_compensates_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    helper.replace_file(receipt, candidate, managed, operation)
    real_write_receipt = helper._write_receipt

    def fail_manifest_write(
        path,
        payload,
        *,
        exclusive=False,
        expected=None,
        record_token_before_publish=False,
    ):
        if path == receipt and not exclusive:
            raise OSError("injected checkpoint manifest persistence failure")
        return real_write_receipt(
            path,
            payload,
            exclusive=exclusive,
            expected=expected,
            record_token_before_publish=record_token_before_publish,
        )

    monkeypatch.setattr(helper, "_write_receipt", fail_manifest_write)
    with pytest.raises(OSError, match="checkpoint manifest"):
        token = helper.checkpoint(receipt, operation, token)

    assert managed.read_text(encoding="utf-8") == "before"
    assert operation.exists()
    assert not receipt.exists()


def test_later_checkpoint_failure_restores_prior_persisted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    first_operation = tmp_path / "first-operation.json"
    second_operation = tmp_path / "second-operation.json"
    managed = tmp_path / "managed"
    managed.write_text("baseline", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    managed.write_text("first", encoding="utf-8")
    write_operation_receipt(first_operation, [operation_entry(managed)])
    token = helper.checkpoint(receipt, first_operation, token)
    managed.write_text("second", encoding="utf-8")
    write_operation_receipt(second_operation, [operation_entry(managed)])
    real_write_receipt = helper._write_receipt

    def fail_manifest_write(
        path,
        payload,
        *,
        exclusive=False,
        expected=None,
        record_token_before_publish=False,
    ):
        if path == receipt and not exclusive:
            raise OSError("injected later checkpoint persistence failure")
        return real_write_receipt(
            path,
            payload,
            exclusive=exclusive,
            expected=expected,
            record_token_before_publish=record_token_before_publish,
        )

    monkeypatch.setattr(helper, "_write_receipt", fail_manifest_write)
    with pytest.raises(OSError, match="later checkpoint"):
        helper.checkpoint(receipt, second_operation, token)

    assert managed.read_text(encoding="utf-8") == "baseline"
    assert not receipt.exists()


def test_checkpoint_post_persist_discard_failure_still_returns_rollback_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    helper.replace_file(receipt, candidate, managed, operation)

    def fail_discard(*_args) -> None:
        raise OSError("injected operation receipt discard failure")

    monkeypatch.setattr(helper, "_discard_operation_receipt", fail_discard)
    token = helper.checkpoint(receipt, operation, token)
    helper.rollback(receipt, token)

    assert managed.read_text(encoding="utf-8") == "before"
    assert operation.exists()
    assert not receipt.exists()


def test_rollback_ledger_recovers_when_checkpoint_output_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    ledger = tmp_path / "token-ledger"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    ledger_fd = os.open(ledger, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    monkeypatch.setenv("UNIFIED_KANBAN_TOKEN_LEDGER_FD", str(ledger_fd))
    try:
        stale_token = helper.begin(receipt, [managed])
        helper.replace_file(receipt, candidate, managed, operation)
        helper.checkpoint(receipt, operation, stale_token)

        with pytest.raises(RuntimeError, match="identity changed"):
            helper.rollback(receipt, stale_token)
        helper.rollback_from_ledger(receipt, ledger_fd)
    finally:
        os.close(ledger_fd)

    assert managed.read_text(encoding="utf-8") == "before"
    assert not receipt.exists()


def test_operation_receipt_rollback_undoes_an_uncheckpointed_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "stage-1.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    ledger_fd = os.open(
        tmp_path / "token-ledger", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
    )
    monkeypatch.setenv("UNIFIED_KANBAN_TOKEN_LEDGER_FD", str(ledger_fd))
    try:
        helper.begin(receipt, [managed])
        helper.replace_file(receipt, candidate, managed, operation)
        assert managed.read_text(encoding="utf-8") == "after"

        helper.rollback_operation_from_ledger(receipt, operation, ledger_fd)

        assert managed.read_text(encoding="utf-8") == "before"
        assert not operation.exists()
    # 이후에도 매니페스트는 호출자의 롤백 권한으로 남는다.
        assert receipt.exists()
        helper.rollback_from_ledger(receipt, ledger_fd)
    finally:
        os.close(ledger_fd)

    assert managed.read_text(encoding="utf-8") == "before"
    assert not receipt.exists()


def test_operation_receipt_rollback_preserves_foreign_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "stage-1.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    ledger_fd = os.open(
        tmp_path / "token-ledger", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
    )
    monkeypatch.setenv("UNIFIED_KANBAN_TOKEN_LEDGER_FD", str(ledger_fd))
    try:
        helper.begin(receipt, [managed])
        helper.replace_file(receipt, candidate, managed, operation)
        successor = tmp_path / "successor"
        successor.write_text("foreign", encoding="utf-8")
        successor.replace(managed)

        with pytest.raises(RuntimeError, match="operation identity no longer present"):
            helper.rollback_operation_from_ledger(receipt, operation, ledger_fd)
    finally:
        os.close(ledger_fd)

    assert managed.read_text(encoding="utf-8") == "foreign"
    assert operation.exists()


def test_operation_receipt_rollback_leaves_checkpointed_work_to_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """체크포인트보다 오래 남은 스테이지 영수증이 두 번 복원해서는 안 된다."""
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "stage-1.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    ledger_fd = os.open(
        tmp_path / "token-ledger", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
    )
    monkeypatch.setenv("UNIFIED_KANBAN_TOKEN_LEDGER_FD", str(ledger_fd))

    def fail_discard(path: Path, expected: dict[str, object]) -> None:
        raise RuntimeError("injected operation receipt discard failure")

    try:
        token = helper.begin(receipt, [managed])
        helper.replace_file(receipt, candidate, managed, operation)
        monkeypatch.setattr(helper, "_discard_operation_receipt", fail_discard)
        helper.checkpoint(receipt, operation, token)
        monkeypatch.undo()
        monkeypatch.setenv("UNIFIED_KANBAN_TOKEN_LEDGER_FD", str(ledger_fd))
        assert operation.exists()

        helper.rollback_operation_from_ledger(receipt, operation, ledger_fd)

        assert managed.read_text(encoding="utf-8") == "after"
        assert not operation.exists()
        helper.rollback_from_ledger(receipt, ledger_fd)
    finally:
        os.close(ledger_fd)

    assert managed.read_text(encoding="utf-8") == "before"


def test_operation_receipt_is_never_its_own_rollback_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "stage-1.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    ledger_fd = os.open(
        tmp_path / "token-ledger", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
    )
    monkeypatch.setenv("UNIFIED_KANBAN_TOKEN_LEDGER_FD", str(ledger_fd))
    try:
        helper.begin(receipt, [managed])
        helper.replace_file(receipt, candidate, managed, operation)

        with pytest.raises(RuntimeError, match="no transaction authority token"):
            helper.rollback_from_ledger(operation, ledger_fd)
    finally:
        os.close(ledger_fd)

    assert managed.read_text(encoding="utf-8") == "after"
    assert operation.exists()


def test_checkpoint_atomic_swap_preserves_foreign_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    replacement = tmp_path / "replacement.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    helper.replace_file(receipt, candidate, managed, operation)
    replacement.write_bytes(receipt.read_bytes())
    replacement.chmod(0o600)
    foreign_identity = operation_entry(replacement)["identity"]
    real_swap = helper._swap_names
    injected = False

    def replace_before_swap(directory_fd, first, second):
        nonlocal injected
        if not injected and second == receipt.name:
            injected = True
            os.replace(replacement, receipt)
        return real_swap(directory_fd, first, second)

    monkeypatch.setattr(helper, "_swap_names", replace_before_swap)
    with pytest.raises(RuntimeError):
        helper.checkpoint(receipt, operation, token)

    assert managed.read_text(encoding="utf-8") == "before"
    assert operation_entry(receipt)["identity"] == foreign_identity
    assert operation.exists()


def test_checkpoint_second_swap_failure_restores_foreign_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    replacement = tmp_path / "replacement.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    helper.replace_file(receipt, candidate, managed, operation)
    replacement.write_text('{"foreign":true}\n', encoding="utf-8")
    replacement.chmod(0o600)
    foreign = replacement.read_bytes()
    real_swap = helper._swap_names
    calls = 0

    def replace_then_fail_recovery(directory_fd, first, second):
        nonlocal calls
        calls += 1
        if calls == 1:
            os.replace(replacement, receipt)
            return real_swap(directory_fd, first, second)
        if calls == 2:
            raise OSError(5, "injected recovery swap failure")
        return real_swap(directory_fd, first, second)

    monkeypatch.setattr(helper, "_swap_names", replace_then_fail_recovery)

    with pytest.raises(RuntimeError, match="atomic publication"):
        helper.checkpoint(receipt, operation, token)

    assert calls == 2
    assert managed.read_text(encoding="utf-8") == "before"
    assert receipt.read_bytes() == foreign
    retained_authority = [
        path
        for path in tmp_path.iterdir()
        if "receipt.json.recovery" in path.name
    ]
    assert retained_authority == []
    assert operation.exists()


def test_begin_does_not_authorize_receipt_replaced_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    real_write = helper._write_receipt

    def replace_after_publish(*args, **kwargs):
        installed = real_write(*args, **kwargs)
        replacement = tmp_path / "replacement.json"
        replacement.write_bytes(receipt.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, receipt)
        return installed

    monkeypatch.setattr(helper, "_write_receipt", replace_after_publish)
    token = helper.begin(receipt, [managed])

    with pytest.raises(RuntimeError, match="identity changed"):
        helper._remove_receipt(receipt, token)
    assert receipt.exists()


def test_checkpoint_does_not_authorize_receipt_replaced_before_token_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    helper.replace_file(receipt, candidate, managed, operation)
    real_discard = helper._discard_operation_receipt_noexcept

    def replace_before_return(*args) -> None:
        real_discard(*args)
        replacement = tmp_path / "replacement.json"
        replacement.write_bytes(receipt.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, receipt)

    monkeypatch.setattr(
        helper, "_discard_operation_receipt_noexcept", replace_before_return
    )
    token = helper.checkpoint(receipt, operation, token)

    with pytest.raises(RuntimeError, match="identity changed"):
        helper._remove_receipt(receipt, token)
    assert receipt.exists()


def test_export_before_is_private_even_when_baseline_is_world_readable(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    managed = tmp_path / "managed"
    exported = tmp_path / "exported"
    managed.write_text("before", encoding="utf-8")
    managed.chmod(0o644)
    helper.begin(receipt, [managed])

    helper.export_before(receipt, managed, exported)

    assert stat.S_IMODE(exported.stat().st_mode) == 0o600


def test_checkpoint_preserves_replacement_of_swapped_old_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    replacement = tmp_path / "replacement.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    ledger_fd = os.open(
        tmp_path / "token-ledger", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
    )
    monkeypatch.setenv("UNIFIED_KANBAN_TOKEN_LEDGER_FD", str(ledger_fd))
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    helper.replace_file(receipt, candidate, managed, operation)
    replacement.write_text('{"foreign":true}\n', encoding="utf-8")
    replacement.chmod(0o600)
    foreign_identity = operation_entry(replacement)["identity"]
    real_unlink = helper._unlink_exact_receipt
    injected = False

    def replace_before_old_receipt_cleanup(
        directory_fd, name, path, expected, label
    ):
        nonlocal injected
        if not injected and label == "previous transaction receipt":
            injected = True
            os.replace(replacement, tmp_path / name)
        return real_unlink(directory_fd, name, path, expected, label)

    monkeypatch.setattr(
        helper, "_unlink_exact_receipt", replace_before_old_receipt_cleanup
    )
    token = helper.checkpoint(receipt, operation, token)

    preserved = [
        path
        for path in tmp_path.iterdir()
        if "receipt.json.receipt." in path.name
    ]
    assert len(preserved) == 1
    assert operation_entry(preserved[0])["identity"] == foreign_identity
    assert managed.read_text(encoding="utf-8") == "after"
    assert not operation.exists()
    helper.rollback(receipt, token)
    assert managed.read_text(encoding="utf-8") == "before"
    os.close(ledger_fd)


def test_replace_file_uses_candidate_mode_for_absent_target(tmp_path: Path) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    target = tmp_path / "target"
    candidate.write_text("content", encoding="utf-8")
    candidate.chmod(0o644)
    helper.begin(receipt, [target])

    helper.replace_file(receipt, candidate, target, operation)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_commit_preserves_receipt_replaced_before_final_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    replacement = tmp_path / "replacement.json"
    managed = tmp_path / "managed"
    managed.write_text("baseline", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    replacement.write_bytes(receipt.read_bytes())
    replacement.chmod(0o600)
    real_read_receipt = helper._read_receipt

    def read_then_replace(path, expected):
        payload = real_read_receipt(path, expected)
        os.replace(replacement, receipt)
        return payload

    monkeypatch.setattr(helper, "_read_receipt", read_then_replace)
    with pytest.raises(RuntimeError, match="changed before commit"):
        helper._remove_receipt(receipt, token)

    assert receipt.exists()
    assert receipt.read_text(encoding="utf-8").startswith("{")


def test_commit_rejects_receipt_replaced_before_initial_snapshot(tmp_path: Path) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    replacement = tmp_path / "replacement.json"
    managed = tmp_path / "managed"
    managed.write_text("baseline", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    replacement.write_bytes(receipt.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, receipt)

    with pytest.raises(RuntimeError, match="receipt identity changed"):
        helper._remove_receipt(receipt, token)

    assert receipt.exists()
    assert receipt.read_text(encoding="utf-8").startswith("{")


def test_commit_preserves_receipt_replaced_at_quarantine_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    replacement = tmp_path / "replacement.json"
    managed = tmp_path / "managed"
    managed.write_text("baseline", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    replacement.write_bytes(receipt.read_bytes())
    replacement.chmod(0o600)
    real_rename = helper.os.rename
    injected = False

    def replace_then_rename(source, destination, *args, **kwargs):
        nonlocal injected
        if source == receipt.name and not injected:
            injected = True
            os.replace(replacement, receipt)
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(helper.os, "rename", replace_then_rename)
    with pytest.raises(RuntimeError, match="changed during removal"):
        helper._remove_receipt(receipt, token)

    assert injected
    assert receipt.exists()


def test_checkpoint_rejects_replaced_operation_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    replacement = tmp_path / "replacement.json"
    managed = tmp_path / "managed"
    managed.write_text("baseline", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    managed.write_text("installed", encoding="utf-8")
    write_operation_receipt(operation, [operation_entry(managed)])
    write_operation_receipt(replacement, [operation_entry(managed)])
    real_read_json = helper._read_json_file
    injected = False

    def replace_then_read(path, label, expected=None):
        nonlocal injected
        if label == "operation receipt" and not injected:
            injected = True
            os.replace(replacement, operation)
        return real_read_json(path, label, expected)

    monkeypatch.setattr(helper, "_read_json_file", replace_then_read)
    with pytest.raises(RuntimeError, match="identity does not match producer receipt"):
        helper.checkpoint(receipt, operation, token)

    assert injected
    assert receipt.exists()
    assert managed.read_text(encoding="utf-8") == "installed"


def test_rollback_restores_file_symlink_and_absent_path(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    regular = tmp_path / "settings.json"
    regular.write_text("before\n", encoding="utf-8")
    regular.chmod(0o640)
    link = tmp_path / "managed-link"
    link.symlink_to("before-target")
    absent = tmp_path / "new-link"

    begun = run("begin", str(receipt), str(regular), str(link), str(absent))
    assert begun.returncode == 0, begun.stderr
    regular.write_text("installed\n", encoding="utf-8")
    link.unlink()
    link.symlink_to("installed-target")
    absent.symlink_to("new-target")
    operation = tmp_path / "operation.json"
    write_operation_receipt(
        operation,
        [operation_entry(regular), operation_entry(link), operation_entry(absent)],
    )
    checkpointed = run("checkpoint", str(receipt), str(operation))
    assert checkpointed.returncode == 0, checkpointed.stderr

    rolled_back = run("rollback", str(receipt))

    assert rolled_back.returncode == 0, rolled_back.stderr
    assert regular.read_text(encoding="utf-8") == "before\n"
    assert regular.stat().st_mode & 0o777 == 0o640
    assert os.readlink(link) == "before-target"
    assert not os.path.lexists(absent)
    assert not receipt.exists()


def test_rollback_refuses_foreign_replacement_but_restores_unchanged_stage(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("before-first", encoding="utf-8")
    second.write_text("before-second", encoding="utf-8")
    assert run("begin", str(receipt), str(first), str(second)).returncode == 0
    first.write_text("installed-first", encoding="utf-8")
    second.write_text("installed-second", encoding="utf-8")
    operation = tmp_path / "operation.json"
    write_operation_receipt(operation, [operation_entry(first), operation_entry(second)])
    assert run("checkpoint", str(receipt), str(operation)).returncode == 0
    foreign = tmp_path / "foreign"
    foreign.write_text("foreign", encoding="utf-8")
    os.replace(foreign, first)

    rolled_back = run("rollback", str(receipt))

    assert rolled_back.returncode != 0
    assert first.read_text(encoding="utf-8") == "foreign"
    assert second.read_text(encoding="utf-8") == "installed-second"
    assert receipt.exists()


def test_checkpoint_does_not_claim_concurrently_replaced_untouched_path(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    changed = tmp_path / "changed"
    untouched = tmp_path / "untouched"
    changed.write_text("before", encoding="utf-8")
    untouched.write_text("before", encoding="utf-8")
    assert run("begin", str(receipt), str(changed), str(untouched)).returncode == 0
    changed.write_text("installed", encoding="utf-8")
    operation = tmp_path / "operation.json"
    write_operation_receipt(operation, [operation_entry(changed)])
    replacement = tmp_path / "replacement"
    replacement.write_text("foreign", encoding="utf-8")
    os.replace(replacement, untouched)

    checkpointed = run("checkpoint", str(receipt), str(operation))
    rolled_back = run("rollback", str(receipt))

    assert checkpointed.returncode == 0, checkpointed.stderr
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert changed.read_text(encoding="utf-8") == "before"
    assert untouched.read_text(encoding="utf-8") == "foreign"


def test_checkpoint_rejects_operation_identity_replaced_before_checkpoint(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    managed = tmp_path / "managed"
    assert run("begin", str(receipt), str(managed)).returncode == 0
    managed.symlink_to("installed")
    operation = tmp_path / "operation.json"
    write_operation_receipt(operation, [operation_entry(managed)])
    managed.unlink()
    managed.symlink_to("foreign")

    result = run("checkpoint", str(receipt), str(operation))

    assert result.returncode != 0
    assert os.readlink(managed) == "foreign"


def test_receipt_symlink_substitution_is_rejected(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    assert run("begin", str(receipt), str(managed)).returncode == 0
    real_receipt = tmp_path / "foreign-receipt.json"
    os.replace(receipt, real_receipt)
    receipt.symlink_to(real_receipt)
    managed.write_text("foreign", encoding="utf-8")
    operation = tmp_path / "operation.json"
    write_operation_receipt(operation, [operation_entry(managed)])

    result = run("checkpoint", str(receipt), str(operation))

    assert result.returncode != 0
    assert managed.read_text(encoding="utf-8") == "foreign"
    assert receipt.is_symlink()


def test_remove_file_receipt_failure_restores_complete_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    managed.chmod(0o644)
    os.utime(
        managed,
        ns=(1_700_000_000_000_000_000, 1_700_000_001_000_000_000),
    )
    os.chflags(managed, stat.UF_HIDDEN)
    subprocess.run(
        ["/usr/bin/xattr", "-w", "com.example.unified-kanban-test", "value", managed],
        check=True,
    )
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read", managed],
        check=True,
    )
    helper.begin(receipt, [managed])
    baseline = helper._read_receipt(receipt)["entries"][0]["before"]
    real_write_receipt = helper._write_receipt

    def fail_operation_receipt(
        path,
        payload,
        exclusive=False,
        expected=None,
        record_token_before_publish=False,
    ):
        if path == operation:
            raise OSError("injected operation receipt failure")
        return real_write_receipt(
            path,
            payload,
            exclusive=exclusive,
            expected=expected,
            record_token_before_publish=record_token_before_publish,
        )

    monkeypatch.setattr(helper, "_write_receipt", fail_operation_receipt)

    with pytest.raises(OSError, match="operation receipt failure"):
        helper.remove_file(receipt, managed, operation)

    after = helper._snapshot(managed)
    assert after["sha256"] == baseline["sha256"]
    assert after["mode"] == baseline["mode"]
    assert after["metadata"] == baseline["metadata"]
    assert not operation.exists()


def test_rollback_does_not_replace_leaf_created_during_exclusive_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    helper.remove_file(receipt, managed, operation)
    token = helper.checkpoint(receipt, operation, token)
    real_link = os.link
    injected = False

    def create_foreign_then_link(src, dst, **kwargs):
        nonlocal injected
        if not injected and dst == managed.name:
            injected = True
            fd = os.open(
                dst,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=kwargs["dst_dir_fd"],
            )
            try:
                os.write(fd, b"foreign")
            finally:
                os.close(fd)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(helper.os, "link", create_foreign_then_link)

    with pytest.raises(FileExistsError):
        helper.rollback(receipt, token)

    assert managed.read_text(encoding="utf-8") == "foreign"
    assert receipt.exists()


def test_replace_file_does_not_replace_leaf_created_after_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    managed = tmp_path / "managed"
    candidate = tmp_path / "candidate"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("candidate", encoding="utf-8")
    helper.begin(receipt, [managed])
    real_link = os.link
    injected = False

    def create_foreign_then_link(src, dst, **kwargs):
        nonlocal injected
        if not injected and dst == managed.name:
            injected = True
            fd = os.open(
                dst,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=kwargs["dst_dir_fd"],
            )
            try:
                os.write(fd, b"foreign")
            finally:
                os.close(fd)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(helper.os, "link", create_foreign_then_link)

    with pytest.raises(FileExistsError):
        helper.replace_file(receipt, candidate, managed, operation)

    assert managed.read_text(encoding="utf-8") == "foreign"
    assert not operation.exists()
    quarantines = list(tmp_path.glob(".managed.original.*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_text(encoding="utf-8") == "before"
    assert stat.S_IMODE(quarantines[0].stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS quarantine privacy")
def test_quarantine_private_mode_clears_extended_acl_and_bsd_flags(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    managed = tmp_path / "managed"
    managed.write_text("private", encoding="utf-8")
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read", managed],
        check=True,
    )
    os.chflags(managed, stat.UF_HIDDEN)
    directory_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        expected = helper._identity(
            os.stat(managed.name, dir_fd=directory_fd, follow_symlinks=False)
        )
        helper._chmod_expected_at(directory_fd, managed.name, expected, 0o600)
        info = os.stat(managed.name, dir_fd=directory_fd, follow_symlinks=False)
        opened = os.open(managed.name, os.O_RDONLY, dir_fd=directory_fd)
        try:
            acl = helper._mac_acl(opened)
        finally:
            os.close(opened)
    finally:
        os.close(directory_fd)

    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_flags == 0
    assert acl in {None, ""}


def test_replace_file_restores_original_when_quarantine_chmod_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    managed = tmp_path / "managed"
    candidate = tmp_path / "candidate"
    managed.write_text("before", encoding="utf-8")
    managed.chmod(0o640)
    candidate.write_text("candidate", encoding="utf-8")
    helper.begin(receipt, [managed])
    real_chmod = helper._chmod_expected_at
    calls = 0

    def fail_first_chmod(directory_fd, name, expected_identity, mode):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected quarantine chmod failure")
        return real_chmod(directory_fd, name, expected_identity, mode)

    monkeypatch.setattr(helper, "_chmod_expected_at", fail_first_chmod)

    with pytest.raises(OSError, match="quarantine chmod failure"):
        helper.replace_file(receipt, candidate, managed, operation)

    assert managed.read_text(encoding="utf-8") == "before"
    assert stat.S_IMODE(managed.stat().st_mode) == 0o640
    assert not operation.exists()
    assert not list(tmp_path.glob(".managed.original.*"))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS metadata contract")
def test_rollback_restores_xattrs_and_extended_acl(tmp_path: Path) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    candidate.write_text("after", encoding="utf-8")
    baseline_atime_ns = 1_700_000_000_123_456_789
    baseline_mtime_ns = 1_700_000_100_987_654_321
    os.utime(managed, ns=(baseline_atime_ns, baseline_mtime_ns))
    os.chflags(managed, stat.UF_HIDDEN)
    subprocess.run(
        ["/usr/bin/xattr", "-w", "com.example.unified-kanban-test", "value", managed],
        check=True,
    )
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read", managed],
        check=True,
    )
    token = helper.begin(receipt, [managed])
    before = helper._read_receipt(receipt)["entries"][0]["before"]
    helper.replace_file(receipt, candidate, managed, operation)
    token = helper.checkpoint(receipt, operation, token)
    helper.rollback(receipt, token)
    after = helper._snapshot(managed)

    assert after["sha256"] == before["sha256"]
    assert after["mode"] == before["mode"]
    assert after["metadata"]["xattrs"] == before["metadata"]["xattrs"]
    assert after["metadata"]["acl"] == before["metadata"]["acl"]
    assert after["metadata"]["uid"] == before["metadata"]["uid"]
    assert after["metadata"]["gid"] == before["metadata"]["gid"]
    assert after["metadata"]["atime_ns"] == before["metadata"]["atime_ns"]
    assert after["metadata"]["mtime_ns"] == before["metadata"]["mtime_ns"]
    assert after["metadata"]["flags"] == before["metadata"]["flags"]
    assert after["metadata"]["flags"] & stat.UF_HIDDEN
    assert not operation.exists()
    assert not list(tmp_path.glob(".managed.original.*"))


def test_rollback_quarantine_is_private_before_baseline_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    managed.chmod(0o644)
    baseline_atime_ns = 1_700_000_000_000_000_000
    baseline_mtime_ns = 1_700_000_001_000_000_000
    os.utime(managed, ns=(baseline_atime_ns, baseline_mtime_ns))
    os.chflags(managed, stat.UF_HIDDEN)
    subprocess.run(
        ["/usr/bin/xattr", "-w", "com.example.unified-kanban-test", "value", managed],
        check=True,
    )
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read", managed],
        check=True,
    )
    token = helper.begin(receipt, [managed])
    managed.write_text("installed", encoding="utf-8")
    write_operation_receipt(operation, [operation_entry(managed)])
    token = helper.checkpoint(receipt, operation, token)
    installed = helper._read_receipt(receipt)["entries"][0]["installed"]
    real_publish = helper._publish_temp_exclusive
    observed_modes = []

    def inspect_then_fail(directory_fd, source, destination, expected):
        if destination == managed.name and ".rollback." in source:
            quarantines = list(tmp_path.glob(".managed.quarantine.*"))
            assert len(quarantines) == 1
            observed_modes.append(stat.S_IMODE(quarantines[0].stat().st_mode))
            raise OSError("injected baseline publish failure")
        return real_publish(directory_fd, source, destination, expected)

    monkeypatch.setattr(helper, "_publish_temp_exclusive", inspect_then_fail)

    with pytest.raises(OSError, match="baseline publish failure"):
        helper.rollback(receipt, token)

    assert observed_modes == [0o600]
    after = helper._snapshot(managed)
    assert managed.read_text(encoding="utf-8") == "installed"
    assert stat.S_IMODE(managed.stat().st_mode) == 0o644
    assert after["metadata"] == installed["metadata"]
    assert receipt.exists()


def test_rollback_cleanup_failure_restores_installed_inode_without_debris(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    managed = tmp_path / "managed"
    managed.write_text("before", encoding="utf-8")
    token = helper.begin(receipt, [managed])
    managed.write_text("installed", encoding="utf-8")
    write_operation_receipt(operation, [operation_entry(managed)])
    token = helper.checkpoint(receipt, operation, token)
    real_unlink = helper._unlink_exact_receipt

    def fail_rollback_quarantine(
        directory_fd, name, display, expected, label
    ):
        if label == "rollback quarantine":
            raise OSError("injected rollback quarantine cleanup failure")
        return real_unlink(directory_fd, name, display, expected, label)

    monkeypatch.setattr(helper, "_unlink_exact_receipt", fail_rollback_quarantine)

    with pytest.raises(OSError, match="quarantine cleanup failure"):
        helper.rollback(receipt, token)

    assert managed.read_text(encoding="utf-8") == "installed"
    assert receipt.exists()
    assert not list(tmp_path.glob(".managed.quarantine.*"))
    assert not list(tmp_path.glob(".managed.failed-rollback.*"))


def test_rollback_rejects_swapped_parent_and_preserves_both_locations(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    parent = tmp_path / "parent"
    old_parent = tmp_path / "old-parent"
    foreign_parent = tmp_path / "foreign-parent"
    parent.mkdir()
    foreign_parent.mkdir()
    managed = parent / "managed"
    managed.write_text("before", encoding="utf-8")
    assert run("begin", str(receipt), str(managed)).returncode == 0
    managed.write_text("installed", encoding="utf-8")
    write_operation_receipt(operation, [operation_entry(managed)])
    assert run("checkpoint", str(receipt), str(operation)).returncode == 0
    parent.rename(old_parent)
    parent.symlink_to(foreign_parent, target_is_directory=True)
    (foreign_parent / "managed").write_text("foreign", encoding="utf-8")

    rolled_back = run("rollback", str(receipt))

    assert rolled_back.returncode != 0
    assert (old_parent / "managed").read_text(encoding="utf-8") == "installed"
    assert (foreign_parent / "managed").read_text(encoding="utf-8") == "foreign"
    assert receipt.exists()


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_begin_rejects_unsupported_leaf_types(tmp_path: Path, kind: str) -> None:
    receipt = tmp_path / "receipt.json"
    managed = tmp_path / "managed"
    if kind == "directory":
        managed.mkdir()
    else:
        os.mkfifo(managed)

    result = run("begin", str(receipt), str(managed))

    assert result.returncode != 0
    assert not receipt.exists()


def test_checkpoint_rejects_duplicate_and_forged_operation_paths(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    managed = tmp_path / "managed"
    forged = tmp_path / "forged"
    assert run("begin", str(receipt), str(managed)).returncode == 0
    managed.symlink_to("installed")
    entry = operation_entry(managed)
    write_operation_receipt(operation, [entry, entry])
    duplicate = run("checkpoint", str(receipt), str(operation))
    forged.symlink_to("foreign")
    write_operation_receipt(operation, [operation_entry(forged)])
    unknown = run("checkpoint", str(receipt), str(operation))

    assert duplicate.returncode != 0
    assert unknown.returncode != 0
    assert os.readlink(managed) == "installed"
    assert os.readlink(forged) == "foreign"


def test_rollback_removes_transaction_created_parent_chain(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    managed = tmp_path / "created/parent/managed"
    assert run("begin", str(receipt), str(managed)).returncode == 0
    managed.symlink_to("installed")
    write_operation_receipt(operation, [operation_entry(managed)])
    assert run("checkpoint", str(receipt), str(operation)).returncode == 0

    rolled_back = run("rollback", str(receipt))

    assert rolled_back.returncode == 0, rolled_back.stderr
    assert not managed.exists()
    assert not (tmp_path / "created").exists()
    assert not receipt.exists()


def test_commit_keeps_transaction_created_parent_chain(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    managed = tmp_path / "created/parent/managed"
    assert run("begin", str(receipt), str(managed)).returncode == 0

    committed = run("commit", str(receipt))

    assert committed.returncode == 0, committed.stderr
    assert managed.parent.is_dir()
    assert not receipt.exists()


def test_chmod_of_transaction_created_parent_prevents_rollback_removal(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    managed = tmp_path / "created/managed"
    assert run("begin", str(receipt), str(managed)).returncode == 0
    managed.parent.chmod(0o755)

    rolled_back = run("rollback", str(receipt))

    assert rolled_back.returncode != 0
    assert managed.parent.is_dir()
    assert stat.S_IMODE(managed.parent.stat().st_mode) == 0o755
    assert receipt.exists()


def test_foreign_content_in_created_parent_prevents_any_leaf_rollback(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    existing = tmp_path / "existing"
    existing.write_text("before", encoding="utf-8")
    nested = tmp_path / "created/managed"
    assert run("begin", str(receipt), str(existing), str(nested)).returncode == 0
    existing.write_text("installed-existing", encoding="utf-8")
    nested.write_text("installed-nested", encoding="utf-8")
    write_operation_receipt(
        operation, [operation_entry(existing), operation_entry(nested)]
    )
    assert run("checkpoint", str(receipt), str(operation)).returncode == 0
    (nested.parent / "foreign").write_text("foreign", encoding="utf-8")

    rolled_back = run("rollback", str(receipt))

    assert rolled_back.returncode != 0
    assert existing.read_text(encoding="utf-8") == "installed-existing"
    assert nested.read_text(encoding="utf-8") == "installed-nested"
    assert (nested.parent / "foreign").read_text(encoding="utf-8") == "foreign"
    assert receipt.exists()


def test_foreign_unmanaged_planned_leaf_prevents_any_rollback(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    existing = tmp_path / "existing"
    existing.write_text("before", encoding="utf-8")
    managed = tmp_path / "created/managed"
    planned = tmp_path / "created/planned"
    assert run("begin", str(receipt), str(existing), str(managed), str(planned)).returncode == 0
    existing.write_text("installed-existing", encoding="utf-8")
    managed.write_text("installed-managed", encoding="utf-8")
    write_operation_receipt(
        operation, [operation_entry(existing), operation_entry(managed)]
    )
    assert run("checkpoint", str(receipt), str(operation)).returncode == 0
    planned.write_text("foreign", encoding="utf-8")

    rolled_back = run("rollback", str(receipt))

    assert rolled_back.returncode != 0
    assert existing.read_text(encoding="utf-8") == "installed-existing"
    assert managed.read_text(encoding="utf-8") == "installed-managed"
    assert planned.read_text(encoding="utf-8") == "foreign"
    assert receipt.exists()


def test_private_cleanup_preserves_substituted_transaction_directory(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    transaction = tmp_path / "transaction"
    moved = tmp_path / "moved-transaction"
    transaction.mkdir(mode=0o700)
    (transaction / "raw-secret").write_text("secret", encoding="utf-8")
    retained_fd = os.open(transaction, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    transaction.rename(moved)
    transaction.mkdir(mode=0o700)
    (transaction / "foreign").write_text("preserve", encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="directory changed"):
            helper.cleanup_private_dir(transaction, retained_fd)
    finally:
        os.close(retained_fd)

    assert (transaction / "foreign").read_text(encoding="utf-8") == "preserve"
    assert list(moved.iterdir()) == []


def test_created_parent_replacement_before_final_rmdir_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "receipt.json"
    operation = tmp_path / "operation.json"
    managed = tmp_path / "created/managed"
    moved = tmp_path / "moved-created"
    token = helper.begin(receipt, [managed])
    managed.write_text("installed", encoding="utf-8")
    write_operation_receipt(operation, [operation_entry(managed)])
    token = helper.checkpoint(receipt, operation, token)
    created_identity = os.stat(managed.parent).st_ino
    real_listdir = helper.os.listdir
    calls = 0

    def replace_before_final_remove(fd):
        nonlocal calls
        if os.fstat(fd).st_ino == created_identity:
            calls += 1
            if calls == 2:
                managed.parent.rename(moved)
                managed.parent.mkdir()
        return real_listdir(fd)

    monkeypatch.setattr(helper.os, "listdir", replace_before_final_remove)
    with pytest.raises(RuntimeError, match="changed before removal"):
        helper.rollback(receipt, token)
    monkeypatch.undo()

    assert managed.parent.is_dir()
    assert list(managed.parent.iterdir()) == []
    assert moved.is_dir()
    assert receipt.exists()


def test_private_cleanup_preserves_replaced_nested_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    transaction = tmp_path / "transaction"
    nested = transaction / "nested"
    nested.mkdir(parents=True, mode=0o700)
    secret = nested / "secret"
    secret.write_text("original", encoding="utf-8")
    retained = nested / "retained-original"
    transaction_fd = os.open(transaction, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    real_stat = helper.os.stat
    replaced = False

    def replace_after_first_stat(path, *args, **kwargs):
        nonlocal replaced
        info = real_stat(path, *args, **kwargs)
        if path == "secret" and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            os.rename("secret", "retained-original", src_dir_fd=kwargs["dir_fd"], dst_dir_fd=kwargs["dir_fd"])
            fd = os.open(
                "secret",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=kwargs["dir_fd"],
            )
            os.write(fd, b"foreign")
            os.close(fd)
        return info

    monkeypatch.setattr(helper.os, "stat", replace_after_first_stat)
    try:
        with pytest.raises(RuntimeError, match="leaf changed before removal"):
            helper.cleanup_private_dir(transaction, transaction_fd)
    finally:
        os.close(transaction_fd)

    assert secret.read_bytes() == b"foreign"
    assert retained.read_bytes() == b"original"


def test_transaction_fd_keeps_receipts_bound_after_directory_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    transaction = tmp_path / "transaction"
    moved = tmp_path / "moved-transaction"
    transaction.mkdir(mode=0o700)
    retained_fd = os.open(transaction, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    monkeypatch.setenv("UNIFIED_KANBAN_TRANSACTION_DIR", str(transaction))
    monkeypatch.setenv("UNIFIED_KANBAN_TRANSACTION_FD", str(retained_fd))
    receipt = transaction / "manifest.json"
    operation = transaction / "operation.json"
    candidate = transaction / "candidate"
    target = tmp_path / "managed"
    target.write_text("before", encoding="utf-8")
    token = helper.begin(receipt, [target])
    candidate.write_text("installed", encoding="utf-8")
    transaction.rename(moved)
    transaction.mkdir(mode=0o700)
    (transaction / "foreign").write_text("preserve", encoding="utf-8")
    try:
        helper.replace_file(receipt, candidate, target, operation)
        token = helper.checkpoint(receipt, operation, token)
        helper.rollback(receipt, token)
        assert target.read_text(encoding="utf-8") == "before"
        with pytest.raises(RuntimeError, match="directory changed"):
            helper.cleanup_private_dir(transaction, retained_fd)
    finally:
        os.close(retained_fd)

    assert (transaction / "foreign").read_text(encoding="utf-8") == "preserve"
    assert list(moved.iterdir()) == []


def test_replace_file_ignores_final_descriptor_close_failure_after_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    receipt = tmp_path / "manifest.json"
    operation = tmp_path / "operation.json"
    candidate = tmp_path / "candidate"
    target = tmp_path / "managed"
    target.write_text("before", encoding="utf-8")
    candidate.write_text("installed", encoding="utf-8")
    helper.begin(receipt, [target])
    real_close = helper.os.close
    real_open_parent = helper._open_parent
    target_fds = set()
    failures = 0

    def track_target_parent(path, **kwargs):
        result = real_open_parent(path, **kwargs)
        if path == target:
            target_fds.add(result[0])
        return result

    def fail_target_close(fd):
        nonlocal failures
        if failures == 0 and operation.exists() and fd in target_fds:
            failures += 1
            raise OSError("injected final close failure")
        return real_close(fd)

    monkeypatch.setattr(helper, "_open_parent", track_target_parent)
    monkeypatch.setattr(helper.os, "close", fail_target_close)
    helper.replace_file(receipt, candidate, target, operation)

    assert failures == 1
    assert target.read_text(encoding="utf-8") == "installed"
    assert operation.exists()


def test_restore_swap_race_preserves_foreign_successor_at_canonical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    target = tmp_path / "managed"
    target.write_text("baseline", encoding="utf-8")
    before = helper._snapshot(target)
    target.write_text("transaction-owned", encoding="utf-8")
    expected = helper._snapshot(target)
    original_swap = helper._swap_names_atomic
    raced = False

    def race_then_swap(directory_fd: int, first: str, second: str) -> None:
        nonlocal raced
        if not raced and first == target.name and ".quarantine." in second:
            raced = True
            target.unlink()
            target.write_text("foreign-successor", encoding="utf-8")
        original_swap(directory_fd, first, second)

    monkeypatch.setattr(helper, "_swap_names_atomic", race_then_swap)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="path changed during transaction restore"):
            helper._restore_bound(target, directory_fd, target.name, before, expected)
    finally:
        os.close(directory_fd)

    assert raced
    assert target.read_text(encoding="utf-8") == "foreign-successor"


def test_restore_swap_fsync_failure_restores_foreign_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    target = tmp_path / "managed"
    target.write_text("baseline", encoding="utf-8")
    before = helper._snapshot(target)
    target.write_text("transaction-owned", encoding="utf-8")
    expected = helper._snapshot(target)
    swap_name = "_swap_names_atomic" if hasattr(helper, "_swap_names_atomic") else "_swap_names"
    original_swap = getattr(helper, swap_name)
    original_fsync = helper.os.fsync
    fail_next_fsync = False
    failed = False
    raced = False

    def race_then_swap(directory_fd: int, first: str, second: str) -> None:
        nonlocal fail_next_fsync, raced
        if not raced and first == target.name and ".quarantine." in second:
            raced = True
            target.unlink()
            target.write_text("foreign-successor", encoding="utf-8")
            fail_next_fsync = True
        original_swap(directory_fd, first, second)

    def fail_post_swap_fsync(fd: int) -> None:
        nonlocal fail_next_fsync, failed
        if fail_next_fsync and not failed:
            fail_next_fsync = False
            failed = True
            raise OSError(5, "injected post-swap fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(helper, swap_name, race_then_swap)
    monkeypatch.setattr(helper.os, "fsync", fail_post_swap_fsync)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError, match="post-swap fsync failure"):
            helper._restore_bound(target, directory_fd, target.name, before, expected)
    finally:
        os.close(directory_fd)

    assert failed
    assert target.read_text(encoding="utf-8") == "foreign-successor"
    assert not list(tmp_path.glob(".managed.quarantine.*"))


def test_restore_post_swap_snapshot_failure_restores_canonical_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    target = tmp_path / "managed"
    target.write_text("baseline", encoding="utf-8")
    before = helper._snapshot(target)
    target.write_text("transaction-owned", encoding="utf-8")
    expected = helper._snapshot(target)
    expected_inode = target.stat().st_ino
    original_swap = helper._swap_names_atomic
    original_snapshot_at = helper._snapshot_at
    exchanged = False
    failed = False

    def observe_swap(directory_fd: int, first: str, second: str) -> None:
        nonlocal exchanged
        original_swap(directory_fd, first, second)
        if first == target.name and ".quarantine." in second:
            exchanged = True

    def fail_quarantine_snapshot(directory_fd, name, display):
        nonlocal failed
        if exchanged and not failed and ".quarantine." in name:
            failed = True
            raise RuntimeError("injected post-swap snapshot failure")
        return original_snapshot_at(directory_fd, name, display)

    monkeypatch.setattr(helper, "_swap_names_atomic", observe_swap)
    monkeypatch.setattr(helper, "_snapshot_at", fail_quarantine_snapshot)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="post-swap snapshot failure"):
            helper._restore_bound(target, directory_fd, target.name, before, expected)
    finally:
        os.close(directory_fd)

    assert failed
    assert target.stat().st_ino == expected_inode
    assert target.read_text(encoding="utf-8") == "transaction-owned"
    assert not list(tmp_path.glob(".managed.quarantine.*"))


@pytest.mark.parametrize("foreign_kind", ["directory", "oversized", "unreadable"])
def test_restore_arbitrary_foreign_race_preserves_inode_at_canonical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign_kind: str
) -> None:
    helper = load_helper()
    target = tmp_path / "managed"
    target.write_text("baseline", encoding="utf-8")
    before = helper._snapshot(target)
    target.write_text("transaction-owned", encoding="utf-8")
    expected = helper._snapshot(target)
    original_swap = helper._swap_names_atomic
    foreign_inode = 0
    raced = False

    def race_then_swap(directory_fd: int, first: str, second: str) -> None:
        nonlocal foreign_inode, raced
        if not raced and first == target.name and ".quarantine." in second:
            raced = True
            target.unlink()
            if foreign_kind == "directory":
                target.mkdir()
            elif foreign_kind == "oversized":
                target.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
            else:
                target.write_text("unreadable-foreign", encoding="utf-8")
                target.chmod(0)
            foreign_inode = target.stat().st_ino
        original_swap(directory_fd, first, second)

    monkeypatch.setattr(helper, "_swap_names_atomic", race_then_swap)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="path changed during transaction restore"):
            helper._restore_bound(target, directory_fd, target.name, before, expected)
    finally:
        os.close(directory_fd)

    assert raced
    if foreign_kind == "directory":
        assert target.is_dir()
    else:
        assert target.is_file()
    assert target.stat().st_ino == foreign_inode
    assert not list(tmp_path.glob(".managed.quarantine.*"))


def test_restore_retries_failed_foreign_recovery_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    target = tmp_path / "managed"
    target.write_text("baseline", encoding="utf-8")
    before = helper._snapshot(target)
    target.write_text("transaction-owned", encoding="utf-8")
    expected = helper._snapshot(target)
    swap_name = "_swap_names_atomic" if hasattr(helper, "_swap_names_atomic") else "_swap_names"
    original_swap = getattr(helper, swap_name)
    calls = 0

    def race_fail_once_then_swap(directory_fd: int, first: str, second: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.unlink()
            target.write_text("foreign-successor", encoding="utf-8")
            original_swap(directory_fd, first, second)
            return
        if calls == 2:
            raise OSError(5, "injected recovery swap failure")
        original_swap(directory_fd, first, second)

    monkeypatch.setattr(helper, swap_name, race_fail_once_then_swap)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="path changed during transaction restore"):
            helper._restore_bound(target, directory_fd, target.name, before, expected)
    finally:
        os.close(directory_fd)

    assert calls == 3
    assert target.read_text(encoding="utf-8") == "foreign-successor"
    assert not list(tmp_path.glob(".managed.quarantine.*"))


def test_replace_file_target_mode_overrides_existing_mode_and_rolls_back(
    tmp_path: Path,
) -> None:
    target = tmp_path / "managed.plist"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)
    candidate = tmp_path / "candidate.plist"
    candidate.write_text("new", encoding="utf-8")
    candidate.chmod(0o600)
    receipt = tmp_path / "transaction.json"
    operation = tmp_path / "operation.json"

    helper = load_helper()
    token = helper.begin(receipt, [target])
    helper.replace_file(receipt, candidate, target, operation, target_mode=0o600)
    token = helper.checkpoint(receipt, operation, token)

    assert target.read_text(encoding="utf-8") == "new"
    assert target.stat().st_mode & 0o777 == 0o600

    helper.rollback(receipt, token)
    assert target.read_text(encoding="utf-8") == "old"
    assert target.stat().st_mode & 0o777 == 0o644


def test_replace_file_checkpoint_rejects_mode_change(tmp_path: Path) -> None:
    helper = load_helper()
    target = tmp_path / "managed.plist"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)
    candidate = tmp_path / "candidate.plist"
    candidate.write_text("new", encoding="utf-8")
    candidate.chmod(0o600)
    receipt = tmp_path / "transaction.json"
    operation = tmp_path / "operation.json"

    token = helper.begin(receipt, [target])
    helper.replace_file(receipt, candidate, target, operation, target_mode=0o600)
    target.chmod(0o640)

    with pytest.raises(RuntimeError, match="file mode changed"):
        helper.checkpoint(receipt, operation, token)

    # 모드 변경으로 게시된 inode는 외부 후속 객체가 된다. 트랜잭션은 이전 기준
    # 상태를 복구한다는 이유만으로 이를 제거해서는 안 된다.
    assert target.read_text(encoding="utf-8") == "new"
    assert target.stat().st_mode & 0o777 == 0o640
    assert receipt.exists()
