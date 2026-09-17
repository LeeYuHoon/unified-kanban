from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import time
import types
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from kanban_adapter.claude_hook import handle_event, run_adapter
from kanban_adapter.compatibility import read_carried_commits, read_supported_upstream
from kanban_adapter.conversation import CollectionPolicyV1
from kanban_adapter.conversation_integration import (
    BindingStore,
    ConversationService,
    OwnerPolicyFile,
    ProductionConversationAuthority,
)
from kanban_adapter.conversation_runtime import (
    _task_membership,
    get_conversation_service,
)


def _receipt(secret: bytes, *, board: str, task: str, created_at: int, nonce: str = "n" * 32):
    body = {
        "schema": "hermes-kanban-observation-receipt-v1",
        "issuer": "hermes-kanban-kernel",
        "key_id": hashlib.sha256(secret).hexdigest()[:24],
        "board": board,
        "task": task,
        "observation": True,
        "created_at_ns": created_at * 1_000_000_000,
        "nonce": nonce,
    }
    wire = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return {**body, "auth_tag": hmac.new(secret, b"observation-receipt\0" + wire, hashlib.sha256).hexdigest()}


def _private(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def _service(tmp_path: Path, *, provider: str, root: Path, clock: int = 3_000_000_000):
    kernel = b"k" * 32
    policy = OwnerPolicyFile(tmp_path / "policy.json", secret=b"a" * 32)
    policy.replace(
        CollectionPolicyV1(
            version=1,
            generation=2,
            activated_at_ns=2_000_000_000,
            expires_at_ns=9_000_000_000,
            enabled_boards={"demo": frozenset({provider})},
        ),
        expected_generation=1,
    )
    authority = ProductionConversationAuthority.bootstrap(
        authority_secret=b"a" * 32,
        kernel_secret=kernel,
        receipt=ProductionConversationAuthority.issue_kernel_receipt(
            kernel_secret=kernel,
            issuer_id="hermes-kanban-kernel",
            key_id="test",
            now_ns=clock,
        ),
    )
    return ConversationService(
        authority=authority,
        policies=policy,
        bindings=BindingStore(tmp_path / "bindings", secret=b"a" * 32),
        task_membership=lambda *_: True,
        principal_board_grants={"owner": frozenset({"demo"})},
        kernel_secret=kernel,
        provider_roots={provider: root},
        clock_ns=lambda: clock,
    ), kernel


def test_provider_root_and_start_inode_are_authoritative(tmp_path: Path):
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    source = root / "rollout.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "expected"}}) + "\n")
    source.chmod(0o600)
    service, kernel = _service(tmp_path, provider="codex", root=root)
    receipt = _receipt(kernel, board="demo", task="t_12345678", created_at=2)

    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"")
    outside.chmod(0o600)
    with pytest.raises(PermissionError, match="configured provider root"):
        service.capture_hook_start(
            board="demo", task="t_12345678", provider="codex", session="expected",
            source_path=outside, task_receipt=receipt,
        )

    prepared = service.capture_hook_start(
        board="demo", task="t_12345678", provider="codex", session="expected",
        source_path=source, task_receipt=receipt,
    )
    source.rename(root / "old.jsonl")
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "expected"}}) + "\n")
    source.chmod(0o600)
    with pytest.raises(PermissionError, match="identity changed"):
        service.seal_hook_binding(
            board="demo", task="t_12345678", prepared=prepared, task_receipt=receipt,
        )


def test_receipt_must_postdate_policy_activation(tmp_path: Path):
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    source = root / "rollout.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "expected"}}) + "\n")
    source.chmod(0o600)
    service, kernel = _service(tmp_path, provider="codex", root=root)
    stale = _receipt(kernel, board="demo", task="t_12345678", created_at=1)
    with pytest.raises(PermissionError, match="policy activation"):
        service.capture_hook_start(
            board="demo", task="t_12345678", provider="codex", session="expected",
            source_path=source, task_receipt=stale,
        )


def test_capture_requires_native_session_identity_and_seal_rejects_root_swap(tmp_path: Path):
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    source = root / "rollout.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "wrong"}}) + "\n")
    source.chmod(0o600)
    service, kernel = _service(tmp_path, provider="codex", root=root)
    receipt = _receipt(kernel, board="demo", task="t_12345678", created_at=2)
    with pytest.raises(PermissionError, match="session"):
        service.capture_hook_start(
            board="demo", task="t_12345678", provider="codex", session="expected",
            source_path=source, task_receipt=receipt,
        )
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "expected"}}) + "\n")
    prepared = service.capture_hook_start(
        board="demo", task="t_12345678", provider="codex", session="expected",
        source_path=source, task_receipt=receipt,
    )
    old_root = tmp_path / "old-root"
    root.rename(old_root)
    root.mkdir(mode=0o700)
    replacement = root / "rollout.jsonl"
    replacement.write_text(json.dumps({"type": "session_meta", "payload": {"id": "expected"}}) + "\n")
    replacement.chmod(0o600)
    with pytest.raises(PermissionError, match="root identity"):
        service.seal_hook_binding(
            board="demo", task="t_12345678", prepared=prepared, task_receipt=receipt,
        )


def test_actual_adapter_receipt_fd_is_passed_to_subprocess(monkeypatch, tmp_path: Path):
    read_fd, write_fd = os.pipe()
    called = []
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".local/bin").mkdir(parents=True)
    (tmp_path / ".local/bin/kanban-adapter").write_text("")

    def fake_run(argv, **kwargs):
        called.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("kanban_adapter.claude_hook.subprocess.run", fake_run)
    try:
        assert run_adapter(["start", f"--conversation-receipt-fd={write_fd}"], tmp_path) == "ok"
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert called[0][1]["pass_fds"] == (write_fd,)


def test_malformed_policy_and_unauthorized_api_never_reach_source(monkeypatch, tmp_path: Path):
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    source = root / "rollout.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "expected"}}) + "\n")
    source.chmod(0o600)
    service, kernel = _service(tmp_path, provider="codex", root=root)
    opened = 0

    def forbidden_open(*args, **kwargs):
        nonlocal opened
        opened += 1
        raise AssertionError("source must not open")

    monkeypatch.setattr("kanban_adapter.conversation_integration.open_verified_jsonl_fd", forbidden_open)
    (tmp_path / "policy.json").write_text("{malformed")
    with pytest.raises((PermissionError, ValueError)):
        service.capture_hook_start(
            board="demo", task="t_12345678", provider="codex", session="expected",
            source_path=source,
            task_receipt=_receipt(kernel, board="demo", task="t_12345678", created_at=2),
        )
    with pytest.raises(PermissionError, match="board grant"):
        service.get_parent_page(
            principal_id="intruder", board="demo", task="t_12345678", cursor=None, limit=10,
        )
    assert opened == 0


def test_claude_native_provider_and_schema_reach_projector(tmp_path: Path):
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    source = root / "transcript.jsonl"
    source.write_text(json.dumps({"type": "system", "sessionId": "expected", "subtype": "init"}) + "\n")
    source.chmod(0o600)
    service, kernel = _service(tmp_path, provider="claude", root=root)
    receipt = _receipt(kernel, board="demo", task="t_12345678", created_at=2)
    prepared = service.capture_hook_start(
        board="demo", task="t_12345678", provider="claude", session="expected",
        source_path=source, task_receipt=receipt,
    )
    with source.open("a") as stream:
        stream.write(json.dumps({
            "type": "user",
            "sessionId": "expected",
            "message": {"role": "user", "content": "hello"},
        }) + "\n")
    service.seal_hook_binding(board="demo", task="t_12345678", prepared=prepared, task_receipt=receipt)
    with source.open("a") as stream:
        stream.write(json.dumps({
            "type": "user", "sessionId": "expected",
            "message": {"role": "user", "content": "later unsealed text"},
        }) + "\n")
    page = service.get_parent_page(principal_id="owner", board="demo", task="t_12345678", cursor=None, limit=10)
    assert isinstance(page, dict)
    assert [event["text"] for event in page["events"]] == ["hello"]
    original = source.read_bytes()
    rewritten = original.replace(b"hello", b"jello", 1)
    assert len(rewritten) == len(original)
    source.write_bytes(rewritten)
    denied = service.get_parent_page(
        principal_id="owner", board="demo", task="t_12345678", cursor=None, limit=10,
    )
    assert denied["source_availability"] == "rotated"


def test_seal_scans_in_bounded_chunks(monkeypatch, tmp_path: Path):
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    source = root / "rollout.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "expected"}}) + "\n")
    source.chmod(0o600)
    service, kernel = _service(tmp_path, provider="codex", root=root)
    receipt = _receipt(kernel, board="demo", task="t_12345678", created_at=2)
    prepared = service.capture_hook_start(
        board="demo", task="t_12345678", provider="codex", session="expected",
        source_path=source, task_receipt=receipt,
    )
    with source.open("ab") as stream:
        stream.write((json.dumps({"type": "session_meta", "payload": {"id": "expected"}}) + "\n").encode() * 4_000)
    real_pread = os.pread
    sizes = []

    def bounded(fd, count, offset):
        sizes.append(count)
        assert count <= 65_536
        return real_pread(fd, count, offset)

    monkeypatch.setattr("kanban_adapter.conversation_integration.os.pread", bounded)
    service.seal_hook_binding(board="demo", task="t_12345678", prepared=prepared, task_receipt=receipt)
    assert len(sizes) > 2


def test_seal_scanner_enforces_core_records_bytes_and_line_limits(tmp_path: Path):
    root = tmp_path / "limits"
    root.mkdir()
    cases = {
        "records": (json.dumps({"type": "session_meta", "payload": {"id": "s"}}) + "\n")
        + ("{}\n" * 5_000),
        "bytes": (json.dumps({"type": "session_meta", "payload": {"id": "s"}}) + "\n")
        + ((json.dumps({"padding": "x" * 2_100}) + "\n") * 4_100),
        "line": (json.dumps({"type": "session_meta", "payload": {"id": "s"}}) + "\n")
        + ("x" * (1024 * 1024 + 1)) + "\n",
    }
    for name, content in cases.items():
        path = root / f"{name}.jsonl"
        path.write_text(content)
        fd = os.open(path, os.O_RDONLY)
        try:
            with pytest.raises(PermissionError, match="budget"):
                ConversationService._scan_sealed_range(
                    fd, start_offset=0, end_offset=path.stat().st_size,
                    provider="codex", session="s",
                )
        finally:
            os.close(fd)


def test_explicit_board_sqlite_uri_preserves_literal_percent_path(monkeypatch, tmp_path: Path):
    intended_home = tmp_path / "encoded%2Fhome"
    decoded_home = tmp_path / "encoded" / "home"
    intended_home.mkdir(parents=True)
    decoded_home.mkdir(parents=True)

    def create_db(path: Path, observation: int) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, observation INTEGER)")
            connection.execute("INSERT INTO tasks VALUES ('t_literal', ?)", (observation,))

    create_db(intended_home / "kanban.db", 0)
    create_db(decoded_home / "kanban.db", 1)
    fake_db = types.SimpleNamespace(
        kanban_home=lambda: intended_home,
        board_dir=lambda board: intended_home / "boards" / board,
    )
    fake_package = types.ModuleType("hermes_cli")
    setattr(fake_package, "kanban_db", fake_db)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_package)
    assert _task_membership("default", "t_literal") is False


def test_seal_scanner_counts_blank_records_and_stops_on_malformed_or_deadline(
    monkeypatch, tmp_path: Path,
):
    session_line = json.dumps({"type": "session_meta", "payload": {"id": "s"}}) + "\n"
    blank = tmp_path / "blank.jsonl"
    blank.write_text(session_line + ("\n" * 5_000))
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text(session_line + "{broken}\n")
    for path, message in ((blank, "record budget"), (malformed, "malformed")):
        fd = os.open(path, os.O_RDONLY)
        try:
            with pytest.raises(PermissionError, match=message):
                ConversationService._scan_sealed_range(
                    fd, start_offset=0, end_offset=path.stat().st_size,
                    provider="codex", session="s",
                )
        finally:
            os.close(fd)

    deadline = tmp_path / "deadline.jsonl"
    deadline.write_text(session_line)
    fd = os.open(deadline, os.O_RDONLY)
    clock = iter((0.0, 3.0))
    monkeypatch.setattr("kanban_adapter.conversation_integration.time.monotonic", lambda: next(clock))
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            ConversationService._scan_sealed_range(
                fd, start_offset=0, end_offset=deadline.stat().st_size,
                provider="codex", session="s",
            )
    finally:
        os.close(fd)

    fd = os.open(deadline, os.O_RDONLY)
    clock = iter((0.0, 3.0))
    monkeypatch.setattr("kanban_adapter.conversation_integration.time.monotonic", lambda: next(clock))
    try:
        with pytest.raises(TimeoutError, match="digest deadline"):
            ConversationService._digest_range(fd, 0, deadline.stat().st_size)
    finally:
        os.close(fd)


@pytest.mark.integration
def test_native_hook_adapter_kernel_cli_fd_chain(monkeypatch, tmp_path: Path):
    source_root_text = os.environ.get("UNIFIED_KANBAN_TEST_HERMES_RUNTIME_SOURCE")
    if not source_root_text:
        pytest.skip(
            "set UNIFIED_KANBAN_TEST_HERMES_RUNTIME_SOURCE to the frozen Hermes "
            "source worktree with its project virtual environment"
        )
    source_root = Path(source_root_text).resolve()
    sys.path.insert(0, str(source_root))
    home = tmp_path / "home"
    adapter_bin = home / ".local/bin"
    adapter_bin.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    unified_python = Path(sys.executable)
    source_python = source_root / ".venv/bin/python"
    adapter = adapter_bin / "kanban-adapter"
    adapter.write_text(f"#!/bin/sh\nexec {unified_python} -m kanban_adapter.cli \"$@\"\n")
    adapter.chmod(0o700)
    hermes = bin_dir / "hermes"
    hermes.write_text(f"#!/bin/sh\nexec {source_python} -m hermes_cli.main \"$@\"\n")
    hermes.chmod(0o700)

    kernel = tmp_path / "kernel.key"
    authority = tmp_path / "authority.key"
    _private(kernel, b"k" * 32)
    _private(authority, b"a" * 32)
    policy_path = tmp_path / "policy.json"
    policy = OwnerPolicyFile(policy_path, secret=b"a" * 32)
    now = time.time_ns()
    policy.replace(CollectionPolicyV1(
        version=1, generation=2, activated_at_ns=1,
        expires_at_ns=now + 60_000_000_000,
        enabled_boards={"default": frozenset({"claude"})},
    ), expected_generation=1)
    transcript_root = tmp_path / "claude"
    transcript_root.mkdir(mode=0o700)
    transcript = transcript_root / "native.jsonl"
    _private(transcript, (json.dumps({
        "type": "system", "sessionId": "native-session", "subtype": "init",
    }) + "\n").encode())
    boot = ProductionConversationAuthority.issue_kernel_receipt(
        kernel_secret=b"k" * 32, issuer_id="hermes-kanban-kernel",
        key_id="native-e2e", now_ns=now,
    )
    config = tmp_path / "runtime.json"
    _private(config, json.dumps({
        "schema_version": 1, "enabled": True,
        "authority_secret_file": str(authority), "kernel_secret_file": str(kernel),
        "kernel_receipt": asdict(boot), "policy_file": str(policy_path),
        "binding_root": str(tmp_path / "bindings"),
        "principal_board_grants": {"basic:owner": ["default"]},
        "provider_roots": {"claude": str(transcript_root)},
    }).encode())
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", f"{source_root}:{Path.cwd() / 'src'}")
    selected_base = tmp_path / "hermes-selected"
    releases = Path(f"{selected_base}.releases")
    releases.mkdir()
    carried_tip = read_carried_commits()[-1]
    supported_upstream = read_supported_upstream()
    expected_release = releases / f"release-{carried_tip}"
    subprocess.run([
        "git", "clone", "--quiet", "--no-checkout",
        str(source_root),
        str(expected_release),
    ], check=True)
    subprocess.run([
        "git", "checkout", "--quiet", carried_tip,
    ], cwd=expected_release, check=True)
    release_bin = expected_release / "venv/bin"
    # setup과 같은 실제 venv를 만들고 native 소스와 의존성을 설치 경로에 둔다.
    subprocess.run([
        str(source_python), "-m", "venv", "--without-pip",
        str(expected_release / "venv"),
    ], check=True)
    release_python = release_bin / "python"
    site_probe = "import sysconfig; print(sysconfig.get_path('purelib'))"
    release_site = Path(subprocess.check_output(
        [str(release_python), "-I", "-c", site_probe], text=True,
    ).strip())
    native_site = subprocess.check_output(
        [str(source_python), "-I", "-c", site_probe], text=True,
    ).strip()
    (release_site / "native-fixture.pth").write_text(f"{source_root}\n{native_site}\n")
    runtime_probe = subprocess.check_output([
        str(release_python), "-I", "-c",
        "import sys, hermes_cli; assert sys.version_info >= (3, 11); print(sys.executable)",
    ], text=True)
    assert runtime_probe.strip() == str(release_python)
    release_hermes = release_bin / "hermes"
    release_hermes.write_text(
        f"#!/bin/sh\nexec {source_python} -m hermes_cli.main \"$@\"\n"
    )
    release_hermes.chmod(0o700)
    release_info = expected_release.stat()
    (expected_release / ".unified-kanban-release.json").write_text(json.dumps({
        "version": 2,
        "upstream": supported_upstream,
        "carried": carried_tip,
        "release_identity": [release_info.st_dev, release_info.st_ino],
    }))
    selector = releases / "current"
    selector.write_text(f"{expected_release}\n")
    selector.chmod(0o600)
    monkeypatch.setenv("HERMES_AGENT_REPO", str(selected_base))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_CONVERSATION_KERNEL_SECRET_FILE", str(kernel))
    monkeypatch.setenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", str(config))

    cache = tmp_path / "cache"
    project = tmp_path / "project"
    project.mkdir()
    handle_event("prompt", {
        "session_id": "native-session", "cwd": str(project), "prompt": "native request",
        "transcript_path": str(transcript),
    }, cache_dir=cache)
    with transcript.open("a") as stream:
        stream.write(json.dumps({
            "type": "user", "sessionId": "native-session",
            "message": {"role": "user", "content": "native request"},
        }) + "\n")
        stream.write(json.dumps({
            "type": "assistant", "sessionId": "native-session",
            "message": {
                "role": "assistant", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "native result"}],
            },
        }) + "\n")
    handle_event("stop", {
        "session_id": "native-session", "last_assistant_message": "native result",
    }, cache_dir=cache)
    service = get_conversation_service()
    assert service is not None
    assert len(list((tmp_path / "bindings").glob("*.json"))) == 1
    with sqlite3.connect(tmp_path / "kanban-home/kanban.db") as connection:
        task = connection.execute(
            "SELECT id FROM tasks WHERE observation=1"
        ).fetchone()[0]
    page = service.get_parent_page(
        principal_id="basic:owner", board="default", task=task, cursor=None, limit=10,
    )
    events = page["events"]
    assert isinstance(events, list)
    assert [event["text"] for event in events] == ["native request", "native result"]
    api_probe = tmp_path / "api-probe.py"
    api_probe.write_text("""
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from kanban_adapter.conversation_runtime import get_conversation_service
from plugins.kanban.dashboard.conversation_api import router

service = get_conversation_service()
assert service is not None
with sqlite3.connect(Path(os.environ["HERMES_KANBAN_HOME"]) / "kanban.db") as connection:
    task = connection.execute("SELECT id FROM tasks WHERE observation=1").fetchone()[0]
app = FastAPI()
app.state.auth_required = True
app.state.dashboard_canonical_origins = frozenset({"http://testserver"})
app.state.kanban_conversation_service = service
@app.middleware("http")
async def verified_session(request: Request, call_next):
    if request.headers.get("Authorization") == "Bearer owner":
        request.state.session = SimpleNamespace(user_id="owner", provider="basic")
    return await call_next(request)
app.include_router(router)
client = TestClient(app)
path = f"/boards/default/tasks/{task}/conversation"
source = Path(os.environ["NATIVE_TRANSCRIPT"])
source.chmod(0)
assert client.get(path, headers={"Origin": "http://testserver"}).status_code == 403
source.chmod(0o600)
response = client.get(path, headers={"Origin": "http://testserver", "Authorization": "Bearer owner"})
assert response.status_code == 200
assert response.headers["cache-control"].startswith("no-store")
print(json.dumps(response.json()))
""")
    probe_env = dict(os.environ)
    probe_env["NATIVE_TRANSCRIPT"] = str(transcript)
    probe = subprocess.run(
        [str(source_python), str(api_probe)], env=probe_env,
        text=True, capture_output=True, check=True,
    )
    api_page = json.loads(probe.stdout)
    assert [event["text"] for event in api_page["events"]] == [
        "native request", "native result",
    ]
