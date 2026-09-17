"""최종 공개 텍스트가 도착하기 전에는 불변 binding을 발행하지 않는다."""
import json

import pytest

from kanban_adapter import claude_file_provenance as v2
from kanban_adapter import claude_hook as hook
from test_claude_file_provenance_v2 import _setup, _history, _request, _final, _row, _page
from test_claude_missing_transcript import PROMPT_ID


def _prepared(tmp_path, monkeypatch, raw=None):
    source, service, _, adapter, payload, cache = _setup(
        tmp_path, monkeypatch, _history() + _request() if raw is None else raw)
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    state = json.loads(hook._state_path(cache, "native").read_text())
    kwargs = dict(board="demo", task="t_native", prepared=state["conversation_prepared"],
                  task_receipt=state["observation_receipt"], prompt_id=PROMPT_ID)
    return source, service, kwargs


@pytest.mark.parametrize("variant", ["prior", "other", "thinking", "thinking_then_text", "empty", "nonfinal"])
def test_nonfinal_sources_remain_pending(tmp_path, monkeypatch, variant):
    source, service, kwargs = _prepared(tmp_path, monkeypatch)
    row = json.loads(_final())
    suffix = b""
    if variant == "other":
        suffix = (_row("user", "other-u", "new-u", "other request", "750e8400-e29b-41d4-a716-446655440000") +
                  _row("assistant", "other-a", "other-u", "other final"))
    elif variant in {"thinking", "thinking_then_text"}:
        row["message"]["content"] = [{"type": "thinking", "thinking": "private"}]
        if variant == "thinking_then_text":
            row["message"]["content"].append({"type": "text", "text": "not projected"})
        suffix = json.dumps(row).encode() + b"\n"
    elif variant == "empty":
        row["message"]["content"][0]["text"] = "   "
        suffix = json.dumps(row).encode() + b"\n"
    elif variant == "nonfinal":
        row["message"]["stop_reason"] = "tool_use"
        suffix = json.dumps(row).encode() + b"\n"
    with source.open("ab") as stream:
        stream.write(suffix)
    assert v2.seal(service, **kwargs, require_final=True) == {"status": "pending", "binding": None}
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


@pytest.mark.parametrize("attack", ["prefix", "inode", "policy", "parent", "prompt", "malformed"])
def test_pending_retry_revalidates_authority(tmp_path, monkeypatch, attack):
    from kanban_adapter.conversation import CollectionPolicyV1
    source, service, kwargs = _prepared(tmp_path, monkeypatch)
    assert v2.seal(service, **kwargs, require_final=True)["status"] == "pending"
    suffix = _final()
    if attack == "parent":
        suffix = suffix.replace(b'"parentUuid": "new-u"', b'"parentUuid": "old-a"')
    elif attack == "prompt":
        row = json.loads(suffix)
        row["promptId"] = "650e8400-e29b-41d4-a716-446655440000"
        suffix = json.dumps(row).encode() + b"\n"
    elif attack == "malformed":
        suffix = b'{broken}\n'
    with source.open("ab") as stream:
        stream.write(suffix)
    if attack == "prefix":
        source.write_bytes(source.read_bytes().replace(b"prior private final", b"other private final"))
    elif attack == "inode":
        raw = source.read_bytes()
        source.rename(source.with_suffix(".old"))
        source.write_bytes(raw)
        source.chmod(0o600)
    elif attack == "policy":
        service.policies.replace(CollectionPolicyV1.disabled(version=2, generation=3), expected_generation=2)
    with pytest.raises((PermissionError, ValueError)):
        v2.seal(service, **kwargs, require_final=True)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence"])
def test_both_public_final_stop_reasons_are_ready(tmp_path, monkeypatch, stop_reason):
    source, service, kwargs = _prepared(tmp_path, monkeypatch)
    row = json.loads(_final())
    row["message"]["stop_reason"] = stop_reason
    with source.open("ab") as stream:
        stream.write(json.dumps(row).encode() + b"\n")
    assert v2.seal(service, **kwargs, require_final=True)["status"] == "ready"
    assert [event["text"] for event in _page(service)["events"]] == ["same request", "current final"]


def test_missing_request_is_pending(tmp_path, monkeypatch):
    _, service, kwargs = _prepared(tmp_path, monkeypatch, _history())
    assert v2.seal(service, **kwargs, require_final=True) == {"status": "pending", "binding": None}
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")


def test_existing_partial_is_rejected_not_expanded(tmp_path, monkeypatch):
    source, service, kwargs = _prepared(tmp_path, monkeypatch)
    binding = v2.seal(service, **kwargs)
    with source.open("ab") as stream:
        stream.write(_final())
    with pytest.raises(PermissionError, match="immutable"):
        v2.seal(service, **kwargs, require_final=True)
    assert service.bindings.get("demo", "t_native")[0] == binding
    assert [event["text"] for event in _page(service)["events"]] == ["same request"]


def test_pending_then_same_inode_final_publishes(tmp_path, monkeypatch):
    source, service, kwargs = _prepared(tmp_path, monkeypatch)
    assert v2.seal(service, **kwargs, require_final=True) == {"status": "pending", "binding": None}
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "t_native")
    inode = source.stat().st_ino
    with source.open("ab") as stream:
        stream.write(_final())
    result = v2.seal(service, **kwargs, require_final=True)
    assert source.stat().st_ino == inode
    assert result["status"] == "ready"
    assert result["binding"] == service.bindings.get("demo", "t_native")[0]
    assert [event["text"] for event in _page(service)["events"]] == ["same request", "current final"]
