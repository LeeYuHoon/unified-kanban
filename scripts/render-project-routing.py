#!/usr/bin/env python3
"""Render project-routing candidates from private transaction snapshots.

This helper never mutates host configuration. The caller exports baseline bytes
from the path transaction, invokes this renderer inside the private transaction
directory, then commits candidates through path-transaction.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import stat
from pathlib import Path


def _bound_parent(path: Path) -> tuple[int | None, str]:
    transaction_dir = os.environ.get("UNIFIED_KANBAN_TRANSACTION_DIR")
    transaction_fd = os.environ.get("UNIFIED_KANBAN_TRANSACTION_FD")
    if transaction_dir and transaction_fd and path.parent == Path(transaction_dir):
        return int(transaction_fd), path.name
    return None, str(path)


def _read_optional(path: Path | None, label: str) -> tuple[bytes, int] | None:
    if path is None:
        return None
    directory_fd, name = _bound_parent(path)
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} snapshot must be a regular non-symlink file")
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError(f"{label} snapshot changed during read")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 8 * 1024 * 1024:
                raise ValueError(f"{label} snapshot exceeds 8 MiB")
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(opened.st_mode)
    finally:
        os.close(fd)


def _write_private(path: Path, content: bytes, mode: int) -> None:
    if not path.is_absolute():
        raise ValueError("routing candidate path must be absolute")
    directory_fd, name = _bound_parent(path)
    fd = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("routing candidate write made no progress")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _optional(value: str) -> Path | None:
    return None if value == "-" else Path(value)


def _parse_projects(content: bytes) -> list[str]:
    projects = json.loads(content.decode("utf-8"))
    if not isinstance(projects, list) or any(
        not isinstance(item, str) or not item or not Path(item).is_absolute()
        for item in projects
    ):
        raise ValueError("managed-projects state must contain absolute path strings")
    if len(set(projects)) != len(projects):
        raise ValueError("managed-projects entries must be unique")
    return projects


def register(args: argparse.Namespace) -> None:
    state_snapshot = _read_optional(_optional(args.state_before), "state")
    envrc_snapshot = _read_optional(_optional(args.envrc_before), ".envrc")
    state_bytes = state_snapshot[0] if state_snapshot else b""
    envrc_bytes = envrc_snapshot[0] if envrc_snapshot else b""
    projects = _parse_projects(state_bytes) if state_snapshot else []
    if args.project not in projects:
        projects.append(args.project)
    lines = envrc_bytes.decode("utf-8").splitlines() if envrc_snapshot else []
    rendered_lines = [item for item in lines if not item.endswith(" # unified-kanban")]
    rendered_lines.append(args.line)
    _write_private(
        Path(args.state_output),
        (json.dumps(projects, indent=2) + "\n").encode("utf-8"),
        0o600,
    )
    _write_private(
        Path(args.envrc_output),
        ("\n".join(rendered_lines) + "\n").encode("utf-8"),
        0o600,
    )


def unregister(args: argparse.Namespace) -> None:
    snapshot = _read_optional(Path(args.before), ".envrc")
    if snapshot is None:
        raise ValueError("unregister requires an existing snapshot")
    content, _mode = snapshot
    lines = content.decode("utf-8").splitlines()
    filtered = [line for line in lines if not line.endswith(" # unified-kanban")]
    rendered = (("\n".join(filtered) + "\n") if filtered else "").encode("utf-8")
    _write_private(Path(args.output), rendered, 0o600)


def validate_state(args: argparse.Namespace) -> None:
    snapshot = _read_optional(Path(args.before), "state")
    if snapshot is None:
        raise ValueError("state validation requires an existing snapshot")
    projects = _parse_projects(snapshot[0])
    if args.preflight:
        preflight_snapshot = _read_optional(Path(args.preflight), "preflight")
        if preflight_snapshot is None:
            raise ValueError("routing preflight is missing")
        preflight = json.loads(preflight_snapshot[0].decode("utf-8"))
        expected = {
            "sha256": hashlib.sha256(snapshot[0]).hexdigest(),
            "projects": projects,
        }
        if preflight != expected:
            raise RuntimeError("managed-projects state changed after preflight")


def preflight(args: argparse.Namespace) -> None:
    snapshot = _read_optional(Path(args.state), "state")
    if snapshot is None:
        raise ValueError("state preflight requires an existing snapshot")
    projects = _parse_projects(snapshot[0])
    envrcs = []
    for project in projects:
        project_path = Path(project)
        try:
            project_info = os.lstat(project_path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(project_info.st_mode):
            continue
        if not stat.S_ISDIR(project_info.st_mode):
            raise ValueError(f"managed project must be a directory: {project_path}")
        envrc = Path(project) / ".envrc"
        try:
            info = os.lstat(envrc)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"managed .envrc must be a regular file: {envrc}")
        envrcs.append(envrc)
    metadata = (json.dumps({
        "sha256": hashlib.sha256(snapshot[0]).hexdigest(),
        "projects": projects,
    }, sort_keys=True) + "\n").encode("utf-8")
    _write_private(Path(args.output), metadata, 0o600)
    paths = b"".join(os.fsencode(envrc) + b"\0" for envrc in envrcs)
    _write_private(Path(args.paths_output), paths, 0o600)
    frozen = [str(Path(args.state)), *(str(envrc) for envrc in envrcs)]
    print("ROUTING_PATHS=(" + " ".join(shlex.quote(path) for path in frozen) + ")")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    add = subparsers.add_parser("register")
    add.add_argument("state_before")
    add.add_argument("envrc_before")
    add.add_argument("project")
    add.add_argument("line")
    add.add_argument("state_output")
    add.add_argument("envrc_output")
    remove = subparsers.add_parser("unregister")
    remove.add_argument("before")
    remove.add_argument("output")
    validate = subparsers.add_parser("validate-state")
    validate.add_argument("before")
    validate.add_argument("preflight", nargs="?")
    inspect = subparsers.add_parser("preflight")
    inspect.add_argument("state")
    inspect.add_argument("output")
    inspect.add_argument("paths_output")
    args = parser.parse_args()
    if args.action == "register":
        register(args)
    elif args.action == "unregister":
        unregister(args)
    elif args.action == "validate-state":
        validate_state(args)
    else:
        preflight(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
