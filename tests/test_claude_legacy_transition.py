import json
import os

import pytest

from kanban_adapter import claude_hook as hook


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("UNIFIED_KANBAN_CONVERSATION_CONFIG", raising=False)
    monkeypatch.setattr(hook.HermesCliBackend, "resolve_board", lambda *a, **k: "new-board")
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    path = hook._state_path(cache, "legacy-session")
    raw = json.dumps({"task_id": "t_87654321", "cwd": str(tmp_path), "result": "private evidence"}).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    calls = []
    def adapter(argv, cwd):
        calls.append(argv)
        return "t_12345678" if argv[0] == "start" else ""
    payload = {"session_id": "legacy-session", "cwd": str(tmp_path), "prompt": "independent new request", "prompt_id": "new-prompt"}
    return cache, path, raw, calls, adapter, payload


def test_legacy_stop_refuses_but_next_prompt_progresses_once(legacy):
    cache, path, raw, calls, adapter, payload = legacy
    with pytest.raises(RuntimeError, match="routing authority"):
        hook.handle_event("stop", {"session_id": payload["session_id"], "last_assistant_message": "old reply"}, adapter=adapter, cache_dir=cache)
    assert calls == []
    old_identity = path.stat().st_ino
    old_bytes = path.read_bytes()
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert [a[0] for a in calls] == ["start"]
    assert json.loads(path.read_bytes())["lifecycle"]["board"] == "new-board"
    retained = list(cache.glob(".*legacy-quarantine*"))
    assert len(retained) == 1
    assert retained[0].stat().st_ino == old_identity
    assert retained[0].read_bytes() == old_bytes


@pytest.mark.parametrize("point", ["before-swap", "inside-swap", "after-detach"])
def test_foreign_successor_is_never_removed(legacy, monkeypatch, point):
    from kanban_adapter import private_files as files
    cache, path, raw, calls, adapter, payload = legacy
    foreign = b'{"foreign": "untouched"}'
    identity = []
    displaced = cache / "saved-original"
    def replace():
        path.rename(displaced)
        path.write_bytes(foreign)
        path.chmod(0o600)
        identity.append(path.stat().st_ino)
    if point == "before-swap":
        original = hook._read_state
        def read(*a, **k):
            result = original(*a, **k)
            replace()
            return result
        monkeypatch.setattr(hook, "_read_state", read)
    elif point == "inside-swap":
        original = files._swap_names
        def swap(*a):
            if not identity:
                replace()
            return original(*a)
        monkeypatch.setattr(files, "_swap_names", swap)
    else:
        original = files.detach_expected
        def detach(*a, **k):
            result = original(*a, **k)
            path.write_bytes(foreign)
            path.chmod(0o600)
            identity.append(path.stat().st_ino)
            return result
        monkeypatch.setattr(files, "detach_expected", detach)
    with pytest.raises((RuntimeError, OSError)):
        hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert calls == []
    assert path.read_bytes() == foreign
    assert path.stat().st_ino == identity[0]
    assert any(f.read_bytes() == raw for f in cache.iterdir() if f.is_file())


@pytest.mark.parametrize("displace_parent", [False, True])
def test_quarantine_fsync_failure_preserves_evidence(legacy, monkeypatch, displace_parent):
    from kanban_adapter import private_files as files
    cache, path, raw, calls, adapter, payload = legacy
    original = files.os.fsync
    fired = []
    moved = cache.with_name("displaced-cache")
    foreign_identity = []
    def fsync(fd):
        if not fired and any(cache.glob(".*legacy-quarantine*")) and path.exists() and path.read_bytes() == b"":
            fired.append(True)
            if displace_parent:
                cache.rename(moved)
                cache.mkdir(mode=0o700)
                successor = cache / path.name
                successor.write_bytes(b"foreign parent")
                foreign_identity.append(successor.stat().st_ino)
            raise OSError("injected durability failure")
        return original(fd)
    monkeypatch.setattr(files.os, "fsync", fsync)
    with pytest.raises(OSError, match="durability"):
        hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert fired and calls == []
    root = moved if displace_parent else cache
    assert any(f.read_bytes() == raw for f in root.iterdir() if f.is_file())
    if displace_parent:
        assert path.read_bytes() == b"foreign parent"
        assert path.stat().st_ino == foreign_identity[0]
        assert not (moved / path.name).exists() or (moved / path.name).read_bytes() != raw
    else:
        assert path.read_bytes() == raw
        hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
        assert [a[0] for a in calls] == ["start"]


def test_start_failure_retry_keeps_quarantine_and_idempotency(legacy):
    cache, path, raw, calls, adapter, payload = legacy
    def fail(argv, cwd):
        calls.append(argv)
        raise RuntimeError("start failed")
    with pytest.raises(RuntimeError, match="start failed"):
        hook.handle_event("prompt", payload, adapter=fail, cache_dir=cache)
    assert not path.exists()
    retained = list(cache.glob(".*legacy-quarantine*"))
    assert len(retained) == 1 and retained[0].read_bytes() == raw
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert [a[0] for a in calls] == ["start", "start"]
    keys = [a[a.index("--idempotency-key") + 1] for a in calls]
    assert keys[0] == keys[1]
    assert list(cache.glob(".*legacy-quarantine*")) == retained


@pytest.mark.parametrize("mode,lifecycle", [(0o644, False), (0o600, True)])
def test_nonprivate_or_malformed_lifecycle_not_migrated(legacy, mode, lifecycle):
    cache, path, raw, calls, adapter, payload = legacy
    if lifecycle:
        state = json.loads(raw)
        state["lifecycle"] = None
        path.write_text(json.dumps(state))
    path.chmod(mode)
    before = path.read_bytes(), path.stat().st_ino
    with pytest.raises(RuntimeError):
        hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert (path.read_bytes(), path.stat().st_ino) == before
    assert calls == []


def test_foreign_swap_reversal_retries_transient_failure(legacy, monkeypatch):
    from kanban_adapter import private_files as files
    cache, path, raw, calls, adapter, payload = legacy
    original = files._swap_names
    count = []
    foreign = []
    def swap(*a):
        count.append(True)
        if len(count) == 1:
            path.rename(cache / "original")
            path.write_bytes(b"foreign")
            foreign.append(path.stat().st_ino)
        elif len(count) == 2:
            raise OSError("transient reversal failure")
        return original(*a)
    monkeypatch.setattr(files, "_swap_names", swap)
    with pytest.raises(RuntimeError):
        hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert path.read_bytes() == b"foreign"
    assert path.stat().st_ino == foreign[0]
    assert calls == []


def test_concurrent_duplicate_prompt_has_one_start(legacy):
    from concurrent.futures import ThreadPoolExecutor
    cache, path, raw, calls, adapter, payload = legacy
    def invoke():
        hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert [a[0] for a in calls] == ["start"]
    assert len(list(cache.glob(".*legacy-quarantine*"))) == 1


def test_known_legacy_prompt_redelivery_waits_for_independent_prompt(legacy):
    cache, path, raw, calls, adapter, payload = legacy
    state = json.loads(raw)
    state["conversation_prepared"] = {"mode": "claude-absent-v1", "prompt_id": payload["prompt_id"]}
    path.write_text(json.dumps(state))
    before = path.read_bytes(), path.stat().st_ino
    hook.handle_event("prompt", payload, adapter=adapter, cache_dir=cache)
    assert calls == []
    assert (path.read_bytes(), path.stat().st_ino) == before
    hook.handle_event("prompt", {**payload, "prompt_id": "independent"}, adapter=adapter, cache_dir=cache)
    assert [a[0] for a in calls] == ["start"]
