import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/manage_repo_link.py"
SPEC = importlib.util.spec_from_file_location("manage_repo_link", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
manage_repo_link = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage_repo_link)


def test_install_is_idempotent_and_refuses_foreign_entry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("source", encoding="utf-8")
    target = tmp_path / "bin/tool"
    target.parent.mkdir()

    manage_repo_link.install(str(source), str(target))
    manage_repo_link.install(str(source), str(target))
    assert target.is_symlink()
    assert os.readlink(target) == str(source)

    target.unlink()
    target.write_text("foreign", encoding="utf-8")
    with pytest.raises(RuntimeError, match="non-symlink"):
        manage_repo_link.install(str(source), str(target))
    assert target.read_text(encoding="utf-8") == "foreign"


def test_install_fsync_failure_after_symlink_creation_compensates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.write_text("source", encoding="utf-8")
    target = tmp_path / "bin/tool"
    target.parent.mkdir()
    real_fsync = os.fsync
    calls = 0

    def fail_publication_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected post-symlink fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_publication_fsync)

    with pytest.raises(OSError, match="post-symlink fsync failure"):
        manage_repo_link.install(str(source), str(target))

    assert not os.path.lexists(target)


def test_open_parent_closes_fd_when_fstat_fails(tmp_path: Path) -> None:
    target = tmp_path / "bin/tool"
    target.parent.mkdir()
    real_open = os.open
    opened_fds = []

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened_fds.append(fd)
        return fd

    with mock.patch.object(manage_repo_link.os, "open", side_effect=tracking_open):
        with mock.patch.object(manage_repo_link.os, "fstat", side_effect=OSError("failed")):
            with pytest.raises(OSError, match="failed"):
                manage_repo_link._open_parent(str(target))

    assert opened_fds
    for fd in set(opened_fds):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_uninstall_preserves_entry_swapped_after_validation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("source", encoding="utf-8")
    target = tmp_path / "bin/tool"
    target.parent.mkdir()
    target.symlink_to(source)
    real_rename = os.rename
    raced = False

    def swap_then_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal raced
        if not raced:
            raced = True
            os.unlink(src, dir_fd=src_dir_fd)
            fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=src_dir_fd)
            try:
                os.write(fd, b"foreign")
            finally:
                os.close(fd)
        return real_rename(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    with mock.patch.object(manage_repo_link.os, "rename", side_effect=swap_then_rename):
        with pytest.raises(RuntimeError, match="foreign entry preserved"):
            manage_repo_link.uninstall(str(source), str(target))

    assert target.is_file()
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == "foreign"
    assert not list(target.parent.glob(".unified-kanban-unlink-*"))


def test_uninstall_retirement_preserves_substituted_quarantine(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("source", encoding="utf-8")
    target = tmp_path / "bin/tool"
    target.parent.mkdir()
    target.symlink_to(source)
    foreign = target.parent / "foreign"
    foreign.write_text("foreign", encoding="utf-8")
    displaced = target.parent / "displaced-original"
    real_rename_exclusive = manage_repo_link._rename_exclusive
    injected = False

    def substitute_before_retirement(directory_fd, name, retired):
        nonlocal injected
        if not injected and name.startswith(".unified-kanban-unlink-"):
            injected = True
            os.rename(
                name,
                displaced.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.rename(
                foreign.name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        return real_rename_exclusive(directory_fd, name, retired)

    with mock.patch.object(
        manage_repo_link, "_rename_exclusive", side_effect=substitute_before_retirement
    ):
        with pytest.raises(RuntimeError, match="cleanup identity changed"):
            manage_repo_link.uninstall(str(source), str(target))

    assert injected
    quarantines = list(target.parent.glob(".unified-kanban-unlink-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_text(encoding="utf-8") == "foreign"
    assert displaced.is_symlink()
    assert os.readlink(displaced) == str(source)


def test_install_and_uninstall_return_operation_owned_identities(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("source", encoding="utf-8")
    target = tmp_path / "bin/tool"
    target.parent.mkdir()

    created = manage_repo_link.install(str(source), str(target))
    no_op = manage_repo_link.install(str(source), str(target))
    removed = manage_repo_link.uninstall(str(source), str(target))

    assert created["changed"] is True
    assert created["identity"] is not None
    assert created["link_target"] == str(source)
    assert no_op["changed"] is False
    assert no_op["identity"] == created["identity"]
    assert removed == {
        "path": str(target),
        "changed": True,
        "identity": None,
        "link_target": None,
    }


def test_cli_writes_private_operation_receipt(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("source", encoding="utf-8")
    target = tmp_path / "bin/tool"
    target.parent.mkdir()
    receipt = tmp_path / "receipt.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "install",
            str(source),
            str(target),
            "--receipt",
            str(receipt),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["entries"][0]["changed"] is True
    assert payload["entries"][0]["identity"] is not None
    assert receipt.stat().st_mode & 0o777 == 0o600
