#!/usr/bin/env python3
"""launchd가 plist에 선언된 바로 그 프로그램을 안정적으로 실행하는지 검증한다."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import time


PROGRAM_RE = re.compile(r"^\s*program = (.+)$", re.MULTILINE)
STATE_RE = re.compile(r"^\s*state = (\S+)$", re.MULTILINE)
EXPECTED_LABEL = "ai.hermes.gateway"
RELEASE_RE = re.compile(r"release-[0-9a-f]{40}")


def identity(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_uid,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def read_plist(
    path: Path,
    *,
    require_sealed_environment: bool = True,
    expected_release: Path | None = None,
) -> tuple[str, tuple[str, ...], dict[str, str]]:
    before = path.lstat()
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            identity(before) != identity(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.getuid()
        ):
            raise RuntimeError("launchd plist must be an owned single-link regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        after = path.lstat()
        if identity(opened) != identity(after):
            raise RuntimeError("launchd plist changed while being read")
    finally:
        os.close(descriptor)
    payload = plistlib.loads(b"".join(chunks))
    label = payload.get("Label")
    arguments = payload.get("ProgramArguments")
    environment = payload.get("EnvironmentVariables")
    if (
        not isinstance(label, str)
        or not isinstance(arguments, list)
        or not arguments
        or not all(isinstance(argument, str) for argument in arguments)
        or not Path(arguments[0]).is_absolute()
        or not isinstance(environment, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        )
    ):
        raise RuntimeError(
            "launchd plist has invalid Label, ProgramArguments, or EnvironmentVariables"
        )
    if label != EXPECTED_LABEL:
        raise RuntimeError("unexpected launchd label")
    program = Path(arguments[0])
    release_root: Path | None = None
    if require_sealed_environment:
        if (
            len(program.parts) < 5
            or program.parts[-3:] != ("venv", "bin", "python")
            or RELEASE_RE.fullmatch(program.parts[-4]) is None
        ):
            raise RuntimeError("gateway program is not an exact managed release Python")
        release_root = program.parents[3]
        if expected_release is not None and program.parents[2] != expected_release:
            raise RuntimeError("gateway plist does not bind the expected release")
        root_info = release_root.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or stat.S_IMODE(root_info.st_mode) != 0o700
            or root_info.st_uid != os.getuid()
        ):
            raise RuntimeError("managed release root must be an owned 0700 real directory")
    lazy_target = environment.get("HERMES_LAZY_INSTALL_TARGET")
    required = {
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_LAZY_INSTALL_TARGET": lazy_target,
    }
    if require_sealed_environment and (
        payload.get("Umask") != 0o077
        or not isinstance(lazy_target, str)
        or not Path(lazy_target).is_absolute()
        or release_root is None
        or Path(lazy_target) != release_root / "lazy-packages"
        or any(environment.get(key) != value for key, value in required.items())
    ):
        raise RuntimeError(
            "launchd plist does not bind sealed lazy dependencies to the managed release root"
        )
    return label, tuple(arguments), environment


def loaded_arguments(output: str) -> tuple[str, ...] | None:
    lines = iter(output.splitlines())
    for line in lines:
        if line.strip() != "arguments = {":
            continue
        arguments: list[str] = []
        for argument in lines:
            if argument.strip() == "}":
                return tuple(arguments)
            arguments.append(argument.strip())
        return None
    return None


def loaded_environment(output: str) -> dict[str, str] | None:
    lines = iter(output.splitlines())
    for line in lines:
        if line.strip() != "environment = {":
            continue
        environment: dict[str, str] = {}
        for entry in lines:
            if entry.strip() == "}":
                return environment
            key, separator, value = entry.strip().partition(" => ")
            if not separator or not key:
                return None
            environment[key] = value
        return None
    return None


def inspect_loaded(
    launchctl: Path,
    label: str,
    expected_arguments: tuple[str, ...],
    expected_environment: dict[str, str],
) -> None:
    result = subprocess.run(
        [str(launchctl), "print", f"gui/{os.getuid()}/{label}"],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    if result.returncode != 0:
        raise RuntimeError("launchd service is not loaded")
    state = STATE_RE.search(result.stdout)
    program = PROGRAM_RE.search(result.stdout)
    if state is None or state.group(1) != "running":
        raise RuntimeError("loaded launchd service is not running")
    if program is None or program.group(1) != expected_arguments[0]:
        raise RuntimeError("loaded launchd program does not match plist")
    if loaded_arguments(result.stdout) != expected_arguments:
        raise RuntimeError("loaded launchd arguments do not match plist")
    environment = loaded_environment(result.stdout)
    launchd_owned = {
        "OSLogRateLimit": "64",
        "XPC_SERVICE_NAME": label,
    }
    if environment is None or any(
        environment.get(key) != value for key, value in expected_environment.items()
    ):
        raise RuntimeError("loaded launchd environment does not match plist")
    additions = {
        key: value for key, value in environment.items() if key not in expected_environment
    }
    if any(launchd_owned.get(key) != value for key, value in additions.items()):
        raise RuntimeError("loaded launchd environment does not match plist")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plist", type=Path)
    parser.add_argument("--launchctl", type=Path, default=Path("/bin/launchctl"))
    parser.add_argument("--stable-seconds", type=float, default=1.0)
    parser.add_argument("--allow-unsealed-environment", action="store_true")
    parser.add_argument("--expected-release", type=Path)
    args = parser.parse_args()
    if not args.plist.is_absolute() or not args.launchctl.is_absolute():
        raise SystemExit("plist and launchctl paths must be absolute")
    if args.expected_release is not None and not args.expected_release.is_absolute():
        raise SystemExit("expected release path must be absolute")
    if not 0 <= args.stable_seconds <= 30:
        raise SystemExit("stable-seconds must be between 0 and 30")
    try:
        label, expected, environment = read_plist(
            args.plist,
            require_sealed_environment=not args.allow_unsealed_environment,
            expected_release=args.expected_release,
        )
        inspect_loaded(args.launchctl, label, expected, environment)
        time.sleep(args.stable_seconds)
        inspect_loaded(args.launchctl, label, expected, environment)
    except (OSError, RuntimeError, plistlib.InvalidFileException, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
