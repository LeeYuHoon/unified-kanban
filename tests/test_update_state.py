from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/update-state.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("update_state", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ensure_directory_does_not_chmod_replacement_between_mkdir_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    state = tmp_path / "state"
    displaced = tmp_path / "displaced-state"
    real_open = helper.os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if (
            path == "state"
            and kwargs.get("dir_fd") is not None
            and not replaced
            and state.exists()
        ):
            replaced = True
            state.rename(displaced)
            state.mkdir(mode=0o755)
            os.chmod(state, 0o755)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(helper.os, "open", replace_before_open)
    with pytest.raises(RuntimeError, match="owner-only"):
        helper._ensure_directory(state)

    assert replaced
    assert stat.S_IMODE(state.stat().st_mode) == 0o755
    assert stat.S_IMODE(displaced.stat().st_mode) == 0o700


def test_append_log_refuses_symlink_replacement_between_stat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    log = tmp_path / "dashboard.log"
    retained = tmp_path / "retained-log"
    victim = tmp_path / "victim"
    log.write_text("original\n", encoding="utf-8")
    victim.write_text("sentinel\n", encoding="utf-8")
    real_open = helper.os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == log.name and flags & os.O_APPEND and not replaced:
            replaced = True
            log.rename(retained)
            log.symlink_to(victim)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(helper.os, "open", replace_before_open)
    with pytest.raises(OSError):
        helper._open_append_log(log)

    assert replaced
    assert victim.read_text(encoding="utf-8") == "sentinel\n"


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_open_parent_closes_descriptor_when_post_open_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    marker = tmp_path / "pending"
    opened_descriptors: list[int] = []
    real_open = helper.os.open
    real_fstat = helper.os.fstat

    def capture_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened_descriptors.append(descriptor)
        return descriptor

    def fail_fstat(_descriptor: int):
        raise OSError("injected post-open validation failure")

    monkeypatch.setattr(helper.os, "open", capture_open)
    monkeypatch.setattr(helper.os, "fstat", fail_fstat)

    with pytest.raises(OSError, match="post-open validation"):
        helper._open_parent(marker)

    assert len(opened_descriptors) == 1
    with pytest.raises(OSError) as closed:
        real_fstat(opened_descriptors[0])
    assert closed.value.errno == 9


def test_remove_writes_logical_tombstone(tmp_path: Path) -> None:
    marker = tmp_path / "pending"
    written = run("write", "pending", str(marker), "a" * 40, "1")
    assert written.returncode == 0
    receipt = written.stdout.strip()
    assert run("remove", "pending", str(marker), receipt).returncode == 0
    assert marker.is_file()
    assert marker.read_text(encoding="ascii") == "cleared\n"
    assert run("read", "pending", str(marker)).returncode == 3


def test_remove_refuses_foreign_replacement_for_read_receipt(tmp_path: Path) -> None:
    marker = tmp_path / "pending"
    written = run("write", "pending", str(marker), "a" * 40, "1")
    assert written.returncode == 0
    receipt_output = run("read-receipt", "pending", str(marker))
    assert receipt_output.returncode == 0
    receipt = receipt_output.stdout.splitlines()[0]
    replacement = tmp_path / "replacement"
    replacement.write_text("foreign\n", encoding="ascii")
    os.replace(replacement, marker)

    removed = run("remove", "pending", str(marker), receipt)

    assert removed.returncode != 0
    assert marker.read_text(encoding="ascii") == "foreign\n"


def test_write_refuses_path_replacement_without_touching_foreign_inode(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "pending"
    marker.write_text("a" * 40 + "\n1\n", encoding="ascii")
    foreign = tmp_path / "foreign"
    foreign.write_text("foreign\n", encoding="ascii")
    injection = tmp_path / "inject"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "_real_stat = os.stat\n"
        "_seen = 0\n"
        "def _stat(path, *args, **kwargs):\n"
        " global _seen\n"
        " if path == 'pending' and kwargs.get('dir_fd') is not None:\n"
        "  _seen += 1\n"
        " if _seen == 2:\n"
        "  _seen += 1\n"
        "  target = Path(os.environ['MARKER'])\n"
        "  replacement = Path(os.environ['FOREIGN'])\n"
        "  os.replace(replacement, target)\n"
        " return _real_stat(path, *args, **kwargs)\n"
        "os.stat = _stat\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(injection),
        "MARKER": str(marker),
        "FOREIGN": str(foreign),
    }
    result = run("write", "pending", str(marker), "b" * 40, "0", env=env)
    assert result.returncode != 0
    assert marker.read_text(encoding="ascii") == "foreign\n"


def test_write_retries_positive_short_writes(tmp_path: Path) -> None:
    marker = tmp_path / "pending"
    injection = tmp_path / "inject"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os\n"
        "_real_write = os.write\n"
        "def _write(fd, data):\n"
        " return _real_write(fd, bytes(data)[:5])\n"
        "os.write = _write\n",
        encoding="utf-8",
    )

    result = run(
        "write", "pending", str(marker), "b" * 40, "0",
        env={**os.environ, "PYTHONPATH": str(injection)},
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="ascii") == "b" * 40 + "\n0\n"
    assert run("read", "pending", str(marker)).returncode == 0


def test_failed_staged_write_preserves_previous_valid_marker(tmp_path: Path) -> None:
    marker = tmp_path / "pending"
    previous = "a" * 40 + "\n1\n"
    marker.write_text(previous, encoding="ascii")
    injection = tmp_path / "inject"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os\n"
        "def _write(fd, data):\n"
        " raise OSError('injected staged write failure')\n"
        "os.write = _write\n",
        encoding="utf-8",
    )

    result = run(
        "write", "pending", str(marker), "b" * 40, "0",
        env={**os.environ, "PYTHONPATH": str(injection)},
    )

    assert result.returncode != 0
    assert marker.read_text(encoding="ascii") == previous
    assert run("read", "pending", str(marker)).returncode == 0


def test_atomic_swap_restores_foreign_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    marker = tmp_path / "pending"
    marker.write_text("a" * 40 + "\n1\n", encoding="ascii")
    original = marker.stat()
    foreign = tmp_path / "foreign"
    foreign.write_text("foreign\n", encoding="ascii")
    real_swap = helper._swap_names
    swaps = 0

    def substitute_then_swap(directory_fd: int, first: str, second: str) -> None:
        nonlocal swaps
        if swaps == 0:
            os.replace(foreign, marker)
        swaps += 1
        real_swap(directory_fd, first, second)

    monkeypatch.setattr(helper, "_swap_names", substitute_then_swap)

    with pytest.raises(RuntimeError, match="foreign successor preserved"):
        helper._write(marker, ["b" * 40, "0"])

    assert swaps == 2
    assert marker.read_text(encoding="ascii") == "foreign\n"
    assert marker.stat().st_ino != original.st_ino


def test_failed_swap_back_does_not_delete_displaced_foreign_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    marker = tmp_path / "pending"
    marker.write_text("a" * 40 + "\n1\n", encoding="ascii")
    foreign = tmp_path / "foreign"
    foreign.write_text("foreign\n", encoding="ascii")
    foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)
    real_swap = helper._swap_names
    swaps = 0

    def substitute_then_fail_swap_back(
        directory_fd: int, first: str, second: str
    ) -> None:
        nonlocal swaps
        if swaps == 0:
            os.replace(foreign, marker)
            swaps += 1
            real_swap(directory_fd, first, second)
            return
        swaps += 1
        raise OSError("injected swap-back failure")

    monkeypatch.setattr(helper, "_swap_names", substitute_then_fail_swap_back)

    with pytest.raises(OSError, match="injected swap-back failure"):
        helper._write(marker, ["b" * 40, "0"])

    assert swaps == 2
    retained = [
        path
        for path in tmp_path.iterdir()
        if path.is_file()
        and (path.stat().st_dev, path.stat().st_ino) == foreign_identity
    ]
    assert len(retained) == 1
    assert retained[0].read_text(encoding="ascii") == "foreign\n"


def test_state_directory_creation_refuses_symlink_parent(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    home = tmp_path / "hermes-home"
    home.symlink_to(foreign, target_is_directory=True)

    result = run("ensure-dir", "directory", str(home / "state"))

    assert result.returncode != 0
    assert not (foreign / "state").exists()


def test_lock_release_preserves_substituted_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    lock = tmp_path / "update.lock"
    token = "123:producer"
    helper._acquire_lock(lock, token)
    displaced = tmp_path / "displaced-lock"
    real_rename_exclusive = helper._rename_exclusive
    calls = 0

    def substitute_before_retirement(directory_fd: int, source: str, destination: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            os.rename(
                source,
                displaced.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.symlink("foreign", source, dir_fd=directory_fd)
        real_rename_exclusive(directory_fd, source, destination)

    monkeypatch.setattr(helper, "_rename_exclusive", substitute_before_retirement)

    with pytest.raises(RuntimeError, match="cleanup identity changed"):
        helper._release_lock(lock, token)

    foreign_quarantines = [
        path
        for path in tmp_path.iterdir()
        if path.is_symlink() and os.readlink(path) == "foreign"
    ]
    assert len(foreign_quarantines) == 1
    assert displaced.is_symlink()
    assert os.readlink(displaced) == token