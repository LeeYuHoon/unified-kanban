"""실제 실행기의 예외 처리와 기존의 안전한 진단 출력 경로를 검증한다."""
import json

import pytest

from kanban_adapter import claude_pending_final as queue
from test_claude_absent_pending_final import _job
from test_claude_attachment_ancestry import topology, wire, CANARY


@pytest.mark.parametrize("failure", ["ancestry", "private-exception"])
def test_pending_catch_reports_original_metadata_without_private_data(tmp_path, monkeypatch, capsys, failure):
    source, service, kwargs, job, _ = _job(tmp_path)
    rows = topology()
    rows[0]["parentUuid"] = "orphan"
    source.write_bytes(wire(rows))
    monkeypatch.delenv("UNIFIED_KANBAN_DIAGNOSTIC_FD", raising=False)
    def private_failure(*args, **kwargs):
        raise PermissionError(CANARY, str(tmp_path / CANARY))
    if failure == "private-exception":
        monkeypatch.setattr(queue.absent, "prepare_final", private_failure)
    assert queue.run_once(service, job) == "rejected"
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.err.splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["error"] == "conversation-preserve-failed"
    assert record["event"] == "stop"
    assert record["exception_class"] == "PermissionError"
    assert record["source_function"] == ("private_failure" if failure == "private-exception" else "add_attachment")
    assert record["source_basename"] == ("test_claude_pending_ancestry_diagnostics.py" if failure == "private-exception" else "claude_native_ancestry.py")
    assert type(record["source_line"]) is int
    assert CANARY not in captured.out + captured.err + job.read_text()
    assert str(tmp_path) not in captured.err
    assert kwargs["task_receipt"]["nonce"] not in captured.err
    assert "first_open" not in json.loads(job.read_text())
    before = job.read_bytes()
    assert queue.run_once(service, job) == "rejected"
    assert job.read_bytes() == before
    assert capsys.readouterr().err == ""  # 과거에 발생한 예외를 꾸며내지 않는다.
