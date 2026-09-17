"""임시 key와 합성 provider 기록으로 owner 준비 경로를 검증한다."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import time

import pytest

from kanban_adapter import conversation_runtime


@pytest.mark.parametrize("option,value", [
    ("--board", "../bad"), ("--board", "UPPER"),
    ("--principal", "github:"), ("--principal", "github:bad user"),
    ("--provider-root", "hermes=/nonexistent"), ("--provider-root", "claude=relative"),
    ("--expires-at-ns", "1"), ("--expires-at-ns", "9223372036854775808"),
    ("--state-root", "relative"), ("--kernel-secret-file", "/wrong-selected-key"),
])
def test_malformed_input_publishes_nothing(setup, option, value):
    _, _, _, state, args = setup
    args[args.index(option) + 1] = value
    assert run_cli(args).returncode != 0
    assert not state.exists()


@pytest.mark.parametrize("kind", ["missing", "mode", "hardlink", "symlink", "fifo", "short", "parent-link"])
def test_unsafe_kernel_rejected(setup, kind):
    home, kernel, _, state, args = setup
    if kind == "missing":
        kernel.unlink()
    elif kind == "mode":
        kernel.chmod(0o644)
    elif kind == "hardlink":
        os.link(kernel, home / "linked")
    elif kind == "short":
        kernel.write_bytes(b"short")
    elif kind == "parent-link":
        alias = home / "alias"
        alias.symlink_to(home, target_is_directory=True)
        path = alias / kernel.name
        args[args.index("--kernel-secret-file") + 1] = str(path)
        os.environ["HERMES_KANBAN_CONVERSATION_KERNEL_SECRET_FILE"] = str(path)
    else:
        kernel.unlink()
        if kind == "fifo":
            os.mkfifo(kernel, 0o600)
        else:
            kernel.symlink_to(home / "absent")
    assert run_cli(args).returncode != 0
    assert not state.exists()


def test_existing_state_is_never_overwritten(setup):
    _, _, _, state, args = setup
    assert run_cli(args).returncode == 0
    before = {p.name: p.read_bytes() for p in state.iterdir() if p.is_file()}
    assert run_cli(args).returncode != 0
    assert before == {p.name: p.read_bytes() for p in state.iterdir() if p.is_file()}


def test_profile_default_selects_existing_key(setup, monkeypatch):
    home, kernel, _, _, args = setup
    journal = home / "conversation-journal"
    journal.mkdir(mode=0o700)
    target = journal / "kernel-receipt.key"
    kernel.rename(target)
    monkeypatch.delenv("HERMES_KANBAN_CONVERSATION_KERNEL_SECRET_FILE")
    args[args.index("--kernel-secret-file") + 1] = str(target)
    assert run_cli(args).returncode == 0


@pytest.mark.parametrize("failure", ["authority.key", "policy.json", "runtime.json", "committed"])
def test_publication_failure_has_no_usable_config(setup, monkeypatch, failure):
    from kanban_adapter import conversation_owner_cli as cli
    _, _, _, state, args = setup
    original = cli.private.atomic_publish
    def publish(path, *a, **kw):
        if path.name == failure:
            raise OSError("injected publication failure")
        receipt = original(path, *a, **kw)
        if failure == "committed" and path.name == "runtime.json":
            raise cli.private.CommittedPublicationError("injected durability failure", receipt)
        return receipt
    monkeypatch.setattr(cli.private, "atomic_publish", publish)
    # 정책 모듈이 import한 동일 helper도 장애 주입 대상으로 지정한다.
    monkeypatch.setattr("kanban_adapter.conversation_integration.atomic_publish", publish)
    assert cli.main(args) == 1
    if (state / "runtime.json").exists():
        with pytest.raises((PermissionError, ValueError)):
            conversation_runtime._build(state / "runtime.json")


def test_excess_principals_fail_before_publication(setup):
    _, _, _, state, args = setup
    args += [value for index in range(129) for value in ("--principal", f"github:user-{index}")]
    assert run_cli(args).returncode != 0
    assert not state.exists()


@pytest.mark.parametrize("target", ["state", "state-parent", "provider"])
def test_directory_symlinks_rejected(setup, target):
    home, _, provider, state, args = setup
    alias = home / "alias"
    alias.symlink_to(provider if target == "provider" else home, target_is_directory=True)
    if target == "provider":
        args[args.index("--provider-root") + 1] = f"claude={alias}"
    elif target == "state-parent":
        args[args.index("--state-root") + 1] = str(alias / "owner")
    else:
        state.symlink_to(provider, target_is_directory=True)
    assert run_cli(args).returncode != 0
    assert not (state / "runtime.json").exists()


@pytest.mark.parametrize("user_id", [
    "nas_user:00000000-0000-4000-8000-000000000001",
    "a" + ":" + "b" * 189,
])
def test_namespaced_principal_exact_grant_and_receipt(setup, user_id):
    _, _, _, state, args = setup
    principal = "nous:" + user_id
    args[args.index("--principal") + 1] = principal
    result = run_cli(args)
    assert result.returncode == 0, result.stderr
    config = json.loads((state / "runtime.json").read_text())
    assert config["principal_board_grants"] == {principal: ["test-board"]}
    service = conversation_runtime._build(state / "runtime.json")
    assert service is not None
    assert service.authorizes_principal(principal, "test-board")
    receipt = service.authority.issue_principal_scope(
        principal=principal, board="test-board", task="task",
        expires_at_ns=time.time_ns() + 60_000_000_000,
    )
    for alternate in ("nous:" + user_id.replace(":", ""),
                      "nous:" + user_id.replace(":", "_"),
                      "github:" + user_id):
        assert not service.authorizes_principal(alternate, "test-board")
        other = service.authority.issue_principal_scope(
            principal=alternate, board="test-board", task="task",
            expires_at_ns=time.time_ns() + 60_000_000_000,
        )
        assert receipt.principal_ref != other.principal_ref


@pytest.mark.parametrize("principal", [
    "nous:", "nous::user", "nous:nas_user:bad user", "nous:nas_user:bad\n",
    "nous:nas_user:../user", "nous:nas_user:..\\user", "nous:nas_user:$(id)",
    "nous:nas_user:user;id", "nous:nas_user:<script>", "nous:" + "a" * 192,
])
def test_unsafe_or_oversized_namespaced_principal_publishes_nothing(setup, principal):
    _, _, _, state, args = setup
    args[args.index("--principal") + 1] = principal
    assert run_cli(args).returncode != 0
    assert not state.exists()


def test_two_explicit_roots_and_principals(setup):
    home, _, _, state, args = setup
    codex = home / "codex"
    codex.mkdir(mode=0o700)
    args += ["--provider-root", f"codex={codex}", "--principal", "google:fixture-user"]
    assert run_cli(args).returncode == 0
    service = conversation_runtime._build(state / "runtime.json")
    assert service is not None
    assert set(service.provider_roots) == {"claude", "codex"}
    assert service.authorizes_principal("google:fixture-user", "test-board")
    assert not service.authorizes_principal("google:fixture-user", "other")


def test_readme_documents_preparation_without_activation():
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    assert "kanban_adapter.conversation_owner_cli init" in readme
    assert "환경 변수나 launchd" in readme
    assert "합성" in readme
    assert "`/var` 대신 `/private/var`" in readme
    assert "UNIFIED_KANBAN_TEST_HERMES_SOURCE" in readme
    assert "issue_observation_receipt → pipe → capture/seal → projection" in readme


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", raising=False)
    kernel = home / "kernel.key"
    kernel.write_bytes(secrets.token_bytes(32))
    kernel.chmod(0o600)
    monkeypatch.setenv("HERMES_KANBAN_CONVERSATION_KERNEL_SECRET_FILE", str(kernel))
    provider = home / "claude"
    provider.mkdir(mode=0o700)
    state = home / "owner"
    args = ["init", "--state-root", str(state), "--kernel-secret-file", str(kernel),
            "--board", "test-board", "--principal", "github:fixture-user",
            "--provider-root", f"claude={provider}", "--expires-at-ns", str(time.time_ns() + 60_000_000_000)]
    return home, kernel, provider, state, args


def run_cli(args):
    env = {name: os.environ[name] for name in (
        "HOME", "HERMES_HOME", "HERMES_KANBAN_CONVERSATION_KERNEL_SECRET_FILE",
    ) if name in os.environ}
    env.update(PYTHONPATH=str(Path(__file__).parents[1] / "src"),
               PYTHONDONTWRITEBYTECODE="1", PATH="/usr/bin:/bin", LANG="C.UTF-8")
    return subprocess.run([sys.executable, "-m", "kanban_adapter.conversation_owner_cli", *args],
                          env=env, capture_output=True, text=True, timeout=10)


def test_cli_config_builds_and_synthetic_native_receipt_projects(setup, monkeypatch):
    home, kernel, provider, state, args = setup
    before = dict(os.environ)
    result = run_cli(args)
    assert result.returncode == 0, result.stderr
    assert dict(os.environ) == before
    config = json.loads((state / "runtime.json").read_text())
    assert config["kernel_receipt"]["issuer_id"] == "hermes-kanban-kernel"
    assert config["kernel_receipt"]["key_id"] == hashlib.sha256(kernel.read_bytes()).hexdigest()[:24]
    assert len(Path(config["authority_secret_file"]).read_bytes()) == 32
    for path in state.rglob("*"):
        info = path.stat()
        assert info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == (0o700 if path.is_dir() else 0o600)
        if path.is_file():
            assert info.st_nlink == 1
    service = conversation_runtime._build(state / "runtime.json")
    assert service is not None
    policy = service.policies.load()
    assert policy.generation == 2
    assert policy.enabled_boards == {"test-board": frozenset({"claude"})}
    assert policy.activated_at_ns < policy.expires_at_ns
    monkeypatch.setattr(service, "task_membership", lambda board, task: (board, task) == ("test-board", "task"))
    source = provider / "fixture.jsonl"
    source.write_text(json.dumps({"type": "system", "sessionId": "session"}) + "\n")
    source.chmod(0o600)
    receipt = {"schema": "hermes-kanban-observation-receipt-v1", "issuer": "hermes-kanban-kernel",
               "key_id": hashlib.sha256(kernel.read_bytes()).hexdigest()[:24], "board": "test-board",
               "task": "task", "observation": True, "created_at_ns": time.time_ns(), "nonce": secrets.token_hex(16)}
    wire = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    receipt["auth_tag"] = hmac.new(kernel.read_bytes(), b"observation-receipt\0" + wire, hashlib.sha256).hexdigest()
    kwargs = dict(board="test-board", task="task", provider="claude", session="session", source_path=source)
    prepared = service.capture_hook_start(**kwargs, task_receipt=receipt)
    with source.open("a") as stream:
        stream.write(json.dumps({"type": "user", "sessionId": "session", "message": {"role": "user", "content": "synthetic visible"}}) + "\n")
    assert service.seal_hook_binding(board="test-board", task="task", prepared=prepared, task_receipt=receipt)
    page = service.get_parent_page(principal_id="github:fixture-user", board="test-board", task="task", cursor=None, limit=10)
    assert "synthetic visible" in json.dumps(page)
    with pytest.raises(PermissionError):
        service.get_parent_page(principal_id="github:other", board="test-board", task="task", cursor=None, limit=10)
    with pytest.raises(PermissionError):
        service.capture_hook_start(**kwargs, task_receipt={**receipt, "auth_tag": "0" * 64})
    original_kernel = kernel.read_bytes()
    kernel.write_bytes(secrets.token_bytes(32))
    with pytest.raises(PermissionError):
        conversation_runtime._build(state / "runtime.json")
    kernel.write_bytes(original_kernel)
    config["kernel_receipt"]["auth_tag"] = "kr_" + "0" * 64
    (state / "runtime.json").write_text(json.dumps(config))
    with pytest.raises(PermissionError):
        conversation_runtime._build(state / "runtime.json")


def test_owner_directory_fstat_failure_closes_fd(setup, monkeypatch):
    from kanban_adapter import conversation_owner_cli as cli
    home, _, _, _, _ = setup
    before = set(os.listdir("/dev/fd"))
    def fail(fd):
        raise OSError("injected fstat")
    with monkeypatch.context() as patch:
        patch.setattr(cli.os, "fstat", fail)
        with pytest.raises(OSError, match="injected fstat"):
            cli._owner_directory(home)
    assert set(os.listdir("/dev/fd")) == before


def test_kernel_dup_failure_closes_fd(setup, monkeypatch):
    from kanban_adapter import conversation_owner_cli as cli
    _, kernel, _, _, _ = setup
    before = set(os.listdir("/dev/fd"))
    def fail(fd):
        raise OSError("injected dup")
    with monkeypatch.context() as patch:
        patch.setattr(cli.os, "dup", fail)
        with pytest.raises(OSError, match="injected dup"):
            cli._kernel(kernel)
    assert set(os.listdir("/dev/fd")) == before


@pytest.mark.parametrize("cleanup", ["ftruncate", "fsync"])
def test_committed_failure_preserves_primary_and_cleanup_note(setup, monkeypatch, capsys, cleanup):
    from kanban_adapter import conversation_owner_cli as cli
    _, _, _, _, args = setup
    original = cli.private.atomic_publish
    receipts = []
    def fail_cleanup(*a):
        raise OSError("injected cleanup failure")
    def publish(path, *a, **kw):
        receipt = original(path, *a, **kw)
        if path.name == "runtime.json":
            receipts.append(receipt)
            monkeypatch.setattr(cli.os, cleanup, fail_cleanup)
            raise cli.private.CommittedPublicationError("injected primary publication failure", receipt)
        return receipt
    monkeypatch.setattr(cli.private, "atomic_publish", publish)
    before = set(os.listdir("/dev/fd"))
    assert cli.main(args) == 1
    stderr = capsys.readouterr().err
    assert "injected primary publication failure" in stderr
    assert "injected cleanup failure" in stderr
    assert receipts[0]._closed
    assert set(os.listdir("/dev/fd")) == before


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin system alias")
def test_provider_system_alias_rejected_before_publication(setup):
    _, _, provider, state, args = setup
    assert str(provider).startswith("/private/var/")
    args[args.index("--provider-root") + 1] = "claude=" + str(provider)[len("/private"):]
    assert run_cli(args).returncode != 0
    assert not state.exists()


@pytest.mark.integration
def test_real_native_receipt_pipe_capture_seal_projection(setup, monkeypatch):
    """실제 네이티브 서명기와 파이프를 사용하되 멤버십과 기록은 가짜이며 카드·추론 E2E가 아니다."""
    native_source = os.environ.get("UNIFIED_KANBAN_TEST_HERMES_SOURCE")
    if not native_source:
        pytest.skip("opt in with UNIFIED_KANBAN_TEST_HERMES_SOURCE")
    native_source = Path(native_source)
    assert native_source.is_absolute()
    assert (native_source / "hermes_cli/kanban_conversation_receipt.py").is_file()
    home, kernel, provider, state, args = setup
    assert run_cli(args).returncode == 0
    service = conversation_runtime._build(state / "runtime.json")
    assert service is not None
    monkeypatch.setattr(service, "task_membership", lambda board, task: (board, task) == ("test-board", "fixture-task"))
    read_fd, write_fd = os.pipe()
    try:
        env = {
            "HOME": str(home), "HERMES_HOME": str(home),
            "HERMES_KANBAN_CONVERSATION_KERNEL_SECRET_FILE": str(kernel),
            "PYTHONPATH": str(native_source), "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
        }
        code = """
import sys, time
from pathlib import Path
import hermes_cli.kanban_conversation_receipt as native
assert Path(native.__file__).parent.parent == Path(sys.argv[3])
native.issue_observation_receipt(secret_file=Path(sys.argv[1]), output_fd=int(sys.argv[2]),
    board="test-board", task="fixture-task", created_at_ns=time.time_ns())
print("REAL_NATIVE_RECEIPT_ISSUED")
"""
        result = subprocess.run([sys.executable, "-B", "-c", code, str(kernel), str(write_fd), str(native_source)],
                                cwd=home, env=env, pass_fds=(write_fd,), capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "REAL_NATIVE_RECEIPT_ISSUED"
        os.close(write_fd)
        write_fd = -1
        receipt = json.loads(os.read(read_fd, 8192))
        assert os.read(read_fd, 1) == b""
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
    source = provider / "fixture.jsonl"
    source.write_text(json.dumps({"type": "system", "sessionId": "fixture-session"}) + "\n")
    source.chmod(0o600)
    kwargs = dict(board="test-board", task="fixture-task", provider="claude", session="fixture-session", source_path=source)
    prepared = service.capture_hook_start(**kwargs, task_receipt=receipt)
    with source.open("a") as stream:
        stream.write(json.dumps({"type": "user", "sessionId": "fixture-session", "message": {"role": "user", "content": "native-signed fixture visible"}}) + "\n")
    assert service.seal_hook_binding(board="test-board", task="fixture-task", prepared=prepared, task_receipt=receipt)
    page = service.get_parent_page(principal_id="github:fixture-user", board="test-board", task="fixture-task", cursor=None, limit=10)
    assert "native-signed fixture visible" in json.dumps(page)
    with pytest.raises(PermissionError):
        service.capture_hook_start(**kwargs, task_receipt={**receipt, "auth_tag": "0" * 64})
    print("REAL_NATIVE_PIPE_CAPTURE_SEAL_PROJECTION_OK (fake membership/transcript; no card/inference)")
