#!/usr/bin/env python3
"""Run a Hermes plugin config mutation inside a retained-FD staging tree."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import tempfile
from pathlib import Path

_MAX_FILE_BYTES = 8 * 1024 * 1024
_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _transaction_name(path: Path) -> str:
    root = os.environ.get("UNIFIED_KANBAN_TRANSACTION_DIR")
    if not root or not path.is_absolute() or path.parent != Path(root):
        raise RuntimeError("staged Hermes artifact must be an immediate transaction child")
    if path.name in {"", ".", ".."}:
        raise RuntimeError("invalid staged Hermes artifact name")
    return path.name


def _read_optional(fd: int, name: str) -> tuple[bytes, int] | None:
    try:
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise RuntimeError("staged Hermes baseline must be a regular file")
    if before.st_size > _MAX_FILE_BYTES:
        raise RuntimeError("staged Hermes baseline exceeds 8 MiB")
    opened = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=fd)
    try:
        current = os.fstat(opened)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("staged Hermes baseline changed during read")
        data = bytearray()
        while True:
            chunk = os.read(opened, 65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _MAX_FILE_BYTES:
                raise RuntimeError("staged Hermes baseline exceeds 8 MiB")
        return bytes(data), stat.S_IMODE(current.st_mode)
    finally:
        os.close(opened)


def _write_exclusive(fd: int, name: str, data: bytes, mode: int) -> None:
    opened = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        0o600,
        dir_fd=fd,
    )
    try:
        os.fchmod(opened, mode)
        view = memoryview(data)
        while view:
            written = os.write(opened, view)
            if written <= 0:
                raise OSError("staged Hermes write made no progress")
            view = view[written:]
        os.fsync(opened)
    finally:
        os.close(opened)


def _render_in_external_stage(
    stage_path: Path,
    baseline: tuple[bytes, int] | None,
    plugin_source: Path,
    action: str,
    hermes_cli: Path,
) -> bytes:
    stage_fd = os.open(stage_path, _DIR_FLAGS)
    try:
        os.mkdir("plugins", 0o700, dir_fd=stage_fd)
        plugins_fd = os.open("plugins", _DIR_FLAGS, dir_fd=stage_fd)
        try:
            os.symlink(str(plugin_source), "hermes-kanban", dir_fd=plugins_fd)
        finally:
            os.close(plugins_fd)
        if baseline is not None:
            _write_exclusive(stage_fd, "config.yaml", baseline[0], baseline[1])

        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "UNIFIED_KANBAN_TRANSACTION_DIR",
                "UNIFIED_KANBAN_TRANSACTION_FD",
                "UNIFIED_KANBAN_TOKEN_LEDGER_FD",
            }
        }
        env["HERMES_HOME"] = "."
        if not hermes_cli.is_absolute():
            raise ValueError("Hermes CLI must be an absolute reviewed-release path")
        command = [str(hermes_cli), "plugins", action]
        if action == "enable":
            command.extend(["--no-allow-tool-override", "hermes-kanban"])
        else:
            command.append("hermes-kanban")
        subprocess.run(
            command,
            env=env,
            check=True,
            close_fds=True,
            cwd=stage_path,
        )

        rendered = _read_optional(stage_fd, "config.yaml")
        if rendered is None:
            raise RuntimeError("Hermes staged plugin operation did not produce config.yaml")
        return rendered[0]
    finally:
        os.close(stage_fd)


def stage(
    root_fd: int,
    baseline_path: Path,
    output_path: Path,
    plugin_source: Path,
    action: str,
    hermes_cli: Path,
) -> None:
    baseline_name = _transaction_name(baseline_path)
    output_name = _transaction_name(output_path)
    baseline = _read_optional(root_fd, baseline_name)
    with tempfile.TemporaryDirectory(prefix="unified-kanban-hermes-stage-") as stage:
        stage_path = Path(stage)
        stage_path.chmod(0o700)
        rendered = _render_in_external_stage(
            stage_path, baseline, plugin_source, action, hermes_cli
        )
    _write_exclusive(root_fd, output_name, rendered, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("transaction_fd", type=int)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("plugin_source", type=Path)
    parser.add_argument("action", choices=("enable", "disable"))
    parser.add_argument("hermes_cli", type=Path)
    args = parser.parse_args()
    stage(
        args.transaction_fd,
        args.baseline,
        args.output,
        args.plugin_source,
        args.action,
        args.hermes_cli,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
