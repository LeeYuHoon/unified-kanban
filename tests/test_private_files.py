from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import kanban_adapter.private_files as private_files


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino


def test_initial_publish_does_not_replace_foreign_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    real_rename = private_files._rename_exclusive

    def race_rename(directory_fd: int, source: str, destination: str) -> None:
        if destination == path.name:
            path.write_text("foreign", encoding="utf-8")
        real_rename(directory_fd, source, destination)

    monkeypatch.setattr(private_files, "_rename_exclusive", race_rename)

    with pytest.raises(FileExistsError):
        private_files.atomic_publish(path, b"ours")

    assert path.read_text(encoding="utf-8") == "foreign"


def test_expected_replacement_restores_foreign_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"old")
    expected = _identity(path)
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign")
    real_swap = private_files._swap_names
    swaps = 0

    def substitute_then_swap(directory_fd: int, first: str, second: str) -> None:
        nonlocal swaps
        if swaps == 0:
            os.replace(foreign, path)
        swaps += 1
        real_swap(directory_fd, first, second)

    monkeypatch.setattr(private_files, "_swap_names", substitute_then_swap)

    with pytest.raises(
        private_files.NamespaceAuthorityError,
        match="restored an untrusted canonical entry",
    ):
        private_files.atomic_publish(path, b"new", expected_identity=expected)

    assert swaps == 1
    assert path.read_bytes() == b"foreign"


def test_failed_swap_back_retains_displaced_foreign_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"old")
    expected = _identity(path)
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign")
    foreign_identity = _identity(foreign)
    real_swap = private_files._swap_names
    swaps = 0

    def substitute_then_fail(directory_fd: int, first: str, second: str) -> None:
        nonlocal swaps
        if swaps == 0:
            os.replace(foreign, path)
            swaps += 1
            real_swap(directory_fd, first, second)
            return
        swaps += 1
        raise OSError("injected swap-back failure")

    monkeypatch.setattr(private_files, "_swap_names", substitute_then_fail)
    real_rename = private_files._rename_exclusive
    def fail_recovery(directory_fd, source, destination):
        if swaps:
            raise OSError("injected exclusive recovery failure")
        real_rename(directory_fd, source, destination)
    monkeypatch.setattr(private_files, "_rename_exclusive", fail_recovery)

    with pytest.raises(
        RuntimeError, match="could not restore canonical namespace"
    ):
        private_files.atomic_publish(path, b"new", expected_identity=expected)

    retained = [item for item in tmp_path.iterdir() if item.is_file() and _identity(item) == foreign_identity]
    assert len(retained) == 1
    assert retained[0].read_bytes() == b"foreign"
    assert path.read_bytes() == b"new"


def test_retirement_restores_foreign_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"ours")
    expected = _identity(path)
    displaced = tmp_path / "displaced"
    real_rename = private_files._rename_exclusive
    calls = 0

    def substitute_before_verification(directory_fd: int, source: str, destination: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_rename(directory_fd, source, destination)
            os.rename(destination, displaced.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            os.close(fd)
            return
        real_rename(directory_fd, source, destination)

    monkeypatch.setattr(private_files, "_rename_exclusive", substitute_before_verification)

    with pytest.raises(RuntimeError, match="identity or link count changed"):
        private_files.retire_expected(path, expected)

    assert path.read_bytes() == b""
    assert displaced.read_bytes() == b"ours"


def test_failed_retirement_restore_retains_foreign_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"ours")
    expected = _identity(path)
    displaced = tmp_path / "displaced"
    real_rename = private_files._rename_exclusive
    calls = 0

    def substitute_then_fail_restore(directory_fd: int, source: str, destination: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_rename(directory_fd, source, destination)
            os.rename(destination, displaced.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
            os.close(fd)
            return
        raise OSError("injected restore failure")

    monkeypatch.setattr(private_files, "_rename_exclusive", substitute_then_fail_restore)

    with pytest.raises(RuntimeError, match="foreign successor retained"):
        private_files.retire_expected(path, expected)

    foreign = [item for item in tmp_path.iterdir() if item.is_file() and item != displaced]
    assert len(foreign) == 1
    assert foreign[0].read_bytes() == b""
    assert displaced.read_bytes() == b"ours"


def test_private_text_uses_random_128_bit_0600_capability_name(tmp_path: Path) -> None:
    predictable = tmp_path / "state.title"
    predictable.write_text("foreign", encoding="utf-8")

    path, identity = private_files.create_private_text(tmp_path, "secret", label="title")

    assert path != predictable
    assert path.parent == tmp_path
    assert len(path.name.rsplit(".", 1)[-1]) == 32
    assert path.read_text(encoding="utf-8") == "secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _identity(path) == identity
    assert predictable.read_text(encoding="utf-8") == "foreign"
    private_files.retire_expected(path, identity)


def test_read_bytes_returns_opened_identity(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"payload")

    payload, identity = private_files.read_bytes(path)

    assert payload == b"payload"
    assert identity == _identity(path)
    identity.close()


def test_anonymous_text_is_unlinked_but_readable_by_fd(tmp_path: Path) -> None:
    receipt = private_files.create_anonymous_text(
        tmp_path, "secret", label="result"
    )
    try:
        assert not list(tmp_path.iterdir())
        assert os.fstat(receipt.file_fd).st_nlink == 0
        assert os.read(receipt.file_fd, 64) == b"secret"
    finally:
        receipt.close()


def test_open_directory_rejects_intermediate_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        private_files.open_directory(link / "cache", create=True)

    assert not (outside / "cache").exists()


def test_read_receipt_rejects_hardlinked_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"state")
    os.link(path, tmp_path / "alias")

    before = len(os.listdir("/dev/fd"))
    with pytest.raises(RuntimeError, match="singly-linked"):
        private_files.read_bytes(path)
    assert len(os.listdir("/dev/fd")) == before


def test_retained_directory_fd_defeats_parent_substitution(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    directory_fd = private_files.open_directory(cache)
    retained = tmp_path / "retained"
    cache.rename(retained)
    cache.mkdir()
    try:
        exposed, receipt = private_files.create_private_text(
            cache, "secret", label="title", directory_fd=directory_fd
        )
    finally:
        os.close(directory_fd)

    assert not exposed.exists()
    assert not list(cache.iterdir())
    assert (retained / receipt.name).read_text(encoding="utf-8") == "secret"
    private_files.retire_expected(retained / receipt.name, receipt)


def test_post_install_failure_carries_installed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    real_fsync = private_files.os.fsync
    calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(private_files.os, "fsync", fail_directory_fsync)
    with pytest.raises(private_files.CommittedPublicationError) as raised:
        private_files.atomic_publish(path, b"installed")

    assert path.read_bytes() == b"installed"
    assert raised.value.receipt.identity == _identity(path)
    raised.value.receipt.close()


def test_replacement_does_not_adopt_foreign_successor_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"old")
    old = private_files.read_bytes(path)[1]
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign")
    real_swap = private_files._swap_names
    raced = False

    def race_once(directory_fd: int, first: str, second: str) -> None:
        nonlocal raced
        if not raced:
            raced = True
            os.replace(foreign, path)
        real_swap(directory_fd, first, second)

    monkeypatch.setattr(private_files, "_swap_names", race_once)
    with pytest.raises(
        private_files.NamespaceAuthorityError,
        match="restored an untrusted canonical entry",
    ):
        private_files.atomic_publish(path, b"first", expected_identity=old)
    assert path.read_bytes() == b"foreign"


def test_restore_detached_rejects_displaced_parent_and_redetaches_state(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    path = cache / "state.json"
    receipt = private_files.atomic_publish(path, b"state")
    directory_fd = os.dup(receipt.directory_fd)
    detached = private_files.detach_expected(
        path, receipt, directory_fd=directory_fd
    )
    detached_name = detached.name
    retained = tmp_path / "retained"
    cache.rename(retained)
    cache.mkdir()

    try:
        with pytest.raises(
            private_files.NamespaceAuthorityError,
            match="no longer occupies its canonical pathname",
        ):
            private_files.restore_detached(path, detached)
    finally:
        os.close(directory_fd)

    assert not (cache / "state.json").exists()
    assert not (retained / "state.json").exists()
    assert (retained / detached_name).read_bytes() == b"state"


def test_restore_detached_fsync_failure_redetaches_after_parent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    path = cache / "state.json"
    receipt = private_files.atomic_publish(path, b"state")
    directory_fd = os.dup(receipt.directory_fd)
    detached = private_files.detach_expected(
        path, receipt, directory_fd=directory_fd
    )
    detached_name = detached.name
    retained = tmp_path / "retained"
    real_fsync = private_files.os.fsync
    raced = False

    def replace_parent_and_fail_once(fd: int) -> None:
        nonlocal raced
        if fd == detached.directory_fd and not raced:
            raced = True
            cache.rename(retained)
            cache.mkdir()
            raise OSError("restore directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(private_files.os, "fsync", replace_parent_and_fail_once)
    try:
        with pytest.raises(
            private_files.NamespaceAuthorityError,
            match="restored state lost canonical authority",
        ):
            private_files.restore_detached(path, detached)
    finally:
        os.close(directory_fd)

    with pytest.raises(OSError):
        os.fstat(detached.directory_fd)
    with pytest.raises(OSError):
        os.fstat(detached.file_fd)
    assert not (cache / "state.json").exists()
    assert not (retained / "state.json").exists()
    assert (retained / detached_name).read_bytes() == b"state"


def test_atomic_publish_dup_failure_has_no_fd_leak(tmp_path, monkeypatch):
    from kanban_adapter import private_files as private
    before = set(os.listdir("/dev/fd"))
    def fail(fd):
        raise OSError("injected receipt dup")
    with monkeypatch.context() as patch:
        patch.setattr(private.os, "dup", fail)
        with pytest.raises(OSError, match="injected receipt dup"):
            private.atomic_publish(tmp_path / "new", b"private")
    assert set(os.listdir("/dev/fd")) == before
    assert not list(tmp_path.iterdir())


def test_atomic_publish_existing_leaf_dup_failure_closes_fd(tmp_path, monkeypatch):
    from kanban_adapter import private_files as private
    path = tmp_path / "existing"
    with private.atomic_publish(path, b"original") as receipt:
        identity = receipt.identity
    before = set(os.listdir("/dev/fd"))
    original_dup = os.dup
    calls = 0
    def fail(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected existing receipt dup")
        return original_dup(fd)
    with monkeypatch.context() as patch:
        patch.setattr(private.os, "dup", fail)
        with pytest.raises(OSError, match="injected existing receipt dup"):
            private.atomic_publish(path, b"replacement", expected_identity=identity)
    assert set(os.listdir("/dev/fd")) == before
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]

@pytest.mark.parametrize("failure", ["successor", "recovery-io"])
def test_recovery_never_overwrites_or_removes_canonical(tmp_path, monkeypatch, failure):
    path = tmp_path / "state.json"
    path.write_bytes(b"old")
    _, expected = private_files.read_bytes(path)
    real_swap = private_files._swap_names
    real_rename = private_files._rename_exclusive
    active = False
    injected = False
    def boundary():
        nonlocal injected
        if not injected:
            injected = True
            if failure == "successor":
                successor = tmp_path / "second"
                successor.write_bytes(b"foreign-two")
                os.replace(successor, path)
            else:
                raise OSError("second recovery failed")
    def swap(fd, first, second):
        nonlocal active
        if active:
            boundary()
        else:
            foreign = tmp_path / "first"
            foreign.write_bytes(b"foreign-one")
            os.replace(foreign, path)
            real_swap(fd, first, second)
            active = True
            return
        real_swap(fd, first, second)
    def rename(fd, source, destination):
        if active:
            boundary()
        real_rename(fd, source, destination)
    monkeypatch.setattr(private_files, "_swap_names", swap)
    monkeypatch.setattr(private_files, "_rename_exclusive", rename)
    with pytest.raises(RuntimeError) as caught:
        private_files.atomic_publish(path, b"producer", expected_identity=expected)
    assert path.read_bytes() == (b"foreign-two" if failure == "successor" else b"producer")
    assert isinstance(caught.value, private_files.NamespaceAuthorityError)
    assert b"foreign-one" in [p.read_bytes() for p in tmp_path.iterdir()]
    assert caught.value.retained_paths
    assert all(str(p) in str(caught.value) for p in caught.value.retained_paths)
    expected.close()


@pytest.mark.parametrize("phase", ["detach-after", "restore-io", "restore-after", "detach-fsync", "restore-fsync", "foreign-fsync", "both-restore-io"])
def test_recovery_durability_retains_exact_capabilities(tmp_path, monkeypatch, phase):
    path = tmp_path / "state.json"
    path.write_bytes(b"old")
    expected = _identity(path)
    real_swap, real_rename, real_sync = private_files._swap_names, private_files._rename_exclusive, os.fsync
    active = False
    renames = syncs = 0
    def swap(fd, first, second):
        nonlocal active
        other = tmp_path / "foreign"
        other.write_bytes(b"foreign-one")
        os.replace(other, path)
        real_swap(fd, first, second)
        active = True
    def rename(fd, source, destination):
        nonlocal renames
        if active:
            renames += 1
            if phase == "restore-io" and renames == 2 or phase == "both-restore-io" and renames >= 2:
                raise OSError("restore failed")
        real_rename(fd, source, destination)
        if active and ((phase == "detach-after" and renames == 1) or (phase == "restore-after" and renames == 2)):
            raise OSError("rename wrapper failed after mutation")
    def sync(fd):
        nonlocal syncs
        if active and stat.S_ISDIR(os.fstat(fd).st_mode):
            syncs += 1
            if (phase == "detach-fsync" and syncs == 1 or phase in {"restore-fsync", "foreign-fsync"} and syncs == 2):
                if phase == "foreign-fsync":
                    other = tmp_path / "second"
                    other.write_bytes(b"foreign-two")
                    os.replace(other, path)
                raise OSError("recovery durability failed")
        real_sync(fd)
    monkeypatch.setattr(private_files, "_swap_names", swap)
    monkeypatch.setattr(private_files, "_rename_exclusive", rename)
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(private_files.NamespaceAuthorityError) as caught:
        private_files.atomic_publish(path, b"producer", expected_identity=expected)
    expected_canonical = (b"foreign-two" if phase == "foreign-fsync" else b"foreign-one"
                          if phase in {"restore-after", "restore-fsync"} else b"producer")
    if phase != "both-restore-io":
        assert path.read_bytes() == expected_canonical
    else:
        assert not path.exists()
    assert caught.value.retained_paths
    assert set(caught.value.retained_paths) == set(tmp_path.iterdir())
    for receipt in caught.value.receipts:
        assert not receipt._closed
        assert os.fstat(receipt.file_fd).st_nlink in {0, 1}
        if os.fstat(receipt.file_fd).st_nlink == 1:
            private_files._validate_receipt(receipt)


@pytest.mark.parametrize("event", ["second-at-restore", "parent-fsync"])
def test_recovery_retained_paths_follow_directory_authority(tmp_path, monkeypatch, event):
    directory = tmp_path / "private"
    directory.mkdir()
    path = directory / "state.json"
    path.write_bytes(b"old")
    expected = _identity(path)
    real_swap, real_rename, real_sync = private_files._swap_names, private_files._rename_exclusive, os.fsync
    active = False
    renames = 0
    moved = tmp_path / "moved"
    foreign_identity = None
    def swap(fd, first, second):
        nonlocal active
        other = directory / "first"
        other.write_bytes(b"foreign-one")
        os.replace(other, path)
        real_swap(fd, first, second)
        active = True
    def rename(fd, source, destination):
        nonlocal renames, foreign_identity
        if active:
            renames += 1
            if event == "second-at-restore" and renames == 2:
                path.symlink_to("absent-foreign-target")
                foreign_identity = _identity(path)
        real_rename(fd, source, destination)
    def sync(fd):
        if active and event == "parent-fsync" and not moved.exists():
            directory.rename(moved)
            directory.mkdir()
            path.write_bytes(b"foreign-parent")
            raise OSError("directory moved during failed recovery fsync")
        real_sync(fd)
    monkeypatch.setattr(private_files, "_swap_names", swap)
    monkeypatch.setattr(private_files, "_rename_exclusive", rename)
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(private_files.NamespaceAuthorityError) as caught:
        private_files.atomic_publish(path, b"producer", expected_identity=expected)
    actual = moved if event == "parent-fsync" else directory
    assert set(caught.value.retained_paths) == set(actual.iterdir())
    if event == "parent-fsync":
        assert path.read_bytes() == b"foreign-parent"
        assert (moved / path.name).read_bytes() == b"producer"
    else:
        assert path.is_symlink() and _identity(path) == foreign_identity
        assert os.readlink(path) == "absent-foreign-target"
    assert caught.value.canonical_authority == "unknown"
