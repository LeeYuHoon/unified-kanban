"""불변 Hermes release 네임스페이스를 위한 하나의 공유 정규형(normal form).

setup, uninstall, updater, release 관리자, 그리고 설치된 런타임 게이트는
모두 정확히 동일한 release 루트, selector, release 디렉터리를 가리켜야
한다. 설정된 checkout에 후행 슬래시, 중복 구분자, 또는 ``/./`` 구성
요소가 있는 순간 셸 문자열 연결과 :meth:`Path.with_name`은 어긋난다:
셸은 ``.releases``를 Hermes checkout *안에* 두는 반면 모든 Python
호출자는 형제(sibling) 경로를 계속 사용하므로, setup은 setup이 쓴 적
없는 selector를 읽는 런처를 설치하게 된다. 따라서 호출자는 먼저 이
모듈을 통해 정규화하며, 그 정규형은 셸이 ``.releases``를 그대로 이어
붙여도 되는 바로 그 문자열이다.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

COMPLETION_RECEIPT_NAME = ".unified-kanban-release.json"
SELECTOR_NAME = "current"
PREVIOUS_SELECTOR_NAME = "previous"
RELEASE_ROOT_SUFFIX = ".releases"
_CARRIED_RE = re.compile(r"[0-9a-f]{40}\Z")


def normalize_agent_repo(value: str | os.PathLike[str]) -> Path:
    """모든 호출자가 경로 유도의 기준으로 삼아야 하는 단일 절대 경로 표기를 반환한다.

    ``pathlib``은 ``//``, ``/./``, 후행 구분자를 이미 정규형으로
    접는다(fold). ``..``은 접히지 않으며, 여기서 이를 어휘적으로(lexically)
    접으면 구성 요소가 심볼릭 링크일 때마다 커널이 해석하는 것과 다른
    디렉터리를 가리키게 되므로, 순회(traversal) 구성 요소는 대신 fail
    closed 한다.
    """
    path = Path(os.fsdecode(value))
    if not path.is_absolute():
        raise ValueError(f"HERMES_AGENT_REPO must be an absolute path: {value!r}")
    if ".." in path.parts:
        raise ValueError(
            f"HERMES_AGENT_REPO must not contain a '..' component: {value!r}"
        )
    if not path.name:
        raise ValueError(f"HERMES_AGENT_REPO must be an absolute non-root path: {value!r}")
    return path


def release_root(agent_repo: str | os.PathLike[str]) -> Path:
    """checkout 옆에 - 절대 안이 아니라 - 있는 private release 루트를 반환한다."""
    repo = normalize_agent_repo(agent_repo)
    return repo.with_name(f"{repo.name}{RELEASE_ROOT_SUFFIX}")


def release_selector(agent_repo: str | os.PathLike[str]) -> Path:
    """사용 중인 release를 가리키는 유일한 일반 파일을 반환한다."""
    return release_root(agent_repo) / SELECTOR_NAME


def previous_release_selector(agent_repo: str | os.PathLike[str]) -> Path:
    """직전의 정상 확인(known-good) release를 가리키는 내구성 있는 일반 파일 참조를 반환한다."""
    return release_root(agent_repo) / PREVIOUS_SELECTOR_NAME


def release_directory(agent_repo: str | os.PathLike[str], carried: str) -> Path:
    """리뷰된 carried tip 하나에 대한 불변 release 디렉터리를 반환한다."""
    if _CARRIED_RE.fullmatch(carried) is None:
        raise ValueError("carried commit must be a full lowercase SHA-1")
    return release_root(agent_repo) / f"release-{carried}"


def main() -> int:
    """셸 호출자가 동일한 경로를 유도하도록 정규형을 출력한다."""
    if len(sys.argv) != 2:
        print("usage: python3 -m kanban_adapter.release_layout AGENT_REPO", file=sys.stderr)
        return 2
    try:
        print(normalize_agent_repo(sys.argv[1]))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
