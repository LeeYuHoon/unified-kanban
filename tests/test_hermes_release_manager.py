"""Tests for immutable Hermes source release construction."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import marshal
import os
import plistlib
import shlex
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

METADATA_NAMESPACE = "contributors/emails/"
# The byte-greatest literal path is the deterministic representative, so the
# lowercase-"e" spelling is the one a normalized release must leave on disk.
REVIEWED_COLLISION = (
    ("100644", f"{METADATA_NAMESPACE}agent@Example-Host.local", "upper-side\n"),
    ("100644", f"{METADATA_NAMESPACE}agent@example-Host.local", "lower-side\n"),
)
REVIEWED_REPRESENTATIVE = f"{METADATA_NAMESPACE}agent@example-Host.local"

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "hermes-release-manager.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("hermes_release_manager", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_manager_uses_trusted_system_git() -> None:
    helper = load_helper()
    assert helper.TRUSTED_GIT == "/usr/bin/git"
    source = (REPO / "scripts/hermes-release-manager.py").read_text(encoding="utf-8")
    assert '["git", "-C"' not in source


def git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            message,
        ],
        cwd=repo,
        check=True,
    )
    return git("rev-parse", "HEAD", cwd=repo)


def fake_uv(tmp_path: Path, name: str, *, fails: bool = False) -> Path:
    script = tmp_path / name
    script.write_text(
        "#!/bin/sh\necho 'injected dependency sync failure' >&2\nexit 1\n"
        if fails
        else (
            "#!/bin/sh\n"
            "if [ \"$1\" = venv ]; then mkdir -p \"$2/bin\"; "
            f"ln -s {shlex.quote(sys.executable)} \"$2/bin/python\"; fi\n"
            "if [ \"$1\" = sync ]; then "
            "printf '#!/bin/sh\\n' > \"$UV_PROJECT_ENVIRONMENT/bin/hermes\"; "
            "chmod +x \"$UV_PROJECT_ENVIRONMENT/bin/hermes\"; fi\n"
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def python_release(tmp_path: Path) -> tuple[Path, Path]:
    release = tmp_path / ("release-" + "b" * 40)
    package = release / "sample_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from .module import VALUE\n", encoding="utf-8")
    source = package / "module.py"
    source.write_text("VALUE = 42\n", encoding="utf-8")
    python = release / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    return release, source


def install_receiptable_launchers(release: Path) -> None:
    """Install a real Python plus a harmless Hermes executable for receipt tests."""
    bin_dir = release / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    hermes = bin_dir / "hermes"
    hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hermes.chmod(0o755)


def build_incomplete_release(
    helper, tmp_path: Path, *, carried_entries: tuple[tuple[str, str, str], ...] = ()
) -> tuple[object, dict[str, object]]:
    """Publish a release exactly as an interrupted dependency sync leaves it."""
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(
        tmp_path, carried_entries=carried_entries
    )
    layout = helper.release_layout(checkout, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": bundle,
        "source_url": str(source),
        "allow_local_source": True,
    }
    helper.build_source_release(layout, **build)
    assert not (layout.release / helper._COMPLETION_RECEIPT).exists()
    return layout, build


def build_completed_release(helper, checkout: Path, work: Path):
    work.mkdir()
    source, bundle, upstream, carried = make_repositories(
        work,
        carried_entries=(("100644", "gc-fixture.txt", f"{work.name}\n"),),
    )
    layout = helper.release_layout(checkout, upstream, carried)
    helper.build_source_release(
        layout,
        upstream=upstream,
        carried=carried,
        bundle=bundle,
        source_url=str(source),
        allow_local_source=True,
    )
    install_receiptable_launchers(layout.release)
    helper._publish_bytecode_fingerprint(layout.release, carried)
    helper._publish_completion_receipt(layout, upstream, carried)
    return layout


def temporary_release_names(layout) -> list[str]:
    return [
        path.name
        for path in layout.root.iterdir()
        if path.name.startswith((".retired-", ".building-"))
    ]


def make_repositories(
    tmp_path: Path, *, carried_entries: tuple[tuple[str, str, str], ...] = ()
) -> tuple[Path, Path, str, str]:
    """Build a synthetic official source plus its reviewed carried bundle.

    The carried commit is assembled in a private index rather than in the
    worktree, so a test may place entries that a case-insensitive volume can
    never materialize side by side without the fixture itself tripping over the
    very collision it is trying to exercise.
    """
    source = tmp_path / "synthetic-official"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=source, check=True)
    # macOS Git precomposes every path it is handed, so a decomposed tree entry
    # can only be staged with that rewrite disabled. Upstream trees written on
    # Linux carry decomposed paths verbatim, which is what this reproduces.
    subprocess.run(
        ["git", "config", "--local", "core.precomposeunicode", "false"],
        cwd=source,
        check=True,
    )
    (source / "payload.txt").write_text("upstream\n", encoding="utf-8")
    (source / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    upstream = commit(source, "upstream")
    index = tmp_path / "carried.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    subprocess.run(["git", "read-tree", upstream], cwd=source, check=True, env=env)
    for mode, path, content in (("100644", "payload.txt", "carried\n"), *carried_entries):
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
            input=content,
            env=env,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}"],
            cwd=source,
            check=True,
            env=env,
        )
    tree = subprocess.run(
        ["git", "write-tree"], cwd=source, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()
    carried = subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit-tree",
            tree,
            "-p",
            upstream,
            "-m",
            "carried",
        ],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    index.unlink()
    subprocess.run(["git", "branch", "carried-01", carried], cwd=source, check=True)
    bundle = tmp_path / "synthetic-carried.bundle"
    subprocess.run(
        ["git", "bundle", "create", str(bundle), "carried-01", f"^{upstream}"],
        cwd=source,
        check=True,
    )
    return source, bundle, upstream, carried


def test_bundle_import_carries_its_own_file_transport_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persisted policy refuses the file transport; the bundle fetch must not.

    Every real install and update builds with ``allow_local_source=False``, and
    no other test in this file uses that value - all of them pass ``True``. A
    persisted ``protocol.file.allow=never`` therefore looked safe while making
    production release construction impossible: Git routes a bundle path through
    the file transport, so the carried import died with ``transport 'file' not
    allowed`` after the upstream fetch had already succeeded.
    """
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    layout = helper.release_layout(checkout, upstream, carried)

    invocations: list[tuple[str, ...]] = []
    real_run_git = helper._run_git

    def recording_run_git(repository: Path, *arguments: str) -> str:
        invocations.append(arguments)
        return real_run_git(repository, *arguments)

    monkeypatch.setattr(helper, "_run_git", recording_run_git)
    helper.build_source_release(
        layout,
        upstream=upstream,
        carried=carried,
        bundle=bundle,
        source_url=str(source),
        allow_local_source=True,
    )

    assert ("config", "--local", "protocol.file.allow", "never") in invocations
    bundle_fetch = [
        arguments
        for arguments in invocations
        if "fetch" in arguments and any(str(bundle.name) in item or item.endswith(".bundle") for item in arguments)
    ]
    assert len(bundle_fetch) == 1, invocations
    assert bundle_fetch[0][: len(helper._FILE_TRANSPORT)] == helper._FILE_TRANSPORT
    assert bundle_fetch[0][len(helper._FILE_TRANSPORT)] == "fetch"

    # The allowance is load-bearing: without it this exact fetch is refused.
    work = tmp_path / "production-policy"
    work.mkdir()
    real_run_git(work, "init", "-q", "--initial-branch=main")
    for key, value in (
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        ("protocol.file.allow", "never"),
    ):
        real_run_git(work, "config", "--local", key, value)
    real_run_git(
        work, *helper._FILE_TRANSPORT, "fetch", "--no-tags", str(source),
        "+refs/heads/main:refs/upstream/main",
    )
    with pytest.raises(RuntimeError, match="transport 'file' not allowed"):
        real_run_git(
            work, "fetch", "--no-tags", str(bundle),
            "+refs/heads/carried-*:refs/unified-kanban/carried/*",
        )
    real_run_git(
        work, *helper._FILE_TRANSPORT, "fetch", "--no-tags", str(bundle),
        "+refs/heads/carried-*:refs/unified-kanban/carried/*",
    )
    assert real_run_git(work, "rev-parse", f"{carried}^{{commit}}") == carried


def test_run_git_reports_decisive_error_after_platform_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    completed = subprocess.CompletedProcess(
        args=["git"],
        returncode=1,
        stdout="",
        stderr="git: warning: auxiliary cache unavailable\nfatal: transport 'file' not allowed\n",
    )
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(RuntimeError, match="fatal: transport 'file' not allowed"):
        helper._run_git(tmp_path, "fetch")


def test_run_git_binary_reports_decisive_error_after_platform_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    completed = subprocess.CompletedProcess(
        args=["git"],
        returncode=1,
        stdout=b"",
        stderr=b"git: warning: auxiliary cache unavailable\nfatal: malformed object\n",
    )
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(RuntimeError, match="fatal: malformed object"):
        helper._run_git_binary(tmp_path, "cat-file")


def test_layout_is_stable_and_outside_mutable_checkout(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    upstream = "1" * 40
    carried = "2" * 40

    layout = helper.release_layout(checkout, upstream, carried)

    assert layout.root == tmp_path / "hermes-agent.releases"
    assert layout.release == layout.root / f"release-{carried}"
    assert layout.selector == layout.root / "current"
    assert layout.release != checkout


@pytest.mark.parametrize("value", [Path("relative"), Path("/")])
def test_layout_rejects_unsafe_checkout_path(value: Path) -> None:
    helper = load_helper()
    with pytest.raises(ValueError):
        helper.release_layout(value, "1" * 40, "2" * 40)


def test_frozen_snapshot_builds_after_official_main_advances(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "installed-hermes"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    (source / "newer.txt").write_text("next update\n", encoding="utf-8")
    newer = commit(source, "newer main")
    assert newer != upstream
    assert git("rev-parse", "main", cwd=source) == newer
    layout = helper.release_layout(checkout, upstream, carried)

    built = helper.build_source_release(
        layout,
        upstream=upstream,
        carried=carried,
        bundle=bundle,
        source_url=str(source),
        allow_local_source=True,
    )

    assert git("rev-parse", "HEAD", cwd=built) == carried
    assert git("rev-parse", "HEAD^", cwd=built) == upstream
    assert not (built / "newer.txt").exists()


def test_build_source_release_uses_exact_refs_and_clean_private_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    upstream_repo = tmp_path / "upstream"
    upstream_repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=upstream_repo, check=True)
    (upstream_repo / "payload.txt").write_text("upstream\n", encoding="utf-8")
    upstream = commit(upstream_repo, "upstream")
    subprocess.run(["git", "checkout", "-qb", "carried"], cwd=upstream_repo, check=True)
    (upstream_repo / "payload.txt").write_text("carried\n", encoding="utf-8")
    carried = commit(upstream_repo, "carried")
    subprocess.run(
        ["git", "update-ref", "refs/heads/carried-01", carried],
        cwd=upstream_repo,
        check=True,
    )
    bundle = tmp_path / "carried.bundle"
    subprocess.run(
        ["git", "bundle", "create", str(bundle), "refs/heads/carried-01"],
        cwd=upstream_repo,
        check=True,
    )
    subprocess.run(["git", "checkout", "-q", "main"], cwd=upstream_repo, check=True)
    checkout = tmp_path / "installed-hermes"
    checkout.mkdir()
    layout = helper.release_layout(checkout, upstream, carried)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/attacker/global")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.probe.smudge")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/attacker/helper")

    built = helper.build_source_release(
        layout,
        upstream=upstream,
        carried=carried,
        bundle=bundle,
        source_url=str(upstream_repo),
        allow_local_source=True,
    )

    assert built == layout.release
    assert git("rev-parse", "HEAD", cwd=built) == carried
    assert git("rev-parse", "HEAD^", cwd=built) == upstream
    assert git("status", "--porcelain", cwd=built) == ""
    assert (built / "payload.txt").read_text(encoding="utf-8") == "carried\n"
    assert oct(layout.root.stat().st_mode & 0o777) == "0o700"
    assert oct(built.stat().st_mode & 0o777) == "0o700"
    config = git("config", "--local", "--list", cwd=built)
    assert "filter.probe.smudge" not in config
    assert "credential.helper=" in config
    assert not any(path.name.startswith(".building-") for path in layout.root.iterdir())


def test_build_refuses_existing_release(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    layout.root.mkdir(mode=0o700)
    layout.release.mkdir(mode=0o700)

    with pytest.raises(FileExistsError):
        helper.build_source_release(
            layout,
            upstream="1" * 40,
            carried="2" * 40,
            bundle=tmp_path / "missing.bundle",
            source_url="https://github.com/NousResearch/hermes-agent.git",
        )


def test_gc_dry_run_protects_explicit_current_and_previous_references(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    current = build_completed_release(helper, checkout, tmp_path / "current-work")
    previous = build_completed_release(helper, checkout, tmp_path / "previous-work")
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    current.selector.write_bytes(helper.release_reference_payload(current.root, current.release))
    current.selector.chmod(0o600)
    previous_reference = current.root / "previous"
    previous_reference.write_bytes(
        helper.release_reference_payload(current.root, previous.release)
    )
    previous_reference.chmod(0o600)
    before = sorted(path.name for path in current.root.iterdir())

    plan = helper.plan_release_gc(checkout, runtime_references=set())

    assert plan == {
        "candidates": [str(stale.release)],
        "preserved": [],
        "protected": sorted(
            [
                {"path": str(current.release), "reasons": ["current"]},
                {"path": str(previous.release), "reasons": ["previous"]},
            ],
            key=lambda item: item["path"],
        ),
        "release_root": str(current.root),
        "version": 1,
    }
    assert sorted(path.name for path in current.root.iterdir()) == before


def test_gc_dry_run_protects_runtime_references(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    running = build_completed_release(helper, checkout, tmp_path / "running-work")

    plan = helper.plan_release_gc(checkout, runtime_references={running.release})

    assert plan["candidates"] == []
    assert plan["protected"] == [
        {"path": str(running.release), "reasons": ["runtime"]}
    ]


def test_gc_dry_run_preserves_tampered_foreign_and_unrecognized_entries(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    tampered = build_completed_release(helper, checkout, tmp_path / "tampered-work")
    (tampered.release / "venv/bin/hermes").write_text(
        "#!/bin/sh\necho tampered\n", encoding="utf-8"
    )
    foreign = tampered.root / f"release-{'a' * 40}"
    foreign.mkdir()
    unknown = tampered.root / "operator-notes"
    unknown.mkdir()

    plan = helper.plan_release_gc(checkout, runtime_references=set())

    assert plan["candidates"] == []
    preserved = {Path(item["path"]).name: item["reason"] for item in plan["preserved"]}
    assert preserved["operator-notes"] == "unrecognized"
    assert preserved[foreign.name].startswith("unverified:")
    assert preserved[tampered.release.name].startswith("unverified:")


def test_gc_dry_run_rejects_a_malformed_durable_reference(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    release = build_completed_release(helper, checkout, tmp_path / "release-work")
    release.selector.write_text("foreign\n", encoding="utf-8")
    release.selector.chmod(0o600)

    with pytest.raises(RuntimeError, match="does not name a managed release"):
        helper.plan_release_gc(checkout, runtime_references=set())

    assert release.release.is_dir()


def test_gc_collects_launchd_and_process_runtime_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent.releases"
    root.mkdir(mode=0o700)
    plist_release = root / f"release-{'1' * 40}"
    loaded_release = root / f"release-{'2' * 40}"
    process_release = root / f"release-{'3' * 40}"
    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_bytes(
        plistlib.dumps(
            {"ProgramArguments": [str(plist_release / "venv/bin/python"), "-m", "hermes_cli.main"]}
        )
    )
    plist.chmod(0o600)

    def fake_run(command, **kwargs):
        if command[:2] == ["/bin/launchctl", "print"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=f"program = {loaded_release}/venv/bin/python\n", stderr=""
            )
        if command == ["/bin/ps", "-axo", "pid=,uid=,state=,lstart=,command=", "-ww"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"123 {os.getuid()} S {process_release}/venv/bin/hermes gateway\n",
                stderr="",
            )
        if command == ["/usr/sbin/lsof", "-n", "-P", "-u", str(os.getuid()), "-F", "pfn"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"p123\nfcwd\nn{process_release}/runtime.db\n",
                stderr="",
            )
        raise AssertionError(command)

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    assert helper.collect_runtime_references(root, plist) == {
        plist_release,
        loaded_release,
        process_release,
    }


def test_gc_runtime_reference_probe_fails_closed_on_indeterminate_launchd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent.releases"
    root.mkdir(mode=0o700)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 77, stdout="", stderr="unavailable")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="launchd reference probe"):
        helper.collect_runtime_references(root, tmp_path / "missing.plist")


def test_gc_apply_deletes_only_a_verified_unreferenced_release(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")

    result = helper.apply_release_gc(
        checkout,
        runtime_reference_supplier=lambda: set(),
        lock_verifier=lambda: None,
    )

    assert result == {"deleted": [str(stale.release)], "retained": []}
    assert not stale.release.exists()
    assert not any(path.name.startswith(".gc-retired-") for path in stale.root.iterdir())


def test_gc_apply_restores_a_release_referenced_after_retirement(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    identity = stale.release.stat().st_ino
    observations = iter((set(), {stale.release}))

    result = helper.apply_release_gc(
        checkout,
        runtime_reference_supplier=lambda: next(observations),
        lock_verifier=lambda: None,
    )

    assert result == {
        "deleted": [],
        "retained": [{"path": str(stale.release), "reason": "referenced-after-retirement"}],
    }
    assert stale.release.stat().st_ino == identity
    assert not any(path.name.startswith(".gc-retired-") for path in stale.root.iterdir())


def test_gc_apply_requires_the_shared_activation_lock(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()

    with pytest.raises(RuntimeError, match="shared activation lock"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=None,
        )


def test_gc_apply_rechecks_lock_before_namespace_mutation(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    checks = 0

    def verify_lock() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("lock identity changed")

    with pytest.raises(RuntimeError, match="lock identity changed"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=verify_lock,
        )

    assert stale.release.is_dir()
    assert not any(path.name.startswith(".gc-retired-") for path in stale.root.iterdir())


def test_gc_cli_defaults_to_deterministic_dry_run_without_lock_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(helper, "collect_runtime_references", lambda root, plist: set())
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout)],
    )

    assert helper.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["candidates"] == [str(stale.release)]
    assert stale.release.is_dir()
    assert not (hermes_home / "state").exists()


def test_gc_cli_apply_respects_an_existing_updater_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    hermes_home = tmp_path / "hermes-home"
    state = hermes_home / "state"
    state.mkdir(parents=True, mode=0o700)
    os.symlink("foreign-token", state / "hermes-kanban-update.lock")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(helper, "collect_runtime_references", lambda root, plist: set())
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout), "--apply"],
    )

    with pytest.raises(subprocess.CalledProcessError):
        helper.main()

    assert stale.release.is_dir()
    assert os.readlink(state / "hermes-kanban-update.lock") == "foreign-token"


def test_gc_cli_apply_uses_and_releases_the_shared_updater_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    def collect_while_shared_lock_is_held(root: Path, plist: Path) -> set[Path]:
        lock = hermes_home / "state/hermes-kanban-update.lock"
        assert lock.is_symlink()
        assert not (hermes_home / "state/update.lock").exists()
        return set()

    monkeypatch.setattr(
        helper, "collect_runtime_references", collect_while_shared_lock_is_held
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout), "--apply"],
    )

    assert helper.main() == 0

    assert json.loads(capsys.readouterr().out) == {
        "deleted": [str(stale.release)],
        "retained": [],
    }
    assert not stale.release.exists()
    assert not (hermes_home / "state/hermes-kanban-update.lock").exists()


def test_gc_apply_never_restores_a_partially_deleted_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")

    original_rmtree = helper.shutil.rmtree

    def fail_after_partial_delete(retirement: str, *, dir_fd: int) -> None:
        os.unlink(f"{retirement}/venv/bin/hermes", dir_fd=dir_fd)
        raise OSError("injected partial delete failure")

    monkeypatch.setattr(helper.shutil, "rmtree", fail_after_partial_delete)

    with pytest.raises(OSError, match="partial delete failure"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert not stale.release.exists()
    retired = [
        path
        for path in stale.root.iterdir()
        if path.name.startswith(".gc-retired-") and path.is_dir()
    ]
    records = list(stale.root.glob(".gc-retired-*.record"))
    assert len(retired) == 1
    assert len(records) == 1
    assert not (retired[0] / "venv/bin/hermes").exists()

    monkeypatch.setattr(helper.shutil, "rmtree", original_rmtree)
    resumed = helper.apply_release_gc(
        checkout,
        runtime_reference_supplier=lambda: set(),
        lock_verifier=lambda: None,
    )

    assert resumed == {"deleted": [str(stale.release)], "retained": []}
    assert not retired[0].exists()
    assert not records[0].exists()


def test_gc_apply_validates_release_root_before_resuming_retirements(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    checkout = linked_parent / "hermes-agent"
    checkout.mkdir()
    root = checkout.with_name(f"{checkout.name}.releases")
    root.mkdir(mode=0o700)
    release = root / f"release-{'4' * 40}"
    retirement = root / f".gc-retired-{'4' * 40}-{'5' * 32}"
    retirement.mkdir(mode=0o700)
    (retirement / "owned-data").write_text("preserve\n", encoding="utf-8")
    info = retirement.stat()
    record = retirement.with_name(f"{retirement.name}.record")
    record.write_bytes(
        helper._gc_retirement_record_payload(
            release, retirement, (info.st_dev, info.st_ino)
        )
    )
    record.chmod(0o600)

    with pytest.raises(RuntimeError, match="symlinked ancestor"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert retirement.is_dir()
    assert record.is_file()
    assert (retirement / "owned-data").read_text(encoding="utf-8") == "preserve\n"


def test_gc_apply_preserves_a_malformed_foreign_retirement_record(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    root = checkout.with_name(f"{checkout.name}.releases")
    root.mkdir(mode=0o700)
    record = root / f".gc-retired-{'5' * 40}-{'6' * 32}.record"
    record.write_text("foreign\n", encoding="utf-8")
    record.chmod(0o600)

    result = helper.apply_release_gc(
        checkout,
        runtime_reference_supplier=lambda: set(),
        lock_verifier=lambda: None,
    )

    assert result["deleted"] == []
    assert result["retained"] == [
        {"path": str(record), "reason": "unverified-record: GC retirement record is invalid"}
    ]
    assert record.read_text(encoding="utf-8") == "foreign\n"


def test_gc_apply_restores_after_post_retirement_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    identity = stale.release.stat().st_ino
    original_fsync = helper._fsync_gc_root
    calls = 0

    def fail_second_directory_fsync(capability, *, allow_moved: bool = False) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected post-retirement fsync failure")
        original_fsync(capability, allow_moved=allow_moved)

    monkeypatch.setattr(helper, "_fsync_gc_root", fail_second_directory_fsync)

    with pytest.raises(OSError, match="post-retirement fsync failure"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert stale.release.stat().st_ino == identity
    assert not list(stale.root.glob(".gc-retired-*"))


def test_gc_dry_run_preserves_release_with_public_completion_receipt(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    (stale.release / helper._COMPLETION_RECEIPT).chmod(0o666)
    plan = helper.plan_release_gc(checkout, runtime_references=set())
    assert stale.release not in plan["candidates"]
    assert any(item["path"] == str(stale.release) for item in plan["preserved"])


def test_gc_dry_run_preserves_release_with_foreign_leaf_metadata(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    release = build_completed_release(helper, checkout, tmp_path / "release-work")
    release.release.chmod(0o755)

    plan = helper.plan_release_gc(checkout, runtime_references=set())

    assert plan["candidates"] == []
    assert plan["preserved"] == [
        {
            "path": str(release.release),
            "reason": "unverified: release directory is not owner-only",
        }
    ]
    assert release.release.is_dir()


def test_gc_dry_run_preserves_symlinked_incomplete_and_hardlinked_receipts(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    hardlinked = build_completed_release(helper, checkout, tmp_path / "hardlink-work")
    receipt = hardlinked.release / helper._COMPLETION_RECEIPT
    receipt_copy = tmp_path / "receipt-copy"
    receipt_copy.write_bytes(receipt.read_bytes())
    receipt_copy.chmod(0o600)
    receipt.unlink()
    os.link(receipt_copy, receipt)
    incomplete = hardlinked.root / f"release-{'8' * 40}"
    incomplete.mkdir()
    outside = tmp_path / "outside-release"
    outside.mkdir()
    symlinked = hardlinked.root / f"release-{'9' * 40}"
    symlinked.symlink_to(outside, target_is_directory=True)

    plan = helper.plan_release_gc(checkout, runtime_references=set())

    assert plan["candidates"] == []
    preserved = {Path(item["path"]).name for item in plan["preserved"]}
    assert {hardlinked.release.name, incomplete.name, symlinked.name} <= preserved
    assert symlinked.is_symlink()
    assert receipt.stat().st_nlink == 2


def test_gc_collects_runtime_references_from_open_files_and_working_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent.releases"
    root.mkdir(mode=0o700)
    open_release = root / f"release-{'4' * 40}"

    def fake_run(command, **kwargs):
        if command[:2] == ["/bin/launchctl", "print"]:
            uid = os.getuid()
            return subprocess.CompletedProcess(
                command,
                113,
                stdout="",
                stderr=(
                    "Bad request.\nCould not find service "
                    f'"ai.hermes.gateway" in domain for user gui: {uid}\n'
                ),
            )
        if command == ["/bin/ps", "-axo", "pid=,uid=,state=,lstart=,command=", "-ww"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"123 {os.getuid()} S python3 gateway\n", stderr="")
        if command == ["/usr/sbin/lsof", "-n", "-P", "-u", str(os.getuid()), "-F", "pfn"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=f"p123\nfcwd\nn{open_release}/runtime.db\n", stderr=""
            )
        raise AssertionError(command)

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    assert helper.collect_runtime_references(root, tmp_path / "missing.plist") == {
        open_release
    }


def test_gc_runtime_reference_probe_fails_closed_on_indeterminate_open_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent.releases"
    root.mkdir(mode=0o700)

    def fake_run(command, **kwargs):
        if command[:2] == ["/bin/launchctl", "print"]:
            uid = os.getuid()
            return subprocess.CompletedProcess(
                command,
                113,
                stdout="",
                stderr=(
                    "Bad request.\nCould not find service "
                    f'"ai.hermes.gateway" in domain for user gui: {uid}\n'
                ),
            )
        if command == ["/bin/ps", "-axo", "pid=,uid=,state=,lstart=,command=", "-ww"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"123 {os.getuid()} S python3 gateway\n", stderr="")
        return subprocess.CompletedProcess(command, 77, stdout="", stderr="unavailable")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="open-file reference probe"):
        helper.collect_runtime_references(root, tmp_path / "missing.plist")


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("", ""),
        ("not-a-field-record\n", ""),
        ("p123\n", ""),
        ("p123\nf1\n", ""),
        ("p123\nfcwd\nn/tmp/runtime.db\n", "lsof: partial result\n"),
    ],
)
def test_gc_runtime_reference_probe_rejects_successful_incomplete_open_file_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    stderr: str,
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent.releases"
    root.mkdir(mode=0o700)

    def fake_run(command, **kwargs):
        if command[:2] == ["/bin/launchctl", "print"]:
            uid = os.getuid()
            return subprocess.CompletedProcess(
                command,
                113,
                stdout="",
                stderr=(
                    "Bad request.\nCould not find service "
                    f'"ai.hermes.gateway" in domain for user gui: {uid}\n'
                ),
            )
        if command[:2] == ["/bin/ps", "-axo"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"123 {os.getuid()} S python3 gateway\n",
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="open-file reference probe"):
        helper.collect_runtime_references(root, tmp_path / "missing.plist")


def test_gc_runtime_reference_probe_fails_closed_on_indeterminate_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent.releases"
    root.mkdir(mode=0o700)

    def fake_run(command, **kwargs):
        if command[:2] == ["/bin/launchctl", "print"]:
            uid = os.getuid()
            return subprocess.CompletedProcess(
                command,
                113,
                stdout="",
                stderr=(
                    "Bad request.\nCould not find service "
                    f'"ai.hermes.gateway" in domain for user gui: {uid}\n'
                ),
            )
        return subprocess.CompletedProcess(command, 77, stdout="", stderr="unavailable")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="process reference probe"):
        helper.collect_runtime_references(root, tmp_path / "missing.plist")


def test_gc_apply_reverifies_each_candidate_at_its_canonical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_plan = helper.plan_release_gc

    def tampering_plan(*args, **kwargs):
        plan = original_plan(*args, **kwargs)
        (stale.release / "venv/bin/hermes").write_text(
            "#!/bin/sh\necho changed\n", encoding="utf-8"
        )
        return plan

    monkeypatch.setattr(helper, "plan_release_gc", tampering_plan)

    with pytest.raises(RuntimeError, match="inventory|fingerprint|digest|does not match content"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert stale.release.is_dir()
    assert not any(path.name.startswith(".gc-retired-") for path in stale.root.iterdir())


def test_gc_apply_preserves_a_foreign_canonical_successor_and_owned_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_rename = helper._rename_exclusive_at
    successor_identity = None

    def inject_successor(directory_fd: int, source: str, destination: str) -> None:
        nonlocal successor_identity
        original_rename(directory_fd, source, destination)
        if source == stale.release.name and destination.startswith(".gc-retired-"):
            os.mkdir(source, dir_fd=directory_fd)
            successor_identity = os.stat(source, dir_fd=directory_fd).st_ino

    monkeypatch.setattr(helper, "_rename_exclusive_at", inject_successor)

    with pytest.raises(RuntimeError, match="foreign canonical successor"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert stale.release.stat().st_ino == successor_identity
    retired = [
        path
        for path in stale.root.iterdir()
        if path.name.startswith(".gc-retired-") and path.is_dir()
    ]
    records = list(stale.root.glob(".gc-retired-*.record"))
    assert len(retired) == 1
    assert len(records) == 1
    assert (retired[0] / helper._COMPLETION_RECEIPT).is_file()


def test_gc_dry_run_rejects_a_writable_release_root_ancestor(tmp_path: Path) -> None:
    helper = load_helper()
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_parent.chmod(0o777)
    checkout = unsafe_parent / "hermes-agent"
    checkout.mkdir()
    root = checkout.with_name(f"{checkout.name}.releases")
    root.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="untrusted writable ancestor"):
        helper.plan_release_gc(checkout, runtime_references=set())

    assert root.is_dir()


def test_gc_dry_run_rejects_a_symlinked_release_root_ancestor(tmp_path: Path) -> None:
    helper = load_helper()
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    checkout = linked_parent / "hermes-agent"
    checkout.mkdir()
    release = build_completed_release(helper, checkout, tmp_path / "release-work")

    with pytest.raises(RuntimeError, match="symlinked ancestor"):
        helper.plan_release_gc(checkout, runtime_references=set())

    assert release.release.is_dir()


def test_gc_runtime_reference_probe_accepts_bare_names_and_ignores_exited_pids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent.releases"
    root.mkdir(mode=0o700)
    ps_calls = 0

    def fake_run(command, **kwargs):
        nonlocal ps_calls
        if command[:2] == ["/bin/launchctl", "print"]:
            uid = os.getuid()
            return subprocess.CompletedProcess(command, 113, stdout="", stderr=("Bad request.\nCould not find service " f'"ai.hermes.gateway" in domain for user gui: {uid}\n'))
        if command[:2] == ["/bin/ps", "-axo"]:
            ps_calls += 1
            extra = f"124 {os.getuid()} S short-lived\n" if ps_calls == 1 else ""
            return subprocess.CompletedProcess(command, 0, stdout=f"123 {os.getuid()} S gateway\n{extra}", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="p123\nf1\nn\n", stderr="")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    assert helper.collect_runtime_references(root, tmp_path / "missing.plist") == set()
    assert ps_calls >= 2
    assert ps_calls % 2 == 0


def test_gc_runtime_reference_probe_retries_pid_reuse_until_identity_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent.releases"
    root.mkdir(mode=0o700)
    release = root / f"release-{'9' * 40}"
    ps_outputs = iter(
        [
            f"123 {os.getuid()} S Fri Aug 30 09:00:00 2026 old-process\n",
            f"123 {os.getuid()} S Fri Aug 30 09:00:01 2026 new-process\n",
            f"123 {os.getuid()} S Fri Aug 30 09:00:01 2026 new-process\n",
            f"123 {os.getuid()} S Fri Aug 30 09:00:01 2026 new-process\n",
        ]
    )
    lsof_calls = 0

    def fake_run(command, **kwargs):
        nonlocal lsof_calls
        if command[:2] == ["/bin/launchctl", "print"]:
            uid = os.getuid()
            return subprocess.CompletedProcess(command, 113, stdout="", stderr=("Bad request.\nCould not find service " f'"ai.hermes.gateway" in domain for user gui: {uid}\n'))
        if command[:2] == ["/bin/ps", "-axo"]:
            return subprocess.CompletedProcess(command, 0, stdout=next(ps_outputs), stderr="")
        lsof_calls += 1
        name = "/tmp/old.db" if lsof_calls == 1 else str(release / "runtime.db")
        return subprocess.CompletedProcess(command, 0, stdout=f"p123\nf1\nn{name}\n", stderr="")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    assert helper.collect_runtime_references(root, tmp_path / "missing.plist") == {release}
    assert lsof_calls == 2


def test_gc_apply_creates_retirement_record_only_in_retained_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    displaced = tmp_path / "displaced-root-before-record"
    original_write = helper._write_private_candidate_at
    displaced_once = False

    def displace_before_record(capability, name, payload, mode):
        nonlocal displaced_once
        if not displaced_once:
            displaced_once = True
            stale.root.rename(displaced)
            stale.root.mkdir(mode=0o700)
        return original_write(capability, name, payload, mode)

    monkeypatch.setattr(helper, "_write_private_candidate_at", displace_before_record)
    with pytest.raises(RuntimeError, match="release root.*moved"):
        helper.apply_release_gc(checkout, runtime_reference_supplier=lambda: set(), lock_verifier=lambda: None)

    assert list(stale.root.iterdir()) == []
    assert (displaced / stale.release.name).is_dir()
    assert not list(displaced.glob(".gc-retired-*.record"))


def test_gc_apply_binds_plan_to_the_retained_release_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    original = build_completed_release(helper, checkout, tmp_path / "original-work")
    foreign_root = tmp_path / "foreign-root"
    foreign_root.mkdir(mode=0o700)
    foreign_release = foreign_root / original.release.name
    helper.shutil.copytree(original.release, foreign_release, symlinks=True)
    foreign_release.chmod(0o700)
    displaced = tmp_path / "displaced-original-root"
    original_plan = helper.plan_release_gc

    def replace_root_after_planning(*args, **kwargs):
        plan = original_plan(*args, **kwargs)
        original.root.rename(displaced)
        foreign_root.rename(original.root)
        return plan

    monkeypatch.setattr(helper, "plan_release_gc", replace_root_after_planning)
    with pytest.raises(RuntimeError, match="release root.*moved|release root.*changed"):
        helper.apply_release_gc(checkout, runtime_reference_supplier=lambda: set(), lock_verifier=lambda: None)
    assert (displaced / original.release.name).is_dir()
    assert (original.root / foreign_release.name).is_dir()


def test_gc_apply_protects_post_retirement_open_file_alias(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    calls = 0

    def runtime_references() -> set[Path]:
        nonlocal calls
        calls += 1
        retirements = [
            path
            for path in stale.root.glob(".gc-retired-*")
            if not path.name.endswith(".record")
        ]
        if not retirements:
            return set()
        return helper._release_references_in_text(stale.root, str(retirements[0] / "runtime.db"))

    result = helper.apply_release_gc(checkout, runtime_reference_supplier=runtime_references, lock_verifier=lambda: None)
    assert result["deleted"] == []
    assert stale.release.is_dir()
    assert calls >= 2


def test_gc_apply_rechecks_lock_immediately_before_retirement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    lock_valid = True
    renamed = False
    original_write = helper._write_private_candidate_at
    original_rename = helper._rename_exclusive_at

    def invalidate_after_record(*args, **kwargs):
        nonlocal lock_valid
        result = original_write(*args, **kwargs)
        lock_valid = False
        return result

    def observe_rename(*args, **kwargs):
        nonlocal renamed
        renamed = True
        return original_rename(*args, **kwargs)

    def verify_lock() -> None:
        if not lock_valid:
            raise RuntimeError("injected lock identity change")

    monkeypatch.setattr(helper, "_write_private_candidate_at", invalidate_after_record)
    monkeypatch.setattr(helper, "_rename_exclusive_at", observe_rename)
    with pytest.raises(RuntimeError, match="lock identity change"):
        helper.apply_release_gc(checkout, runtime_reference_supplier=lambda: set(), lock_verifier=verify_lock)
    assert not renamed
    assert stale.release.is_dir()


def test_gc_apply_retains_root_authority_if_namespace_is_displaced_after_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    root = stale.root
    displaced = tmp_path / "displaced-release-root"
    identity = stale.release.stat().st_ino
    original_fsync = helper._fsync_gc_root
    calls = 0

    def displace_root_during_post_retirement_fsync(
        capability, *, allow_moved: bool = False
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            root.rename(displaced)
            root.mkdir(mode=0o700)
        original_fsync(capability, allow_moved=allow_moved)

    monkeypatch.setattr(
        helper, "_fsync_gc_root", displace_root_during_post_retirement_fsync
    )

    with pytest.raises(RuntimeError, match="release root.*changed|release root.*moved"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    restored = displaced / stale.release.name
    assert restored.stat().st_ino == identity
    assert not list(displaced.glob(".gc-retired-*"))
    assert list(root.iterdir()) == []


def test_selector_payload_names_exact_absolute_release(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    layout.root.mkdir(mode=0o700)
    layout.release.mkdir(mode=0o700)

    assert helper.selector_payload(layout) == f"{layout.release}\n".encode()


def test_managed_launcher_executes_only_selected_release(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "Hermes Agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    launcher = tmp_path / "hermes"
    layout.release.joinpath("venv", "bin").mkdir(parents=True)
    selected = layout.release / "venv" / "bin" / "hermes"
    selected.write_text(
        "#!/bin/sh\n"
        "printf 'SELECTED:%s\\n' \"$1\"\n"
        "printf 'SEALED:%s\\n' \"${HERMES_DISABLE_LAZY_INSTALLS-}\"\n"
        "printf 'LAZY_TARGET:%s\\n' \"${HERMES_LAZY_INSTALL_TARGET-}\"\n"
        "printf 'PYTHONPATH:%s\\n' \"${PYTHONPATH-}\"\n"
        "printf 'PYTHONHOME:%s\\n' \"${PYTHONHOME-}\"\n",
        encoding="utf-8",
    )
    selected.chmod(0o755)
    layout.selector.write_bytes(helper.selector_payload(layout))
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)

    injection = tmp_path / "injection"
    injection.mkdir()
    injection.joinpath("stat.py").write_text(
        "raise RuntimeError('launcher imported attacker-controlled stat')\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(launcher), "argument with spaces"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(injection), "PYTHONHOME": str(injection)},
    )

    assert completed.returncode == 0
    assert completed.stdout == (
        "SELECTED:argument with spaces\n"
        "SEALED:1\n"
        f"LAZY_TARGET:{layout.root / 'lazy-packages'}\n"
        "PYTHONPATH:\n"
        "PYTHONHOME:\n"
    )
    assert stat.S_IMODE((layout.root / "lazy-packages").stat().st_mode) == 0o700

    layout.selector.write_text(f"{tmp_path / 'foreign' / ('release-' + '2' * 40)}\n")
    rejected = subprocess.run([str(launcher)], capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert "invalid Hermes release selector" in rejected.stderr


def test_managed_launcher_blocks_native_update_before_selector_access(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    launcher = tmp_path / "hermes"
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)

    completed = subprocess.run(
        [str(launcher), "update"], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (
        "Native `hermes update` is disabled for this unified-kanban managed immutable release.\n"
        "Check managed release status with:\n"
        "  ./scripts/update-hermes-if-needed.sh --check\n"
        "Activate the reviewed release with:\n"
        "  ./scripts/update-hermes-if-needed.sh\n"
    )
    assert not layout.root.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["update", "--check"],
        ["update", "--gateway"],
        ["update", "--yes"],
        ["update", "--force"],
        ["update", "--branch", "main"],
        ["update", "-b", "main"],
        ["update", "--plan", "--yes"],
        ["update", "--help", "--force"],
        ["update", "--unknown"],
    ],
)
def test_managed_launcher_blocks_all_mutating_or_mixed_update_forms_before_selector_access(
    tmp_path: Path, arguments: list[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    launcher = tmp_path / "hermes"
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)

    completed = subprocess.run(
        [str(launcher), *arguments], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 2
    assert "Native `hermes update` is disabled" in completed.stderr
    assert not layout.root.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--provider", "auto", "update", "--check"],
        ["--provider=auto", "update", "--yes"],
        ["-m", "model-name", "update", "--force"],
        ["--verbose", "update"],
        ["--", "update", "--gateway"],
    ],
)
def test_managed_launcher_blocks_update_after_top_level_global_options(
    tmp_path: Path, arguments: list[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    launcher = tmp_path / "hermes"
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)

    completed = subprocess.run(
        [str(launcher), *arguments], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 2
    assert "Native `hermes update` is disabled" in completed.stderr
    assert not layout.root.exists()


@pytest.mark.parametrize(
    "arguments",
    [["update", "--plan"], ["update", "-h"], ["update", "--help"]],
)
def test_managed_launcher_allows_only_exact_read_only_update_forms(
    tmp_path: Path, arguments: list[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    layout.release.joinpath("venv", "bin").mkdir(parents=True)
    selected = layout.release / "venv/bin/hermes"
    selected.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8"
    )
    selected.chmod(0o755)
    layout.selector.write_bytes(helper.selector_payload(layout))
    launcher = tmp_path / "hermes"
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)

    completed = subprocess.run(
        [str(launcher), *arguments], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0
    assert completed.stdout.splitlines() == arguments
    assert "Native `hermes update` is disabled" not in completed.stderr


@pytest.mark.parametrize(
    "arguments",
    [["chat", "update"], ["--provider", "auto", "chat", "update"]],
)
def test_managed_launcher_does_not_misclassify_update_as_an_ordinary_argument(
    tmp_path: Path, arguments: list[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    layout.release.joinpath("venv", "bin").mkdir(parents=True)
    selected = layout.release / "venv/bin/hermes"
    selected.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8"
    )
    selected.chmod(0o755)
    layout.selector.write_bytes(helper.selector_payload(layout))
    launcher = tmp_path / "hermes"
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)

    completed = subprocess.run(
        [str(launcher), *arguments], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0
    assert completed.stdout.splitlines() == arguments


def test_baseline_token_binds_the_exact_retained_launcher_bytes(tmp_path: Path) -> None:
    helper = load_helper()
    retained = tmp_path / "hermes-launcher.before-unified-kanban"
    original = b"#!/bin/sh\necho ORIGINAL\n"
    retained.write_bytes(original)

    assert helper.baseline_token(None) == helper.BASELINE_ABSENT
    assert helper.baseline_token(retained) == (
        "sha256:" + hashlib.sha256(original).hexdigest()
    )

    retained.write_bytes(b"#!/bin/sh\necho FOREIGN\n")
    assert helper.baseline_token(retained) != (
        "sha256:" + hashlib.sha256(original).hexdigest()
    )


def test_baseline_token_refuses_a_relinked_retained_launcher(tmp_path: Path) -> None:
    helper = load_helper()
    retained = tmp_path / "hermes-launcher.before-unified-kanban"
    retained.write_bytes(b"#!/bin/sh\necho ORIGINAL\n")
    os.link(retained, tmp_path / "attacker-link")

    with pytest.raises(RuntimeError, match="stable regular file"):
        helper.baseline_token(retained)


def test_managed_launcher_distinguishes_every_retained_baseline(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    other = helper.release_layout(tmp_path / "other-agent", "1" * 40, "2" * 40)
    absent = helper.launcher_payload(layout, helper.BASELINE_ABSENT)
    bound = helper.launcher_payload(layout, "sha256:" + "a" * 64)
    rebound = helper.launcher_payload(layout, "sha256:" + "b" * 64)

    # Uninstall proves provenance by re-rendering and comparing bytes, so every
    # distinguishable install decision must produce a distinguishable launcher.
    assert len({absent, bound, rebound, helper.launcher_payload(other, helper.BASELINE_ABSENT)}) == 4
    assert absent == helper.launcher_payload(layout, helper.BASELINE_ABSENT)
    assert absent.startswith(b"#!/bin/sh\n# unified-kanban-hermes-baseline absent\n")


@pytest.mark.parametrize(
    "baseline",
    ["", "none", "absent\n", "sha256:" + "a" * 63, "sha256:" + "A" * 64, "sha256:xyz"],
)
def test_managed_launcher_refuses_unbound_baselines(tmp_path: Path, baseline: str) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)

    with pytest.raises(ValueError, match="launcher baseline"):
        helper.launcher_payload(layout, baseline)


def test_sync_dependencies_uses_stable_release_path_and_locked_uv(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    release = tmp_path / ("release-" + "2" * 40)
    release.mkdir(mode=0o700)
    (release / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    fake_uv = tmp_path / "uv"
    log = tmp_path / "uv.log"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s|%s\\n' \"$PWD\" \"$UV_PROJECT_ENVIRONMENT\" \"$*\" >> \"$UV_TEST_LOG\"\n"
        "if [ \"$1\" = venv ]; then mkdir -p \"$2/bin\"; printf '#!/bin/sh\\n' > \"$2/bin/python\"; chmod +x \"$2/bin/python\"; fi\n"
        "if [ \"$1\" = sync ]; then printf '#!/bin/sh\\n' > \"$UV_PROJECT_ENVIRONMENT/bin/hermes\"; chmod +x \"$UV_PROJECT_ENVIRONMENT/bin/hermes\"; fi\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    env = os.environ.copy()
    env["UV_TEST_LOG"] = str(log)

    launcher = helper.sync_release_dependencies(
        release,
        uv=fake_uv,
        base_env=env,
        extra_env={"UV_TEST_LOG": str(log)},
    )

    assert launcher == release / "venv" / "bin" / "hermes"
    assert launcher.stat().st_mode & 0o111
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines == [
        f"{release}|{release / 'venv'}|venv {release / 'venv'} --python 3.11",
        f"{release}|{release / 'venv'}|sync --extra all --extra messaging --locked",
    ]



def test_build_release_web_ui_uses_reviewed_lock_and_publishes_dist(tmp_path: Path) -> None:
    helper = load_helper()
    release = tmp_path / ("release-" + "2" * 40)
    (release / "web").mkdir(parents=True, mode=0o700)
    (release / "package.json").write_text("{}\n", encoding="utf-8")
    (release / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (release / "web/package.json").write_text("{}\n", encoding="utf-8")
    fake_npm = tmp_path / "npm"
    log = tmp_path / "npm.log"
    fake_npm.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s\\n' \"$PWD\" \"$*\" >> \"$NPM_TEST_LOG\"\n"
        "if [ \"$1 $2 $3\" = 'run build --workspace' ]; then "
        "mkdir -p node_modules hermes_cli/web_dist; printf '<html></html>\\n' > hermes_cli/web_dist/index.html; fi\n",
        encoding="utf-8",
    )
    fake_npm.chmod(0o700)

    output = helper.build_release_web_ui(
        release,
        npm=fake_npm,
        base_env={"HOME": str(tmp_path)},
        extra_env={"NPM_TEST_LOG": str(log)},
    )

    assert output == release / "hermes_cli/web_dist/index.html"
    assert output.read_text(encoding="utf-8") == "<html></html>\n"
    assert not (release / "node_modules").exists()
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"{release}|ci --workspace web",
        f"{release}|run build --workspace web",
    ]

def test_precompile_bytecode_is_checked_hash_stable_and_runtime_immutable(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    release, source = python_release(tmp_path)

    inventory = helper._precompile_release_bytecode(release)
    pyc = Path(importlib.util.cache_from_source(str(source)))
    data = pyc.read_bytes()
    code = marshal.loads(data[16:])

    assert int.from_bytes(data[4:8], "little") == 3
    assert code.co_filename == str(source)
    assert ".building-" not in code.co_filename
    assert inventory == helper._bytecode_inventory(release)
    before = helper._tree_digest(release)

    completed = subprocess.run(
        [str(release / "venv" / "bin" / "python"), "-c", "import sample_package"],
        cwd=release,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert helper._tree_digest(release) == before
    assert helper._bytecode_inventory(release) == inventory


def test_precompile_bytecode_ignores_manager_pycache_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apple's manager Python must not relocate the release's expected pyc paths."""
    helper = load_helper()
    release, source = python_release(tmp_path)
    external = tmp_path / "manager-pycache"
    monkeypatch.setattr(sys, "pycache_prefix", str(external))

    inventory = helper._precompile_release_bytecode(release)

    local = source.parent / "__pycache__"
    assert inventory["count"] == 2
    assert len(list(local.glob("module.*.pyc"))) == 1
    assert not external.exists()


def test_precompile_bytecode_uses_release_python_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation must not assume the manager and release use one pyc format."""
    helper = load_helper()
    release, _source = python_release(tmp_path)
    monkeypatch.setattr(helper.importlib.util, "MAGIC_NUMBER", b"\x00\x00\x00\x00")

    inventory = helper._precompile_release_bytecode(release)

    assert inventory["count"] == 2


def test_precompile_bytecode_fails_closed_on_invalid_source(tmp_path: Path) -> None:
    helper = load_helper()
    release, source = python_release(tmp_path)
    source.write_text("def broken(:\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="bytecode precompilation"):
        helper._precompile_release_bytecode(release)


def test_prepare_records_hermes_bytecode_fingerprint_before_receipt(tmp_path: Path) -> None:
    """The first full Hermes launch must not sweep the sealed bytecode."""
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(
        tmp_path,
        carried_entries=(("100644", "sample_module.py", "VALUE = 42\n"),),
    )
    layout = helper.release_layout(checkout, upstream, carried)

    helper.prepare_release(
        layout,
        upstream=upstream,
        carried=carried,
        bundle=bundle,
        uv=fake_uv(tmp_path, "uv-fingerprint"),
        source_url=str(source),
        allow_local_source=True,
    )

    stamp = layout.release / ".bytecode-fingerprint"
    stamp_info = os.lstat(stamp)
    assert stamp_info.st_nlink == 1
    assert stat.S_IMODE(stamp_info.st_mode) == 0o600
    assert stamp.read_text(encoding="utf-8") == f"git:refs/heads/main:{carried}"


def test_full_launch_guard_preserves_sealed_bytecode_and_reuse(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    guard = """\
import shutil
from pathlib import Path

root = Path(__file__).resolve().parent
head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
assert head.startswith("ref: ")
ref = head.removeprefix("ref: ")
revision = (root / ".git" / ref).read_text(encoding="utf-8").strip()
expected = f"git:{ref}:{revision}"
stamp = root / ".bytecode-fingerprint"
observed = stamp.read_text(encoding="utf-8") if stamp.is_file() else ""
if observed != expected:
    for cache in root.rglob("__pycache__"):
        if "venv" not in cache.parts:
            shutil.rmtree(cache)
"""
    source, bundle, upstream, carried = make_repositories(
        tmp_path,
        carried_entries=(("100644", "full_launch_guard.py", guard),),
    )
    layout = helper.release_layout(checkout, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": bundle,
        "source_url": str(source),
        "allow_local_source": True,
    }
    release = helper.prepare_release(
        layout, uv=fake_uv(tmp_path, "uv-full-launch"), **build
    )
    receipt = (release / helper._COMPLETION_RECEIPT).read_bytes()
    before = helper._bytecode_inventory(release)
    identity = release.stat().st_ino

    for _ in range(2):
        subprocess.run(
            [str(release / "venv" / "bin" / "python"), str(release / "full_launch_guard.py")],
            cwd=release,
            check=True,
        )
        helper._verify_completed_release(layout, upstream, carried)
        assert helper._bytecode_inventory(release) == before
        assert (release / helper._COMPLETION_RECEIPT).read_bytes() == receipt

    reused = helper.prepare_release(layout, uv=Path("/definitely/unused"), **build)
    assert reused.stat().st_ino == identity


def test_bytecode_fingerprint_fsync_failure_compensates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    layout = helper.release_layout(checkout, upstream, carried)
    original_fsync = helper.os.fsync

    def fail_installed_stamp_fsync(descriptor: int) -> None:
        opened = helper.os.fstat(descriptor)
        if layout.release.exists():
            release = helper.os.lstat(layout.release)
            if (
                (opened.st_dev, opened.st_ino) == (release.st_dev, release.st_ino)
                and (layout.release / helper._BYTECODE_FINGERPRINT).exists()
                and not (layout.release / helper._COMPLETION_RECEIPT).exists()
            ):
                raise OSError("injected bytecode fingerprint fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(helper.os, "fsync", fail_installed_stamp_fsync)
    with pytest.raises(OSError, match="fingerprint fsync"):
        helper.prepare_release(
            layout,
            upstream=upstream,
            carried=carried,
            bundle=bundle,
            uv=fake_uv(tmp_path, "uv-fingerprint-fsync"),
            source_url=str(source),
            allow_local_source=True,
        )

    assert not (layout.release / helper._BYTECODE_FINGERPRINT).exists()
    assert not list(layout.release.glob(f"{helper._BYTECODE_FINGERPRINT}.*"))
    assert not (layout.release / helper._COMPLETION_RECEIPT).exists()


def test_bytecode_fingerprint_missing_during_compensation_preserves_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    layout = helper.release_layout(checkout, upstream, carried)
    helper.build_source_release(
        layout,
        upstream=upstream,
        carried=carried,
        bundle=bundle,
        source_url=str(source),
        allow_local_source=True,
    )
    original_fsync_directory = helper._fsync_directory

    def unlink_then_fail(directory: Path) -> None:
        stamp = directory / helper._BYTECODE_FINGERPRINT
        if stamp.exists():
            stamp.unlink()
            raise OSError("injected original stamp fsync failure")
        original_fsync_directory(directory)

    monkeypatch.setattr(helper, "_fsync_directory", unlink_then_fail)
    with pytest.raises(OSError, match="original stamp fsync"):
        helper._publish_bytecode_fingerprint(layout.release, carried)

    assert not (layout.release / helper._BYTECODE_FINGERPRINT).exists()
    assert not list(layout.release.glob(f"{helper._BYTECODE_FINGERPRINT}.*"))


def test_incomplete_release_rejects_a_tampered_bytecode_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(
        tmp_path,
        carried_entries=(
            ("100644", ".gitignore", ".bytecode-fingerprint\n__pycache__/\n*.pyc\n"),
        ),
    )
    layout = helper.release_layout(checkout, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": bundle,
        "source_url": str(source),
        "allow_local_source": True,
    }

    def crash_before_receipt(*_args, **_kwargs) -> None:
        raise RuntimeError("injected crash before receipt")

    monkeypatch.setattr(helper, "_publish_completion_receipt", crash_before_receipt)
    with pytest.raises(RuntimeError, match="injected crash"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-stamp-crash"), **build)
    stamp = layout.release / helper._BYTECODE_FINGERPRINT
    assert stamp.is_file()
    stamp.write_text("foreign\n", encoding="utf-8")
    identity = layout.release.stat().st_ino

    with pytest.raises(RuntimeError, match="changed after construction"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-must-not-run"), **build)

    assert layout.release.stat().st_ino == identity
    assert stamp.read_text(encoding="utf-8") == "foreign\n"


def test_incomplete_release_with_detached_head_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    layout = helper.release_layout(checkout, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": bundle,
        "source_url": str(source),
        "allow_local_source": True,
    }

    def crash_before_receipt(*_args, **_kwargs) -> None:
        raise RuntimeError("injected crash before receipt")

    monkeypatch.setattr(helper, "_publish_completion_receipt", crash_before_receipt)
    with pytest.raises(RuntimeError, match="injected crash"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-detached-crash"), **build)
    identity = layout.release.stat().st_ino
    subprocess.run(
        ["git", "checkout", "--detach", "-q", carried], cwd=layout.release, check=True
    )

    with pytest.raises(RuntimeError, match="required symbolic HEAD"):
        helper.prepare_release(layout, uv=Path("/definitely/unused"), **build)

    assert layout.release.stat().st_ino == identity


def test_completed_release_rejects_tampered_receipted_bytecode(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(
        tmp_path,
        carried_entries=(("100644", "sample_module.py", "VALUE = 42\n"),),
    )
    layout = helper.release_layout(checkout, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": bundle,
        "source_url": str(source),
        "allow_local_source": True,
    }
    helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-bytecode"), **build)
    pyc = Path(importlib.util.cache_from_source(str(layout.release / "sample_module.py")))
    assert pyc.is_file()
    damaged = bytearray(pyc.read_bytes())
    damaged[-1] ^= 1
    pyc.write_bytes(damaged)

    with pytest.raises(RuntimeError, match="bytecode|completion receipt"):
        helper.prepare_release(layout, uv=tmp_path / "unused-uv", **build)


def test_completed_release_rejects_detached_head(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    layout = helper.release_layout(checkout, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": bundle,
        "source_url": str(source),
        "allow_local_source": True,
    }
    helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-detached-head"), **build)
    subprocess.run(
        ["git", "checkout", "--detach", "-q", carried], cwd=layout.release, check=True
    )

    with pytest.raises(RuntimeError, match="symbolic HEAD|completion receipt"):
        helper.prepare_release(layout, uv=Path("/definitely/unused"), **build)


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_completed_release_rejects_tampered_fingerprint(
    tmp_path: Path, mutation: str
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    layout = helper.release_layout(checkout, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": bundle,
        "source_url": str(source),
        "allow_local_source": True,
    }
    helper.prepare_release(layout, uv=fake_uv(tmp_path, f"uv-stamp-{mutation}"), **build)
    stamp = layout.release / helper._BYTECODE_FINGERPRINT
    excluded = {".git", helper._COMPLETION_RECEIPT}
    before = helper._tree_digest(layout.release, excluded_top_level=excluded)
    if mutation == "edit":
        stamp.write_text("foreign", encoding="utf-8")
    else:
        stamp.unlink()
    after = helper._tree_digest(layout.release, excluded_top_level=excluded)
    assert after != before

    with pytest.raises(RuntimeError, match="fingerprint|completion receipt"):
        helper.prepare_release(layout, uv=Path("/definitely/unused"), **build)


def test_prepare_rebuilds_after_bytecode_compile_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(
        tmp_path,
        carried_entries=(("100644", "sample_module.py", "VALUE = 42\n"),),
    )
    layout = helper.release_layout(checkout, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": bundle,
        "source_url": str(source),
        "allow_local_source": True,
    }
    real_precompile = helper._precompile_release_bytecode

    def compile_then_crash(release: Path):
        real_precompile(release)
        raise RuntimeError("injected crash after bytecode compile")

    monkeypatch.setattr(helper, "_precompile_release_bytecode", compile_then_crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-crash"), **build)
    assert not (layout.release / helper._COMPLETION_RECEIPT).exists()
    assert list(layout.release.rglob("*.pyc"))

    monkeypatch.setattr(helper, "_precompile_release_bytecode", real_precompile)
    first_identity = layout.release.stat().st_ino
    assert helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-rebuild"), **build) == layout.release
    assert layout.release.stat().st_ino != first_identity
    assert helper.prepare_release(layout, uv=tmp_path / "unused-uv", **build) == layout.release


def test_prepare_rejects_dirty_existing_release_with_attacker_launcher(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=source, check=True)
    (source / "payload.txt").write_text("upstream\n", encoding="utf-8")
    upstream = commit(source, "upstream")
    (source / "payload.txt").write_text("carried\n", encoding="utf-8")
    carried = commit(source, "carried")
    layout = helper.release_layout(checkout, upstream, carried)
    layout.root.mkdir(mode=0o700)
    subprocess.run(["git", "clone", "-q", str(source), str(layout.release)], check=True)
    launcher = layout.release / "venv" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\necho ATTACKER\n", encoding="utf-8")
    launcher.chmod(0o755)
    (layout.release / "payload.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="completion receipt|not clean"):
        helper.prepare_release(
            layout,
            upstream=upstream,
            carried=carried,
            bundle=tmp_path / "missing.bundle",
        )


def test_prepare_reuses_only_receipted_exact_release(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=source, check=True)
    (source / "payload.txt").write_text("upstream\n", encoding="utf-8")
    upstream = commit(source, "upstream")
    (source / "payload.txt").write_text("carried\n", encoding="utf-8")
    carried = commit(source, "carried")
    layout = helper.release_layout(checkout, upstream, carried)
    layout.root.mkdir(mode=0o700)
    subprocess.run(["git", "clone", "-q", str(source), str(layout.release)], check=True)
    install_receiptable_launchers(layout.release)
    helper._publish_bytecode_fingerprint(layout.release, carried)
    helper._publish_completion_receipt(layout, upstream, carried)

    assert helper.prepare_release(
        layout,
        upstream=upstream,
        carried=carried,
        bundle=tmp_path / "missing.bundle",
    ) == layout.release


def test_prepare_rebuilds_after_an_interrupted_dependency_sync(tmp_path: Path) -> None:
    """A receipt-less release must never brick every later run.

    The first run publishes the stable release path and then loses its
    dependency sync, so nothing can complete that release afterwards. The next
    run has to retire it and rebuild instead of refusing forever.
    """
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    layout = helper.release_layout(checkout, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": bundle,
        "source_url": str(source),
        "allow_local_source": True,
    }

    with pytest.raises(RuntimeError, match="uv"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-fails", fails=True), **build)

    assert layout.release.is_dir()
    assert not (layout.release / helper._COMPLETION_RECEIPT).exists()
    incomplete = layout.release.stat().st_ino

    prepared = helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-works"), **build)

    assert prepared == layout.release
    assert (layout.release / helper._COMPLETION_RECEIPT).is_file()
    assert (layout.release / "venv" / "bin" / "hermes").is_file()
    assert layout.release.stat().st_ino != incomplete
    assert temporary_release_names(layout) == []
    # The completed release is now reusable without touching uv at all.
    assert helper.prepare_release(
        layout, uv=fake_uv(tmp_path, "uv-unused", fails=True), **build
    ) == layout.release


def test_prepare_refuses_to_retire_the_selected_release(tmp_path: Path) -> None:
    helper = load_helper()
    layout, build = build_incomplete_release(helper, tmp_path)
    layout.selector.write_text(f"{layout.release}\n", encoding="utf-8")
    identity = layout.release.stat().st_ino

    with pytest.raises(RuntimeError, match="selector still names"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-works"), **build)

    assert layout.release.stat().st_ino == identity
    assert temporary_release_names(layout) == []


def test_prepare_refuses_to_retire_a_release_modified_after_construction(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    layout, build = build_incomplete_release(helper, tmp_path)
    (layout.release / "payload.txt").write_text("attacker\n", encoding="utf-8")
    identity = layout.release.stat().st_ino

    with pytest.raises(RuntimeError, match="completion receipt"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-works"), **build)

    assert layout.release.stat().st_ino == identity
    assert (layout.release / "payload.txt").read_text(encoding="utf-8") == "attacker\n"
    assert temporary_release_names(layout) == []


def test_incomplete_release_retirement_preserves_a_foreign_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    layout, build = build_incomplete_release(helper, tmp_path)
    foreign = tmp_path / "foreign-release"
    foreign.mkdir()
    (foreign / "foreign-marker").write_text("not ours\n", encoding="utf-8")
    real_rename = helper._rename_exclusive

    def substitute_then_rename(source: Path, destination: Path) -> None:
        if Path(source) == layout.release and Path(destination).name.startswith(".retired-"):
            Path(layout.release).rename(tmp_path / "displaced")
            foreign.rename(layout.release)
        real_rename(source, destination)

    monkeypatch.setattr(helper, "_rename_exclusive", substitute_then_rename)
    with pytest.raises(RuntimeError, match="identity changed during retirement"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-works"), **build)

    assert (layout.release / "foreign-marker").read_text(encoding="utf-8") == "not ours\n"
    assert temporary_release_names(layout) == []


def test_incomplete_release_retirement_fsync_failure_restores_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    layout, build = build_incomplete_release(helper, tmp_path)
    identity = layout.release.stat().st_ino
    original_fsync = helper.os.fsync

    def fail_retirement_fsync(descriptor: int) -> None:
        observed = helper.os.fstat(descriptor)
        root = helper.os.lstat(layout.root)
        if (observed.st_dev, observed.st_ino) == (
            root.st_dev,
            root.st_ino,
        ) and not layout.release.exists():
            raise OSError("injected retirement fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(helper.os, "fsync", fail_retirement_fsync)
    with pytest.raises(OSError, match="retirement fsync"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-works"), **build)

    assert layout.release.is_dir()
    assert layout.release.stat().st_ino == identity
    assert temporary_release_names(layout) == []


def test_private_candidate_writer_handles_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    candidate = tmp_path / "candidate"
    original_write = helper.os.write

    def short_write(descriptor: int, data: bytes | memoryview) -> int:
        return original_write(descriptor, data[:3])

    monkeypatch.setattr(helper.os, "write", short_write)
    helper._write_private_candidate(candidate, b"0123456789", 0o600)

    assert candidate.read_bytes() == b"0123456789"


def test_build_rejects_bundle_when_final_ref_is_not_requested_carried(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=source, check=True)
    (source / "payload.txt").write_text("upstream\n", encoding="utf-8")
    upstream = commit(source, "upstream")
    (source / "payload.txt").write_text("carried\n", encoding="utf-8")
    carried = commit(source, "carried")
    (source / "payload.txt").write_text("foreign-final\n", encoding="utf-8")
    foreign_final = commit(source, "foreign final")
    subprocess.run(["git", "branch", "carried-01", carried], cwd=source, check=True)
    subprocess.run(["git", "branch", "carried-02", foreign_final], cwd=source, check=True)
    subprocess.run(["git", "checkout", "-q", "--detach", upstream], cwd=source, check=True)
    subprocess.run(["git", "branch", "-f", "main", upstream], cwd=source, check=True)
    bundle = tmp_path / "carried.bundle"
    subprocess.run(
        ["git", "bundle", "create", str(bundle), "carried-01", "carried-02", f"^{upstream}"],
        cwd=source,
        check=True,
    )
    layout = helper.release_layout(checkout, upstream, carried)

    with pytest.raises(RuntimeError, match="bundle refs|final carried"):
        helper.build_source_release(
            layout,
            upstream=upstream,
            carried=carried,
            bundle=bundle,
            source_url=str(source),
            allow_local_source=True,
        )


def test_release_publication_fsync_failure_compensates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    layout = helper.release_layout(checkout, upstream, carried)
    original_fsync = helper.os.fsync

    def fail_published_root_fsync(descriptor: int) -> None:
        observed = helper.os.fstat(descriptor)
        if layout.root.exists():
            root = helper.os.lstat(layout.root)
            if (
                (observed.st_dev, observed.st_ino) == (root.st_dev, root.st_ino)
                and layout.release.exists()
            ):
                raise OSError("injected release publication fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(helper.os, "fsync", fail_published_root_fsync)
    with pytest.raises(OSError, match="publication fsync"):
        helper.build_source_release(
            layout,
            upstream=upstream,
            carried=carried,
            bundle=bundle,
            source_url=str(source),
            allow_local_source=True,
        )

    assert not layout.release.exists()


def test_sync_rejects_relative_uv_and_scrubs_ambient_configuration(tmp_path: Path) -> None:
    helper = load_helper()
    release = tmp_path / ("release-" + "a" * 40)
    release.mkdir()
    (release / "uv.lock").write_text("lock\n", encoding="utf-8")

    with pytest.raises(ValueError, match="absolute uv"):
        helper.sync_release_dependencies(release, uv="uv")

    fake_uv = tmp_path / "uv"
    env_log = tmp_path / "env.log"
    config_log = tmp_path / "config-dir.log"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "env | LC_ALL=C sort > \"$SAFE_ENV_LOG\"\n"
        "ls -A \"$XDG_CONFIG_HOME\" > \"$SAFE_CONFIG_LOG\" 2>&1\n"
        "if [ \"$1\" = venv ]; then mkdir -p \"$2/bin\"; printf '#!/bin/sh\\n' > \"$2/bin/python\"; chmod +x \"$2/bin/python\"; fi\n"
        "if [ \"$1\" = sync ]; then printf '#!/bin/sh\\n' > \"$UV_PROJECT_ENVIRONMENT/bin/hermes\"; chmod +x \"$UV_PROJECT_ENVIRONMENT/bin/hermes\"; fi\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    helper.sync_release_dependencies(
        release,
        uv=fake_uv,
        base_env={
            "PATH": "/attacker",
            "HOME": str(tmp_path),
            "UV_CONFIG_FILE": "/attacker/uv.toml",
            "UV_INDEX_URL": "https://attacker.invalid",
            "PIP_REQUIRE_VIRTUALENV": "1",
            "HTTPS_PROXY": "http://attacker.invalid",
            "DYLD_INSERT_LIBRARIES": "/attacker.dylib",
            "XDG_CONFIG_HOME": "/attacker/config",
        },
        extra_env={"SAFE_ENV_LOG": str(env_log), "SAFE_CONFIG_LOG": str(config_log)},
    )
    observed = env_log.read_text(encoding="utf-8")
    for forbidden in (
        "UV_CONFIG_FILE=",
        "UV_INDEX_URL=",
        "PIP_REQUIRE_VIRTUALENV=",
        "HTTPS_PROXY=",
        "DYLD_INSERT_LIBRARIES=",
        "XDG_CONFIG_HOME=/attacker/config",
    ):
        assert forbidden not in observed

    # `UV_NO_CONFIG` must not come back: it also discards the reviewed upstream's
    # own `[tool.uv]` settings, and Hermes locks under a `[tool.uv] exclude-newer`
    # span, so a no-config sync re-resolves and `--locked` refuses. User
    # configuration is neutralised by an empty private directory instead.
    assert "UV_NO_CONFIG=" not in observed
    isolated = next(
        line.split("=", 1)[1]
        for line in observed.splitlines()
        if line.startswith("XDG_CONFIG_HOME=")
    )
    assert Path(isolated).is_absolute()
    assert not isolated.startswith(str(release))
    assert config_log.read_text(encoding="utf-8") == ""


def test_completed_release_rejects_mutated_git_config(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    layout = helper.release_layout(checkout, upstream, carried)
    helper.build_source_release(
        layout,
        upstream=upstream,
        carried=carried,
        bundle=bundle,
        source_url=str(source),
        allow_local_source=True,
    )
    install_receiptable_launchers(layout.release)
    helper._publish_bytecode_fingerprint(layout.release, carried)
    helper._publish_completion_receipt(layout, upstream, carried)
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", str(tmp_path / "attacker-hooks")],
        cwd=layout.release,
        check=True,
    )

    with pytest.raises(RuntimeError, match="Git config"):
        helper.prepare_release(
            layout,
            upstream=upstream,
            carried=carried,
            bundle=bundle,
        )


def test_completion_receipt_fsync_failure_compensates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path)
    layout = helper.release_layout(checkout, upstream, carried)
    helper.build_source_release(
        layout,
        upstream=upstream,
        carried=carried,
        bundle=bundle,
        source_url=str(source),
        allow_local_source=True,
    )
    install_receiptable_launchers(layout.release)
    receipt = layout.release / helper._COMPLETION_RECEIPT
    original_fsync = helper.os.fsync

    def fail_receipt_publication_fsync(descriptor: int) -> None:
        observed = helper.os.fstat(descriptor)
        release_info = helper.os.lstat(layout.release)
        if (
            (observed.st_dev, observed.st_ino)
            == (release_info.st_dev, release_info.st_ino)
            and receipt.exists()
        ):
            raise OSError("injected completion receipt fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(helper.os, "fsync", fail_receipt_publication_fsync)
    with pytest.raises(OSError, match="receipt fsync"):
        helper._publish_bytecode_fingerprint(layout.release, carried)
        helper._publish_completion_receipt(layout, upstream, carried)

    assert not receipt.exists()


def prepare_synthetic_release(
    helper, tmp_path: Path, *, carried_entries: tuple[tuple[str, str, str], ...] = ()
):
    """Prepare one completed release, faking only the dependency installer."""
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(
        tmp_path, carried_entries=carried_entries
    )
    layout = helper.release_layout(checkout, upstream, carried)
    build = {
        "upstream": upstream,
        "carried": carried,
        "bundle": bundle,
        "source_url": str(source),
        "allow_local_source": True,
    }
    release = helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-works"), **build)
    return layout, build, release


def collision_record(layout, helper) -> dict:
    receipt = json.loads((layout.release / helper._COMPLETION_RECEIPT).read_bytes())
    assert len(receipt["case_collisions"]) == 1
    return receipt["case_collisions"][0]


def test_build_normalizes_the_reviewed_metadata_case_collision(tmp_path: Path) -> None:
    """The one reviewed upstream collision must build, not fail the whole install.

    Upstream carries two contributor-email metadata files whose names differ only
    by case. A supported case-insensitive volume can hold exactly one of them, so
    the release has to name the survivor deterministically and prove its bytes
    rather than either crashing or waving the resulting dirt through.
    """
    helper = load_helper()

    layout, _, release = prepare_synthetic_release(
        helper, tmp_path, carried_entries=REVIEWED_COLLISION
    )

    materialized = release / REVIEWED_REPRESENTATIVE
    assert materialized.read_text(encoding="utf-8") == "lower-side\n"
    directory = release / METADATA_NAMESPACE.rstrip("/")
    assert sorted(entry.name for entry in directory.iterdir()) == [
        "agent@example-Host.local"
    ]
    record = collision_record(layout, helper)
    assert record["representative"] == REVIEWED_REPRESENTATIVE
    assert [member[0] for member in record["members"]] == [
        REVIEWED_COLLISION[0][1],
        REVIEWED_COLLISION[1][1],
    ]
    assert record["materialized"] == [
        [
            REVIEWED_REPRESENTATIVE,
            hashlib.sha256(b"lower-side\n").hexdigest(),
        ]
    ]
    assert record["key"] == f"{METADATA_NAMESPACE}agent@example-host.local"


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param(
            (
                ("100644", "hermes/Runtime.py", "runtime = 'upper'\n"),
                ("100644", "hermes/runtime.py", "runtime = 'lower'\n"),
            ),
            id="runtime-module",
        ),
        pytest.param(
            (
                ("100644", "Uv.lock", "version = 2\n"),
                ("100644", "uV.lock", "version = 3\n"),
            ),
            id="dependency-lock",
        ),
        pytest.param(
            (
                ("100644", "Config/settings.toml", "a = 1\n"),
                ("100644", "config/settings.toml", "a = 2\n"),
            ),
            id="config-directory",
        ),
        pytest.param(
            (
                ("100755", f"{METADATA_NAMESPACE}agent@Example-Host.local", "#!/bin/sh\n"),
                ("100644", f"{METADATA_NAMESPACE}agent@example-Host.local", "login\n"),
            ),
            id="executable-member",
        ),
        pytest.param(
            (
                ("120000", f"{METADATA_NAMESPACE}agent@Example-Host.local", "../../uv.lock"),
                ("100644", f"{METADATA_NAMESPACE}agent@example-Host.local", "login\n"),
            ),
            id="symlink-member",
        ),
        pytest.param(
            (
                ("100644", f"{METADATA_NAMESPACE}Team", "login\n"),
                ("100644", f"{METADATA_NAMESPACE}team/lead", "login\n"),
            ),
            id="directory-member",
        ),
        pytest.param(
            (
                (
                    "100644",
                    unicodedata.normalize("NFC", "hermes/café.py"),
                    "cafe = 'nfc'\n",
                ),
                (
                    "100644",
                    unicodedata.normalize("NFD", "hermes/café.py"),
                    "cafe = 'nfd'\n",
                ),
            ),
            id="unicode-normalization-runtime",
        ),
    ],
)
def test_build_refuses_a_case_collision_the_policy_does_not_allow(
    tmp_path: Path, entries: tuple[tuple[str, str, str], ...]
) -> None:
    """Only the reviewed non-runtime metadata namespace may ever alias on disk."""
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    source, bundle, upstream, carried = make_repositories(tmp_path, carried_entries=entries)
    layout = helper.release_layout(checkout, upstream, carried)

    with pytest.raises(RuntimeError, match="case-fold collision"):
        helper.build_source_release(
            layout,
            upstream=upstream,
            carried=carried,
            bundle=bundle,
            source_url=str(source),
            allow_local_source=True,
        )

    assert not layout.release.exists()
    assert temporary_release_names(layout) == []


def test_build_normalizes_a_unicode_normalization_collision(tmp_path: Path) -> None:
    """A supported volume folds NFD onto NFC, so the tree must be checked folded."""
    helper = load_helper()
    composed = unicodedata.normalize("NFC", f"{METADATA_NAMESPACE}café@example.invalid")
    decomposed = unicodedata.normalize("NFD", f"{METADATA_NAMESPACE}café@example.invalid")
    assert composed != decomposed

    layout, _, release = prepare_synthetic_release(
        helper,
        tmp_path,
        carried_entries=(
            ("100644", composed, "composed\n"),
            ("100644", decomposed, "decomposed\n"),
        ),
    )

    record = collision_record(layout, helper)
    assert sorted(member[0] for member in record["members"]) == sorted([composed, decomposed])
    assert len(record["materialized"]) == 1
    survivor = record["materialized"][0][0]
    assert (release / survivor).read_bytes() in (b"composed\n", b"decomposed\n")
    directory = release / METADATA_NAMESPACE.rstrip("/")
    assert len(list(directory.iterdir())) == 1


def test_completed_release_rejects_tampering_with_the_normalized_collision(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    layout, build, release = prepare_synthetic_release(
        helper, tmp_path, carried_entries=REVIEWED_COLLISION
    )

    (release / REVIEWED_REPRESENTATIVE).write_text("attacker\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="case-fold collision"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-unused", fails=True), **build)


def test_completed_release_rejects_renaming_the_normalized_collision(
    tmp_path: Path,
) -> None:
    """Swapping the survivor for the other spelling must not verify."""
    helper = load_helper()
    layout, build, release = prepare_synthetic_release(
        helper, tmp_path, carried_entries=REVIEWED_COLLISION
    )

    (release / REVIEWED_REPRESENTATIVE).rename(release / REVIEWED_COLLISION[0][1])
    assert [
        entry.name for entry in (release / METADATA_NAMESPACE.rstrip("/")).iterdir()
    ] == ["agent@Example-Host.local"]

    with pytest.raises(RuntimeError, match="case-fold collision"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-unused", fails=True), **build)


def test_normalized_release_is_reused_without_rebuilding(tmp_path: Path) -> None:
    helper = load_helper()
    layout, build, release = prepare_synthetic_release(
        helper, tmp_path, carried_entries=REVIEWED_COLLISION
    )
    identity = release.stat().st_ino

    assert helper.prepare_release(
        layout, uv=fake_uv(tmp_path, "uv-unused", fails=True), **build
    ) == layout.release
    assert layout.release.stat().st_ino == identity
    assert temporary_release_names(layout) == []


def test_incomplete_release_with_a_normalized_collision_is_still_retired(
    tmp_path: Path,
) -> None:
    """Crash recovery must not read the reviewed collision as attacker damage."""
    helper = load_helper()
    layout, build = build_incomplete_release(
        helper, tmp_path, carried_entries=REVIEWED_COLLISION
    )
    incomplete = layout.release.stat().st_ino

    prepared = helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-works"), **build)

    assert prepared == layout.release
    assert layout.release.stat().st_ino != incomplete
    assert (layout.release / helper._COMPLETION_RECEIPT).is_file()
    assert temporary_release_names(layout) == []


def test_incomplete_release_retirement_still_refuses_foreign_dirt(tmp_path: Path) -> None:
    helper = load_helper()
    layout, build = build_incomplete_release(
        helper, tmp_path, carried_entries=REVIEWED_COLLISION
    )
    (layout.release / "payload.txt").write_text("attacker\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="completion receipt"):
        helper.prepare_release(layout, uv=fake_uv(tmp_path, "uv-works"), **build)


def test_case_sensitive_volume_materializes_every_collision_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A volume that keeps both members apart must keep both, and stay clean.

    The supported macOS volume folds every Unicode spelling this policy folds, so
    the only way to drive the non-aliasing branch on real files here is to widen
    the policy's equivalence for one synthetic pair. That is exactly the state a
    case-sensitive volume presents: two distinct on-disk files for one group.
    """
    helper = load_helper()
    real_key = helper._collision_key

    def merged_key(path: str) -> str:
        return real_key(path.replace("twin-a@", "twin@").replace("twin-b@", "twin@"))

    monkeypatch.setattr(helper, "_collision_key", merged_key)
    entries = (
        ("100644", f"{METADATA_NAMESPACE}twin-a@example.invalid", "first\n"),
        ("100644", f"{METADATA_NAMESPACE}twin-b@example.invalid", "second\n"),
    )

    layout, _, release = prepare_synthetic_release(helper, tmp_path, carried_entries=entries)

    record = collision_record(layout, helper)
    assert record["materialized"] == [
        [entries[0][1], hashlib.sha256(b"first\n").hexdigest()],
        [entries[1][1], hashlib.sha256(b"second\n").hexdigest()],
    ]
    assert (release / entries[0][1]).read_text(encoding="utf-8") == "first\n"
    assert (release / entries[1][1]).read_text(encoding="utf-8") == "second\n"
    tracked = [
        record for record in helper._porcelain_status(release) if not record.startswith("?? ")
    ]
    assert tracked == []


def test_collision_free_release_records_no_collisions(tmp_path: Path) -> None:
    helper = load_helper()

    layout, _, _ = prepare_synthetic_release(helper, tmp_path)

    receipt = json.loads((layout.release / helper._COMPLETION_RECEIPT).read_bytes())
    assert receipt["case_collisions"] == []
