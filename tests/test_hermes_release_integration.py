"""End-to-end release production against the real reviewed upstream and bundle.

Every other release test builds from a synthetic three-commit repository, so
nothing in the fast suite ever saw the real upstream tree - which is exactly why
a case-fold collision that only exists upstream survived review and broke the
primary install path on a supported macOS volume.

This module drives the real producer instead: the reviewed pin served from a real
Hermes object database, the reviewed carried bundle from ``patches/``, the real
carried-bundle verifier, and the real managed launcher. Only the dependency
installer is controlled - resolving Hermes' full dependency tree is a network
download with no bearing on what this proves - and the source checkout,
materialization, receipt, verifier, and launcher are all the shipped code.

Opt in by pointing ``UNIFIED_KANBAN_TEST_HERMES_SOURCE`` at any Git repository
whose object database holds the reviewed upstream commit, for example::

    git clone --bare --no-tags --single-branch https://github.com/NousResearch/hermes-agent.git /tmp/hermes-source.git
    UNIFIED_KANBAN_TEST_HERMES_SOURCE=/tmp/hermes-source.git uv run pytest tests/test_hermes_release_integration.py

Set ``UNIFIED_KANBAN_TEST_HERMES_REQUIRED=1`` to turn the opt-out skip into a
failure, so a release gate cannot pass by quietly skipping this file.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from kanban_adapter.compatibility import (
    check_selected_release,
    read_carried_commits,
    read_supported_upstream,
)

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "hermes-release-manager.py"
VERIFIER = REPO / "scripts" / "verify-carried-bundle.py"
BUNDLE = REPO / "patches" / "hermes-agent-carried.bundle"
SOURCE_VARIABLE = "UNIFIED_KANBAN_TEST_HERMES_SOURCE"
REQUIRED_VARIABLE = "UNIFIED_KANBAN_TEST_HERMES_REQUIRED"

pytestmark = pytest.mark.integration


def load_helper():
    spec = importlib.util.spec_from_file_location("hermes_release_manager", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reviewed_pins() -> tuple[str, str]:
    return read_supported_upstream(), read_carried_commits()[-1]


def unavailable(reason: str) -> None:
    """Skip, unless this run declared that the integration must really happen."""
    if os.environ.get(REQUIRED_VARIABLE) == "1":
        pytest.fail(f"{REQUIRED_VARIABLE}=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def pinned_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Serve the reviewed pin from a real object database without mutating it.

    The producer refuses to build unless ``refs/heads/main`` is exactly the
    reviewed upstream, and the official branch has moved well past it. Borrowing
    the caller's objects through ``alternates`` gives that exact ref view while
    leaving the donated repository - which may be someone's working clone -
    untouched.
    """
    upstream, _ = reviewed_pins()
    configured = os.environ.get(SOURCE_VARIABLE)
    if not configured:
        unavailable(
            f"{SOURCE_VARIABLE} is unset; point it at a Git repository whose object "
            f"database holds the reviewed upstream {upstream}"
        )
    source = Path(configured)
    if not source.is_dir():
        unavailable(f"{SOURCE_VARIABLE}={configured} is not a directory")
    resolved = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if resolved.returncode != 0:
        unavailable(f"{SOURCE_VARIABLE}={configured} is not a Git repository")
    objects = Path(resolved.stdout.strip()) / "objects"
    present = subprocess.run(
        ["git", "-C", str(source), "cat-file", "-e", f"{upstream}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if present.returncode != 0:
        unavailable(
            f"{SOURCE_VARIABLE}={configured} does not hold the reviewed upstream {upstream}"
        )
    view = tmp_path_factory.mktemp("reviewed-upstream") / "hermes-agent.git"
    subprocess.run(["git", "init", "-q", "--bare", str(view)], check=True)
    (view / "objects" / "info" / "alternates").write_text(f"{objects}\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(view), "update-ref", "refs/heads/main", upstream], check=True
    )
    subprocess.run(
        ["git", "-C", str(view), "rev-parse", f"{upstream}^{{tree}}"],
        check=True,
        capture_output=True,
    )
    return view


@pytest.fixture
def release_workspace(tmp_path: Path):
    """Yield a Hermes checkout path and remove its release root afterwards.

    One real release is roughly three quarters of a gigabyte, and pytest keeps
    recent temporary trees around, so this cleans up rather than leaving several
    copies behind.
    """
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    try:
        yield checkout
    finally:
        shutil.rmtree(f"{checkout}.releases", ignore_errors=True)


def controlled_uv(tmp_path: Path) -> Path:
    """Stand in for the dependency installer, and only for that.

    It creates the same artefact a locked sync creates - a real executable at the
    release-local ``venv/bin/hermes`` - so the launcher, the selector, and the
    executable gate downstream are all exercised for real.
    """
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = venv ]; then mkdir -p "$2/bin"; '
        f"ln -s {shlex.quote(sys.executable)} \"$2/bin/python\"; fi\n"
        'if [ "$1" = sync ]; then '
        "printf '#!/bin/sh\\nprintf \"HERMES-RELEASE-SMOKE:%%s\\\\n\" \"$1\"\\n' "
        '> "$UV_PROJECT_ENVIRONMENT/bin/hermes"; '
        'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/hermes"; fi\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return uv


def test_real_producer_builds_verifies_and_launches_the_reviewed_release(
    pinned_source: Path, release_workspace: Path, tmp_path: Path
) -> None:
    helper = load_helper()
    upstream, carried = reviewed_pins()
    layout = helper.release_layout(release_workspace, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": BUNDLE,
        "source_url": str(pinned_source),
        "allow_local_source": True,
    }

    release = helper.prepare_release(layout, uv=controlled_uv(tmp_path), **build)

    assert release == layout.release
    assert release.parent == layout.root
    # The producer must never touch the mutable Hermes checkout it builds beside.
    assert list(release_workspace.iterdir()) == []

    receipt_path = release / helper._COMPLETION_RECEIPT
    receipt = json.loads(receipt_path.read_bytes())
    stamp = release / helper._BYTECODE_FINGERPRINT
    stamp_info = os.lstat(stamp)
    assert receipt["version"] == 2
    assert receipt["upstream"] == upstream
    assert receipt["carried"] == carried
    assert receipt["git_tree"] == helper._run_git(release, "rev-parse", "HEAD^{tree}")
    assert receipt["git_head_sha256"] == hashlib.sha256(
        helper._stable_regular_bytes(release / ".git" / "HEAD")
    ).hexdigest()
    assert helper._run_git(release, "rev-parse", "HEAD") == carried
    assert stamp_info.st_nlink == 1
    assert stat.S_IMODE(stamp_info.st_mode) == 0o600
    assert stamp.read_text(encoding="utf-8") == f"git:refs/heads/main:{carried}"
    assert receipt["release_sha256"] == helper._tree_digest(
        release, excluded_top_level={".git", helper._COMPLETION_RECEIPT}
    )

    # The reviewed upstream really does carry a folded metadata pair, so an empty
    # record here would mean the detector stopped seeing the very thing it exists
    # for rather than that upstream got tidier.
    collisions = receipt["case_collisions"]
    assert collisions, "the reviewed upstream tree carries a case-fold collision"
    for record in collisions:
        namespace, _, _leaf = record["representative"].rpartition("/")
        assert f"{namespace}/" in helper.METADATA_COLLISION_NAMESPACES
        assert [member[1] for member in record["members"]] == ["100644"] * len(
            record["members"]
        )
        materialized = [entry[0] for entry in record["materialized"]]
        assert materialized == [record["representative"]]
        directory = release / namespace
        survivors = [
            entry.name
            for entry in directory.iterdir()
            if helper._collision_key(f"{namespace}/{entry.name}") == record["key"]
        ]
        assert survivors == [record["representative"].rpartition("/")[2]]

    # Nothing beyond the predicted normalization and this project's own untracked
    # output may appear in the constructed worktree.
    expected = helper._expected_collision_status(
        helper.case_collisions(release, "HEAD"),
        helper._materialized_case_collisions(
            release, helper.case_collisions(release, "HEAD"), rewrite=False
        ),
    )
    observed = set(helper._porcelain_status(release))
    assert observed - {"?? venv/", f"?? {helper._COMPLETION_RECEIPT}"} == expected
    ignored = set(
        filter(
            None,
            helper._run_git(
                release,
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
            ).split("\0"),
        )
    )
    assert helper._BYTECODE_FINGERPRINT in ignored

    # The real carried-bundle verifier, run against the produced release.
    verified = subprocess.run(
        [sys.executable, str(VERIFIER), "--hermes-repo", str(release)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert verified.stdout.startswith("CARRIED_BUNDLE_PASS ")

    # The managed launcher must reach the release-local executable through the
    # selector, and only through it.
    launcher = tmp_path / "hermes"
    layout.selector.write_bytes(helper.selector_payload(layout))
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)
    # The gate every Hermes turn and adapter command runs before recording
    # anything must accept this produced release, receipt included.
    assert check_selected_release(release_workspace, upstream, carried) == ""
    launched = subprocess.run(
        [str(launcher), "gateway"], capture_output=True, text=True, check=False
    )
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout == "HERMES-RELEASE-SMOKE:gateway\n"
    assert (
        helper.launcher_baseline(layout, launcher.read_bytes()) == helper.BASELINE_ABSENT
    )

    # A second run must reuse the receipted release rather than rebuild it, and
    # must not need the dependency installer at all.
    identity = release.stat().st_ino
    assert helper.prepare_release(layout, uv=tmp_path / "absent-uv", **build) == release
    assert release.stat().st_ino == identity

    # Rewriting the normalized survivor must fail the reuse verification, and
    # restoring its reviewed bytes must make the release trustworthy again.
    survivor = release / collisions[0]["representative"]
    original = survivor.read_bytes()
    survivor.write_bytes(b"attacker\n")
    with pytest.raises(RuntimeError, match="case-fold collision"):
        helper.prepare_release(layout, uv=tmp_path / "absent-uv", **build)
    survivor.write_bytes(original)
    assert helper.prepare_release(layout, uv=tmp_path / "absent-uv", **build) == release


def test_reviewed_bundle_matches_its_recorded_integrity_metadata() -> None:
    """The producer's inputs are the shipped ones, checked without any network."""
    verified = subprocess.run(
        [sys.executable, str(VERIFIER)], capture_output=True, text=True, check=False
    )

    assert verified.returncode == 0, verified.stderr
    assert read_supported_upstream() in verified.stdout
