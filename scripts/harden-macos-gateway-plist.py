#!/usr/bin/env python3
"""관리되는 Hermes 게이트웨이를 위한 봉인된 durable-overlay launchd plist를 렌더링한다."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import re
import stat
from typing import cast


LABEL = "ai.hermes.gateway"
RELEASE_RE = re.compile(r"release-[0-9a-f]{40}")


def signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def stable_plist(path: Path) -> dict[str, object]:
    before = path.lstat()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if (
            signature(before) != signature(opened)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) not in (0o600, 0o644)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
        ):
            raise RuntimeError("gateway plist must be an owned 0600/0644 single-link regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        completed = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = path.lstat()
    if signature(opened) != signature(completed) or signature(opened) != signature(after):
        raise RuntimeError("gateway plist changed while being read")
    payload = plistlib.loads(b"".join(chunks))
    if not isinstance(payload, dict):
        raise RuntimeError("gateway plist payload must be a dictionary")
    return payload


def validate_authority(payload: dict[str, object], release_root: Path) -> None:
    if payload.get("Label") != LABEL:
        raise RuntimeError("unexpected gateway launchd label")
    arguments = payload.get("ProgramArguments")
    if (
        not isinstance(arguments, list)
        or not arguments
        or not all(isinstance(value, str) for value in arguments)
    ):
        raise RuntimeError("gateway plist has invalid ProgramArguments")
    program = Path(arguments[0])
    try:
        relative = program.relative_to(release_root)
    except ValueError as exc:
        raise RuntimeError("gateway program is outside the managed release root") from exc
    if (
        len(relative.parts) != 4
        or RELEASE_RE.fullmatch(relative.parts[0]) is None
        or relative.parts[1:] != ("venv", "bin", "python")
    ):
        raise RuntimeError("gateway program is not an exact managed release Python")
    link_info = program.lstat()
    info = program.stat()
    if (
        not (stat.S_ISREG(link_info.st_mode) or stat.S_ISLNK(link_info.st_mode))
        or link_info.st_nlink != 1
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o111 == 0
    ):
        raise RuntimeError("gateway program is not an executable regular file")
    environment = payload.get("EnvironmentVariables")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise RuntimeError("gateway plist has invalid EnvironmentVariables")


def render(source: Path, candidate: Path, release_root: Path) -> None:
    if not source.is_absolute() or not candidate.is_absolute() or not release_root.is_absolute():
        raise RuntimeError("all paths must be absolute")
    root_info = release_root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or stat.S_IMODE(root_info.st_mode) != 0o700
        or root_info.st_uid != os.getuid()
    ):
        raise RuntimeError("release root must be an owned 0700 real directory")
    payload = stable_plist(source)
    validate_authority(payload, release_root)
    environment = cast(dict[str, str], payload["EnvironmentVariables"]).copy()
    environment["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    environment["HERMES_LAZY_INSTALL_TARGET"] = str(release_root / "lazy-packages")
    payload["EnvironmentVariables"] = environment
    payload["Umask"] = 0o077
    encoded = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    descriptor = os.open(
        candidate,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("candidate write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(candidate.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("release_root", type=Path)
    args = parser.parse_args()
    render(args.source, args.candidate, args.release_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
