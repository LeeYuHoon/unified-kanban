"""The runtime gate must validate the release that actually executes.

The Hermes checkout is a read-only input: no flow ever moves its ``HEAD`` onto
the carried tip, because the carried commits only exist inside the immutable
release built from the reviewed bundle. These tests therefore keep the checkout
at the reviewed upstream and never at the carried tip - exactly what a real
installation looks like - and prove the gate decides on the selector, the
release, and the producer's completion receipt.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from kanban_adapter.compatibility import check_hermes_compatibility, read_supported_upstream
from kanban_adapter.release_layout import COMPLETION_RECEIPT_NAME


ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _agent_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    _git("init", cwd=repo)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git(
        "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "fixture", cwd=repo,
    )
    sha = _git("rev-parse", "HEAD", cwd=repo)
    _git("update-ref", "refs/remotes/origin/main", sha, cwd=repo)
    return repo, sha


def _carried_manifest(tmp_path: Path, *commits: str) -> Path:
    manifest = tmp_path / "carried-commits"
    manifest.write_text("\n".join(commits) + "\n", encoding="utf-8")
    return manifest


def _carried_head(repo: Path, upstream: str) -> str:
    """Return a carried tip that exists nowhere in the mutable checkout."""
    carried = "c0ffee" + upstream[6:]
    assert carried != upstream
    return carried


def test_runtime_compatibility_accepts_the_selected_reviewed_release(
    tmp_path: Path, reviewed_release
) -> None:
    repo, sha = _agent_repo(tmp_path)
    carried = _carried_head(repo, sha)
    expected = tmp_path / "supported-upstream"
    expected.write_text(sha + "\n", encoding="utf-8")
    reviewed_release(repo, sha, carried)

    assert _git("rev-parse", "HEAD", cwd=repo) != carried
    assert check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, carried),
        hermes_version_output=f"Hermes Agent test · upstream {sha[:8]}",
    ) == (True, "")


def test_runtime_compatibility_does_not_spawn_recursive_hermes_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reviewed_release
) -> None:
    repo, sha = _agent_repo(tmp_path)
    carried = _carried_head(repo, sha)
    expected = tmp_path / "supported-upstream"
    expected.write_text(sha + "\n", encoding="utf-8")
    reviewed_release(repo, sha, carried)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("compatibility recursively spawned Hermes"),
    )

    assert check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, carried),
    ) == (True, "")


def test_runtime_compatibility_rejects_host_outside_selected_release(
    tmp_path: Path, reviewed_release
) -> None:
    repo, sha = _agent_repo(tmp_path)
    carried = _carried_head(repo, sha)
    expected = tmp_path / "supported-upstream"
    expected.write_text(sha + "\n", encoding="utf-8")
    reviewed_release(repo, sha, carried)

    compatible, reason = check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, carried),
        runtime_prefix=repo / "venv",
    )

    assert compatible is False
    assert "host runtime" in reason


def test_runtime_compatibility_honors_custom_checkout_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reviewed_release
) -> None:
    repo, sha = _agent_repo(tmp_path)
    carried = _carried_head(repo, sha)
    expected = tmp_path / "supported-upstream"
    expected.write_text(sha + "\n", encoding="utf-8")
    reviewed_release(repo, sha, carried)
    monkeypatch.setenv("HERMES_AGENT_REPO", f"{repo}/")

    assert check_hermes_compatibility(
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, carried),
        hermes_version_output=f"Hermes Agent test · upstream {sha[:8]}",
    ) == (True, "")


def test_runtime_compatibility_ignores_stale_read_only_checkout(
    tmp_path: Path, reviewed_release
) -> None:
    repo, old_sha = _agent_repo(tmp_path)
    carried = _carried_head(repo, old_sha)
    expected = tmp_path / "supported-upstream"
    expected.write_text(old_sha + "\n", encoding="utf-8")
    reviewed_release(repo, old_sha, carried)
    (repo / "README.md").write_text("updated\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git(
        "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "manual update", cwd=repo,
    )
    new_sha = _git("rev-parse", "HEAD", cwd=repo)
    _git("update-ref", "refs/remotes/origin/main", new_sha, cwd=repo)
    assert check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, carried),
        hermes_version_output=f"Hermes Agent test · upstream {old_sha[:8]}",
    ) == (True, "")


def test_runtime_compatibility_rejects_an_unselected_reviewed_release(
    tmp_path: Path, reviewed_release
) -> None:
    repo, sha = _agent_repo(tmp_path)
    carried = _carried_head(repo, sha)
    expected = tmp_path / "supported-upstream"
    expected.write_text(sha + "\n", encoding="utf-8")
    layout = reviewed_release(repo, sha, carried, select=False)

    compatible, reason = check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, carried),
        hermes_version_output=f"Hermes Agent test · upstream {sha[:8]}",
    )

    assert compatible is False
    assert str(layout.selector) in reason


def test_runtime_compatibility_rejects_a_foreign_selection(
    tmp_path: Path, reviewed_release
) -> None:
    repo, sha = _agent_repo(tmp_path)
    carried = _carried_head(repo, sha)
    expected = tmp_path / "supported-upstream"
    expected.write_text(sha + "\n", encoding="utf-8")
    layout = reviewed_release(repo, sha, carried)
    foreign = layout.root / f"release-{'f' * 40}"
    (foreign / "venv" / "bin").mkdir(parents=True)
    layout.selector.write_text(f"{foreign}\n", encoding="utf-8")

    compatible, reason = check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, carried),
        hermes_version_output=f"Hermes Agent test · upstream {sha[:8]}",
    )

    assert compatible is False
    assert str(foreign) in reason
    assert str(layout.release) in reason


def test_runtime_compatibility_rejects_a_symlinked_selector(
    tmp_path: Path, reviewed_release
) -> None:
    repo, sha = _agent_repo(tmp_path)
    carried = _carried_head(repo, sha)
    expected = tmp_path / "supported-upstream"
    expected.write_text(sha + "\n", encoding="utf-8")
    layout = reviewed_release(repo, sha, carried)
    elsewhere = tmp_path / "attacker-selector"
    elsewhere.write_text(f"{layout.release}\n", encoding="utf-8")
    layout.selector.unlink()
    layout.selector.symlink_to(elsewhere)

    compatible, reason = check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, carried),
        hermes_version_output=f"Hermes Agent test · upstream {sha[:8]}",
    )

    assert compatible is False
    assert "selector" in reason


@pytest.mark.parametrize(
    "damage",
    [
        "release",
        "executable",
        "receipt",
        "receipt-schema",
        "receipt-version",
        "receipt-identity",
    ],
)
def test_runtime_compatibility_rejects_an_untrustworthy_release(
    tmp_path: Path, damage: str, reviewed_release
) -> None:
    repo, sha = _agent_repo(tmp_path)
    carried = _carried_head(repo, sha)
    expected = tmp_path / "supported-upstream"
    expected.write_text(sha + "\n", encoding="utf-8")
    layout = reviewed_release(repo, sha, carried)
    receipt = layout.release / COMPLETION_RECEIPT_NAME
    if damage == "release":
        layout.release.rename(layout.root / "moved-away")
    elif damage == "executable":
        (layout.release / "venv" / "bin" / "hermes").unlink()
    elif damage == "receipt":
        receipt.unlink()
    elif damage == "receipt-schema":
        receipt.write_text("not a receipt\n", encoding="utf-8")
    elif damage == "receipt-version":
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["version"] = 1
        receipt.write_text(json.dumps(payload), encoding="utf-8")
    else:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["release_identity"] = [payload["release_identity"][0], 1]
        receipt.write_text(json.dumps(payload), encoding="utf-8")

    compatible, reason = check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, carried),
        hermes_version_output=f"Hermes Agent test · upstream {sha[:8]}",
    )

    assert compatible is False
    assert "Hermes release" in reason
    if damage == "receipt-version":
        assert "schema" in reason


def test_runtime_compatibility_rejects_cli_from_different_upstream(
    tmp_path: Path, reviewed_release
) -> None:
    repo, supported_sha = _agent_repo(tmp_path)
    carried = _carried_head(repo, supported_sha)
    expected = tmp_path / "supported-upstream"
    expected.write_text(supported_sha + "\n", encoding="utf-8")
    reviewed_release(repo, supported_sha, carried)

    compatible, reason = check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, carried),
        hermes_version_output="Hermes Agent test · upstream deadbeef",
    )

    assert compatible is False
    assert "active Hermes CLI upstream deadbeef" in reason


def test_runtime_compatibility_rejects_traversal_checkout_path(tmp_path: Path) -> None:
    repo, sha = _agent_repo(tmp_path)
    expected = tmp_path / "supported-upstream"
    expected.write_text(sha + "\n", encoding="utf-8")

    compatible, reason = check_hermes_compatibility(
        agent_repo=Path(f"{repo}/.."),
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, sha),
    )

    assert compatible is False
    assert "HERMES_AGENT_REPO" in reason


def test_runtime_compatibility_rejects_symlink_pin(tmp_path: Path) -> None:
    repo, sha = _agent_repo(tmp_path)
    target = tmp_path / "target"
    target.write_text(sha + "\n", encoding="utf-8")
    expected = tmp_path / "supported-upstream"
    expected.symlink_to(target)

    compatible, reason = check_hermes_compatibility(
        agent_repo=repo, expected_file=expected,
    )

    assert compatible is False
    assert "not a trusted" in reason


@pytest.mark.parametrize("kind", ["missing", "invalid"])
def test_supported_upstream_reader_fails_closed(tmp_path: Path, kind: str) -> None:
    expected = tmp_path / "supported-upstream"
    if kind == "invalid":
        expected.write_text("not-a-commit\n", encoding="utf-8")

    with pytest.raises((OSError, ValueError)):
        read_supported_upstream(expected)


@pytest.mark.parametrize(
    ("entrypoint", "arguments", "expected_returncode"),
    [
        ("claude-kanban-hook", ["prompt"], 0),
        ("codex-kanban-hook", ["prompt"], 0),
        ("kanban-adapter", ["start", "--title", "test"], 1),
    ],
)
def test_installed_entrypoints_refuse_unsupported_hermes(
    tmp_path: Path,
    entrypoint: str,
    arguments: list[str],
    expected_returncode: int,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git = fake_bin / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        "echo deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    home = tmp_path / "home"
    (home / ".hermes/hermes-agent").mkdir(parents=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [str(ROOT / "bin" / entrypoint), *arguments],
        input="{}",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == expected_returncode
    assert "unsupported Hermes" in result.stderr
