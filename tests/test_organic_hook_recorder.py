"""합성 입력으로만 선택적 진단을 검증하며 네이티브 훅은 재실행하지 않는다."""
import json
import os
import time
import io
import sys
import pytest
from pathlib import Path

from kanban_adapter import claude_hook_entry as entry

SID = "11111111-1111-4111-8111-111111111111"


def configured(tmp_path, monkeypatch):
    directory = tmp_path / "diagnostics"
    directory.mkdir(mode=0o700)
    config = directory / "config.json"
    config.write_text(json.dumps({"session_id": SID, "ancestor_pid": os.getppid(),
                                  "expires_at": int(time.time()) + 300}))
    config.chmod(0o600)
    monkeypatch.setattr(entry, "diagnostic_directory", lambda: directory, raising=False)
    return directory


def records(directory):
    return sorted((json.loads(p.read_text()) for p in directory.glob(".event.*")),
                  key=lambda record: record["time_ns"])


def test_entry_captures_preimport_failure_without_input_or_exception_text(tmp_path, monkeypatch):
    import sys
    directory = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["hook", "prompt"])
    def broken():
        raise ImportError("PRIVATE_EXCEPTION_CANARY")
    monkeypatch.setattr(entry, "check_hermes_compatibility", broken)
    assert entry.main() == 0
    events = records(directory)
    assert [e["stage"] for e in events] == ["entry", "compatibility-failed"]
    assert events[-1]["category"] == "import"
    assert "PRIVATE_" not in json.dumps(events)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.glob(".event.*"))


@pytest.mark.parametrize("failure", [False, True])
def test_opted_in_handler_records_create_boundary(tmp_path, monkeypatch, failure):
    from kanban_adapter import claude_hook
    directory = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["hook", "prompt"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": SID, "prompt": "PRIVATE_PAYLOAD"})))
    monkeypatch.setattr(entry, "check_hermes_compatibility", lambda: (True, "ok"))
    def adapter(argv, cwd):
        if failure:
            raise PermissionError("PRIVATE_CHILD_STDERR")
        return "t_fixture"
    def handle(event, payload, *, adapter=None):
        assert adapter is not None
        adapter(["start", "PRIVATE_TITLE"], Path("/PRIVATE_CWD"))
    monkeypatch.setattr(claude_hook, "run_adapter", adapter)
    monkeypatch.setattr(claude_hook, "handle_event", handle)
    assert entry.main() == 0
    events = records(directory)
    stages = [e["stage"] for e in events]
    assert "session-matched" in stages
    assert "create-enter" in stages
    assert ("create-failed" if failure else "create-returned") in stages
    assert ("handler-failed" if failure else "handler-returned") in stages
    assert "PRIVATE_" not in json.dumps(events)
    assert all(e["scope"] == "session" for e in events if e["stage"].startswith("create-"))


def test_real_wrapper_records_import_failure_before_runtime(tmp_path):
    import shutil
    import subprocess
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    shutil.copytree(root / "src", repo / "src")
    shutil.copytree(root / "bin", repo / "bin")
    (repo / "src/kanban_adapter/wrapper_runtime.py").write_text('raise ImportError("PRIVATE_IMPORT_BODY")\n')
    directory = tmp_path / ".cache/kanban-adapter/claude-diagnostics"
    directory.mkdir(parents=True, mode=0o700)
    config = directory / "config.json"
    config.write_text(json.dumps({"session_id": SID, "ancestor_pid": os.getpid(),
                                  "expires_at": int(time.time()) + 300}))
    config.chmod(0o600)
    env = dict(os.environ, HOME=str(tmp_path))
    result = subprocess.run([str(repo / "bin/claude-kanban-hook"), "prompt"],
                            input="PRIVATE_STDIN_BODY", text=True, capture_output=True,
                            env=env, timeout=10)
    assert result.returncode == 0
    events = records(directory)
    assert [e["stage"] for e in events] == ["bootstrap", "bootstrap-failed"]
    assert events[-1]["category"] == "import"
    assert "PRIVATE_" not in result.stdout + result.stderr + json.dumps(events)


@pytest.mark.parametrize("damage", ["disabled", "directory-mode", "config-mode", "symlink-dir", "symlink-config", "hardlink-config", "fifo-config", "expired", "other-ancestor", "oversized"])
def test_recorder_rejects_unsafe_or_unscoped_configuration(tmp_path, monkeypatch, damage):
    from kanban_adapter.hook_diagnostics import Recorder
    directory = configured(tmp_path, monkeypatch)
    config = directory / "config.json"
    if damage == "disabled":
        config.unlink()
    elif damage == "directory-mode":
        directory.chmod(0o755)
    elif damage == "config-mode":
        config.chmod(0o644)
    elif damage == "symlink-dir":
        real = tmp_path / "real"
        directory.rename(real)
        directory.symlink_to(real, target_is_directory=True)
    elif damage == "symlink-config":
        real = directory / "real.json"
        config.rename(real)
        config.symlink_to(real)
    elif damage == "hardlink-config":
        os.link(config, directory / "linked")
    elif damage == "fifo-config":
        config.unlink()
        os.mkfifo(config, 0o600)
    elif damage == "oversized":
        config.write_text(" " * 1025)
    else:
        data = json.loads(config.read_text())
        data["expires_at" if damage == "expired" else "ancestor_pid"] = 1
        config.write_text(json.dumps(data))
    assert Recorder.open(directory) is None
    assert not records(directory)


def test_replaced_directory_never_redirects_or_records(tmp_path, monkeypatch):
    from kanban_adapter.hook_diagnostics import Recorder
    directory = configured(tmp_path, monkeypatch)
    recorder = Recorder.open(directory)
    assert recorder is not None
    old = tmp_path / "old"
    directory.rename(old)
    directory.mkdir(mode=0o700)
    try:
        recorder.emit("PRIVATE_EVENT", "entry", RuntimeError("PRIVATE_EXCEPTION"))
    finally:
        recorder.close()
    assert not records(directory) and not records(old)


def test_session_mismatch_never_observes_adapter(tmp_path, monkeypatch):
    from kanban_adapter import claude_hook
    directory = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["hook", "prompt"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id":"other","prompt":"PRIVATE_OTHER"}'))
    monkeypatch.setattr(entry, "check_hermes_compatibility", lambda: (True, "ok"))
    calls = []
    monkeypatch.setattr(claude_hook, "handle_event", lambda *args, **kwargs: calls.append(kwargs))
    assert entry.main() == 0
    assert calls == [{}]
    assert not any(e["scope"] == "session" for e in records(directory))


def test_site_categories_are_fixed_not_traceback_names(tmp_path, monkeypatch):
    from kanban_adapter.hook_diagnostics import Recorder
    from kanban_adapter.claude_hook import _ensure_cache
    directory = configured(tmp_path, monkeypatch)
    cache = tmp_path / "bad-cache"
    cache.mkdir(mode=0o755)
    recorder = Recorder.open(directory)
    try:
        try:
            _ensure_cache(cache)
        except RuntimeError as error:
            recorder.emit("prompt", "handler-failed", error)
    finally:
        recorder.close()
    assert records(directory)[0]["site"] == "private-cache"


def test_wrapper_failopen_report_is_retained(tmp_path, monkeypatch):
    directory = configured(tmp_path, monkeypatch)
    entry.report_failure("prompt", "compatibility-rejected")
    assert records(directory)[0]["stage"] == "compatibility-rejected"


@pytest.mark.parametrize("change", ["unlink", "replace", "mode", "rewrite"])
def test_revocation_stops_already_open_recorder(tmp_path, monkeypatch, change):
    from kanban_adapter.hook_diagnostics import Recorder
    directory = configured(tmp_path, monkeypatch)
    recorder = Recorder.open(directory)
    config = directory / "config.json"
    if change == "unlink":
        config.unlink()
    elif change == "replace":
        replacement = directory / "replacement"
        replacement.write_bytes(config.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(config)
    elif change == "mode":
        config.chmod(0o644)
    else:
        config.write_text("{}")
    try:
        recorder.emit("prompt", "entry")
    finally:
        recorder.close()
    assert records(directory) == []


def test_close_error_is_failopen_and_never_retried(tmp_path, monkeypatch):
    from kanban_adapter import hook_diagnostics as diagnostics
    recorder = diagnostics.Recorder.open(configured(tmp_path, monkeypatch))
    fd = recorder.fd
    os.close(fd)
    recorder.close()
    assert recorder.fd == -1
    recorder.close()


def test_system_python_bootstrap_import_and_private_write(tmp_path):
    import subprocess
    interpreter = Path("/usr/bin/python3")
    if not interpreter.exists():
        pytest.skip("시스템 Python이 없는 플랫폼")
    root = Path(__file__).resolve().parents[1]
    code = '''
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from kanban_adapter.hook_diagnostics import Recorder
p = Path(sys.argv[2])
p.mkdir(mode=0o700)
c = p / "config.json"
c.write_text(json.dumps({"session_id": sys.argv[3], "ancestor_pid": os.getppid(), "expires_at": int(time.time()) + 60}))
c.chmod(0o600)
r = Recorder.open(p)
assert r is not None
r.emit("prompt", "bootstrap")
r.close()
assert len(list(p.glob(".event.*"))) == 1
'''
    result = subprocess.run([str(interpreter), "-I", "-c", code, str(root / "src"),
                             str(tmp_path / "diagnostics"), SID],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("body", ['[]', '{', json.dumps({"session_id": SID}),
                                  '{"session_id":"other"}'])
@pytest.mark.parametrize("failure", [False, True])
def test_observed_and_normal_output_and_handler_parity(tmp_path, monkeypatch, capsys, body, failure):
    from kanban_adapter import claude_hook
    directory = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["hook", "prompt"])
    monkeypatch.setattr(entry, "check_hermes_compatibility", lambda: (True, "ok"))
    calls = []
    def handle(event, payload, **kwargs):
        calls.append((event, payload))
        if failure:
            raise RuntimeError("PRIVATE_PARITY")
    monkeypatch.setattr(claude_hook, "handle_event", handle)
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    assert entry.main() == 0
    observed = capsys.readouterr()
    expected_calls = list(calls)
    calls.clear()
    (directory / "config.json").unlink()
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    assert entry.main() == 0
    normal = capsys.readouterr()
    assert calls == expected_calls
    assert normal.out == observed.out
    def stable(text):
        if not text:
            return None
        value = json.loads(text)
        value.pop("time")
        return value
    assert stable(normal.err) == stable(observed.err)
