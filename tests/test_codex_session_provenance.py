"""턴 ID 일치만으로 다른 세션의 원본을 인증하지 않는다."""
import pytest

from kanban_adapter import codex_file_provenance as native
from test_codex_authenticated_prepare import setup, append, message
from test_codex_file_provenance import marker, row


def metadata(session, ordinal=39):
    return row("session_meta", {"id": session}, ordinal)


def ready_source(source, session="CURRENT"):
    source.write_bytes(metadata(session) + marker("task_started", ordinal=40)
                       + message("user", 41) + message("assistant", 42)
                       + marker("task_complete", ordinal=43))


@pytest.mark.parametrize("evidence", ["foreign", "missing", "duplicate", "conflicting", "missing-id", "invalid-id", "duplicate-id"])
def test_prepare_requires_unique_matching_native_session(tmp_path, evidence):
    source, service, args, _ = setup(tmp_path)
    ready_source(source)
    raw = source.read_bytes()
    if evidence == "foreign":
        raw = raw.replace(b"CURRENT", b"FOREIGN")
    elif evidence == "missing":
        raw = raw.split(b"\n", 1)[1]
    elif evidence in {"duplicate", "conflicting"}:
        raw += metadata("CURRENT" if evidence == "duplicate" else "FOREIGN", 44)
    elif evidence == "missing-id":
        raw = raw.replace(b'"id": "CURRENT"', b'"other": "CURRENT"')
    elif evidence == "invalid-id":
        raw = raw.replace(b'"id": "CURRENT"', b'"id": null')
    else:
        raw = raw.replace(b'"id": "CURRENT"', b'"id": "FOREIGN", "id": "CURRENT"')
    source.write_bytes(raw)
    with pytest.raises(PermissionError):
        native.prepare(service, session="CURRENT", source_path=source, **args)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "task")


@pytest.mark.parametrize("reconcile", [False, True])
@pytest.mark.parametrize("session", ["CURRENT", "FOREIGN"])
def test_seal_rechecks_metadata_appended_after_target_terminal(tmp_path, reconcile, session):
    source, service, args, _ = setup(tmp_path)
    ready_source(source)
    prepared = native.prepare(service, session="CURRENT", source_path=source, **args)
    original = None
    if reconcile:
        original = native.seal(service, prepared=prepared, **args)["binding"]
    append(source, metadata(session, 44))
    with pytest.raises(PermissionError):
        native.seal(service, prepared=prepared, reconcile_existing=reconcile, **args)
    if reconcile:
        assert service.bindings.get("demo", "task")[0] == original
    else:
        with pytest.raises(FileNotFoundError):
            service.bindings.get("demo", "task")


@pytest.mark.parametrize("reconcile", [False, True])
@pytest.mark.parametrize("evidence", ["foreign", "missing"])
def test_seal_revalidates_original_session_evidence(tmp_path, reconcile, evidence):
    source, service, args, _ = setup(tmp_path)
    ready_source(source)
    prepared = native.prepare(service, session="CURRENT", source_path=source, **args)
    if reconcile:
        native.seal(service, prepared=prepared, **args)
    raw = source.read_bytes()
    source.write_bytes(raw.replace(b"CURRENT", b"FOREIGN") if evidence == "foreign"
                       else raw.split(b"\n", 1)[1])
    # prefix 오류에 기대지 않고 원본 세션 증거 자체를 다시 확인해야 한다.
    with pytest.raises(PermissionError, match="native session metadata"):
        native.seal(service, prepared=prepared, reconcile_existing=reconcile, **args)


def test_authenticated_old_preparation_cannot_substitute_caller_session(tmp_path):
    source, service, args, _ = setup(tmp_path)
    ready_source(source, "FOREIGN")
    prepared = native.prepare(service, session="FOREIGN", source_path=source, **args)
    # 이전 취약 구현이 발급할 수 있었던 유효한 CURRENT 준비 서명을 재현한다.
    prepared["session"] = "CURRENT"
    prepared["mac"] = native._mac(service, {k: v for k, v in prepared.items() if k != "mac"})
    with pytest.raises(PermissionError, match="native session metadata"):
        native.seal(service, prepared=prepared, **args)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "task")


@pytest.mark.parametrize("session", ["native", "FOREIGN"])
def test_pending_reconciliation_rejects_appended_session_metadata(tmp_path, monkeypatch, session):
    from kanban_adapter import codex_pending_final as queue
    from test_codex_pending_reconciliation import crashed, stored

    source, service, _, job = crashed(tmp_path, monkeypatch)
    before = stored(service)
    append(source, metadata(session, 46))
    assert queue.run_once(service, job) == "rejected"
    assert stored(service) == before
