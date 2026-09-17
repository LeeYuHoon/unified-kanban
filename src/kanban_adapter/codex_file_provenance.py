"""Codex 0.154 네이티브 파일 준비/봉인. 공통 EOF와 hook 계약은 변경하지 않는다.

prepare는 기존 원본과 목표 턴을 인증한다. seal은 매번 권한과 원본을 재검증하여
pending/None 또는 ready/binding을 반환한다. Stop 자체는 terminal 증거가 아니다.
parse_turn의 ready만으로는 발행할 수 없으며 기존 binding은 절대 확장하지 않는다.
"""
from __future__ import annotations

from .conversation_transaction import collection_operation

import hashlib
import hmac
import json
import os
from pathlib import Path
from dataclasses import dataclass
from .claude_absent import _ancestry
from .conversation import (
    JSONL_SCAN_BYTES, JSONL_LINE_BYTES, JSONL_RECORD_LIMIT,
    CODEX_TARGET_TURN_SCHEMA_PIN, SourceIdentityV1, SourceBoundaryV1,
    open_verified_root, open_verified_jsonl_fd,
)

MODE = "codex-file-provenance-v1"
SCHEMA_PIN = CODEX_TARGET_TURN_SCHEMA_PIN


def _mac(service, body):
    wire = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(service.bindings.secret, MODE.encode() + b"\0" + wire, hashlib.sha256).hexdigest()


def _snapshot(service, locator, ancestors, turn_id, session):
    """전체 조상과 열린 원본의 바이트/식별자를 파싱 전후에 확인한다."""
    if _ancestry(locator, expected=ancestors) != ancestors:
        raise PermissionError("native ancestry changed")
    with open_verified_root(locator) as root, open_verified_jsonl_fd(locator, root_capability=root) as source:
        if source.identity.size > JSONL_SCAN_BYTES:
            raise PermissionError("native byte budget exceeded")
        raw = os.pread(source.fd, source.identity.size, 0)
        if len(raw) != source.identity.size:
            raise PermissionError("native short read")
        _validate_session(raw, session)
        selected = parse_turn(raw, turn_id)
        if service._digest_range(source.fd, 0, len(raw)) != hashlib.sha256(raw).hexdigest():
            raise PermissionError("native snapshot bytes changed")
        if SourceIdentityV1.from_stat(os.fstat(source.fd)) != source.identity:
            raise PermissionError("native snapshot identity changed")
        if _ancestry(locator, expected=ancestors) != ancestors:
            raise PermissionError("native snapshot ancestry changed")
        with open_verified_jsonl_fd(locator) as current:
            if current.identity != source.identity:
                raise PermissionError("native snapshot replaced")
        return raw, root.identity, source.identity, selected


def _authorized(service, board, task, receipt):
    policy = service._enabled_policy(board, "codex")
    created = service._verify_task_receipt(receipt, board=board, task=task, policy=policy, provider="codex")
    if not service.task_membership(board, task):
        raise PermissionError("observation membership required")
    return policy, created


def _unbound(service, board, task):
    try:
        service.bindings.get(board, task)
    except FileNotFoundError:
        return
    raise PermissionError("existing immutable binding cannot be sealed")


@collection_operation
def prepare(service, *, board, task, session, source_path, task_receipt, turn_id):
    """신뢰된 호출자의 네이티브 ID만 사용하며 텍스트/경로에서 ID를 추론하지 않는다."""
    if any(not isinstance(v, str) or not v or any(ord(c) < 32 for c in v) for v in (session, turn_id)):
        raise PermissionError("native session and turn ID required")
    policy, created = _authorized(service, board, task, task_receipt)
    _unbound(service, board, task)
    locator = service._locator("codex", Path(source_path))
    ancestors = _ancestry(locator)
    raw, _, identity, selected = _snapshot(service, locator, ancestors, turn_id, session)
    if raw and not raw.endswith(b"\n"):
        raise PermissionError("native preparation requires complete captured line")
    body = dict(mode=MODE, schema_pin=SCHEMA_PIN, board=board, task=task, provider="codex",
                session=session, turn_id=turn_id, allowed_root=str(locator.allowed_root),
                relative_path=str(locator.relative_path), ancestors=ancestors,
                source_identity=service._identity_payload(identity), captured_eof=len(raw),
                prefix_digest=hashlib.sha256(raw).hexdigest(),
                start_offset=selected.start_offset, start_ordinal=selected.start_ordinal,
                receipt_nonce=task_receipt["nonce"], receipt_created_at_ns=created,
                policy_generation=policy.generation, policy_version=policy.version)
    if _authorized(service, board, task, task_receipt) != (policy, created):
        raise PermissionError("native preparation policy changed")
    return {**body, "mac": _mac(service, body)}


# 기존 capture 명명과 호환하되 공통 hook에는 아직 연결하지 않는다.
capture = prepare


@collection_operation
def seal(service, *, board, task, prepared, task_receipt, turn_id, reconcile_existing=False):
    """terminal과 공개 요청/final이 모두 있을 때만 불변 binding을 발행한다."""
    body = {k: v for k, v in prepared.items() if k != "mac"}
    if not hmac.compare_digest(str(prepared.get("mac", "")), _mac(service, body)):
        raise PermissionError("native preparation authentication failed")
    if (body["mode"], body["schema_pin"], body["provider"], body["board"], body["task"], body["turn_id"], body["receipt_nonce"]) != (MODE, SCHEMA_PIN, "codex", board, task, turn_id, task_receipt.get("nonce")):
        raise PermissionError("native preparation scope mismatch")
    policy, created = _authorized(service, board, task, task_receipt)
    if (created, policy.generation, policy.version) != (body["receipt_created_at_ns"], body["policy_generation"], body["policy_version"]):
        raise PermissionError("native preparation policy changed")
    existing = None
    try:
        existing = service.bindings.get(board, task)
    except FileNotFoundError:
        pass
    else:
        if not reconcile_existing:
            raise PermissionError("existing immutable binding cannot be sealed")
    locator = service._locator("codex", Path(body["allowed_root"]) / body["relative_path"])
    if str(locator.allowed_root) != body["allowed_root"]:
        raise PermissionError("native provider root changed")
    raw, root_identity, identity, selected = _snapshot(service, locator, body["ancestors"], turn_id, body["session"])
    original = body["source_identity"]
    if (identity.device, identity.inode) != (original["device"], original["inode"]):
        raise PermissionError("native source identity changed")
    eof = body["captured_eof"]
    if eof != original["size"] or len(raw) < eof or hashlib.sha256(raw[:eof]).hexdigest() != body["prefix_digest"]:
        raise PermissionError("native captured prefix changed")
    if body["start_offset"] is not None:
        if (selected.start_offset, selected.start_ordinal) != (body["start_offset"], body["start_ordinal"]):
            raise PermissionError("native turn boundary changed")
    elif selected.start_offset is not None and selected.start_offset < eof:
        raise PermissionError("native turn precedes captured boundary")
    if not selected.ready:
        if existing is not None:
            raise PermissionError("existing immutable binding is not terminal-ready")
        return {"status": "pending", "binding": None}
    start, end = selected.start_offset, selected.end_offset
    if start is None or end is None:
        raise PermissionError("native terminal boundaries required")
    if existing is not None:
        from dataclasses import replace
        binding, stored_locator = existing
        service.authority._validate_binding(binding)
        sealed_identity = binding.source_identity
        # 이후 턴 append는 허용하지만 원래 준비 prefix 검증은 위에서 그대로 수행한다.
        # 봉인 시점 크기/mtime은 재작성하지 않고 inode와 최소 크기를 별도로 확인한다.
        if (sealed_identity.inode_tuple() != identity.inode_tuple()
                or not max(eof, end) <= sealed_identity.size <= identity.size
                or (sealed_identity.size == identity.size and sealed_identity != identity)):
            raise PermissionError("existing immutable binding source identity changed")
        expected = replace(
            binding, board=board, task=task, provider="codex", schema_pin=SCHEMA_PIN,
            profile_root_identity=root_identity, locator_ref=service.authority._locator_ref(locator),
            session=body["session"], turn_start=SourceBoundaryV1(start, selected.start_ordinal),
            turn_end=SourceBoundaryV1(end, selected.end_ordinal),
            boundary_alignment_proof="producer_jsonl_lines_v1", binding_version=2, generation=1,
            policy_version=policy.version, producer_execution=str(task_receipt["nonce"]),
            created_at_ns=created, source_range_digest=hashlib.sha256(raw[start:end]).hexdigest(),
            issuer_id=service.authority.issuer_id, codex_target_turn_id=turn_id)
        if stored_locator != locator or binding != expected:
            raise PermissionError("existing immutable binding differs from terminal-ready scope")
    else:
        pending = service.authority.begin_binding(
            board=board, task=task, provider="codex", schema_pin=SCHEMA_PIN,
            profile_root_identity=root_identity, locator=locator, source_identity=identity,
            session=body["session"], turn_start=SourceBoundaryV1(start, selected.start_ordinal),
            binding_version=2, generation=1, policy_version=policy.version,
            producer_execution=str(task_receipt["nonce"]), now_ns=created, codex_target_turn_id=turn_id)
        binding = service.authority.seal_binding(
            pending, turn_end=SourceBoundaryV1(end, selected.end_ordinal), source_identity=identity,
            expected_generation=1, now_ns=service.clock_ns(),
            source_range_digest=hashlib.sha256(raw[start:end]).hexdigest())
    # 실제 공개 projector와 같은 판단으로 request-only 불변 봉인을 방지한다.
    from .transcript_projection import CodexTargetTurnProjector, ProjectionError
    kinds = set()
    projector = CodexTargetTurnProjector()
    for line in raw[start:end].splitlines():
        record = json.loads(line)
        try:
            outcome = projector.project(record, binding=binding,
                                        source_event_id=str(record["ordinal"]), seq=0)
        except ProjectionError:
            continue
        kinds.update(event.kind for event in outcome.events if event.text and event.text.strip())
    if not {"user_message", "final_assistant"} <= kinds:
        if existing is not None:
            raise PermissionError("existing immutable binding is not public-final-ready")
        return {"status": "pending", "binding": None}
    if _authorized(service, board, task, task_receipt) != (policy, created):
        raise PermissionError("native policy changed before publication")
    if existing is not None:
        if service.bindings.get(board, task) != existing:
            raise PermissionError("immutable binding changed during reconciliation")
        return {"status": "ready", "binding": binding}
    service.bindings.put(binding=binding, locator=locator)
    return {"status": "ready", "binding": binding}


def _validate_session(raw, session):
    """목표 종료 뒤까지 전체 스냅샷에서 유일한 네이티브 세션 증거를 확인한다."""
    if not isinstance(session, str) or not session or any(ord(c) < 32 for c in session):
        raise PermissionError("native session ID required")
    seen = False
    for count, line in enumerate(raw.splitlines(keepends=True), 1):
        if count > JSONL_RECORD_LIMIT or len(line) > JSONL_LINE_BYTES:
            raise PermissionError("native record budget exceeded")
        if not line.endswith(b"\n"):
            break
        try:
            record = json.loads(line, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError) as exc:
            raise PermissionError("invalid native record") from exc
        if not isinstance(record, dict):
            raise PermissionError("invalid native envelope")
        if record.get("type") == "session_meta":
            payload = record.get("payload")
            if seen or not isinstance(payload, dict) or payload.get("id") != session:
                raise PermissionError("native session metadata mismatch or duplicate")
            seen = True
    if not seen:
        raise PermissionError("native session metadata required")


def _unique_object(pairs):
    """중복 ID의 마지막 값 우선 해석을 금지한다."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise PermissionError("duplicate native field")
        result[key] = value
    return result


@dataclass(frozen=True)
class TurnReadiness:
    start_offset: int | None = None
    end_offset: int | None = None
    start_ordinal: int | None = None
    end_ordinal: int | None = None
    request_count: int = 0
    final_count: int = 0

    @property
    def ready(self):
        return self.end_offset is not None and self.request_count > 0 and self.final_count > 0


def parse_turn(raw: bytes, turn_id: str) -> TurnReadiness:
    """정확한 시작부터 종료까지 선택하고 전역 ordinal을 유지한다."""
    if not isinstance(turn_id, str) or not turn_id:
        raise PermissionError("native turn ID required")
    start = first = previous = active = None
    seen = set()
    position = requests = finals = 0
    if len(raw) > JSONL_SCAN_BYTES:
        raise PermissionError("native byte budget exceeded")
    for count, line in enumerate(raw.splitlines(keepends=True), 1):
        if count > JSONL_RECORD_LIMIT or len(line) > JSONL_LINE_BYTES:
            raise PermissionError("native record budget exceeded")
        if not line.endswith(b"\n"):
            break
        try:
            record = json.loads(line, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError) as exc:
            raise PermissionError("invalid native record") from exc
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            raise PermissionError("invalid native envelope")
        payload = record["payload"]
        kind = payload.get("type")
        ordinal = record.get("ordinal")
        if type(ordinal) is not int or ordinal < 0 or (previous is not None and ordinal != previous + 1):
            raise PermissionError("noncontinuous native ordinal")
        previous = ordinal
        if record.get("type") == "event_msg" and kind in {"task_started", "task_complete"}:
            native = payload.get("turn_id")
            if not isinstance(native, str) or not native:
                raise PermissionError("missing native marker identity")
            if kind == "task_started":
                if active is not None or native in seen:
                    raise PermissionError("ambiguous native start")
                active = native
                seen.add(native)
                if native == turn_id:
                    start, first = position, ordinal
            else:
                if active != native:
                    raise PermissionError("unmatched native completion")
                active = None
                if native == turn_id:
                    return TurnReadiness(start, position + len(line), first, ordinal + 1, requests, finals)
        if start is not None and record.get("type") == "response_item" and kind == "message":
            metadata = payload.get("internal_chat_message_metadata_passthrough")
            if isinstance(metadata, dict) and metadata.get("turn_id") == turn_id:
                user = payload.get("role") == "user" and payload.get("phase") is None
                final = payload.get("role") == "assistant" and payload.get("phase") == "final_answer"
                content = payload.get("content")
                public = isinstance(content, list) and bool(content) and all(
                    isinstance(b, dict) and set(b) == {"type", "text"} and
                    b["type"] == ("input_text" if user else "output_text") and
                    isinstance(b["text"], str) for b in content)
                if public and any(b["text"].strip() for b in content):
                    requests += user
                    finals += final
        position += len(line)
    return TurnReadiness(start, None, first, None, requests, finals)
