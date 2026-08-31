"""불변 Hermes 소스 릴리스 생성 테스트."""

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
# 바이트 값이 가장 큰 리터럴 경로가 결정적 대표이므로, 정규화된 릴리스가 디스크에
# 남겨야 하는 것은 소문자 "e" 표기다.
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
    """영수증 테스트를 위해 실제 Python과 무해한 Hermes 실행 파일을 설치한다."""
    bin_dir = release / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    hermes = bin_dir / "hermes"
    hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hermes.chmod(0o755)


def build_incomplete_release(
    helper, tmp_path: Path, *, carried_entries: tuple[tuple[str, str, str], ...] = ()
) -> tuple[object, dict[str, object]]:
    """중단된 의존성 동기화가 남기는 그대로 릴리스를 게시한다."""
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
    """합성 공식 소스와 검토된 반입 번들을 함께 빌드한다.

    반입된 커밋은 작업 트리가 아닌 비공개 인덱스에서 조립된다. 따라서 픽스처
    자체가 검증하려는 충돌에 걸리지 않으면서, 대소문자를 구분하지 않는 볼륨에서는
    절대 나란히 구체화할 수 없는 엔트리들을 테스트가 배치할 수 있다.
    """
    source = tmp_path / "synthetic-official"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=source, check=True)
    # macOS Git은 전달받은 모든 경로를 미리 조합하므로, 분해된 트리 엔트리는 해당
    # 재작성을 비활성화해야만 스테이징할 수 있다. Linux에서 작성된 업스트림 트리는
    # 분해된 경로를 그대로 담으며, 이 테스트는 그 상태를 재현한다.
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
    """저장된 정책은 파일 전송을 거부하지만 번들 가져오기는 거부하면 안 된다.

    모든 실제 설치와 업데이트는 ``allow_local_source=False``로 빌드하며 이 파일의
    다른 테스트는 모두 ``True``를 전달한다. 따라서 저장된
    ``protocol.file.allow=never``는 안전해 보였지만 실제 릴리스 생성을 불가능하게
    했다. Git은 번들 경로를 파일 전송으로 처리하므로 업스트림 가져오기가 이미
    성공한 뒤 반입 과정이 ``transport 'file' not allowed`` 오류로 중단됐다.
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

    # 이 허용 설정은 필수다. 없으면 바로 이 가져오기가 거부된다.
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


def test_release_root_creation_fsyncs_containing_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    root = helper.release_root(checkout)
    synced: list[Path] = []
    monkeypatch.setattr(helper, "_fsync_directory", synced.append)

    helper._ensure_private_root(root)
    helper._ensure_private_root(root)

    assert synced == [root.parent, root.parent]


def test_release_root_creation_propagates_parent_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    root = helper.release_root(checkout)

    def fail_fsync(_path: Path) -> None:
        raise OSError("injected release-root parent fsync failure")

    monkeypatch.setattr(helper, "_fsync_directory", fail_fsync)
    with pytest.raises(OSError, match="injected release-root parent fsync failure"):
        helper._ensure_private_root(root)
    assert root.is_dir()


def test_existing_unsafe_release_root_is_rejected_before_parent_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    root = helper.release_root(checkout)
    root.mkdir(mode=0o700)
    root.chmod(0o777)
    synced: list[Path] = []
    monkeypatch.setattr(helper, "_fsync_directory", synced.append)

    with pytest.raises(RuntimeError, match="writable ancestor|mode 0700"):
        helper._ensure_private_root(root)

    assert synced == []


def test_release_root_creation_rejects_writable_ancestor(tmp_path: Path) -> None:
    helper = load_helper()
    unsafe_parent = tmp_path / "unsafe-construction-parent"
    unsafe_parent.mkdir(mode=0o700)
    checkout = unsafe_parent / "hermes-agent"
    checkout.mkdir()
    root = helper.release_root(checkout)
    unsafe_parent.chmod(0o777)

    with pytest.raises(RuntimeError, match="untrusted writable ancestor"):
        helper._ensure_private_root(root)


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


def test_gc_apply_preserves_lock_failure_when_record_cleanup_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    checks = 0

    def fail_lock_before_retirement() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError("primary lock verification failure")

    def fail_record_cleanup(*args, **kwargs) -> None:
        raise OSError("secondary record cleanup failure")

    monkeypatch.setattr(helper, "_remove_gc_retirement_record", fail_record_cleanup)

    with pytest.raises(RuntimeError, match="primary lock verification failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=fail_lock_before_retirement,
        )

    assert any("secondary record cleanup failure" in note for note in raised.value.__notes__)
    assert stale.release.is_dir()
    assert list(stale.root.glob(".gc-retired-*.record"))
    assert not any(
        path.is_dir() and path.name.startswith(".gc-retired-")
        for path in stale.root.iterdir()
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


def test_gc_cli_emits_partial_results_before_a_later_candidate_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    releases = sorted(
        (
            build_completed_release(helper, checkout, tmp_path / "partial-first"),
            build_completed_release(helper, checkout, tmp_path / "partial-second"),
        ),
        key=lambda layout: str(layout.release),
    )
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(helper.sys, "platform", "darwin")
    reference_calls = 0

    def fail_for_second_candidate(root: Path, plist: Path) -> set[Path]:
        nonlocal reference_calls
        reference_calls += 1
        if reference_calls == 3:
            raise RuntimeError("injected later reference failure")
        return set()

    monkeypatch.setattr(helper, "collect_runtime_references", fail_for_second_candidate)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout), "--apply"],
    )

    with pytest.raises(RuntimeError, match="injected later reference failure"):
        helper.main()

    records = list(releases[1].root.glob(".*.record"))
    assert len(records) == 1
    assert json.loads(capsys.readouterr().out) == {
        "deleted": [str(releases[0].release)],
        "error": {
            "message": "injected later reference failure",
            "type": "RuntimeError",
        },
        "retained": [
            {"path": str(records[0]), "reason": "record-retained-after-compensation"},
            {"path": str(releases[1].release), "reason": "restored-after-failure"},
        ],
        "status": "partial",
    }
    assert not releases[0].release.exists()
    assert releases[1].release.is_dir()
    assert not (hermes_home / "state/hermes-kanban-update.lock").exists()


def test_gc_restoration_probe_failure_reports_indeterminate_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "restore-probe-work")
    calls = 0
    restoration_failed = False
    primary = RuntimeError("primary post-retirement failure")
    restore_failure = OSError("injected restoration failure")
    probe_failure = OSError("injected restoration outcome probe failure")
    original_stat = helper.os.stat

    def fail_after_retirement() -> set[Path]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise primary
        return set()

    def fail_restoration(capability, retirement_name, release_name, identity):
        nonlocal restoration_failed
        restoration_failed = True
        raise restore_failure

    def fail_retirement_outcome_probe(path, *args, **kwargs):
        if (
            restoration_failed
            and isinstance(path, str)
            and path.startswith(".gc-retired-")
            and not path.endswith(".record")
            and kwargs.get("dir_fd") is not None
        ):
            raise probe_failure
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(helper, "_restore_retired_release", fail_restoration)
    monkeypatch.setattr(helper.os, "stat", fail_retirement_outcome_probe)

    with pytest.raises(RuntimeError, match="primary post-retirement failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=fail_after_retirement,
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    retirements = list(stale.root.glob(".gc-retired-*"))
    retirement = next(path for path in retirements if not path.name.endswith(".record"))
    record = next(path for path in retirements if path.name.endswith(".record"))
    assert partial["retained"] == [
        {
            "path": str(retirement),
            "reason": (
                "indeterminate-restoration-state: "
                "injected restoration outcome probe failure"
            ),
        },
        {"path": str(record), "reason": "record-retained-after-compensation"},
    ]
    assert retirement.is_dir()
    assert record.is_file()
    assert any(
        "restoration outcome probe also failed" in note
        for note in raised.value.__notes__
    )


def test_gc_restoration_failure_does_not_report_disappeared_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    calls = 0
    primary = RuntimeError("primary post-retirement failure")
    original_restore = helper._restore_retired_release

    def fail_after_retirement() -> set[Path]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise primary
        return set()

    def disappear_during_restoration(
        capability, retirement_name, release_name, identity
    ):
        helper.shutil.rmtree(stale.root / retirement_name)
        return original_restore(
            capability, retirement_name, release_name, identity
        )

    monkeypatch.setattr(
        helper, "_restore_retired_release", disappear_during_restoration
    )
    with pytest.raises(RuntimeError, match="primary post-retirement failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=fail_after_retirement,
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    assert len(partial["retained"]) == 1
    retained = partial["retained"][0]
    assert retained["path"].endswith(".record")
    assert Path(retained["path"]).is_file()
    assert retained["reason"] == "record-retained-after-compensation"
    assert not stale.release.exists()
    assert not any(
        path.is_dir() and path.name.startswith(".gc-retired-")
        for path in stale.root.iterdir()
    )
    assert any(
        "GC restoration also failed" in note for note in raised.value.__notes__
    )


def test_gc_cli_reports_retirement_when_safe_restoration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    releases = sorted(
        (
            build_completed_release(helper, checkout, tmp_path / "restore-first"),
            build_completed_release(helper, checkout, tmp_path / "restore-second"),
        ),
        key=lambda layout: str(layout.release),
    )
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(helper.sys, "platform", "darwin")
    reference_calls = 0

    def install_successor_then_fail(root: Path, plist: Path) -> set[Path]:
        nonlocal reference_calls
        reference_calls += 1
        if reference_calls == 3:
            releases[1].release.mkdir(mode=0o700)
            raise RuntimeError("injected later reference failure")
        return set()

    monkeypatch.setattr(helper, "collect_runtime_references", install_successor_then_fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout), "--apply"],
    )

    with pytest.raises(RuntimeError, match="injected later reference failure") as raised:
        helper.main()

    assert any(
        "GC restoration also failed" in note
        and "foreign canonical successor" in note
        for note in raised.value.__notes__
    )
    output = json.loads(capsys.readouterr().out)
    retirements = [
        path
        for path in releases[1].root.glob(".gc-retired-*")
        if path.is_dir()
    ]
    assert len(retirements) == 1
    assert output["deleted"] == [str(releases[0].release)]
    records = list(releases[1].root.glob(".*.record"))
    assert len(records) == 1
    assert output["retained"] == [
        {
            "path": str(retirements[0]),
            "reason": (
                "restoration-failed: foreign canonical successor prevents GC "
                f"restoration; retained {retirements[0]}"
            ),
        },
        {"path": str(records[0]), "reason": "record-retained-after-compensation"},
        {
            "path": str(releases[1].release),
            "reason": "foreign-canonical-successor",
        },
    ]
    assert output["status"] == "partial"
    assert output["error"] == {
        "message": "injected later reference failure",
        "type": "RuntimeError",
    }


def test_gc_cli_preserves_original_failure_when_lock_release_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(helper.sys, "platform", "darwin")
    original_failure = RuntimeError("original GC failure")
    setattr(
        original_failure,
        "gc_partial_result",
        {"deleted": [str(tmp_path / "deleted-release")], "retained": []},
    )

    def fail_gc(*args, **kwargs):
        raise original_failure

    original_run = helper.subprocess.run

    def fail_lock_release(command, **kwargs):
        if "lock-release" in command:
            raise subprocess.CalledProcessError(71, command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(helper, "apply_release_gc", fail_gc)
    monkeypatch.setattr(helper.subprocess, "run", fail_lock_release)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout), "--apply"],
    )

    with pytest.raises(RuntimeError, match="original GC failure") as raised:
        helper.main()

    assert raised.value is original_failure
    traceback_names = []
    observed_traceback = raised.value.__traceback__
    while observed_traceback is not None:
        traceback_names.append(observed_traceback.tb_frame.f_code.co_name)
        observed_traceback = observed_traceback.tb_next
    assert traceback_names.count("main") == 1
    assert traceback_names[-1] == "fail_gc"
    assert json.loads(capsys.readouterr().out) == {
        "deleted": [str(tmp_path / "deleted-release")],
        "error": {"message": "original GC failure", "type": "RuntimeError"},
        "retained": [],
        "status": "partial",
    }
    assert (hermes_home / "state/hermes-kanban-update.lock").is_symlink()


def test_gc_apply_retains_a_partially_deleted_retirement(
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

    assert resumed["deleted"] == []
    assert resumed["retained"] == [
        {
            "path": str(retired[0]),
            "reason": "unverified-partial-retirement: existing release completion receipt does not match content",
        },
        {
            "path": str(records[0]),
            "reason": "record-retained-with-unverified-partial-retirement",
        },
    ]
    assert retired[0].is_dir()
    assert records[0].is_file()
    assert not stale.release.exists()


def test_gc_cli_reports_resumed_deletion_when_record_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_remove = helper._remove_gc_retirement_record

    def fail_record_cleanup(*args, **kwargs) -> None:
        raise OSError("injected record cleanup failure")

    monkeypatch.setattr(helper, "_remove_gc_retirement_record", fail_record_cleanup)
    with pytest.raises(OSError, match="record cleanup failure"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )
    assert not stale.release.exists()
    assert not any(
        path.is_dir() and path.name.startswith(".gc-retired-")
        for path in stale.root.iterdir()
    )
    records = list(stale.root.glob(".gc-retired-*.record"))
    assert len(records) == 1

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(helper.sys, "platform", "darwin")
    monkeypatch.setattr(helper, "collect_runtime_references", lambda root, plist: set())
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout), "--apply"],
    )

    with pytest.raises(OSError, match="record cleanup failure"):
        helper.main()

    assert json.loads(capsys.readouterr().out) == {
        "deleted": [str(stale.release)],
        "error": {
            "message": "injected record cleanup failure",
            "type": "OSError",
        },
        "retained": [
            {
                "path": str(records[0]),
                "reason": (
                    "record-cleanup-failed-after-deletion: "
                    "injected record cleanup failure"
                ),
            }
        ],
        "status": "partial",
    }
    assert not (hermes_home / "state/hermes-kanban-update.lock").exists()
    monkeypatch.setattr(helper, "_remove_gc_retirement_record", original_remove)


def test_gc_apply_refreshes_runtime_references_before_resumed_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")

    def crash_before_delete(_retirement: str, *, dir_fd: int) -> None:
        raise OSError("injected pre-delete crash")

    monkeypatch.setattr(helper.shutil, "rmtree", crash_before_delete)
    with pytest.raises(OSError, match="pre-delete crash"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    retired = next(
        path
        for path in stale.root.iterdir()
        if path.name.startswith(".gc-retired-") and path.is_dir()
    )
    record = next(stale.root.glob(".gc-retired-*.record"))
    monkeypatch.undo()
    runtime_calls = 0

    def runtime_references() -> set[Path]:
        nonlocal runtime_calls
        runtime_calls += 1
        return set() if runtime_calls == 1 else {stale.release}

    resumed = helper.apply_release_gc(
        checkout,
        runtime_reference_supplier=runtime_references,
        lock_verifier=lambda: None,
    )

    assert runtime_calls >= 2
    assert resumed == {
        "deleted": [],
        "retained": [
            {"path": str(retired), "reason": "referenced-partial-retirement"},
            {
                "path": str(record),
                "reason": "record-retained-with-referenced-partial-retirement",
            },
        ],
    }
    assert retired.is_dir()
    assert record.is_file()
    assert not stale.release.exists()


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


def test_gc_apply_reports_canonical_retention_when_record_cleanup_fails_after_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_fsync = helper._fsync_gc_root
    calls = 0

    def fail_retirement_then_cleanup_fsync(
        capability, *, allow_moved: bool = False
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected post-retirement fsync failure")
        if calls == 4:
            raise OSError("injected record cleanup fsync failure")
        original_fsync(capability, allow_moved=allow_moved)

    monkeypatch.setattr(
        helper, "_fsync_gc_root", fail_retirement_then_cleanup_fsync
    )

    with pytest.raises(OSError, match="post-retirement fsync failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    assert len(partial["retained"]) == 2
    record, canonical = partial["retained"]
    assert record["path"].endswith(".record")
    assert record["reason"] == (
        "indeterminate-record-cleanup-durability: "
        "injected record cleanup fsync failure"
    )
    assert not Path(record["path"]).exists()
    assert canonical == {
        "path": str(stale.release),
        "reason": (
            "restored-canonical-record-cleanup-failed: "
            "injected record cleanup fsync failure"
        ),
    }
    assert stale.release.is_dir()
    assert not list(stale.root.glob(".gc-retired-*"))


def test_gc_apply_records_referenced_restoration_before_record_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    calls = 0

    def runtime_references() -> set[Path]:
        nonlocal calls
        calls += 1
        return set() if calls == 1 else {stale.release}

    def fail_record_cleanup(*args, **kwargs) -> None:
        raise OSError("injected referenced record cleanup failure")

    monkeypatch.setattr(helper, "_remove_gc_retirement_record", fail_record_cleanup)

    with pytest.raises(OSError, match="referenced record cleanup failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=runtime_references,
            lock_verifier=lambda: None,
        )

    records = list(stale.root.glob(".*.record"))
    assert len(records) == 1
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [],
        "retained": [
            {
                "path": str(records[0]),
                "reason": (
                    "record-cleanup-failed-after-restoration: "
                    "injected referenced record cleanup failure"
                ),
            },
            {"path": str(stale.release), "reason": "referenced-after-retirement"},
        ],
    }
    assert stale.release.is_dir()


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


def test_gc_dry_run_rejects_a_writable_root_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    build_completed_release(helper, checkout, tmp_path / "release-work-root-anchor")
    original_lstat = helper.os.lstat

    def writable_root(path):
        observed = original_lstat(path)
        if Path(path) == Path(Path(path).anchor):
            values = list(observed)
            values[0] |= stat.S_IWOTH
            return os.stat_result(values)
        return observed

    monkeypatch.setattr(helper.os, "lstat", writable_root)
    with pytest.raises(RuntimeError, match="untrusted writable ancestor"):
        helper.plan_release_gc(checkout, runtime_references=set())


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
    real_checkout = real_parent / "hermes-agent"
    real_checkout.mkdir()
    release = build_completed_release(helper, real_checkout, tmp_path / "release-work")
    linked_checkout = linked_parent / "hermes-agent"

    with pytest.raises(RuntimeError, match="symlinked ancestor"):
        helper.plan_release_gc(linked_checkout, runtime_references=set())

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


def test_gc_apply_rechecks_root_after_post_retirement_reference_probe(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    root = stale.root
    displaced = tmp_path / "displaced-during-reference-probe"
    calls = 0

    def displace_during_late_probe() -> set[Path]:
        nonlocal calls
        calls += 1
        if calls == 2:
            root.rename(displaced)
            root.mkdir(mode=0o700)
        return set()

    with pytest.raises(RuntimeError, match="release root.*changed|release root.*moved"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=displace_during_late_probe,
            lock_verifier=lambda: None,
        )

    assert (displaced / stale.release.name).is_dir()
    assert list(root.iterdir()) == []


def test_gc_resume_reports_retirement_and_record_when_runtime_probe_fails(
    tmp_path: Path
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    identity = stale.release.stat()
    retirement = stale.root / (
        f".gc-retired-{stale.release.name.removeprefix('release-')}-{'5' * 32}"
    )
    record = retirement.with_name(f"{retirement.name}.record")
    record.write_bytes(
        helper._gc_retirement_record_payload(
            stale.release, retirement, (identity.st_dev, identity.st_ino)
        )
    )
    record.chmod(0o600)
    stale.release.rename(retirement)
    primary = RuntimeError("primary resumed runtime probe failure")
    calls = 0

    def fail_runtime_probe() -> set[Path]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise primary
        return set()

    with pytest.raises(RuntimeError, match="resumed runtime probe failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=fail_runtime_probe,
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [],
        "retained": [
            {
                "path": str(retirement),
                "reason": "resume-pre-deletion-failed: primary resumed runtime probe failure",
            },
            {
                "path": str(record),
                "reason": (
                    "record-retained-after-resume-pre-deletion-failure: "
                    "primary resumed runtime probe failure"
                ),
            },
        ],
    }
    assert retirement.is_dir()
    assert record.is_file()
    assert not stale.release.exists()


def test_gc_resume_rechecks_root_after_runtime_reference_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    root = stale.root

    def crash_before_delete(_retirement: str, *, dir_fd: int) -> None:
        raise OSError("injected pre-delete crash")

    monkeypatch.setattr(helper.shutil, "rmtree", crash_before_delete)
    with pytest.raises(OSError, match="pre-delete crash"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )
    monkeypatch.undo()

    displaced = tmp_path / "displaced-during-resume-probe"
    calls = 0

    def displace_during_resume_probe() -> set[Path]:
        nonlocal calls
        calls += 1
        if calls == 2:
            root.rename(displaced)
            root.mkdir(mode=0o700)
        return set()

    with pytest.raises(RuntimeError, match="release root.*changed|release root.*moved"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=displace_during_resume_probe,
            lock_verifier=lambda: None,
        )

    assert any(
        path.is_dir() and path.name.startswith(".gc-retired-")
        for path in displaced.iterdir()
    )
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


def test_managed_launcher_requires_complete_stable_selector_read(tmp_path: Path) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)

    payload = helper.launcher_payload(layout, helper.BASELINE_ABSENT).decode("utf-8")

    assert "while remaining:" in payload
    assert "len(data)!=opened.st_size" in payload


@pytest.mark.parametrize("unsafe_kind", ["writable", "hardlinked"])
def test_managed_launcher_rejects_unsafe_selector_leaf(
    tmp_path: Path, unsafe_kind: str
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    layout.release.joinpath("venv", "bin").mkdir(parents=True)
    executable = layout.release / "venv/bin/hermes"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    layout.selector.write_bytes(helper.selector_payload(layout))
    if unsafe_kind == "writable":
        layout.selector.chmod(0o666)
    else:
        (tmp_path / "selector-peer").hardlink_to(layout.selector)
    launcher = tmp_path / "managed-hermes"
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)

    completed = subprocess.run([str(launcher)], capture_output=True, text=True, check=False)

    assert completed.returncode != 0
    assert "invalid Hermes release selector" in completed.stderr


@pytest.mark.parametrize("unsafe_kind", ["writable", "hardlinked"])
def test_managed_launcher_rejects_unsafe_selected_executable_leaf(
    tmp_path: Path, unsafe_kind: str
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    layout.release.joinpath("venv", "bin").mkdir(parents=True)
    marker = tmp_path / "unsafe-executable-ran"
    executable = layout.release / "venv/bin/hermes"
    executable.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    executable.chmod(0o755)
    if unsafe_kind == "writable":
        executable.chmod(0o777)
    else:
        (tmp_path / "executable-peer").hardlink_to(executable)
    layout.selector.write_bytes(helper.selector_payload(layout))
    launcher = tmp_path / "managed-hermes"
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)

    completed = subprocess.run([str(launcher)], capture_output=True, text=True, check=False)

    assert completed.returncode != 0
    assert "invalid Hermes release selector" in completed.stderr
    assert not marker.exists()


def test_managed_launcher_rejects_release_root_beneath_writable_ancestor(
    tmp_path: Path,
) -> None:
    helper = load_helper()
    unsafe_parent = tmp_path / "unsafe-release-parent"
    unsafe_parent.mkdir(mode=0o700)
    checkout = unsafe_parent / "hermes-agent"
    checkout.mkdir()
    layout = helper.release_layout(checkout, "1" * 40, "2" * 40)
    layout.release.joinpath("venv", "bin").mkdir(parents=True)
    marker = tmp_path / "attacker-executed"
    executable = layout.release / "venv/bin/hermes"
    executable.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    executable.chmod(0o755)
    layout.selector.write_bytes(helper.selector_payload(layout))
    launcher = tmp_path / "managed-hermes"
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)
    unsafe_parent.chmod(0o777)

    completed = subprocess.run([str(launcher)], capture_output=True, text=True, check=False)

    assert completed.returncode != 0
    assert "invalid Hermes release selector" in completed.stderr
    assert not marker.exists()


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
        ["--reasoning", "high", "update", "--yes"],
        ["--reasoning=high", "update", "--force"],
        ["--profile", "reviewed", "update", "--yes"],
        ["--profile=reviewed", "update", "--force"],
        ["-p", "reviewed", "update", "--check"],
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

    # 제거 과정은 다시 렌더링한 뒤 바이트를 비교해 출처를 입증하므로, 구별 가능한
    # 모든 설치 결정은 구별 가능한 실행기를 만들어야 한다.
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
    """Apple의 관리자 Python은 릴리스가 기대하는 pyc 경로를 옮기면 안 된다."""
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
    """검증은 관리자와 릴리스가 동일한 pyc 형식을 쓴다고 가정하면 안 된다."""
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
    """최초의 전체 Hermes 실행이 봉인된 바이트코드를 쓸어버리면 안 된다."""
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
    """영수증 없는 릴리스가 이후 모든 실행을 절대 망가뜨려서는 안 된다.

    첫 실행은 안정 릴리스 경로를 게시한 뒤 의존성 동기화에 실패하므로 이후에는
    어떤 것도 그 릴리스를 완성할 수 없다. 다음 실행은 영원히 거부하는 대신 해당
    릴리스를 폐기하고 다시 빌드해야 한다.
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
    # 이제 완성된 릴리스는 uv를 전혀 건드리지 않고 재사용할 수 있다.
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

    # `UV_NO_CONFIG`를 다시 사용하면 안 된다. 이 변수는 검토된 업스트림 자체의
    # `[tool.uv]` 설정도 버리며, Hermes는 `[tool.uv] exclude-newer` 범위 아래에서
    # 잠그므로 설정 없는 동기화는 다시 해석되어 `--locked`가 거부한다. 대신 빈
    # 비공개 디렉터리로 사용자 설정을 무력화한다.
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
    """의존성 설치기만 모의 처리하여 완성된 릴리스 하나를 준비한다."""
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
    """검토된 업스트림의 유일한 충돌은 빌드되어야 하며 전체 설치를 실패시키면 안 된다.

    업스트림에는 이름의 대소문자만 다른 기여자 이메일 메타데이터 파일 두 개가
    있다. 지원되는 대소문자 비구분 볼륨은 그중 하나만 담을 수 있으므로, 릴리스는
    충돌하거나 그 결과로 생긴 변경을 무시해 통과시키는 대신 남길 파일을 결정적으로
    정하고 그 바이트를 입증해야 한다.
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
    """검토된 비런타임 메타데이터 네임스페이스만 디스크에서 별칭 충돌할 수 있다."""
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
    """지원되는 볼륨은 NFD를 NFC로 접으므로 트리도 접은 형태로 검사해야 한다."""
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
    """남은 파일을 다른 표기의 파일로 바꾸면 검증을 통과해서는 안 된다."""
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
    """충돌 복구는 검토된 충돌을 공격자가 만든 손상으로 해석하면 안 된다."""
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
    """두 구성원을 구분해 보존하는 볼륨은 둘 다 유지하면서 깨끗한 상태여야 한다.

    지원되는 macOS 볼륨은 이 정책이 접는 모든 Unicode 표기를 접는다. 따라서 실제
    파일로 비별칭 분기를 실행하는 유일한 방법은 합성 쌍 하나에 대해 정책의 동등성
    범위를 넓히는 것이다. 이는 대소문자 구분 볼륨이 나타내는 상태, 즉 한 그룹에
    서로 다른 디스크 파일 두 개가 있는 상태와 정확히 같다.
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



def test_gc_record_publication_probe_failure_reports_fallback_record_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_write = helper._write_private_candidate_at
    original_stat = helper.os.stat
    publication_failed = False
    primary = OSError("primary record publication failure")

    def publish_then_fail(*args, **kwargs) -> None:
        nonlocal publication_failed
        original_write(*args, **kwargs)
        publication_failed = True
        raise primary

    def fail_record_probe(path, *args, **kwargs):
        if publication_failed and str(path).endswith(".record"):
            raise OSError("secondary record state probe failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(helper, "_write_private_candidate_at", publish_then_fail)
    monkeypatch.setattr(helper.os, "stat", fail_record_probe)

    with pytest.raises(OSError, match="primary record publication failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    assert any(
        "secondary record state probe failure" in note
        for note in raised.value.__notes__
    )
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    assert len(partial["retained"]) == 1
    retained = partial["retained"][0]
    assert retained["path"].endswith(".record")
    assert retained["reason"] == (
        "indeterminate-record-state: secondary record state probe failure"
    )
    assert stale.release.is_dir()


def test_gc_record_publication_cleanup_fsync_failure_reports_indeterminate_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-publication-work")
    primary = OSError("primary record publication write failure")
    durability = OSError("secondary record cleanup durability failure")

    def fail_write(descriptor: int, payload) -> int:
        raise primary

    def fail_cleanup_fsync(capability, *, allow_moved=False) -> None:
        raise durability

    monkeypatch.setattr(helper.os, "write", fail_write)
    monkeypatch.setattr(helper, "_fsync_gc_root", fail_cleanup_fsync)

    with pytest.raises(OSError, match="record publication write failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    assert any(
        "secondary record cleanup durability failure" in note
        for note in raised.value.__notes__
    )
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    assert len(partial["retained"]) == 1
    retained = partial["retained"][0]
    assert retained["path"].endswith(".record")
    assert retained["reason"] == (
        "indeterminate-record-cleanup-durability: "
        "secondary record cleanup durability failure"
    )
    assert not Path(retained["path"]).exists()
    assert stale.release.is_dir()


def test_gc_record_publication_preserves_write_failure_when_cleanup_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent.releases"
    root.mkdir(mode=0o700)
    capability = helper._open_gc_root(root)
    original_close = helper.os.close
    opened_descriptor = None

    def fail_write(descriptor: int, payload) -> int:
        nonlocal opened_descriptor
        opened_descriptor = descriptor
        raise OSError("primary record write failure")

    def fail_close(descriptor: int) -> None:
        if descriptor == opened_descriptor:
            raise OSError("secondary record close failure")
        original_close(descriptor)

    def fail_unlink(*args, **kwargs) -> None:
        raise OSError("secondary record unlink failure")

    monkeypatch.setattr(helper.os, "write", fail_write)
    monkeypatch.setattr(helper.os, "close", fail_close)
    monkeypatch.setattr(helper.os, "unlink", fail_unlink)

    with pytest.raises(OSError, match="primary record write failure") as raised:
        helper._write_private_candidate_at(
            capability, ".gc-retired-test.record", b"payload\n", 0o600
        )

    assert any("secondary record close failure" in note for note in raised.value.__notes__)
    assert any("secondary record unlink failure" in note for note in raised.value.__notes__)
    monkeypatch.undo()
    if opened_descriptor is not None:
        original_close(opened_descriptor)
    original_close(capability.descriptor)


def test_gc_apply_preserves_primary_failure_when_capability_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_open = helper._open_gc_root
    original_close = helper.os.close
    capability_descriptor = None

    def capture_capability(root: Path):
        nonlocal capability_descriptor
        capability = original_open(root)
        capability_descriptor = capability.descriptor
        return capability

    def fail_capability_close(descriptor: int) -> None:
        if descriptor == capability_descriptor:
            raise OSError("secondary capability close failure")
        original_close(descriptor)

    monkeypatch.setattr(helper, "_open_gc_root", capture_capability)
    monkeypatch.setattr(helper.os, "close", fail_capability_close)

    with pytest.raises(RuntimeError, match="primary runtime probe failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: (_ for _ in ()).throw(
                RuntimeError("primary runtime probe failure")
            ),
            lock_verifier=lambda: None,
        )

    assert any("secondary capability close failure" in note for note in raised.value.__notes__)
    monkeypatch.undo()
    assert capability_descriptor is not None
    original_close(capability_descriptor)


def test_gc_apply_reports_canonical_state_when_restore_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_fsync = helper._fsync_gc_root
    calls = 0

    def fail_retirement_and_restoration_fsync(
        capability, *, allow_moved: bool = False
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("primary post-retirement fsync failure")
        if calls == 3:
            raise OSError("secondary restoration fsync failure")
        original_fsync(capability, allow_moved=allow_moved)

    monkeypatch.setattr(
        helper, "_fsync_gc_root", fail_retirement_and_restoration_fsync
    )

    with pytest.raises(OSError, match="primary post-retirement fsync failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert any(
        "GC restoration also failed" in note
        and "secondary restoration fsync failure" in note
        for note in raised.value.__notes__
    )
    records = list(stale.root.glob(".*.record"))
    assert len(records) == 1
    retirement = records[0].with_name(records[0].name.removesuffix(".record"))
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [],
        "retained": [
            {
                "path": str(retirement),
                "reason": (
                    "indeterminate-retirement-after-restoration-durability-failure: "
                    "secondary restoration fsync failure"
                ),
            },
            {
                "path": str(records[0]),
                "reason": (
                    "record-retained-after-restoration-failure: "
                    "secondary restoration fsync failure"
                ),
            },
            {
                "path": str(stale.release),
                "reason": (
                    "restored-canonical-durability-failed: "
                    "secondary restoration fsync failure"
                ),
            },
        ],
    }
    assert stale.release.is_dir()
    assert not any(
        path.is_dir() and path.name.startswith(".gc-retired-")
        for path in stale.root.iterdir()
    )


def test_gc_apply_finalizes_deletion_when_post_rmtree_path_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "post-rmtree-path-work")
    original_rmtree = helper.shutil.rmtree
    original_root_path = helper._gc_root_path
    deletion_completed = False
    injected = False
    primary = RuntimeError("injected post-rmtree path lookup failure")

    def complete_then_mark(path, *args, **kwargs) -> None:
        nonlocal deletion_completed
        original_rmtree(path, *args, **kwargs)
        deletion_completed = True

    def fail_first_post_rmtree_path_lookup(capability, *, allow_moved=False):
        nonlocal injected
        if deletion_completed and allow_moved and not injected:
            injected = True
            raise primary
        return original_root_path(capability, allow_moved=allow_moved)

    monkeypatch.setattr(helper.shutil, "rmtree", complete_then_mark)
    monkeypatch.setattr(helper, "_gc_root_path", fail_first_post_rmtree_path_lookup)

    with pytest.raises(RuntimeError, match="post-rmtree path lookup failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [str(stale.release)],
        "retained": [],
    }
    assert not stale.release.exists()
    assert not list(stale.root.glob(".*.record"))


def test_gc_apply_reports_actual_deleted_path_when_rmtree_displaces_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-displaced-work")
    root = stale.root
    displaced = tmp_path / "displaced-during-rmtree"
    original_rmtree = helper.shutil.rmtree

    def displace_then_remove(retirement: str, *, dir_fd: int) -> None:
        root.rename(displaced)
        root.mkdir(mode=0o700)
        original_rmtree(retirement, dir_fd=dir_fd)

    monkeypatch.setattr(helper.shutil, "rmtree", displace_then_remove)

    with pytest.raises(RuntimeError, match="release root.*changed|release root.*moved") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    records = list(displaced.glob(".*.record"))
    assert len(records) == 1
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [str(displaced / stale.release.name)],
        "retained": [
            {
                "path": str(records[0]),
                "reason": (
                    "record-retained-after-deletion-post-fsync-validation-failure: "
                    "release root moved during GC"
                ),
            }
        ],
    }
    assert not (displaced / stale.release.name).exists()
    assert not stale.release.exists()


def test_gc_apply_records_deletion_when_rmtree_removes_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_rmtree = helper.shutil.rmtree

    def remove_then_fail(retirement: str, *, dir_fd: int) -> None:
        original_rmtree(retirement, dir_fd=dir_fd)
        raise OSError("injected post-rmtree failure")

    monkeypatch.setattr(helper.shutil, "rmtree", remove_then_fail)

    with pytest.raises(OSError, match="post-rmtree failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    records = list(stale.root.glob(".*.record"))
    assert len(records) == 1
    retirement = records[0].with_name(records[0].name.removesuffix(".record"))
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [str(stale.release)],
        "retained": [
            {
                "path": str(retirement),
                "reason": (
                    "indeterminate-retirement-after-deletion-exception: "
                    "injected post-rmtree failure"
                ),
            },
            {
                "path": str(records[0]),
                "reason": "record-retained-after-deletion-exception: injected post-rmtree failure",
            },
        ],
    }


def test_gc_apply_preserves_rmtree_failure_when_outcome_probe_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_stat = helper.os.stat
    deletion_failed = False

    def fail_rmtree(retirement: str, *, dir_fd: int) -> None:
        nonlocal deletion_failed
        deletion_failed = True
        raise OSError("primary rmtree failure")

    def fail_outcome_probe(path, *args, **kwargs):
        if deletion_failed and str(path).startswith(".gc-retired-"):
            raise OSError("secondary deletion outcome probe failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(helper.shutil, "rmtree", fail_rmtree)
    monkeypatch.setattr(helper.os, "stat", fail_outcome_probe)

    with pytest.raises(OSError, match="primary rmtree failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert any(
        "secondary deletion outcome probe failure" in note
        for note in raised.value.__notes__
    )
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    retirement = next(
        path
        for path in stale.root.iterdir()
        if path.is_dir() and path.name.startswith(".gc-retired-")
    )
    record = retirement.with_name(f"{retirement.name}.record")
    assert partial["retained"] == [
        {
            "path": str(retirement),
            "reason": (
                "indeterminate-retirement-state-after-deletion-probe-failure: "
                "secondary deletion outcome probe failure"
            ),
        },
        {
            "path": str(record),
            "reason": (
                "indeterminate-deletion-state: "
                "secondary deletion outcome probe failure"
            ),
        },
    ]
    assert not stale.release.exists()
    assert retirement.is_dir()
    assert record.is_file()


def test_gc_resume_records_deletion_when_rmtree_removes_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")

    def crash_before_delete(_retirement: str, *, dir_fd: int) -> None:
        raise OSError("injected pre-delete crash")

    monkeypatch.setattr(helper.shutil, "rmtree", crash_before_delete)
    with pytest.raises(OSError, match="pre-delete crash"):
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )
    monkeypatch.undo()
    original_rmtree = helper.shutil.rmtree

    def remove_then_fail(retirement: str, *, dir_fd: int) -> None:
        original_rmtree(retirement, dir_fd=dir_fd)
        raise OSError("injected resumed post-rmtree failure")

    monkeypatch.setattr(helper.shutil, "rmtree", remove_then_fail)
    with pytest.raises(OSError, match="resumed post-rmtree failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    records = list(stale.root.glob(".*.record"))
    assert len(records) == 1
    retirement = records[0].with_name(records[0].name.removesuffix(".record"))
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [str(stale.release)],
        "retained": [
            {
                "path": str(retirement),
                "reason": (
                    "indeterminate-retirement-after-deletion-exception: "
                    "injected resumed post-rmtree failure"
                ),
            },
            {
                "path": str(records[0]),
                "reason": "record-retained-after-deletion-exception: injected resumed post-rmtree failure",
            },
        ],
    }


def test_gc_resume_records_restored_canonical_before_record_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    identity = stale.release.stat()
    retirement = stale.root / (
        f".gc-retired-{stale.release.name.removeprefix('release-')}-{'6' * 32}"
    )
    record = retirement.with_name(f"{retirement.name}.record")
    record.write_bytes(
        helper._gc_retirement_record_payload(
            stale.release, retirement, (identity.st_dev, identity.st_ino)
        )
    )
    record.chmod(0o600)
    stale.release.rename(retirement)
    retirement.rename(stale.release)

    displaced = tmp_path / "displaced-restored-canonical-root"

    def fail_record_cleanup(*args, **kwargs) -> None:
        stale.root.rename(displaced)
        stale.root.mkdir(mode=0o700)
        raise OSError("injected restored record cleanup failure")

    monkeypatch.setattr(helper, "_remove_gc_retirement_record", fail_record_cleanup)
    with pytest.raises(OSError, match="restored record cleanup failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [],
        "retained": [
            {
                "path": str(displaced / record.name),
                "reason": (
                    "record-cleanup-failed-after-restored-canonical-recovery: "
                    "injected restored record cleanup failure"
                ),
            },
            {
                "path": str(displaced / stale.release.name),
                "reason": "restored-canonical-after-crash",
            },
        ],
    }
    assert (displaced / stale.release.name).is_dir()
    assert not stale.release.exists()
    assert not (displaced / retirement.name).exists()
    assert (displaced / record.name).is_file()


def test_gc_resume_reports_foreign_successor_and_existing_retirement_separately(
    tmp_path: Path
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    prior = build_completed_release(helper, checkout, tmp_path / "prior-existing-work")
    prior_identity = prior.release.stat()
    successor = build_completed_release(
        helper, checkout, tmp_path / "foreign-existing-successor-work"
    )
    retirement = successor.root / (
        f".gc-retired-{successor.release.name.removeprefix('release-')}-{'6' * 32}"
    )
    prior.release.rename(retirement)
    record = retirement.with_name(f"{retirement.name}.record")
    record.write_bytes(
        helper._gc_retirement_record_payload(
            successor.release,
            retirement,
            (prior_identity.st_dev, prior_identity.st_ino),
        )
    )
    record.chmod(0o600)
    for reference_name in ("current", "previous"):
        reference = successor.root / reference_name
        if reference.exists() or reference.is_symlink():
            reference.unlink()

    result = helper.apply_release_gc(
        checkout,
        runtime_reference_supplier=lambda: set(),
        lock_verifier=lambda: None,
    )

    assert result["deleted"] == []
    assert result["retained"] == [
        {
            "path": str(retirement),
            "reason": "retirement-retained-with-foreign-canonical-successor",
        },
        {
            "path": str(record),
            "reason": "record-retained-with-foreign-canonical-successor",
        },
        {"path": str(successor.release), "reason": "foreign-canonical-successor"},
    ]
    assert successor.release.is_dir()
    assert retirement.is_dir()
    assert record.is_file()


@pytest.mark.parametrize("boundary", ["record-read", "retirement-verify"])
def test_gc_resume_retries_moved_root_before_classifying_recovery_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / f"resume-{boundary}-work")
    identity = stale.release.stat()
    root = stale.root
    retirement = root / (
        f".gc-retired-{stale.release.name.removeprefix('release-')}-{'3' * 32}"
    )
    record = retirement.with_name(f"{retirement.name}.record")
    stale.release.rename(retirement)
    record.write_bytes(
        helper._gc_retirement_record_payload(
            stale.release,
            retirement,
            (identity.st_dev, identity.st_ino),
        )
    )
    record.chmod(0o600)
    displaced = tmp_path / f"displaced-during-{boundary}"
    moved = False

    def move_root() -> None:
        nonlocal moved
        if moved:
            return
        root.rename(displaced)
        root.mkdir(mode=0o700)
        moved = True

    if boundary == "record-read":
        original_read = helper._read_gc_retirement_record

        def move_during_record_read(
            root_path: Path,
            record_path: Path,
            *,
            authority_root: Path | None = None,
        ):
            move_root()
            return original_read(
                root_path,
                record_path,
                authority_root=authority_root,
            )

        monkeypatch.setattr(
            helper, "_read_gc_retirement_record", move_during_record_read
        )
    else:
        original_verify = helper._verify_gc_release

        def move_during_retirement_verify(
            agent_repo: Path, release_path: Path, *, canonical_name: str
        ) -> None:
            move_root()
            original_verify(
                agent_repo, release_path, canonical_name=canonical_name
            )

        monkeypatch.setattr(helper, "_verify_gc_release", move_during_retirement_verify)

    with pytest.raises(RuntimeError, match="release root moved during GC") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    moved_retirement = displaced / retirement.name
    moved_record = displaced / record.name
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [],
        "retained": [
            {
                "path": str(moved_retirement),
                "reason": "resume-pre-deletion-failed: release root moved during GC",
            },
            {
                "path": str(moved_record),
                "reason": (
                    "record-retained-after-resume-pre-deletion-failure: "
                    "release root moved during GC"
                ),
            },
        ],
    }
    assert moved_retirement.is_dir()
    assert moved_record.is_file()


def test_gc_resume_probe_failure_reports_retirement_and_record_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "resume-probe-failure-work")
    identity = stale.release.stat()
    retirement = stale.root / (
        f".gc-retired-{stale.release.name.removeprefix('release-')}-{'4' * 32}"
    )
    record = retirement.with_name(f"{retirement.name}.record")
    stale.release.rename(retirement)
    record.write_bytes(
        helper._gc_retirement_record_payload(
            stale.release,
            retirement,
            (identity.st_dev, identity.st_ino),
        )
    )
    record.chmod(0o600)
    primary = OSError("injected canonical recovery probe failure")
    original_stat = helper.os.stat

    def fail_canonical_probe(path, *args, **kwargs):
        if path == stale.release.name and kwargs.get("dir_fd") is not None:
            raise primary
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(helper.os, "stat", fail_canonical_probe)

    with pytest.raises(OSError, match="canonical recovery probe failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [],
        "retained": [
            {
                "path": str(retirement),
                "reason": (
                    "indeterminate-retirement-state-after-recovery-probe-failure: "
                    "injected canonical recovery probe failure"
                ),
            },
            {
                "path": str(record),
                "reason": (
                    "record-retained-after-recovery-probe-failure: "
                    "injected canonical recovery probe failure"
                ),
            },
            {
                "path": str(stale.release),
                "reason": (
                    "indeterminate-canonical-state-after-recovery-probe-failure: "
                    "injected canonical recovery probe failure"
                ),
            },
        ],
    }
    assert retirement.is_dir()
    assert record.is_file()


def test_gc_resume_reports_actual_deleted_path_when_absent_probes_displace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "record-only-displaced-work")
    root = stale.root
    identity = stale.release.stat()
    retirement = root / (
        f".gc-retired-{stale.release.name.removeprefix('release-')}-{'5' * 32}"
    )
    record = retirement.with_name(f"{retirement.name}.record")
    record.write_bytes(
        helper._gc_retirement_record_payload(
            stale.release,
            retirement,
            (identity.st_dev, identity.st_ino),
        )
    )
    record.chmod(0o600)
    helper.shutil.rmtree(stale.release)
    displaced = tmp_path / "displaced-during-absent-resume-probes"
    original_stat = helper.os.stat
    canonical_probed = False
    moved = False

    def displace_during_retirement_probe(path, *args, **kwargs):
        nonlocal canonical_probed, moved
        if path == stale.release.name and kwargs.get("dir_fd") is not None:
            canonical_probed = True
        if (
            path == retirement.name
            and canonical_probed
            and not moved
            and kwargs.get("dir_fd") is not None
        ):
            root.rename(displaced)
            root.mkdir(mode=0o700)
            moved = True
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(helper.os, "stat", displace_during_retirement_probe)

    with pytest.raises(RuntimeError, match="release root.*changed|release root.*moved") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    displaced_record = displaced / record.name
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [str(displaced / stale.release.name)],
        "retained": [
            {
                "path": str(displaced_record),
                "reason": (
                    "record-retained-after-deletion-post-fsync-validation-failure: "
                    "release root moved during GC"
                ),
            }
        ],
    }
    assert displaced_record.is_file()
    assert not (displaced / stale.release.name).exists()
    assert not stale.release.exists()


def test_gc_resume_reports_existing_foreign_successor_when_retirement_is_absent(
    tmp_path: Path
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    prior = build_completed_release(helper, checkout, tmp_path / "prior-work")
    prior_identity = prior.release.stat()
    successor = build_completed_release(
        helper, checkout, tmp_path / "foreign-successor-work"
    )
    assert successor.release != prior.release
    retirement = successor.root / (
        f".gc-retired-{successor.release.name.removeprefix('release-')}-{'7' * 32}"
    )
    record = retirement.with_name(f"{retirement.name}.record")
    record.write_bytes(
        helper._gc_retirement_record_payload(
            successor.release,
            retirement,
            (prior_identity.st_dev, prior_identity.st_ino),
        )
    )
    record.chmod(0o600)
    for reference_name in ("current", "previous"):
        reference = successor.root / reference_name
        if reference.exists() or reference.is_symlink():
            reference.unlink()
    runtime_references = {prior.release}
    plan = helper.plan_release_gc(
        checkout, runtime_references=runtime_references
    )
    assert str(successor.release) in plan["candidates"]

    result = helper.apply_release_gc(
        checkout,
        runtime_reference_supplier=lambda: runtime_references,
        lock_verifier=lambda: None,
    )

    assert result["deleted"] == []
    assert result["retained"] == [
        {
            "path": str(record),
            "reason": "record-retained-with-foreign-canonical-successor",
        },
        {"path": str(successor.release), "reason": "foreign-canonical-successor"},
    ]
    assert prior.release.is_dir()
    assert successor.release.is_dir()
    assert not retirement.exists()
    assert record.is_file()



def test_restore_retired_release_returns_actual_path_after_fsync_displacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    retirement_name = ".gc-retired-release-a-" + "0" * 32
    release_name = "release-" + "a" * 40
    retirement = root / retirement_name
    retirement.mkdir(mode=0o700)
    identity = retirement.stat()
    capability = helper._open_gc_root(root)
    displaced = tmp_path / "displaced-restore-success"
    original_fsync = helper._fsync_gc_root

    def displace_during_restore_fsync(capability, *, allow_moved=False) -> None:
        root.rename(displaced)
        root.mkdir(mode=0o700)
        original_fsync(capability, allow_moved=allow_moved)

    monkeypatch.setattr(helper, "_fsync_gc_root", displace_during_restore_fsync)
    try:
        restored = helper._restore_retired_release(
            capability,
            retirement_name,
            release_name,
            (identity.st_dev, identity.st_ino),
        )
    finally:
        helper.os.close(capability.descriptor)

    assert restored == displaced / release_name
    assert restored.is_dir()
    assert not (root / release_name).exists()


def test_restore_retired_release_reports_actual_path_when_fsync_fails_after_displacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    retirement_name = ".gc-retired-release-b-" + "1" * 32
    release_name = "release-" + "b" * 40
    retirement = root / retirement_name
    retirement.mkdir(mode=0o700)
    identity = retirement.stat()
    capability = helper._open_gc_root(root)
    displaced = tmp_path / "displaced-restore-failure"
    primary = OSError("injected restoration fsync failure")

    def fail_after_displacement(capability, *, allow_moved=False) -> None:
        root.rename(displaced)
        root.mkdir(mode=0o700)
        raise primary

    monkeypatch.setattr(helper, "_fsync_gc_root", fail_after_displacement)
    try:
        with pytest.raises(OSError, match="restoration fsync failure") as raised:
            helper._restore_retired_release(
                capability,
                retirement_name,
                release_name,
                (identity.st_dev, identity.st_ino),
            )
    finally:
        helper.os.close(capability.descriptor)

    assert raised.value is primary
    assert getattr(raised.value, "gc_restored_path") == displaced / release_name
    assert (displaced / release_name).is_dir()
    assert not (root / release_name).exists()


def test_gc_restore_marks_second_path_lookup_failure_as_fsync_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "second-path-work")
    capability = helper._open_gc_root(stale.root)
    identity_stat = stale.release.stat()
    identity = (identity_stat.st_dev, identity_stat.st_ino)
    retirement_name = ".gc-retired-second-path"
    helper.os.rename(
        stale.release.name,
        retirement_name,
        src_dir_fd=capability.descriptor,
        dst_dir_fd=capability.descriptor,
    )
    original_fsync = helper._fsync_gc_root
    original_root_path = helper._gc_root_path
    restoration_fsync_completed = False
    primary = RuntimeError("injected post-restoration-fsync path lookup failure")

    def mark_completed_fsync(capability_arg, *, allow_moved=False):
        nonlocal restoration_fsync_completed
        original_fsync(capability_arg, allow_moved=allow_moved)
        if allow_moved:
            restoration_fsync_completed = True

    def fail_second_lookup(capability_arg, *, allow_moved=False):
        if restoration_fsync_completed and allow_moved:
            raise primary
        return original_root_path(capability_arg, allow_moved=allow_moved)

    monkeypatch.setattr(helper, "_fsync_gc_root", mark_completed_fsync)
    monkeypatch.setattr(helper, "_gc_root_path", fail_second_lookup)
    try:
        with pytest.raises(RuntimeError, match="post-restoration-fsync path lookup") as raised:
            helper._restore_retired_release(
                capability,
                retirement_name,
                stale.release.name,
                identity,
            )
    finally:
        helper.os.close(capability.descriptor)

    assert raised.value is primary
    assert getattr(primary, "gc_root_fsync_completed") is True
    assert getattr(primary, "gc_restored_path") == stale.release
    assert not hasattr(primary, "gc_retirement_path")
    assert stale.release.is_dir()


def test_gc_referenced_restoration_pre_rename_failure_reports_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "referenced-pre-rename-work")
    calls = 0
    primary = OSError("injected pre-rename restoration probe failure")

    def become_referenced_after_retirement() -> set[Path]:
        nonlocal calls
        calls += 1
        return {stale.release} if calls >= 2 else set()

    monkeypatch.setattr(
        helper,
        "_restore_retired_release",
        lambda *args, **kwargs: (_ for _ in ()).throw(primary),
    )

    with pytest.raises(OSError, match="pre-rename restoration probe failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=become_referenced_after_retirement,
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    partial = getattr(primary, "gc_partial_result")
    retirement = next(path for path in stale.root.glob(".gc-retired-*") if path.is_dir())
    record = retirement.with_name(f"{retirement.name}.record")
    assert partial == {
        "deleted": [],
        "retained": [
            {
                "path": str(retirement),
                "reason": "restoration-failed: injected pre-rename restoration probe failure",
            },
            {
                "path": str(record),
                "reason": (
                    "record-retained-after-restoration-failure: "
                    "injected pre-rename restoration probe failure"
                ),
            },
        ],
    }


def test_gc_apply_reports_displaced_root_after_successful_restoration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    root = stale.root
    displaced = tmp_path / "displaced-release-root-reporting"
    original_fsync = helper._fsync_gc_root
    calls = 0

    def displace_during_post_retirement_fsync(
        capability, *, allow_moved: bool = False
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            root.rename(displaced)
            root.mkdir(mode=0o700)
        original_fsync(capability, allow_moved=allow_moved)

    monkeypatch.setattr(
        helper, "_fsync_gc_root", displace_during_post_retirement_fsync
    )

    with pytest.raises(RuntimeError, match="release root.*changed|release root.*moved") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    restored = displaced / stale.release.name
    assert restored.is_dir()
    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [],
        "retained": [
            {"path": str(restored), "reason": "restored-after-retirement-failure"}
        ],
    }



def test_gc_cli_attaches_completed_state_when_success_output_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(helper.sys, "platform", "darwin")
    completed = {
        "deleted": [str(tmp_path / "deleted-release")],
        "retained": [],
    }
    primary = OSError("injected success output failure")

    def successful_gc(*args, **kwargs):
        return completed

    def fail_output(*args, **kwargs):
        assert kwargs["flush"] is True
        raise primary

    monkeypatch.setattr(helper, "apply_release_gc", successful_gc)
    monkeypatch.setattr(helper, "print", fail_output, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout), "--apply"],
    )

    with pytest.raises(OSError, match="success output failure") as raised:
        helper.main()

    assert raised.value is primary
    assert getattr(raised.value, "gc_partial_result") is completed
    assert not (hermes_home / "state/hermes-kanban-update.lock").exists()


def test_gc_cli_reports_completed_state_when_lock_release_fails_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(helper.sys, "platform", "darwin")
    completed = {
        "deleted": [str(tmp_path / "deleted-release")],
        "retained": [],
    }
    original_run = helper.subprocess.run

    def successful_gc(*args, **kwargs):
        return completed

    def fail_lock_release(command, **kwargs):
        if "lock-release" in command:
            raise subprocess.CalledProcessError(71, command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(helper, "apply_release_gc", successful_gc)
    monkeypatch.setattr(helper.subprocess, "run", fail_lock_release)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout), "--apply"],
    )

    with pytest.raises(subprocess.CalledProcessError) as raised:
        helper.main()

    assert raised.value.returncode == 71
    assert json.loads(capsys.readouterr().out) == {
        "deleted": completed["deleted"],
        "error": {
            "message": str(raised.value),
            "type": "CalledProcessError",
        },
        "retained": [],
        "status": "partial",
    }
    assert (hermes_home / "state/hermes-kanban-update.lock").is_symlink()



def test_gc_cli_preserves_failure_when_partial_output_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(helper.sys, "platform", "darwin")
    primary = RuntimeError("primary GC failure")
    setattr(
        primary,
        "gc_partial_result",
        {"deleted": [str(tmp_path / "deleted-release")], "retained": []},
    )

    def fail_gc(*args, **kwargs):
        raise primary

    def fail_output(*args, **kwargs):
        raise OSError("secondary partial output failure")

    monkeypatch.setattr(helper, "apply_release_gc", fail_gc)
    monkeypatch.setattr(helper, "print", fail_output, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout), "--apply"],
    )

    with pytest.raises(RuntimeError, match="primary GC failure") as raised:
        helper.main()

    assert raised.value is primary
    assert any(
        "secondary partial output failure" in note
        for note in raised.value.__notes__
    )


def test_gc_cli_preserves_lock_release_failure_when_partial_output_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(helper.sys, "platform", "darwin")
    completed = {
        "deleted": [str(tmp_path / "deleted-release")],
        "retained": [],
    }
    original_run = helper.subprocess.run

    def successful_gc(*args, **kwargs):
        return completed

    def fail_lock_release(command, **kwargs):
        if "lock-release" in command:
            raise subprocess.CalledProcessError(71, command)
        return original_run(command, **kwargs)

    def fail_output(*args, **kwargs):
        raise OSError("secondary partial output failure")

    monkeypatch.setattr(helper, "apply_release_gc", successful_gc)
    monkeypatch.setattr(helper.subprocess, "run", fail_lock_release)
    monkeypatch.setattr(helper, "print", fail_output, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes-release-manager.py", "gc", str(checkout), "--apply"],
    )

    with pytest.raises(subprocess.CalledProcessError) as raised:
        helper.main()

    assert raised.value.returncode == 71
    assert any(
        "secondary partial output failure" in note
        for note in raised.value.__notes__
    )



def test_gc_apply_reports_retirement_when_rmtree_fails_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent"
    root.mkdir()
    stale = build_completed_release(helper, root, tmp_path / "stale-work")
    primary = OSError("primary rmtree failure")

    def fail_rmtree(path, *, dir_fd=None):
        raise primary

    monkeypatch.setattr(helper.shutil, "rmtree", fail_rmtree)

    with pytest.raises(OSError, match="primary rmtree failure") as raised:
        helper.apply_release_gc(
            root,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    records = list(stale.root.glob(".*.record"))
    assert len(records) == 1
    assert len(partial["retained"]) == 2
    retirement = partial["retained"][0]
    assert Path(retirement["path"]).name.startswith(".gc-retired-")
    assert retirement["reason"] == "deletion-failed: primary rmtree failure"
    assert Path(retirement["path"]).is_dir()
    assert partial["retained"][1] == {
        "path": str(records[0]),
        "reason": "record-retained-after-deletion-failure: primary rmtree failure",
    }
    assert not stale.release.exists()


def test_gc_record_publication_fsync_preserves_indeterminate_cleanup_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-wrapped-cleanup-work")
    primary = RuntimeError("primary retirement record publication fsync failure")
    cleanup = OSError("secondary post-unlink cleanup fsync failure")
    original_fsync = helper._fsync_gc_root
    calls = 0

    def fail_publication_and_cleanup_fsync(capability, *, allow_moved=False) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise primary
        if calls == 2:
            raise cleanup
        original_fsync(capability, allow_moved=allow_moved)

    monkeypatch.setattr(helper, "_fsync_gc_root", fail_publication_and_cleanup_fsync)

    with pytest.raises(RuntimeError, match="record publication fsync failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    assert any(
        "secondary post-unlink cleanup fsync failure" in note
        for note in raised.value.__notes__
    )
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    assert len(partial["retained"]) == 1
    retained = partial["retained"][0]
    assert retained["path"].endswith(".record")
    assert retained["reason"] == (
        "indeterminate-record-cleanup-durability: "
        "secondary post-unlink cleanup fsync failure"
    )
    assert not Path(retained["path"]).exists()
    assert stale.release.is_dir()


def test_gc_record_fsync_preserves_primary_when_record_cleanup_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "hermes-agent"
    root.mkdir()
    stale = build_completed_release(helper, root, tmp_path / "stale-work")
    primary = RuntimeError("primary retirement record fsync failure")
    cleanup = OSError("secondary retirement record cleanup failure")
    original_fsync = helper._fsync_gc_root
    calls = 0

    def fail_first_fsync(capability, *, allow_moved=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise primary
        return original_fsync(capability, allow_moved=allow_moved)

    def fail_record_cleanup(capability, record_name, *, allow_moved=False):
        raise cleanup

    monkeypatch.setattr(helper, "_fsync_gc_root", fail_first_fsync)
    monkeypatch.setattr(helper, "_remove_gc_retirement_record", fail_record_cleanup)

    with pytest.raises(RuntimeError, match="primary retirement record fsync failure") as raised:
        helper.apply_release_gc(
            root,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    assert any(
        "secondary retirement record cleanup failure" in note
        for note in raised.value.__notes__
    )
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    assert len(partial["retained"]) == 1
    assert partial["retained"][0]["path"].endswith(".record")
    assert partial["retained"][0]["reason"].startswith(
        "record-cleanup-failed-after-publication: "
    )
    assert stale.release.is_dir()
    assert list(stale.root.glob(".*.record"))
    assert not [path for path in stale.root.glob(".gc-retired-*") if path.is_dir()]



def test_gc_apply_reports_completed_state_when_capability_close_fails_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_open_root = helper._open_gc_root
    original_close = helper.os.close
    capability_fd = None

    def capture_root(path):
        nonlocal capability_fd
        capability = original_open_root(path)
        capability_fd = capability.descriptor
        return capability

    def fail_capability_close(descriptor):
        if descriptor == capability_fd:
            raise OSError("injected successful GC capability close failure")
        return original_close(descriptor)

    monkeypatch.setattr(helper, "_open_gc_root", capture_root)
    monkeypatch.setattr(helper.os, "close", fail_capability_close)

    with pytest.raises(OSError, match="successful GC capability close failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [str(stale.release)],
        "retained": [],
    }
    assert not stale.release.exists()
    assert capability_fd is not None
    original_close(capability_fd)



def test_gc_open_root_preserves_validation_failure_when_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    original_open = helper.os.open
    original_close = helper.os.close
    original_fstat = helper.os.fstat
    root_fd = None
    primary = RuntimeError("primary root validation failure")

    def capture_open(path, flags, *args, **kwargs):
        nonlocal root_fd
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == root:
            root_fd = descriptor
        return descriptor

    def fail_validation(descriptor):
        if descriptor == root_fd:
            raise primary
        return original_fstat(descriptor)

    def fail_close(descriptor):
        if descriptor == root_fd:
            raise OSError("secondary root descriptor close failure")
        return original_close(descriptor)

    monkeypatch.setattr(helper.os, "open", capture_open)
    monkeypatch.setattr(helper.os, "fstat", fail_validation)
    monkeypatch.setattr(helper.os, "close", fail_close)

    with pytest.raises(RuntimeError, match="primary root validation failure") as raised:
        helper._open_gc_root(root)

    assert raised.value is primary
    assert any(
        "secondary root descriptor close failure" in note
        for note in raised.value.__notes__
    )
    assert root_fd is not None
    original_close(root_fd)


def test_gc_record_close_failure_reports_retained_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    original_open = helper.os.open
    original_close = helper.os.close
    record_fd = None

    def capture_open(path, flags, *args, **kwargs):
        nonlocal record_fd
        descriptor = original_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and path.endswith(".record"):
            record_fd = descriptor
        return descriptor

    def fail_record_close(descriptor):
        if descriptor == record_fd:
            raise OSError("injected retirement record close failure")
        return original_close(descriptor)

    monkeypatch.setattr(helper.os, "open", capture_open)
    monkeypatch.setattr(helper.os, "close", fail_record_close)

    with pytest.raises(OSError, match="retirement record close failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    assert len(partial["retained"]) == 1
    retained = partial["retained"][0]
    assert retained["path"].endswith(".record")
    assert retained["reason"].startswith(
        "record-publication-failed: injected retirement record close failure"
    )
    assert Path(retained["path"]).is_file()
    assert stale.release.is_dir()
    assert not [path for path in stale.root.glob(".gc-retired-*") if path.is_dir()]
    assert record_fd is not None
    original_close(record_fd)



def test_gc_cleanup_does_not_report_indeterminate_record_after_fsync_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "durable-cleanup-move-work")
    root = stale.root
    displaced = tmp_path / "displaced-after-record-cleanup-fsync"
    original_fsync = helper.os.fsync
    calls = 0

    def move_root_after_cleanup_fsync(descriptor: int) -> None:
        nonlocal calls
        original_fsync(descriptor)
        calls += 1
        if calls == 5:
            root.rename(displaced)
            root.mkdir(mode=0o700)

    monkeypatch.setattr(helper.os, "fsync", move_root_after_cleanup_fsync)

    with pytest.raises(RuntimeError, match="release root moved during GC") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert getattr(raised.value, "gc_partial_result") == {
        "deleted": [str(stale.release)],
        "retained": [],
    }
    assert not list(displaced.glob(".*.record"))
    assert not (displaced / stale.release.name).exists()


def test_gc_apply_reports_retirement_when_deletion_root_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "deletion-fsync-work")
    original_fsync = helper._fsync_gc_root
    primary = OSError("injected deletion root fsync failure")
    calls = 0

    def fail_deletion_fsync(capability, *, allow_moved=False) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise primary
        original_fsync(capability, allow_moved=allow_moved)

    monkeypatch.setattr(helper, "_fsync_gc_root", fail_deletion_fsync)

    with pytest.raises(OSError, match="deletion root fsync failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == [str(stale.release)]
    record = next(stale.root.glob(".*.record"))
    retirement = record.with_name(record.name.removesuffix(".record"))
    assert partial["retained"] == [
        {
            "path": str(retirement),
            "reason": (
                "indeterminate-retirement-after-deletion-durability-failure: "
                "injected deletion root fsync failure"
            ),
        },
        {
            "path": str(record),
            "reason": (
                "record-retained-after-deletion-durability-failure: "
                "injected deletion root fsync failure"
            ),
        },
    ]
    assert not retirement.exists()
    assert record.is_file()


def test_gc_apply_reports_indeterminate_record_when_cleanup_fsync_fails_after_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-cleanup-fsync-work")
    original_fsync = helper._fsync_gc_root
    primary = OSError("injected post-unlink record fsync failure")
    calls = 0

    def fail_record_cleanup_fsync(capability, *, allow_moved=False) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise primary
        original_fsync(capability, allow_moved=allow_moved)

    monkeypatch.setattr(helper, "_fsync_gc_root", fail_record_cleanup_fsync)

    with pytest.raises(OSError, match="post-unlink record fsync failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert raised.value is primary
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == [str(stale.release)]
    assert len(partial["retained"]) == 1
    retained = partial["retained"][0]
    assert retained["path"].endswith(".record")
    assert retained["reason"] == (
        "indeterminate-record-cleanup-durability: "
        "injected post-unlink record fsync failure"
    )
    assert not Path(retained["path"]).exists()
    assert not stale.release.exists()


def test_gc_apply_reports_record_when_cleanup_fails_after_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    cleanup = OSError("injected post-deletion record cleanup failure")

    def fail_cleanup(capability, record_name, *, allow_moved=False):
        raise cleanup

    monkeypatch.setattr(helper, "_remove_gc_retirement_record", fail_cleanup)

    with pytest.raises(OSError, match="post-deletion record cleanup failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    assert raised.value is cleanup
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == [str(stale.release)]
    assert len(partial["retained"]) == 1
    assert partial["retained"][0]["path"].endswith(".record")
    assert partial["retained"][0]["reason"].startswith(
        "record-cleanup-failed-after-deletion: "
    )
    assert Path(partial["retained"][0]["path"]).is_file()


def test_gc_apply_uses_last_verified_retirement_path_when_current_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    primary = OSError("primary retained retirement failure")
    original_root_path = helper._gc_root_path
    deletion_failed = False

    def fail_rmtree(path, *, dir_fd=None):
        nonlocal deletion_failed
        deletion_failed = True
        raise primary

    def fail_current_lookup(capability, *, allow_moved=False):
        if deletion_failed and allow_moved:
            raise RuntimeError("secondary current path lookup failure")
        return original_root_path(capability, allow_moved=allow_moved)

    monkeypatch.setattr(helper.shutil, "rmtree", fail_rmtree)
    monkeypatch.setattr(helper, "_gc_root_path", fail_current_lookup)

    with pytest.raises(OSError, match="primary retained retirement failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=lambda: None,
        )

    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    assert len(partial["retained"]) == 2
    retirement, record = partial["retained"]
    assert Path(retirement["path"]).is_dir()
    assert retirement["reason"] == "deletion-failed: primary retained retirement failure"
    assert Path(record["path"]).is_file()
    assert record["reason"] == (
        "record-retained-after-deletion-failure: primary retained retirement failure"
    )
    assert any(
        "secondary current path lookup failure" in note
        for note in raised.value.__notes__
    )



def test_gc_compensation_probe_and_path_failure_reports_last_verified_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    primary = RuntimeError("primary pre-deletion lock failure")
    original_stat = helper.os.stat
    original_root_path = helper._gc_root_path
    lock_calls = 0
    primary_raised = False
    compensation_probe_failed = False

    def fail_second_lock():
        nonlocal lock_calls, primary_raised
        lock_calls += 1
        if lock_calls == 4:
            primary_raised = True
            raise primary

    def fail_compensation_probe(path, *args, **kwargs):
        nonlocal compensation_probe_failed
        if (
            primary_raised
            and isinstance(path, str)
            and path.startswith(".gc-retired-")
        ):
            compensation_probe_failed = True
            raise OSError("secondary compensation state probe failure")
        return original_stat(path, *args, **kwargs)

    def fail_record_path(capability, *, allow_moved=False):
        if compensation_probe_failed and allow_moved:
            raise RuntimeError("secondary retained record path lookup failure")
        return original_root_path(capability, allow_moved=allow_moved)

    monkeypatch.setattr(helper.os, "stat", fail_compensation_probe)
    monkeypatch.setattr(helper, "_gc_root_path", fail_record_path)

    with pytest.raises(RuntimeError, match="primary pre-deletion lock failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=fail_second_lock,
        )

    assert raised.value is primary
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    retirement = next(path for path in stale.root.glob(".gc-retired-*") if path.is_dir())
    record = retirement.with_name(f"{retirement.name}.record")
    assert partial["retained"] == [
        {
            "path": str(retirement),
            "reason": (
                "indeterminate-retirement-state-after-compensation-probe-failure: "
                "secondary compensation state probe failure"
            ),
        },
        {
            "path": str(record),
            "reason": (
                "indeterminate-deletion-state: "
                "secondary compensation state probe failure"
            ),
        },
    ]
    assert record.is_file()
    assert any(
        "compensation state probe" in note for note in raised.value.__notes__
    )
    assert any(
        "retained artifact path lookup" in note for note in raised.value.__notes__
    )
    assert not stale.release.exists()
    assert any(path.is_dir() for path in stale.root.glob(".gc-retired-*"))



def test_gc_pre_retirement_failure_reports_surviving_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    stale = build_completed_release(helper, checkout, tmp_path / "stale-work")
    primary = RuntimeError("primary pre-retirement lock failure")
    cleanup = OSError("secondary pre-retirement record cleanup failure")
    lock_calls = 0

    def fail_pre_retirement_lock():
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 3:
            raise primary

    def fail_cleanup(capability, record_name, *, allow_moved=False):
        raise cleanup

    monkeypatch.setattr(helper, "_remove_gc_retirement_record", fail_cleanup)

    with pytest.raises(RuntimeError, match="primary pre-retirement lock failure") as raised:
        helper.apply_release_gc(
            checkout,
            runtime_reference_supplier=lambda: set(),
            lock_verifier=fail_pre_retirement_lock,
        )

    assert raised.value is primary
    assert any(
        "secondary pre-retirement record cleanup failure" in note
        for note in raised.value.__notes__
    )
    partial = getattr(raised.value, "gc_partial_result")
    assert partial["deleted"] == []
    assert len(partial["retained"]) == 1
    retained = partial["retained"][0]
    assert retained["path"].endswith(".record")
    assert retained["reason"].startswith(
        "record-cleanup-failed-before-retirement: "
    )
    assert Path(retained["path"]).is_file()
    assert stale.release.is_dir()
