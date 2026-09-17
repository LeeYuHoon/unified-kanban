"""호환성 거부가 대화 차단 없이 관측 가능하며 비밀을 노출하지 않는지 검증한다."""
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from kanban_adapter.compatibility import read_carried_commits, read_supported_upstream
from kanban_adapter.release_layout import COMPLETION_RECEIPT_NAME

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("damage", ["receipt-identity", "selected-release"])
def test_wrapper_reports_rejected_runtime_without_consuming_prompt(tmp_path, reviewed_release, damage):
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    layout = reviewed_release(repo, read_supported_upstream(), read_carried_commits()[-1])
    if damage == "receipt-identity":
        receipt_path = layout.release / COMPLETION_RECEIPT_NAME
        receipt = json.loads(receipt_path.read_text())
        receipt["release_identity"] = [0, 0]
        receipt_path.write_text(json.dumps(receipt))
    else:
        layout.selector.write_text(str(tmp_path / "unreviewed") + "\n")
    before = {p: p.read_bytes() for p in (layout.selector, layout.release / COMPLETION_RECEIPT_NAME)}
    result = subprocess.run(
        [str(ROOT / "bin/claude-kanban-hook"), "prompt"],
        input='{"prompt":"SECRET_PROMPT_TOKEN"}', text=True, capture_output=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "HERMES_AGENT_REPO": str(repo)},
        timeout=10,
    )
    assert result.returncode == 0
    assert "systemMessage" in result.stdout
    assert "compatibility-rejected" in result.stderr
    assert "SECRET_PROMPT_TOKEN" not in result.stdout + result.stderr
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert not (tmp_path / ".cache").exists()


def test_body_failure_is_visible_but_never_logs_exception_payload(monkeypatch, capsys):
    from kanban_adapter import claude_hook

    def fail(*args, **kwargs):
        raise RuntimeError("SECRET_PROMPT_TOKEN")

    monkeypatch.setattr(claude_hook, "handle_event", fail)
    assert claude_hook.main(["prompt"], stdin=io.StringIO("{}")) == 0
    captured = capsys.readouterr()
    assert "systemMessage" in captured.out
    assert "collection-failed" in captured.err
    assert "SECRET_PROMPT_TOKEN" not in captured.out + captured.err


def test_inner_error_never_exposes_free_form_text(capsys):
    from kanban_adapter.claude_hook import log_error
    log_error("usage-comment: SYNTHETIC_PRIVATE_CANARY", kind="PRIVATE_KIND")
    captured = capsys.readouterr()
    assert "SYNTHETIC_PRIVATE_CANARY" not in captured.out + captured.err
    assert "PRIVATE_KIND" not in captured.out + captured.err
    assert "collection-failed" in captured.err


def test_entry_keeps_general_stderr_separate_from_diagnostic_fd(monkeypatch):
    import sys
    from kanban_adapter import claude_hook, claude_hook_entry as entry
    before = sys.stderr
    monkeypatch.setenv("UNIFIED_KANBAN_DIAGNOSTIC_FD", "3")
    monkeypatch.setattr(entry.os, "dup", lambda fd: fd)
    monkeypatch.setattr(entry.os, "fdopen", lambda *args: io.StringIO())
    monkeypatch.setattr(entry, "check_hermes_compatibility", lambda: (True, "ok"))
    monkeypatch.setattr(claude_hook, "main", lambda: 0)
    try:
        assert entry.main() == 0
        assert sys.stderr is before
    finally:
        sys.stderr = before


def test_diagnostic_fd_receives_only_allowlisted_metadata(monkeypatch, capsys):
    from kanban_adapter import claude_hook_entry as entry
    writes = []
    monkeypatch.setenv("UNIFIED_KANBAN_DIAGNOSTIC_FD", "3")
    monkeypatch.setattr(entry.os, "write", lambda fd, data: writes.append((fd, data)))
    entry.report_failure("PRIVATE_EVENT", "PRIVATE_ERROR")
    assert len(writes) == 1
    fd, raw = writes[0]
    assert fd == 3
    record = json.loads(raw)
    assert record["event"] == "unknown"
    assert record["error"] == "collection-failed"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "PRIVATE_" not in captured.out + raw.decode()
