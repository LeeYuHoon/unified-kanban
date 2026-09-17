"""합성 fixture에서 작업 전체의 reader/writer 상호 배제를 검증한다."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import subprocess
import sys

import pytest

from kanban_adapter import codex_file_provenance as native
from test_codex_authenticated_prepare import setup
from test_conversation_owner_cli import setup as owner_setup  # noqa: F401


def test_owner_runtime_publication_keeps_exclusive_lease(owner_setup, monkeypatch):
    from kanban_adapter import conversation_owner_cli as cli
    from kanban_adapter.conversation_integration import OwnerPolicyFile
    from types import SimpleNamespace
    _, _, _, state, args = owner_setup
    original = cli.private.atomic_publish
    observed = []

    def publish(path, *values, **kwargs):
        if path.name == "runtime.json":
            policies = OwnerPolicyFile(state / "policy.json", secret=(state / "authority.key").read_bytes())
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(disable, SimpleNamespace(policies=policies))
                with pytest.raises(PermissionError, match="policy.*lease"):
                    future.result(timeout=5)
            observed.append(True)
        return original(path, *values, **kwargs)

    monkeypatch.setattr(cli.private, "atomic_publish", publish)
    assert cli.main(args) == 0
    assert observed == [True]


def disable(service):
    old = service.policies.load()
    service.policies.replace(
        replace(old, generation=old.generation + 1, enabled_boards={}, pair_activated_at_ns={}),
        expected_generation=old.generation,
    )


def test_direct_prepare_excludes_other_thread_writer_until_return(tmp_path, monkeypatch):
    source, service, args, _ = setup(tmp_path)
    original = native._snapshot
    observations = []

    def snapshot(*values, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(disable, service)
            with pytest.raises(PermissionError, match="policy.*lease"):
                future.result(timeout=5)
        observations.append(bool(service.policies.load().enabled_boards))
        return original(*values, **kwargs)

    monkeypatch.setattr(native, "_snapshot", snapshot)
    prepared = native.prepare(service, session="native", source_path=source, **args)
    assert prepared["policy_generation"] == 2
    assert observations == [True]
    disable(service)
    assert not service.policies.load().enabled_boards


def test_shared_lease_excludes_separate_process_writer_and_releases(tmp_path):
    _, service, _, _ = setup(tmp_path)
    code = """
import sys
from pathlib import Path
from dataclasses import replace
from kanban_adapter.conversation_integration import OwnerPolicyFile
p = OwnerPolicyFile(Path(sys.argv[1]), secret=bytes.fromhex(sys.argv[2]))
v = p.load()
try:
    p.replace(replace(v, generation=v.generation+1, enabled_boards={}, pair_activated_at_ns={}), expected_generation=v.generation)
except PermissionError:
    sys.exit(23)
"""
    argv = [sys.executable, "-B", "-c", code, str(service.policies.path), service.policies.secret.hex()]
    with service.collection_operation():
        result = subprocess.run(argv, capture_output=True, timeout=10)
        assert result.returncode == 23, result.stderr
    result = subprocess.run(argv, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not service.policies.load().enabled_boards


def test_reader_exception_releases_lease(tmp_path):
    _, service, _, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="fixture"):
        with service.collection_operation():
            raise ValueError("fixture")
    disable(service)
    assert not service.policies.load().enabled_boards


def test_nested_shared_operations_do_not_upgrade_or_unlock_outer(tmp_path):
    _, service, _, _ = setup(tmp_path)
    with service.collection_operation():
        with service.collection_operation():
            assert service.authorizes_principal("owner", "demo")
        with pytest.raises(PermissionError, match="policy.*lease"):
            disable(service)
    disable(service)


@pytest.mark.parametrize("module,names", [
    ("claude_absent", ("capture", "seal", "prepare_final", "seal_final")),
    ("claude_file_provenance", ("capture", "seal")),
    ("codex_file_provenance", ("prepare", "capture", "seal")),
    ("claude_pending_final", ("enqueue", "run_once")),
    ("codex_pending_final", ("enqueue", "run_once")),
    (None, ("capture_hook_start", "seal_hook_binding", "get_hook_final",
            "get_parent_page", "_project_parent_page", "get_child_links")),
])
def test_all_collection_entrypoints_admit_before_arguments(tmp_path, module, names):
    # 인자 해석/인증/원본 접근보다 lease admission이 먼저임을 실제 배타 lock으로 검증한다.
    import importlib
    from kanban_adapter.conversation_transaction import policy_lease
    _, service, _, _ = setup(tmp_path)
    functions = ([getattr(importlib.import_module("kanban_adapter." + module), name) for name in names]
                 if module else [getattr(service, name) for name in names])
    with policy_lease(service.policies.path, exclusive=True):
        with ThreadPoolExecutor(max_workers=1) as pool:
            for function in functions:
                args = (service,) if module else ()
                with pytest.raises(PermissionError, match="policy.*lease"):
                    pool.submit(function, *args).result(timeout=5)
            assert pool.submit(service.authorizes_principal, "owner", "demo").result(timeout=5) is False


def test_policy_cli_takes_writer_lease_before_planning(tmp_path, monkeypatch):
    from kanban_adapter import conversation_policy_cli as cli
    _, service, _, _ = setup(tmp_path)
    key = tmp_path / "fixture.key"
    key.write_bytes(service.policies.secret)
    key.chmod(0o600)
    reads = []
    original = cli.OwnerPolicyFile.load

    def load(store):
        reads.append(True)
        return original(store)

    monkeypatch.setattr(cli.OwnerPolicyFile, "load", load)
    with service.collection_operation():
        assert cli.main(["--policy-file", str(service.policies.path), "--secret-file", str(key),
                         "disable", "--board", "demo"]) == 1
    assert reads == []


def test_shared_readers_can_overlap(tmp_path):
    _, service, _, _ = setup(tmp_path)
    with service.collection_operation():
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(service.authorizes_principal, "owner", "demo").result(timeout=5)
