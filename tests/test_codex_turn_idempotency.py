"""네이티브 계약: openai/codex rust-v0.154.0, hooks/schema/generated/
user-prompt-submit.command.input.schema.json 및 stop.command.input.schema.json.
두 스키마는 prompt_id가 아닌 문자열 turn_id를 요구하며,
events/{user_prompt_submit,stop}.rs는 request.turn_id를 그대로 직렬화한다.
실제 네이티브 턴을 캡처한 것이 아닌 합성 계약 픽스처이며,
소비자 프로세스나 운영 백엔드를 호출하지 않는다.
"""

import json
from pathlib import Path

import pytest

from kanban_adapter import claude_hook as hook
from kanban_adapter.backend import HermesCliBackend
from kanban_adapter.codex_hook import normalize_payload


class IdempotentAdapter:
    """보드 범위의 영속 생성 중복 제거를 모사하는 인메모리 백엔드 경계."""

    def __init__(self):
        self.calls = []
        self.tasks = {}

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), cwd))
        board = argv[argv.index("--board") + 1]
        if argv[0] == "start":
            assert Path(argv[argv.index("--title-file") + 1]).read_text() == "identical prompt"
            key = argv[argv.index("--idempotency-key") + 1]
            return self.tasks.setdefault((board, key), f"t_{len(self.tasks) + 1}")
        return ""


@pytest.fixture
def harness(tmp_path, monkeypatch):
    board = ["original"]
    monkeypatch.setattr(HermesCliBackend, "resolve_board", lambda self, **kw: board[0])
    monkeypatch.delenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", raising=False)
    adapter = IdempotentAdapter()
    cache = tmp_path / "cache"

    def send(event, turn_id, session="session"):
        native = {
            "hook_event_name": "UserPromptSubmit" if event == "prompt" else "Stop",
            "session_id": session, "turn_id": turn_id, "cwd": str(tmp_path),
            "transcript_path": None, "model": "gpt-5", "permission_mode": "default",
        }
        if event == "prompt":
            native["prompt"] = "identical prompt"
        else:
            native.update(last_assistant_message="finished", stop_hook_active=False)
        hook.handle_event(event, normalize_payload(native), adapter=adapter,
                          cache_dir=cache, source="codex")

    return send, adapter, cache, board


@pytest.mark.parametrize("after_stop", [False, True])
def test_new_identical_native_turn_creates_distinct_task(harness, after_stop):
    send, adapter, cache, board = harness
    send("prompt", "turn-1")
    first = json.loads(hook._state_path(cache, "session").read_text())
    if after_stop:
        send("stop", "turn-1")
        assert not hook._state_path(cache, "session").exists()
    send("prompt", "turn-2")
    second = json.loads(hook._state_path(cache, "session").read_text())
    assert len(adapter.tasks) == 2
    assert first["task_id"] != second["task_id"]
    assert first["lifecycle"]["idempotency_key"] != second["lifecycle"]["idempotency_key"]
    assert second["lifecycle"]["source"] == "codex"
    assert second["lifecycle"]["session"] == "session"
    assert second["lifecycle"]["board"] == "original"
    done = [a for a, _ in adapter.calls if a[0] == "done"]
    assert len(done) == 1
    assert done[0][done[0].index("--task") + 1] == first["task_id"]


@pytest.mark.parametrize("after_stop", [False, True])
def test_same_native_turn_retry_does_not_create_another_task(harness, after_stop):
    send, adapter, cache, board = harness
    send("prompt", "turn-1")
    first = json.loads(hook._state_path(cache, "session").read_text())
    if after_stop:
        send("stop", "turn-1")
    before = len(adapter.calls)
    send("prompt", "turn-1")
    second = json.loads(hook._state_path(cache, "session").read_text())
    assert len(adapter.tasks) == 1
    assert second["task_id"] == first["task_id"]
    assert second["lifecycle"] == first["lifecycle"]
    if not after_stop:
        assert len(adapter.calls) == before  # 활성 상태의 재시도는 백엔드에 도달하지 않는다
    else:
        starts = [a for a, _ in adapter.calls if a[0] == "start"]
        assert len(starts) == 2  # 삭제된 로컬 상태가 아닌 영속 백엔드 키로 중복을 제거한다
        assert starts[0][-1] == starts[1][-1]


def test_native_turn_remains_bound_to_session_and_original_board(harness):
    send, adapter, cache, board = harness
    send("prompt", "turn-1")
    send("prompt", "turn-1", session="other-session")
    assert len(adapter.tasks) == 2
    board[0] = "remapped"
    send("prompt", "turn-1")  # 재시도는 기존 lifecycle 권한을 유지한다
    assert len(adapter.tasks) == 2
    send("stop", "turn-1")
    done = [a for a, _ in adapter.calls if a[0] == "done"]
    assert done[0][done[0].index("--board") + 1] == "original"
    assert hook._state_path(cache, "other-session").exists()
    send("prompt", "turn-2")
    current = json.loads(hook._state_path(cache, "session").read_text())
    assert current["lifecycle"]["board"] == "remapped"


def test_turn_key_scope_and_opaque_identity_are_preserved(tmp_path):
    key = hook._create_idempotency_key("codex", "s", tmp_path, "one", "turn-1")
    assert key == hook._create_idempotency_key("codex", "s", tmp_path, "two", "turn-1")
    for source, session, cwd, identity in [
        ("claude-code", "s", tmp_path, "turn-1"),
        ("codex", "other", tmp_path, "turn-1"),
        ("codex", "s", tmp_path / "other", "turn-1"),
        ("codex", "s", tmp_path, "turn-2"),
        ("codex", "s", tmp_path, " turn-1"),
    ]:
        assert key != hook._create_idempotency_key(source, session, cwd, "one", identity)


@pytest.mark.parametrize("identity", [None, "", 1, True, [], {}])
def test_missing_or_invalid_native_identity_keeps_exact_legacy_key(tmp_path, identity):
    import hashlib

    normalized = normalize_payload({"turn_id": identity, "prompt_id": "invented"})
    assert normalized["prompt_id"] is None
    values = ["unified-kanban/claude-create/v2", "codex", "s", str(tmp_path), "same"]
    expected = hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert hook._create_idempotency_key("codex", "s", tmp_path, "same", normalized["prompt_id"]) == expected
    assert hook._create_idempotency_key("codex", "s", tmp_path, "same", identity) == expected


@pytest.mark.parametrize("after_stop", [False, True])
def test_legacy_identical_prompts_remain_conservatively_deduplicated(harness, after_stop):
    send, adapter, cache, board = harness
    send("prompt", None)
    if after_stop:
        send("stop", None)
    send("prompt", None)
    assert len(adapter.tasks) == 1  # 네이티브 식별자가 없으면 새 턴임을 입증할 수 없다


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop"])
def test_native_identity_is_mapped_exactly_not_invented(event):
    payload = normalize_payload({"hook_event_name": event, "turn_id": "opaque-turn-1",
                                 "prompt_id": "not-a-native-field"})
    assert payload["prompt_id"] == "opaque-turn-1"
    assert normalize_payload({"prompt_id": "not-a-native-field"})["prompt_id"] is None


def test_late_stop_cannot_complete_new_identical_turn(harness):
    """이전 turn의 늦은 Stop이 새 카드의 결과를 덮어쓰지 않는다."""
    send, adapter, cache, _ = harness
    send("prompt", "turn-1")
    send("prompt", "turn-2")
    state_path = hook._state_path(cache, "session")
    before = state_path.read_bytes()
    call_count = len(adapter.calls)
    send("stop", "turn-1")
    assert state_path.read_bytes() == before
    assert len(adapter.calls) == call_count
    send("stop", "turn-2")
    assert not state_path.exists()


def test_claude_uuid_and_legacy_key_contract_unchanged(tmp_path):
    import hashlib

    for identity, version, last in [
        ("d13140ad-3176-4718-a140-dfb363e93316", "v3", "d13140ad-3176-4718-a140-dfb363e93316"),
        ("turn-1", "v2", "same"),
    ]:
        values = [f"unified-kanban/claude-create/{version}", "claude-code", "s", str(tmp_path), last]
        expected = hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        assert hook._create_idempotency_key("claude-code", "s", tmp_path, "same", identity) == expected
