"""Regression tests for the portable Hermes carried bundle."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PATCHES = REPO / "patches"
BUNDLE_FILES = (
    "hermes-agent-carried.bundle",
    "hermes-agent-carried-commits",
    "hermes-agent-supported-upstream",
    "hermes-agent-carried-bundle-metadata.json",
)


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_carried_bundle", REPO / "scripts/verify-carried-bundle.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_patches(tmp_path: Path) -> Path:
    patches = tmp_path / "patches"
    patches.mkdir()
    for name in BUNDLE_FILES:
        shutil.copy2(PATCHES / name, patches / name)
    return patches


def _run(patches: Path = PATCHES, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "scripts/verify-carried-bundle.py",
            "--patches-dir",
            str(patches),
            *extra_args,
        ],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )


def _metadata(patches: Path) -> dict[str, str | int]:
    return json.loads(
        (patches / "hermes-agent-carried-bundle-metadata.json").read_text(encoding="utf-8")
    )


def _write_metadata(patches: Path, metadata: dict[str, str | int]) -> None:
    (patches / "hermes-agent-carried-bundle-metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )


def _rewrite_header(patches: Path, lines: list[str]) -> None:
    bundle_path = patches / "hermes-agent-carried.bundle"
    _old_header, separator, pack = bundle_path.read_bytes().partition(b"\n\n")
    assert separator
    bundle = "\n".join(lines).encode("utf-8") + separator + pack
    bundle_path.write_bytes(bundle)
    metadata = _metadata(patches)
    metadata["sha256"] = hashlib.sha256(bundle).hexdigest()
    metadata["size_bytes"] = len(bundle)
    _write_metadata(patches, metadata)


def _header(patches: Path) -> list[str]:
    header, separator, _pack = (patches / "hermes-agent-carried.bundle").read_bytes().partition(
        b"\n\n"
    )
    assert separator
    return header.decode("utf-8").splitlines()


def test_carried_bundle_metadata_and_order_are_consistent() -> None:
    completed = _run()
    metadata = _metadata(PATCHES)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith(
        f"CARRIED_BUNDLE_PASS refs=13 size={metadata['size_bytes']} "
        f"sha256={metadata['sha256']} prerequisite={metadata['prerequisite']}"
    )


def test_pinned_updater_argv_contract_accepts_reviewed_sequence() -> None:
    verifier = _load_verifier()
    verifier._verify_pinned_updater_argv(
        'git_cmd + ["rev-list", f"HEAD..origin/{branch}", "--count"]\n'
        'git_cmd + ["merge", "--ff-only", f"origin/{branch}"]\n'
    )


def test_pinned_updater_argv_contract_rejects_argument_order_drift() -> None:
    verifier = _load_verifier()
    with pytest.raises(SystemExit, match="argv contract has drifted"):
        verifier._verify_pinned_updater_argv(
            'git_cmd + ["rev-list", "--count", f"HEAD..origin/{branch}"]\n'
            'git_cmd + ["merge", "--ff-only", f"origin/{branch}"]\n'
        )


def test_git_verifier_checks_updater_contract_at_prerequisite_and_tip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_verifier()
    prerequisite = "1" * 40
    tip = "2" * 40
    checked: list[str] = []
    source = (
        'git_cmd + ["rev-list", f"HEAD..origin/{branch}", "--count"]\n'
        'git_cmd + ["merge", "--ff-only", f"origin/{branch}"]\n'
    )

    def fake_run(command, **_kwargs):
        if "show" in command and any(
            str(item).endswith(":hermes_cli/update_cmd.py") for item in command
        ):
            revision = next(
                str(item).split(":", 1)[0]
                for item in command
                if str(item).endswith(":hermes_cli/update_cmd.py")
            )
            checked.append(revision)
            return subprocess.CompletedProcess(command, 0, source, "")
        if "--format=%P" in command:
            return subprocess.CompletedProcess(command, 0, prerequisite + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    verifier._verify_with_git(
        b"bundle",
        tmp_path,
        prerequisite,
        [tip],
        verify_updater_contract=True,
    )

    assert checked == [prerequisite, tip]


def test_rejects_malformed_manifest_entry(tmp_path: Path) -> None:
    patches = _copy_patches(tmp_path)
    manifest = patches / "hermes-agent-carried-commits"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "not-a-sha\n", encoding="utf-8")

    completed = _run(patches)

    assert completed.returncode != 0
    assert "malformed carried manifest entry" in completed.stderr


def test_rejects_unexpected_bundle_ref_even_with_updated_metadata(tmp_path: Path) -> None:
    patches = _copy_patches(tmp_path)
    lines = _header(patches)
    lines.append(f"{'1' * 40} refs/heads/unexpected")
    _rewrite_header(patches, lines)

    completed = _run(patches)

    assert completed.returncode != 0
    assert "unexpected, duplicate, or misnumbered" in completed.stderr


@pytest.mark.parametrize("replacement", ["carried-12", "carried-14"])
def test_rejects_duplicate_or_misnumbered_bundle_ref(
    tmp_path: Path, replacement: str
) -> None:
    patches = _copy_patches(tmp_path)
    lines = _header(patches)
    lines[-1] = lines[-1].replace("carried-13", replacement)
    _rewrite_header(patches, lines)

    completed = _run(patches)

    assert completed.returncode != 0
    assert "unexpected, duplicate, or misnumbered" in completed.stderr


def test_rejects_checksum_mismatch(tmp_path: Path) -> None:
    patches = _copy_patches(tmp_path)
    bundle = patches / "hermes-agent-carried.bundle"
    data = bundle.read_bytes()
    bundle.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))

    completed = _run(patches)

    assert completed.returncode != 0
    assert "checksum" in completed.stderr


def test_rejects_size_mismatch(tmp_path: Path) -> None:
    patches = _copy_patches(tmp_path)
    metadata = _metadata(patches)
    metadata["size_bytes"] = int(metadata["size_bytes"]) + 1
    _write_metadata(patches, metadata)

    completed = _run(patches)

    assert completed.returncode != 0
    assert "bundle metadata mismatch" in completed.stderr


def test_rejects_prerequisite_that_differs_from_pin(tmp_path: Path) -> None:
    patches = _copy_patches(tmp_path)
    lines = _header(patches)
    lines[1] = lines[1].replace(lines[1][1:41], "1" * 40)
    _rewrite_header(patches, lines)
    metadata = _metadata(patches)
    metadata["prerequisite"] = "1" * 40
    _write_metadata(patches, metadata)

    completed = _run(patches)

    assert completed.returncode != 0
    assert "does not equal the supported-upstream pin" in completed.stderr


def test_rejects_multiple_prerequisites(tmp_path: Path) -> None:
    patches = _copy_patches(tmp_path)
    lines = _header(patches)
    lines.insert(2, f"-{'2' * 40} unexpected prerequisite")
    _rewrite_header(patches, lines)

    completed = _run(patches)

    assert completed.returncode != 0
    assert "exactly one prerequisite" in completed.stderr


def test_rejects_manifest_order_mismatch(tmp_path: Path) -> None:
    patches = _copy_patches(tmp_path)
    manifest_path = patches / "hermes-agent-carried-commits"
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    positions = [index for index, line in enumerate(lines) if len(line) == 40]
    lines[positions[0]], lines[positions[1]] = lines[positions[1]], lines[positions[0]]
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    completed = _run(patches)

    assert completed.returncode != 0
    assert "ordered bundle refs" in completed.stderr


def test_rejects_duplicate_metadata_keys(tmp_path: Path) -> None:
    patches = _copy_patches(tmp_path)
    metadata_path = patches / "hermes-agent-carried-bundle-metadata.json"
    text = metadata_path.read_text(encoding="utf-8")
    text = text.replace(
        '  "sha256": ',
        f'  "sha256": "{"0" * 64}",\n  "sha256": ',
        1,
    )
    metadata_path.write_text(text, encoding="utf-8")

    completed = _run(patches)

    assert completed.returncode != 0
    assert "duplicate metadata key" in completed.stderr


def test_rejects_corrupt_pack_with_updated_metadata(tmp_path: Path) -> None:
    patches = _copy_patches(tmp_path)
    bundle_path = patches / "hermes-agent-carried.bundle"
    header, separator, pack = bundle_path.read_bytes().partition(b"\n\n")
    assert separator and len(pack) > 40
    pack = pack[:-21] + bytes([pack[-21] ^ 1]) + pack[-20:]
    bundle = header + separator + pack
    bundle_path.write_bytes(bundle)
    metadata = _metadata(patches)
    metadata["sha256"] = hashlib.sha256(bundle).hexdigest()
    _write_metadata(patches, metadata)

    completed = _run(patches)

    assert completed.returncode != 0
    assert "pack payload checksum" in completed.stderr


def test_rejects_duplicate_commit_shas_in_manifest_and_bundle(tmp_path: Path) -> None:
    patches = _copy_patches(tmp_path)
    header = _header(patches)
    first_sha = header[2][:40]
    header[3] = first_sha + header[3][40:]
    _rewrite_header(patches, header)
    manifest_path = patches / "hermes-agent-carried-commits"
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    positions = [index for index, line in enumerate(lines) if len(line) == 40]
    lines[positions[1]] = lines[positions[0]]
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    completed = _run(patches)

    assert completed.returncode != 0
    assert "duplicate commit SHA" in completed.stderr


@pytest.mark.parametrize("name", BUNDLE_FILES)
def test_rejects_symlink_policy_inputs(tmp_path: Path, name: str) -> None:
    patches = _copy_patches(tmp_path)
    path = patches / name
    target = patches / f"target-{name}"
    path.replace(target)
    path.symlink_to(target.name)

    completed = _run(patches)

    assert completed.returncode != 0
    assert "regular non-symlink file" in completed.stderr


def test_read_regular_rejects_same_inode_in_place_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_verifier()
    path = tmp_path / "policy-file"
    path.write_bytes(b"original-policy")
    inode = path.stat().st_ino
    original_fdopen = verifier.os.fdopen

    class MutatingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self):
            data = self.stream.read()
            path.write_bytes(b"mutated-policy!")
            assert path.stat().st_ino == inode
            return data

    def mutating_fdopen(*args, **kwargs):
        return MutatingStream(original_fdopen(*args, **kwargs))

    monkeypatch.setattr(verifier.os, "fdopen", mutating_fdopen)

    with pytest.raises(SystemExit, match="stable regular non-symlink file"):
        verifier._read_regular(path)


def test_isolated_git_unbundle_path(tmp_path: Path) -> None:
    verifier = _load_verifier()
    repo = tmp_path / "source"
    bundle_path = tmp_path / "fixture.bundle"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "config", "user.name", "Bundle Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "bundle-test@users.noreply.github.com"],
        cwd=repo,
        check=True,
    )
    fixture = repo / "fixture.txt"
    fixture.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "fixture.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "switch", "-q", "-c", "carried"], cwd=repo, check=True)
    fixture.write_text("carried\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "carried"], cwd=repo, check=True)
    subprocess.run(
        ["git", "bundle", "create", str(bundle_path), "refs/heads/carried", f"^{base}"],
        cwd=repo,
        check=True,
    )
    carried = subprocess.check_output(
        ["git", "rev-parse", "carried"], cwd=repo, text=True
    ).strip()
    subprocess.run(["git", "switch", "-q", "main"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-D", "carried"], cwd=repo, check=True, capture_output=True)

    verifier._verify_with_git(bundle_path.read_bytes(), repo, base, [carried])
