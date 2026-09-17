"""기존 runtime 추가 grant의 미구현 계약과 reader 차단 경계를 재현한다.

의도적인 RED 인수 테스트다. 운영 명령이나 실제 authority를 실행하지 않는다.
임시 HOME의 기존 init만 사용하며 카드, transcript, observation 영수증은 만들지 않는다.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from kanban_adapter import conversation_runtime as runtime
from kanban_adapter import conversation_policy_cli as policy_cli
from test_conversation_owner_cli import setup, run_cli  # noqa: F401


@pytest.fixture
def existing(setup, monkeypatch):
    home, _, _, state, args = setup
    codex = home / "codex"
    codex.mkdir(mode=0o700)
    args += ["--provider-root", f"codex={codex}"]
    result = run_cli(args)
    assert result.returncode == 0, result.stderr
    # 새 provider 권한은 없지만 설치된 provider root는 그대로 존재한다.
    assert policy_cli.main([
        "--policy-file", str(state / "policy.json"),
        "--secret-file", str(state / "authority.key"),
        "disable", "--board", "test-board", "--provider", "codex",
    ]) == 0
    project = home / "project-two"
    project.mkdir(mode=0o700)
    board = home / "kanban" / "boards" / "project-two"
    board.mkdir(mode=0o700, parents=True)
    (board / "board.json").write_text(json.dumps({
        "slug": "project-two", "archived": False, "default_workdir": str(project),
    }))
    monkeypatch.setenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", str(state / "runtime.json"))
    monkeypatch.setattr(runtime, "_cached_path", None)
    monkeypatch.setattr(runtime, "_cached_service", None)
    return state


def test_grant_existing_dry_run_contract_is_missing(existing):
    state = existing
    raw = (state / "runtime.json").read_bytes()
    service = runtime._build(state / "runtime.json")
    assert service is not None
    policy = service.policies.load()
    before = {p.name: p.read_bytes() for p in state.iterdir() if p.is_file()}
    result = run_cli([
        "grant-existing", "--runtime-config", str(state / "runtime.json"),
        "--board", "project-two", "--provider", "claude",
        "--principal", "github:fixture-user", "--dry-run",
        "--expected-policy-generation", str(policy.generation),
        "--expected-config-sha256", hashlib.sha256(raw).hexdigest(),
    ])
    assert before == {p.name: p.read_bytes() for p in state.iterdir() if p.is_file()}
    # 이 인터페이스는 설계 후보이며 아직 지원된다고 문서화하지 않는다.
    assert result.returncode == 0, "grant-existing dry-run is not implemented"


def test_disabling_runtime_does_not_fence_an_already_cached_reader(existing):
    path = existing / "runtime.json"
    service = runtime.get_conversation_service()
    assert service is not None
    assert service.authorizes_principal("github:fixture-user", "test-board")
    payload = json.loads(path.read_bytes())
    payload["enabled"] = False
    # owner가 완전한 disabled config를 써도 이미 열린 reader에는 적용되지 않는다.
    path.write_text(json.dumps(payload))
    assert runtime._build(path) is None
    assert runtime.get_conversation_service() is None, (
        "cached reader ignores runtime disable; owner-only publication cannot prove quiescence"
    )


def test_policy_first_publication_exposes_new_pair_to_old_runtime_reader(existing):
    path = existing / "runtime.json"
    service = runtime.get_conversation_service()
    assert service is not None
    old = service.policies.load()
    assert old.activation_for("test-board", "codex") is None
    before = path.read_bytes()
    # 실제 정책 게시 경로까지만 실행한다. runtime 게시 전 중단 경계를 재현한다.
    assert policy_cli.main([
        "--policy-file", str(existing / "policy.json"),
        "--secret-file", str(existing / "authority.key"),
        "enable", "--board", "test-board", "--provider", "codex",
        "--expires-at-ns", str(old.expires_at_ns),
    ]) == 0
    current = service._enabled_policy("test-board", "codex")
    cutoff = current.activation_for("test-board", "codex")
    assert cutoff is not None
    assert path.read_bytes() == before
    assert current.activation_for("test-board", "claude") == old.activation_for("test-board", "claude")
    assert current.expires_at_ns == old.expires_at_ns
    # source를 열지 않고 실제 principal/policy gate의 합성 결과만 검사한다.
    exposed = service.authorizes_principal("github:fixture-user", "test-board") and current.allows(
        "test-board", "codex", current.minimum_binding_version, cutoff,
    )
    assert not exposed, (
        "policy-only publication opens a new pair before runtime commit; no shared reader fence"
    )
