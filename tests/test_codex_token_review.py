"""범위 내 증거의 거부와 읽기 자원 한도를 오프라인 회귀 테스트로 검증한다."""
import io
import json

import pytest
from test_codex_measured_usage import record, rows, write_rows
from test_codex_usage_publication import TASK, Transport, message

from kanban_adapter.backend import HermesCliBackend
from kanban_adapter.token_usage import codex_token_events, wait_for_codex_token_events


@pytest.mark.parametrize('response', ['response-private', 'second'])
@pytest.mark.parametrize('fault', ['time', 'bad-time', 'cache', 'response', 'counts', 'usage'])
def test_malformed_scoped_record_cannot_freeze_partial_batch(tmp_path, response, fault):
    bad = record(response)
    if fault == 'time': bad.pop('timestamp')
    elif fault == 'bad-time': bad['timestamp'] = 'invalid'
    elif fault == 'cache': bad['payload']['usage']['input_tokens'] = 1
    elif fault == 'response': bad['payload'].pop('response_id')
    elif fault == 'counts': bad['payload']['usage'] = {}
    elif fault == 'usage': bad['payload'].pop('usage')
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, rows()[:4] + [bad] + rows()[4:])
    events = codex_token_events(path, session='session-a', turn='turn-a', root=tmp_path)
    assert events == []
    transport = Transport()
    backend = HermesCliBackend(runner=transport)
    with pytest.raises(RuntimeError, match='not ready'):
        backend.publish_codex_usage(board='default', task_id=TASK, messages=events)
    assert not transport.comments
    write_rows(path, rows()[:4] + [record('second')] + rows()[4:])
    repaired = codex_token_events(path, session='session-a', turn='turn-a', root=tmp_path)
    backend.publish_codex_usage(board='default', task_id=TASK,
        messages=[message(e['request_hash'], e['usage_at']) for e in repaired])
    assert len([c for c in transport.comments.values() if c['body'].startswith('Codex tool usage\n')]) == 2


def test_reader_stops_decoding_at_selected_completion(tmp_path, monkeypatch):
    from kanban_adapter import token_usage as reader
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, rows() + [{'body': 'x' * 1024}] * 1000)
    decode = json.loads
    calls = []
    def counted(line):
        calls.append(len(line))
        return decode(line)
    monkeypatch.setattr(reader.json, 'loads', counted)
    assert len(codex_token_events(path, session='session-a', turn='turn-a', root=tmp_path)) == 1
    assert len(calls) == len(rows())


@pytest.mark.parametrize('count', [128, 129])
def test_reader_request_budget(tmp_path, count):
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, rows()[:3] + [record(str(i)) for i in range(count)] + rows()[4:])
    events = codex_token_events(path, session='session-a', turn='turn-a', root=tmp_path)
    assert len(events) == (count if count <= 128 else 0)


def test_reader_rejects_large_identity_before_hashing(tmp_path, monkeypatch):
    from kanban_adapter import token_usage as reader
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, rows()[:3] + [record('x' * 4097)] + rows()[4:])
    def forbidden(*args, **kwargs):
        pytest.fail('oversized identity reached hashing')
    monkeypatch.setattr(reader.hashlib, 'sha256', forbidden)
    assert codex_token_events(path, session='session-a', turn='turn-a', root=tmp_path) == []


def test_reader_caps_line_before_decode(tmp_path, monkeypatch):
    from kanban_adapter import token_usage as reader
    class Bounded(io.StringIO):
        def readline(self, size=-1):
            assert 0 < size <= 262145
            return super().readline(size)
    handle = Bounded(json.dumps({'body': 'x' * 300000}) + '\n')
    monkeypatch.setattr(reader, '_open_runtime_jsonl', lambda *a, **k: handle)
    def forbidden(*args, **kwargs):
        pytest.fail('oversized line reached decoder')
    monkeypatch.setattr(reader.json, 'loads', forbidden)
    assert codex_token_events(tmp_path / 'unused', session='session-a', turn='turn-a', root=tmp_path) == []


def test_reader_total_byte_budget_discards_earlier_valid_event(tmp_path, monkeypatch):
    from kanban_adapter import token_usage as reader
    prefix = ''.join(json.dumps(row) + '\n' for row in rows()[:4])
    body = json.dumps({'body': 'x' * 250000}) + '\n'
    handle = io.StringIO(prefix + body * 68 + json.dumps(rows()[-1]) + '\n')
    monkeypatch.setattr(reader, '_open_runtime_jsonl', lambda *a, **k: handle)
    assert codex_token_events(tmp_path / 'unused', session='session-a', turn='turn-a', root=tmp_path) == []


def test_valid_unicode_identity_and_foreign_malformed_record(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    foreign = record('foreign', turn='other')
    foreign.pop('timestamp')
    write_rows(path, rows()[:3] + [record('응답-😀'), foreign] + rows()[4:])
    events = codex_token_events(path, session='session-a', turn='turn-a', root=tmp_path)
    assert events is not None and len(events) == 1


@pytest.mark.parametrize('data', [[], [{'type': 'session_meta', 'payload': {'id': 'session-a'}}]])
def test_unknown_format_is_pending_not_legacy(tmp_path, data):
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, data)
    assert codex_token_events(path, session='session-a', turn='turn-a', root=tmp_path) == []


def test_wait_collects_completion_appended_during_poll(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, rows()[:-1])
    now = [0.0]
    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
        with path.open('a') as stream:
            stream.write(json.dumps(rows()[-1]) + '\n')

    events = wait_for_codex_token_events(
        path, session='session-a', turn='turn-a', root=tmp_path,
        timeout=6.0, interval=0.1, sleeper=sleep, clock=lambda: now[0],
    )

    assert events is not None and len(events) == 1
    assert sleeps == [0.1]


def test_wait_leaves_incomplete_evidence_fail_closed_at_deadline(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, rows()[:-1])
    now = [0.0]
    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    assert wait_for_codex_token_events(
        path, session='session-a', turn='turn-a', root=tmp_path,
        timeout=0.25, interval=0.1, sleeper=sleep, clock=lambda: now[0],
    ) == []
    assert sleeps == [0.1, 0.1, pytest.approx(0.05)]


def test_wait_rejects_completed_malformed_evidence_without_sleep(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    bad = rows()
    bad[3]['payload']['usage']['input_tokens'] = True
    write_rows(path, bad)

    def forbidden(_delay):
        pytest.fail('완료된 malformed 증거를 기다리면 안 된다')

    assert wait_for_codex_token_events(
        path, session='session-a', turn='turn-a', root=tmp_path,
        timeout=6.0, sleeper=forbidden,
    ) == []


def test_wait_never_borrows_next_turn_completion(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    data = rows()[:-1] + [
        rows()[1] | {'payload': {'type': 'task_started', 'turn_id': 'turn-b'}},
        rows()[-1] | {'payload': {'type': 'task_complete', 'turn_id': 'turn-b'}},
    ]
    write_rows(path, data)

    def forbidden(_delay):
        pytest.fail('다음 턴이 시작된 뒤에는 기다리면 안 된다')

    assert wait_for_codex_token_events(
        path, session='session-a', turn='turn-a', root=tmp_path,
        timeout=6.0, sleeper=forbidden,
    ) == []


def test_wait_propagates_interruption(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, rows()[:-1])

    def interrupt(_delay):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        wait_for_codex_token_events(
            path, session='session-a', turn='turn-a', root=tmp_path,
            timeout=6.0, sleeper=interrupt,
        )


def test_wait_budget_is_capped_below_hook_timeout(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, rows()[:-1])
    now = [0.0]
    slept = [0.0]

    def sleep(delay):
        slept[0] += delay
        now[0] += delay

    assert wait_for_codex_token_events(
        path, session='session-a', turn='turn-a', root=tmp_path,
        timeout=999.0, interval=10.0, sleeper=sleep, clock=lambda: now[0],
    ) == []
    assert slept[0] == pytest.approx(6.0)


def test_wait_deadline_includes_slow_initial_scan(monkeypatch):
    from kanban_adapter import token_usage as reader

    now = [10.0]
    scans = []

    def slow_scan(*args, **kwargs):
        scans.append(kwargs["deadline"])
        now[0] += 6.5
        return "pending", [], None

    monkeypatch.setattr(reader, "_inspect_codex_token_events", slow_scan)

    assert wait_for_codex_token_events(
        "/unused", session="session-a", turn="turn-a",
        timeout=6.0, sleeper=lambda _delay: pytest.fail("기한 뒤에 sleep하면 안 된다"),
        clock=lambda: now[0],
    ) == []
    assert scans == [16.0]
