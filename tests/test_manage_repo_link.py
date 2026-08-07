import importlib.util
import os
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

    assert len(opened_fds) == 1
    with pytest.raises(OSError):
        os.fstat(opened_fds[0])


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
