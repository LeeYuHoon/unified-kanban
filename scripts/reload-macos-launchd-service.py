#!/usr/bin/env python3
"""Reload the fixed Hermes gateway LaunchAgent without regenerating its plist."""

from __future__ import annotations

import argparse
import os
import plistlib
import stat
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

LABEL = "ai.hermes.gateway"
_MAX_PLIST_BYTES = 1024 * 1024
_RETRY_DELAYS = (1, 2, 3, 5, 8)


class ReloadError(RuntimeError):
    """The exact managed LaunchAgent could not be safely reloaded."""


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _read_plist(path: Path) -> dict[str, object]:
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ReloadError("launchd plist must be a regular non-symlink file")
    if before.st_uid != os.getuid() or before.st_nlink != 1:
        raise ReloadError("launchd plist has unsafe owner or link count")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise ReloadError("launchd plist mode must be 0600")
    if before.st_size > _MAX_PLIST_BYTES:
        raise ReloadError("launchd plist is too large")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ReloadError("launchd plist changed while opening")
        data = bytearray()
        while True:
            chunk = os.read(fd, min(65536, _MAX_PLIST_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _MAX_PLIST_BYTES:
                raise ReloadError("launchd plist is too large")
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ReloadError("launchd plist changed while reading")
    finally:
        os.close(fd)
    payload = plistlib.loads(bytes(data))
    if not isinstance(payload, dict) or payload.get("Label") != LABEL:
        raise ReloadError(f"launchd plist Label must be {LABEL}")
    return payload


def _absent_stderr(uid: int) -> str:
    return (
        'Bad request.\nCould not find service "ai.hermes.gateway" '
        f"in domain for user gui: {uid}\n"
    )


def reload_service(
    plist: Path,
    *,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = _run,
    sleeper: Callable[[float], None] = time.sleep,
    uid: int | None = None,
) -> None:
    _read_plist(plist)
    actual_uid = os.getuid() if uid is None else uid
    domain = f"gui/{actual_uid}"
    service = f"{domain}/{LABEL}"
    status = runner(("/bin/launchctl", "print", service))
    if status.returncode == 0:
        stopped = runner(("/bin/launchctl", "bootout", service))
        if stopped.returncode != 0:
            raise ReloadError(f"launchctl bootout failed: {stopped.returncode}")
    elif not (
        status.returncode == 113
        and status.stdout == ""
        and status.stderr == _absent_stderr(actual_uid)
    ):
        raise ReloadError(f"launchctl state is indeterminate: {status.returncode}")

    failures: list[int] = []
    for delay in _RETRY_DELAYS:
        sleeper(delay)
        loaded = runner(("/bin/launchctl", "bootstrap", domain, str(plist)))
        if loaded.returncode == 0:
            started = runner(("/bin/launchctl", "kickstart", "-k", service))
            if started.returncode != 0:
                raise ReloadError(f"launchctl kickstart failed: {started.returncode}")
            return
        failures.append(loaded.returncode)
    raise ReloadError(f"launchctl bootstrap failed after retries: {failures}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plist", type=Path)
    args = parser.parse_args()
    reload_service(args.plist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
