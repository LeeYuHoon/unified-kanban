from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kanban_adapter.compatibility import check_hermes_compatibility, read_supported_upstream


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


def test_runtime_compatibility_accepts_exact_upstream(tmp_path: Path) -> None:
    repo, sha = _agent_repo(tmp_path)
    expected = tmp_path / "supported-upstream"
    expected.write_text(sha + "\n", encoding="utf-8")
    assert check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, sha),
        hermes_version_output=f"Hermes Agent test · upstream {sha[:8]}",
    ) == (True, "")


def test_runtime_compatibility_rejects_manual_hermes_update(
    tmp_path: Path,
) -> None:
    repo, old_sha = _agent_repo(tmp_path)
    expected = tmp_path / "supported-upstream"
    expected.write_text(old_sha + "\n", encoding="utf-8")
    (repo / "README.md").write_text("updated\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git(
        "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "manual update", cwd=repo,
    )
    new_sha = _git("rev-parse", "HEAD", cwd=repo)
    _git("update-ref", "refs/remotes/origin/main", new_sha, cwd=repo)
    compatible, reason = check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, old_sha),
        hermes_version_output=f"Hermes Agent test · upstream {new_sha[:8]}",
    )

    assert compatible is False
    assert new_sha in reason
    assert old_sha in reason


def test_runtime_compatibility_rejects_changed_checkout_with_stale_origin(
    tmp_path: Path,
) -> None:
    repo, supported_sha = _agent_repo(tmp_path)
    expected = tmp_path / "supported-upstream"
    expected.write_text(supported_sha + "\n", encoding="utf-8")
    carried = _carried_manifest(tmp_path, supported_sha)
    (repo / "README.md").write_text("different active checkout\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git(
        "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "different active checkout", cwd=repo,
    )

    compatible, reason = check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=carried,
        hermes_version_output=f"Hermes Agent test · upstream {supported_sha[:8]}",
    )

    assert compatible is False
    assert "active Hermes checkout" in reason
    assert supported_sha in reason


def test_runtime_compatibility_rejects_cli_from_different_upstream(
    tmp_path: Path,
) -> None:
    repo, supported_sha = _agent_repo(tmp_path)
    expected = tmp_path / "supported-upstream"
    expected.write_text(supported_sha + "\n", encoding="utf-8")

    compatible, reason = check_hermes_compatibility(
        agent_repo=repo,
        expected_file=expected,
        carried_commits_file=_carried_manifest(tmp_path, supported_sha),
        hermes_version_output="Hermes Agent test · upstream deadbeef",
    )

    assert compatible is False
    assert "active Hermes CLI upstream deadbeef" in reason


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
    assert "unsupported Hermes upstream" in result.stderr