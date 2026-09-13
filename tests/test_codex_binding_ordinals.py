"""비식별 네이티브 형태의 바인딩 회귀 테스트이며 운영 소스는 읽지 않는다."""
import hashlib
import json

import pytest

from test_codex_0154_projection import MARKER, REQUEST, native_records
from test_conversation_integration_security_fixes import _receipt, _service


def wire(rows):
    return b"".join((json.dumps(row) + "\n").encode() for row in rows)


def setup_source(tmp_path, native=True):
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    source = root / "rollout.jsonl"
    prefix = [{"type": "session_meta", "payload": {"id": "synthetic-session"}}]
    prefix += [{"type": "turn_context", "payload": {}} for _ in range(7)]
    if native:
        for i, row in enumerate(prefix):
            row["ordinal"] = i
    source.write_bytes(wire(prefix))
    source.chmod(0o600)
    service, kernel = _service(tmp_path, provider="codex", root=root)
    receipt = _receipt(kernel, board="demo", task="t_synthetic", created_at=2)
    prepared = service.capture_hook_start(board="demo", task="t_synthetic", provider="codex", session="synthetic-session", source_path=source, task_receipt=receipt)
    return source, service, receipt, prepared


def seal(service, receipt, prepared):
    return service.seal_hook_binding(board="demo", task="t_synthetic", prepared=prepared, task_receipt=receipt)


def test_native_prefix_eight_exact_suffix_binding_and_projection(tmp_path):
    source, service, receipt, prepared = setup_source(tmp_path)
    prefix = source.read_bytes()
    rows = native_records()
    for i, row in enumerate(rows, 8):
        row["ordinal"] = i
    suffix = wire(rows)
    with source.open("ab") as stream:
        stream.write(suffix)
    binding = seal(service, receipt, prepared)
    assert (binding.turn_start.ordinal, binding.turn_end.ordinal) == (8, 14)
    assert (binding.turn_start.byte_offset, binding.turn_end.byte_offset) == (len(prefix), len(prefix + suffix))
    assert binding.source_range_digest == hashlib.sha256(suffix).hexdigest()
    assert service.bindings.get("demo", "t_synthetic")[0] == binding
    page = service.get_parent_page(principal_id="owner", board="demo", task="t_synthetic", cursor=None, limit=10)
    assert [event["text"] for event in page["events"]] == [REQUEST, MARKER]
    assert page["malformed_count"] == 0
    assert page["dropped_count"] == 4
    assert seal(service, receipt, prepared) == binding


@pytest.mark.parametrize("bad", [True, -1, 9, 7, 6, None, "8", [], {}])
def test_invalid_native_suffix_never_seals(tmp_path, bad):
    source, service, receipt, prepared = setup_source(tmp_path)
    with source.open("ab") as stream:
        stream.write(wire([{"type": "turn_context", "ordinal": bad, "payload": {}}]))
    with pytest.raises(PermissionError):
        seal(service, receipt, prepared)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_synthetic")


@pytest.mark.parametrize("suffix", [b'\n', b'{"type":"turn_context","payload":{}}\n'])
def test_native_missing_ordinal_or_blank_rejected(tmp_path, suffix):
    source, service, receipt, prepared = setup_source(tmp_path)
    with source.open("ab") as stream:
        stream.write(suffix)
    with pytest.raises(PermissionError):
        seal(service, receipt, prepared)


def test_legacy_absent_ordinal_domain_preserved(tmp_path):
    source, service, receipt, prepared = setup_source(tmp_path, native=False)
    with source.open("ab") as stream:
        stream.write(wire([{"type": "event_msg", "payload": {"type": "user_message", "message": "legacy"}}]))
    binding = seal(service, receipt, prepared)
    assert (binding.turn_start.ordinal, binding.turn_end.ordinal) == (0, 1)
    page = service.get_parent_page(principal_id="owner", board="demo", task="t_synthetic", cursor=None, limit=10)
    assert [event["text"] for event in page["events"]] == ["legacy"]


def test_prepared_offset_must_match_captured_metadata(tmp_path):
    source, service, receipt, prepared = setup_source(tmp_path)
    prepared["start_offset"] = 0
    with source.open("ab") as stream:
        stream.write(wire([{"type": "turn_context", "ordinal": 8, "payload": {}}]))
    with pytest.raises((ValueError, PermissionError)):
        seal(service, receipt, prepared)


@pytest.mark.parametrize("prepared,receipt", [([], {}), ({}, []), (None, {})])
def test_untrusted_envelopes_reject_cleanly(tmp_path, prepared, receipt):
    _, service, _, _ = setup_source(tmp_path)
    with pytest.raises((ValueError, PermissionError)):
        seal(service, receipt, prepared)
