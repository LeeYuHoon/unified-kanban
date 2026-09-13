from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kanban_adapter.conversation import CollectionPolicyV1
from kanban_adapter.conversation_integration import (
    BindingStore,
    ConversationService,
    OwnerPolicyFile,
    ProductionConversationAuthority,
)
from kanban_adapter.conversation_policy_cli import main
from test_conversation_product_integration import private_file, task_receipt


def run_policy_cli(tmp_path: Path, provider: str) -> subprocess.CompletedProcess[str]:
    """실제 CLI를 임시 owner 파일과 저장소 source만 사용해 실행한다."""
    secret = tmp_path / "secret"
    private_file(secret, b"s" * 32)
    return subprocess.run(
        [sys.executable, "-B", "-m", "kanban_adapter.conversation_policy_cli",
         "--policy-file", str(tmp_path / "policy.json"), "--secret-file", str(secret),
         "enable", "--board", "demo", "--provider", provider,
         "--expires-at-ns", "200000000000", "--now-ns", "100"],
        env={"PATH": os.defpath, "HOME": str(tmp_path),
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("cli_provider, provider", [
    ("claude-code", "claude"), ("claude", "claude"), ("codex", "codex"),
])
def test_cli_signed_policy_authorizes_canonical_hook_capture(
    tmp_path: Path, cli_provider: str, provider: str,
) -> None:
    result = run_policy_cli(tmp_path, cli_provider)
    assert result.returncode == 0, result.stderr
    policies = OwnerPolicyFile(tmp_path / "policy.json", secret=b"s" * 32)
    source = tmp_path / "source" / "session.jsonl"
    record = ({"sessionId": "session"} if provider == "claude" else
              {"type": "session_meta", "payload": {"id": "session"}})
    private_file(source, (json.dumps(record) + "\n").encode())
    service = ConversationService(
        authority=ProductionConversationAuthority.for_test(kernel_secret=b"k" * 32),
        policies=policies,
        bindings=BindingStore(tmp_path / "bindings", secret=b"b" * 32),
        task_membership=lambda board, task: (board, task) == ("demo", "t_12345678"),
        kernel_secret=b"k" * 32, provider_roots={provider: source.parent},
        clock_ns=lambda: 100_000_000_001,
    )
    prepared = service.capture_hook_start(
        board="demo", task="t_12345678", provider=provider, session="session",
        source_path=source, task_receipt=task_receipt(b"k" * 32, "t_12345678"),
    )
    assert prepared["provider"] == provider
    assert prepared["start_offset"] == source.stat().st_size
    assert policies.load().enabled_boards == {"demo": frozenset({provider})}
    assert not policies.load().allows("demo", "claude-code", 1, 101)
    assert not policies.load().allows("demo", "unsupported", 1, 101)
    with pytest.raises(PermissionError, match="policy is disabled"):
        service.capture_hook_start(
            board="demo", task="t_12345678", provider="unsupported", session="session",
            source_path=source, task_receipt=task_receipt(b"k" * 32, "t_12345678"),
        )
    receipt = task_receipt(b"k" * 32, "t_12345678")
    receipt["auth_tag"] = "0" * 64
    with pytest.raises(PermissionError, match="receipt authentication failed"):
        service.capture_hook_start(
            board="demo", task="t_12345678", provider=provider, session="session",
            source_path=source, task_receipt=receipt,
        )


@pytest.mark.parametrize("provider", ["unsupported", "Claude", "CLAUDE", "claude_code", "claude "])
def test_cli_unsupported_provider_cannot_write_policy(tmp_path: Path, provider: str) -> None:
    assert run_policy_cli(tmp_path, "codex").returncode == 0
    path = tmp_path / "policy.json"
    before = path.read_bytes()
    result = run_policy_cli(tmp_path, provider)
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert path.read_bytes() == before


@pytest.mark.parametrize("provider", ["claude", "claude-code", "codex"])
def test_cli_policy_signature_is_verified_before_replacement(tmp_path: Path, provider: str) -> None:
    assert run_policy_cli(tmp_path, provider).returncode == 0
    path = tmp_path / "policy.json"
    envelope = json.loads(path.read_text())
    envelope["payload"]["enabled_boards"]["demo"] = ["claude-code"]
    path.write_text(json.dumps(envelope))
    before = path.read_bytes()
    with pytest.raises(PermissionError, match="policy MAC is invalid"):
        OwnerPolicyFile(path, secret=b"s" * 32).load()
    result = run_policy_cli(tmp_path, provider)
    assert result.returncode == 1
    assert "policy MAC is invalid" in result.stderr
    assert path.read_bytes() == before


def test_legacy_signed_alias_is_not_silently_migrated(tmp_path: Path) -> None:
    store = OwnerPolicyFile(tmp_path / "policy.json", secret=b"s" * 32)
    store.replace(CollectionPolicyV1(
        version=1, generation=2, activated_at_ns=100, expires_at_ns=200_000_000_000,
        enabled_boards={"legacy": frozenset({"claude-code"})},
    ), expected_generation=1)
    assert not store.load().allows("legacy", "claude", 1, 101)
    assert run_policy_cli(tmp_path, "claude-code").returncode == 0
    loaded = store.load()
    assert loaded.enabled_boards == {
        "legacy": frozenset({"claude-code"}), "demo": frozenset({"claude"}),
    }
    assert not loaded.allows("legacy", "claude", 1, 101)
    assert loaded.generation == 3


def test_policy_cli_explicitly_enables_and_disables_temp_board(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_bytes(b"s" * 32)
    secret.chmod(0o600)
    policy = tmp_path / "policy.json"

    assert main([
        "--policy-file", str(policy), "--secret-file", str(secret),
        "enable", "--board", "demo", "--provider", "codex",
        "--expires-at-ns", "10000", "--now-ns", "100",
    ]) == 0
    enabled = json.loads(policy.read_text())["payload"]
    assert enabled["enabled_boards"] == {"demo": ["codex"]}
    assert enabled["generation"] == 2

    assert main([
        "--policy-file", str(policy), "--secret-file", str(secret),
        "disable", "--board", "demo", "--now-ns", "200",
    ]) == 0
    disabled = json.loads(policy.read_text())["payload"]
    assert disabled["enabled_boards"] == {}
    assert disabled["generation"] == 3
