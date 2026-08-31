"""실제로 검토된 업스트림과 번들을 대상으로 하는 릴리스 생산 종단 간 테스트.

다른 모든 릴리스 테스트는 합성한 3개 커밋 저장소에서 빌드하므로 빠른 테스트 모음은
실제 업스트림 트리를 전혀 확인하지 않았다. 바로 그 때문에 업스트림에만 존재하는
대소문자 접기 충돌이 검토에서 걸러지지 않고 지원되는 macOS 볼륨의 기본 설치
경로를 망가뜨렸다.

대신 이 모듈은 실제 생산자를 구동한다. 실제 Hermes 객체 데이터베이스에서 제공한
검토된 핀, ``patches/``의 검토된 반입 번들, 실제 반입 번들 검증기, 실제 관리형
실행기를 사용한다. Hermes의 전체 의존성 트리를 해석하는 작업은 이 테스트의 입증
대상과 무관한 네트워크 다운로드이므로 의존성 설치기만 통제하며, 소스 체크아웃,
구체화, 영수증, 검증기, 실행기는 모두 배포되는 코드를 사용한다.

검토된 업스트림 커밋을 객체 데이터베이스에 보유한 Git 저장소를
``UNIFIED_KANBAN_TEST_HERMES_SOURCE``로 지정하여 선택적으로 실행한다. 예::

    git clone --bare --no-tags --single-branch https://github.com/NousResearch/hermes-agent.git /tmp/hermes-source.git
    UNIFIED_KANBAN_TEST_HERMES_SOURCE=/tmp/hermes-source.git uv run pytest tests/test_hermes_release_integration.py

``UNIFIED_KANBAN_TEST_HERMES_REQUIRED=1``을 설정하면 선택 해제에 따른 건너뛰기를
실패로 바꾸어, 릴리스 게이트가 이 파일을 조용히 건너뛰고 통과하지 못하게 한다.
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
import tomllib
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
    """이번 실행에서 통합 테스트가 반드시 수행되어야 한다고 선언하지 않았다면 건너뛴다."""
    if os.environ.get(REQUIRED_VARIABLE) == "1":
        pytest.fail(f"{REQUIRED_VARIABLE}=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def pinned_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """실제 객체 데이터베이스를 변경하지 않고 검토된 핀을 제공한다.

    생산자는 ``refs/heads/main``이 검토된 업스트림과 정확히 일치하지 않으면 빌드를
    거부하며 공식 브랜치는 이미 그 지점을 훨씬 지나갔다. ``alternates``를 통해
    호출자의 객체를 빌리면 정확히 그 참조 뷰를 제공하면서 누군가의 작업용 복제본일
    수도 있는 제공 저장소는 건드리지 않을 수 있다.
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
    """Hermes 체크아웃 경로를 제공하고 이후 릴리스 루트를 제거한다.

    실제 릴리스 하나는 약 0.75기가바이트이고 pytest는 최근 임시 트리를 보존하므로,
    여러 복사본을 남기는 대신 정리한다.
    """
    checkout = tmp_path / "hermes-agent"
    checkout.mkdir()
    try:
        yield checkout
    finally:
        shutil.rmtree(f"{checkout}.releases", ignore_errors=True)


def controlled_uv(tmp_path: Path) -> Path:
    """의존성 설치기만을 대체한다.

    잠금 동기화가 만드는 것과 같은 산출물, 즉 릴리스 로컬 경로
    ``venv/bin/hermes``에 실제 실행 파일을 만든다. 따라서 이후의 실행기, 선택자,
    실행 파일 게이트는 모두 실제로 검증된다.
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
    # 생산자는 옆에 릴리스를 빌드하는 동안 변경 가능한 Hermes 체크아웃을 절대 건드리면 안 된다.
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

    # 검토된 업스트림에는 실제로 접을 때 충돌하는 메타데이터 쌍이 있으므로 여기의
    # 빈 레코드는 업스트림이 정리됐다는 뜻이 아니라, 탐지기가 존재 목적인 바로 그
    # 대상을 더 이상 찾지 못한다는 뜻이다.
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

    # 예측된 정규화와 이 프로젝트 자체의 미추적 출력 외에는 생성된 작업 트리에
    # 어떤 것도 나타나서는 안 된다.
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

    # 생산된 릴리스를 대상으로 실제 반입 번들 검증기를 실행한다.
    verified = subprocess.run(
        [sys.executable, str(VERIFIER), "--hermes-repo", str(release)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert verified.stdout.startswith("CARRIED_BUNDLE_PASS ")

    # 관리형 실행기는 선택자를 통해서만 릴리스 로컬 실행 파일에 도달해야 한다.
    launcher = tmp_path / "hermes"
    layout.selector.write_bytes(helper.selector_payload(layout))
    launcher.write_bytes(helper.launcher_payload(layout, helper.BASELINE_ABSENT))
    launcher.chmod(0o755)
    # 모든 Hermes 턴과 어댑터 명령이 무엇이든 기록하기 전에 실행하는 게이트는
    # 영수증을 포함한 이 생산된 릴리스를 받아들여야 한다.
    assert check_selected_release(release_workspace, upstream, carried) == ""
    launched = subprocess.run(
        [str(launcher), "gateway"], capture_output=True, text=True, check=False
    )
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout == "HERMES-RELEASE-SMOKE:gateway\n"
    assert (
        helper.launcher_baseline(layout, launcher.read_bytes()) == helper.BASELINE_ABSENT
    )

    # 두 번째 실행은 영수증이 있는 릴리스를 다시 빌드하지 않고 재사용해야 하며,
    # 의존성 설치기가 전혀 필요하지 않아야 한다.
    identity = release.stat().st_ino
    assert helper.prepare_release(layout, uv=tmp_path / "absent-uv", **build) == release
    assert release.stat().st_ino == identity

    # 정규화 후 남은 파일을 다시 쓰면 재사용 검증이 실패해야 하며, 검토된 바이트를
    # 복원하면 릴리스를 다시 신뢰할 수 있어야 한다.
    survivor = release / collisions[0]["representative"]
    original = survivor.read_bytes()
    survivor.write_bytes(b"attacker\n")
    with pytest.raises(RuntimeError, match="case-fold collision"):
        helper.prepare_release(layout, uv=tmp_path / "absent-uv", **build)
    survivor.write_bytes(original)
    assert helper.prepare_release(layout, uv=tmp_path / "absent-uv", **build) == release


def test_documented_hermes_version_matches_reviewed_release(
    pinned_source: Path, tmp_path: Path
) -> None:
    """README에 기록한 버전이 실제 carried release와 같은지 확인한다."""
    _, carried_head = reviewed_pins()
    release = tmp_path / "documented-version.git"
    subprocess.run(
        ["git", "clone", "--bare", "--no-local", str(pinned_source), str(release)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(release), "bundle", "unbundle", str(BUNDLE)],
        check=True,
        capture_output=True,
    )
    project = subprocess.run(
        ["git", "-C", str(release), "show", f"{carried_head}:pyproject.toml"],
        check=True,
        capture_output=True,
    ).stdout
    actual = tomllib.loads(project.decode("utf-8"))["project"]["version"]
    documented = (REPO / "patches/hermes-agent-version").read_text(
        encoding="utf-8"
    ).strip()

    assert documented == actual


def test_reviewed_bundle_matches_its_recorded_integrity_metadata() -> None:
    """생산자의 입력이 배포본 그대로인지 네트워크 없이 검사한다."""
    verified = subprocess.run(
        [sys.executable, str(VERIFIER)], capture_output=True, text=True, check=False
    )

    assert verified.returncode == 0, verified.stderr
    assert read_supported_upstream() in verified.stdout
