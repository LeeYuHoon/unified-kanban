#!/usr/bin/env python3
"""carried 번들의 체크섬, 크기, prerequisite, 순서 있는 ref를 검증한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
METADATA_KEYS = {"sha256", "size_bytes", "prerequisite", "ref_count"}


class DuplicateMetadataKey(ValueError):
    """JSON 메타데이터에 키가 중복될 때 발생한다."""


def _verify_pinned_updater_argv(source: str) -> None:
    """위임된 Git argv 계약이 어긋난 고정(pinned) Hermes 업데이터를 거부한다."""
    required = (
        'git_cmd + ["rev-list", f"HEAD..origin/{branch}", "--count"]',
        'git_cmd + ["merge", "--ff-only", f"origin/{branch}"]',
    )
    if any(fragment not in source for fragment in required):
        raise SystemExit("pinned Hermes updater Git argv contract has drifted")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateMetadataKey(key)
        result[key] = value
    return result


def _read_regular(path: Path) -> bytes:
    """말단 심볼릭 링크를 따라가지 않고 안정된 일반 파일을 읽는다."""
    def signature(status: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            status.st_dev,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        )

    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("opened descriptor is not regular")
            if signature(before) != signature(opened):
                raise OSError("file changed before open")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read()
            completed = os.fstat(descriptor)
            after = os.lstat(path)
            if signature(opened) != signature(completed) or signature(opened) != signature(after):
                raise OSError("file changed during read")
            return data
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SystemExit(f"{path} must be a stable regular non-symlink file") from exc


def _verify_with_git(
    bundle: bytes,
    hermes_repo: Path,
    prerequisite: str,
    manifest: list[str],
    *,
    verify_updater_contract: bool = False,
) -> None:
    """Git이 오브젝트와 델타 무결성을 검증하도록 전용 클론에 unbundle한다."""
    with tempfile.TemporaryDirectory(prefix="carried-bundle-verify-") as temporary:
        temporary_path = Path(temporary)
        isolated_repo = temporary_path / "hermes.git"
        bundle_copy = temporary_path / "carried.bundle"
        bundle_copy.write_bytes(bundle)
        clone = subprocess.run(
            ["git", "clone", "--bare", "--no-local", str(hermes_repo), str(isolated_repo)],
            check=False,
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            raise SystemExit("could not create isolated Hermes repository for bundle verification")
        for command in ("verify", "unbundle"):
            completed = subprocess.run(
                ["git", "-C", str(isolated_repo), "bundle", command, str(bundle_copy)],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                raise SystemExit(f"git bundle {command} rejected the carried payload")
        if verify_updater_contract:
            for revision, label in (
                (prerequisite, "pinned Hermes commit"),
                (manifest[-1], "final carried Hermes commit"),
            ):
                updater_source = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(isolated_repo),
                        "show",
                        f"{revision}:hermes_cli/update_cmd.py",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if updater_source.returncode != 0:
                    raise SystemExit(f"could not read updater source from {label}")
                _verify_pinned_updater_argv(updater_source.stdout)
        expected_parent = prerequisite
        for commit in manifest:
            parents = subprocess.run(
                ["git", "-C", str(isolated_repo), "show", "-s", "--format=%P", commit],
                check=False,
                capture_output=True,
                text=True,
            )
            if parents.returncode != 0 or parents.stdout.strip() != expected_parent:
                raise SystemExit("carried manifest is not one sole-parent linear chain")
            expected_parent = commit


def verify(patches: Path, *, hermes_repo: Path | None = None) -> dict[str, str | int]:
    """선언되지 않은 매니페스트/헤더 항목을 모두 거부한 뒤 실제 메타데이터를 반환한다."""
    bundle_path = patches / "hermes-agent-carried.bundle"
    manifest_path = patches / "hermes-agent-carried-commits"
    pin_path = patches / "hermes-agent-supported-upstream"
    metadata_path = patches / "hermes-agent-carried-bundle-metadata.json"

    try:
        metadata = json.loads(
            _read_regular(metadata_path).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except DuplicateMetadataKey as exc:
        raise SystemExit(f"duplicate metadata key: {exc.args[0]}") from exc
    if not isinstance(metadata, dict) or set(metadata) != METADATA_KEYS:
        raise SystemExit("bundle metadata has an unexpected schema")
    metadata_sha256 = metadata.get("sha256")
    if not isinstance(metadata_sha256, str) or SHA256_RE.fullmatch(metadata_sha256) is None:
        raise SystemExit("bundle metadata SHA-256 is malformed")
    if type(metadata.get("size_bytes")) is not int or metadata["size_bytes"] < 1:
        raise SystemExit("bundle metadata size is malformed")
    metadata_prerequisite = metadata.get("prerequisite")
    if (
        not isinstance(metadata_prerequisite, str)
        or SHA_RE.fullmatch(metadata_prerequisite) is None
    ):
        raise SystemExit("bundle metadata prerequisite is malformed")
    if type(metadata.get("ref_count")) is not int or metadata["ref_count"] < 1:
        raise SystemExit("bundle metadata ref count is malformed")

    bundle = _read_regular(bundle_path)
    header_bytes, separator, _pack = bundle.partition(b"\n\n")
    if not separator:
        raise SystemExit("carried bundle has no complete v2 header")
    try:
        header = header_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SystemExit("carried bundle header is not UTF-8") from exc
    if not header or header[0] != "# v2 git bundle":
        raise SystemExit("carried bundle does not use the expected v2 format")
    if len(_pack) < 32 or not _pack.startswith(b"PACK"):
        raise SystemExit("carried bundle pack payload is malformed")
    # Git bundle v2 packfile은 포맷 체크섬으로 SHA-1을 사용한다. 이는 무결성
    # 포맷 비교일 뿐, 보안 결정이나 신뢰 앵커가 아니다.
    # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1 -- 포맷 체크섬 비교 억제
    if hashlib.sha1(_pack[:-20], usedforsecurity=False).digest() != _pack[-20:]:
        raise SystemExit("carried bundle pack payload checksum is invalid")

    pin = _read_regular(pin_path).decode("utf-8").strip()
    if SHA_RE.fullmatch(pin) is None:
        raise SystemExit("supported-upstream pin is malformed")

    manifest: list[str] = []
    for line_number, line in enumerate(
        _read_regular(manifest_path).decode("utf-8").splitlines(), start=1
    ):
        if not line or line.startswith("#"):
            continue
        if SHA_RE.fullmatch(line) is None:
            raise SystemExit(f"malformed carried manifest entry at line {line_number}")
        manifest.append(line)
    if not manifest:
        raise SystemExit("carried manifest has no commit entries")
    if len(set(manifest)) != len(manifest):
        raise SystemExit("carried manifest contains a duplicate commit SHA")

    prerequisites: list[str] = []
    refs: list[tuple[str, str]] = []
    for line in header[1:]:
        prerequisite_match = re.fullmatch(r"-([0-9a-f]{40})(?: .*)?", line)
        if prerequisite_match:
            prerequisites.append(prerequisite_match.group(1))
            continue
        ref_match = re.fullmatch(r"([0-9a-f]{40}) (\S+)", line)
        if ref_match:
            refs.append((ref_match.group(1), ref_match.group(2)))
            continue
        raise SystemExit(f"unexpected carried bundle header entry: {line!r}")
    if len(prerequisites) != 1:
        raise SystemExit("carried bundle must declare exactly one prerequisite")

    expected_names = [f"refs/heads/carried-{index:02d}" for index in range(1, len(refs) + 1)]
    ref_names = [name for _sha, name in refs]
    if ref_names != expected_names:
        raise SystemExit("bundle refs have unexpected, duplicate, or misnumbered names")
    ref_shas = [sha for sha, _name in refs]
    if len(set(ref_shas)) != len(ref_shas):
        raise SystemExit("carried bundle refs contain a duplicate commit SHA")

    actual = {
        "sha256": hashlib.sha256(bundle).hexdigest(),
        "size_bytes": len(bundle),
        "prerequisite": prerequisites[0],
        "ref_count": len(refs),
    }
    if actual != metadata:
        raise SystemExit(f"bundle metadata mismatch: expected={metadata!r} actual={actual!r}")
    if metadata["prerequisite"] != pin:
        raise SystemExit("bundle prerequisite does not equal the supported-upstream pin")
    if ref_shas != manifest:
        raise SystemExit("ordered bundle refs do not equal the carried manifest")
    if hermes_repo is not None:
        _verify_with_git(
            bundle,
            hermes_repo,
            pin,
            manifest,
            verify_updater_contract=True,
        )
    return actual


def main(argv: list[str] | None = None) -> int:
    """모든 중복 carried 번들 무결성 기록이 일치하지 않으면 실패한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--patches-dir",
        type=Path,
        default=ROOT / "patches",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--hermes-repo",
        type=Path,
        help="Hermes checkout used for isolated git bundle verify/unbundle",
    )
    args = parser.parse_args(argv)
    actual = verify(args.patches_dir, hermes_repo=args.hermes_repo)

    print(
        "CARRIED_BUNDLE_PASS "
        f"refs={actual['ref_count']} size={actual['size_bytes']} "
        f"sha256={actual['sha256']} prerequisite={actual['prerequisite']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
