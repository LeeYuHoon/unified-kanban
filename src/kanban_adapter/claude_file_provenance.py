"""Claude 2.1.268 기존 파일 전용 인증 준비/봉인. 공통 EOF 계약은 변경하지 않는다."""
from __future__ import annotations

from .conversation_transaction import collection_operation

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from .claude_absent import _ancestry, _UUID
from .conversation import (
    JSONL_LINE_BYTES, JSONL_RECORD_LIMIT, JSONL_SCAN_BYTES, JSONL_SCAN_DEADLINE_MS,
    SourceBoundaryV1, SourceIdentityV1, open_verified_jsonl_fd, open_verified_root,
)
from .transcript_projection import ClaudeProjector
from .claude_native_ancestry import add_attachment

MODE = "claude-file-provenance-v2"
SCHEMA_PIN = ClaudeProjector.schema_pin + ":claude-2.1.268-file-provenance-v2"


def _mac(service, body):
    wire = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(service.bindings.secret, b"claude-file-provenance-v2\0" + wire, hashlib.sha256).hexdigest()


def _range(raw, session, prompt_id):
    """이전 턴은 연결 검증에만 사용하고 정확한 현재 promptId부터 선택한다."""
    start = end = request_uuid = None
    position = 0
    seen = set()
    identities = set()  # 알 수 없는 레코드는 조상 관계를 연결하거나 UUID를 가리지 않는다.
    turn_seen = set()
    active_prompt = None
    prompts = set()
    deadline = time.monotonic() + JSONL_SCAN_DEADLINE_MS / 1000
    for records, line in enumerate(raw.splitlines(keepends=True), 1):
        if time.monotonic() > deadline:
            raise TimeoutError("file snapshot deadline exceeded")
        if len(line) > JSONL_LINE_BYTES or records > JSONL_RECORD_LIMIT:
            raise PermissionError("file snapshot record budget exceeded")
        if not line.endswith(b"\n"):
            break
        record = json.loads(line)
        if not isinstance(record, dict):
            raise PermissionError("invalid file snapshot record")
        kind = record.get("type")
        if not isinstance(kind, str) or not kind:
            raise PermissionError("invalid file snapshot type")
        for name in ("sessionId", "promptId", "uuid"):
            if name in record and (not isinstance(record[name], str) or not record[name]):
                raise PermissionError("invalid file snapshot identity field")
        parent = record.get("parentUuid")

        if parent is not None and (not isinstance(parent, str) or not parent):
            raise PermissionError("invalid file snapshot parent")
        if "sessionId" in record and record["sessionId"] != session:
            raise PermissionError("file snapshot session mismatch")
        if kind == "attachment":
            add_attachment(record, session=session, active_prompt=active_prompt,
                           seen=seen, turn_seen=turn_seen)
        if kind in {"user", "assistant"}:
            if record.get("sessionId") != session or not ClaudeProjector()._public_entry(record):
                raise PermissionError("ambiguous file snapshot conversation")
            message = record.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("role"), str) or message["role"] != kind:
                raise PermissionError("invalid file snapshot message")
            content = message.get("content")
            if "stop_reason" in message and message["stop_reason"] is not None and not isinstance(message["stop_reason"], str):
                raise PermissionError("invalid file snapshot stop reason")
            if isinstance(content, str) and kind == "user":
                pass
            elif isinstance(content, list) and content:
                for block in content:
                    if not isinstance(block, dict) or not isinstance(block.get("type"), str) or not block["type"]:
                        raise PermissionError("invalid file snapshot content block")
                    if block["type"] == "text" and not isinstance(block.get("text"), str):
                        raise PermissionError("invalid file snapshot text")
                    for name in ("id", "name", "tool_use_id"):
                        if name in block and (not isinstance(block[name], str) or not block[name]):
                            raise PermissionError("invalid file snapshot tool identity")
                    if "is_error" in block and not isinstance(block["is_error"], bool):
                        raise PermissionError("invalid file snapshot tool status")
            else:
                raise PermissionError("invalid file snapshot content")
            tool_result = kind == "user" and isinstance(content, list) and all(b.get("type") == "tool_result" for b in content)
            uuid = record.get("uuid")
            if not isinstance(uuid, str) or not uuid or uuid in seen:
                raise PermissionError("ambiguous message identity")
            if kind == "user" and not tool_result:
                native = record.get("promptId")
                if not isinstance(native, str) or not _UUID.fullmatch(native) or native in prompts:
                    raise PermissionError("ambiguous prompt boundary")
                if parent is not None and parent not in seen:
                    raise PermissionError("unbound request parent")
                if start is not None:
                    break
                prompts.add(native)
                active_prompt = native
                turn_seen = set()
                if native == prompt_id:
                    start, request_uuid = position, uuid
            elif active_prompt is None or parent not in turn_seen:
                raise PermissionError("unbound message parent")
            if record.get("promptId", active_prompt) != active_prompt:
                raise PermissionError("cross-prompt conversation")
            seen.add(uuid)
            turn_seen.add(uuid)
        if "uuid" in record:
            if record["uuid"] in identities:
                raise PermissionError("duplicate native record identity")
            identities.add(record["uuid"])
        position += len(line)
        if start is not None:
            end = position
    return None if start is None else (start, end, request_uuid)


def _snapshot(service, locator, ancestors, validate):
    """정규 경로, 전체 조상, 열린 inode와 두 번 읽은 원본 바이트를 함께 검증한다."""
    if _ancestry(locator, expected=ancestors) != ancestors:
        raise PermissionError("file ancestry identity changed")
    with open_verified_root(locator) as root, open_verified_jsonl_fd(locator, root_capability=root) as source:
        if source.identity.size > JSONL_SCAN_BYTES:
            raise PermissionError("file snapshot byte budget exceeded")
        raw = os.pread(source.fd, source.identity.size, 0)
        if len(raw) != source.identity.size:
            raise PermissionError("file snapshot short read")
        selected = validate(raw)
        if service._digest_range(source.fd, 0, len(raw)) != hashlib.sha256(raw).hexdigest():
            raise PermissionError("file snapshot bytes changed")
        if SourceIdentityV1.from_stat(os.fstat(source.fd)) != source.identity:
            raise PermissionError("file snapshot identity changed")
        if _ancestry(locator, expected=ancestors) != ancestors:
            raise PermissionError("file snapshot ancestry changed")
        with open_verified_jsonl_fd(locator) as current:
            if current.identity != source.identity:
                raise PermissionError("file snapshot replaced after pin")
        return raw, root.identity, source.identity, selected


@collection_operation
def capture(service, *, board, task, session, source_path, task_receipt, prompt_id):
    if not isinstance(prompt_id, str) or not _UUID.fullmatch(prompt_id):
        raise PermissionError("native prompt UUID required")
    if not isinstance(session, str) or not session:
        raise PermissionError("native session required")
    policy = service._enabled_policy(board, "claude")
    created = service._verify_task_receipt(task_receipt, board=board, task=task, policy=policy, provider="claude")
    if not service.task_membership(board, task):
        raise PermissionError("observation membership required")
    locator = service._locator("claude", Path(source_path))
    ancestors = _ancestry(locator)
    raw, _, identity, selected = _snapshot(
        service, locator, ancestors, lambda raw: _range(raw, session, prompt_id))
    if selected is None:
        if raw and not raw.endswith(b"\n"):
            raise PermissionError("waiting boundary is not captured EOF line boundary")
        start, uuid = len(raw), None
    else:
        start, _, uuid = selected
    body = dict(mode=MODE, board=board, task=task, provider="claude", session=session,
                prompt_id=prompt_id, allowed_root=str(locator.allowed_root),
                relative_path=str(locator.relative_path), ancestors=ancestors,
                source_identity=service._identity_payload(identity), captured_eof=len(raw),
                prefix_digest=hashlib.sha256(raw).hexdigest(), start_offset=start, request_uuid=uuid,
                receipt_nonce=task_receipt["nonce"], receipt_created_at_ns=created,
                policy_generation=policy.generation, policy_version=policy.version)
    return {**body, "mac": _mac(service, body)}


def _has_public_final(raw):
    """검증된 선택 범위만 검사하며 projector가 공개하는 텍스트만 인정한다."""
    deadline = time.monotonic() + JSONL_SCAN_DEADLINE_MS / 1000
    for line in raw.splitlines():
        if time.monotonic() > deadline:
            raise TimeoutError("file final readiness deadline exceeded")
        record = json.loads(line)
        if record.get("type") != "assistant":
            continue
        message = record["message"]
        if message.get("stop_reason") not in {"end_turn", "stop_sequence"}:
            continue
        for block in message["content"]:
            if block["type"] == "text" and block["text"].strip():
                return True
            if block["type"] not in {"text", "tool_use"}:
                break  # projector도 비공개/미지원 블록 뒤의 텍스트는 공개하지 않는다
    return False


@collection_operation
def seal(service, *, board, task, prepared, task_receipt, prompt_id, require_final=False,
         reconcile_existing=False):
    """명시적 final 모드는 ready/binding 또는 pending/None을 반환한다.

    pending은 발행하지 않으며 재시도마다 준비 시점의 권한/inode/prefix를 검증한다.
    복구는 저장된 binding과 현재 ready 범위가 정확히 같을 때만 허용한다.
    기본 호출의 기존 계약은 유지하며 기존 binding을 재봉인/확장하지 않는다.
    """
    body = {k: v for k, v in prepared.items() if k != "mac"}
    if not hmac.compare_digest(str(prepared.get("mac", "")), _mac(service, body)):
        raise PermissionError("file preparation authentication failed")
    if (body["mode"], body["provider"], body["board"], body["task"], body["prompt_id"], body["receipt_nonce"]) != (MODE, "claude", board, task, prompt_id, task_receipt.get("nonce")):
        raise PermissionError("file preparation scope mismatch")
    policy = service._enabled_policy(board, "claude")
    created = service._verify_task_receipt(task_receipt, board=board, task=task, policy=policy, provider="claude")
    if (created, policy.generation, policy.version) != (body["receipt_created_at_ns"], body["policy_generation"], body["policy_version"]):
        raise PermissionError("file preparation policy changed")
    if not service.task_membership(board, task):
        raise PermissionError("observation membership required")
    existing = None
    try:
        existing = service.bindings.get(board, task)
    except FileNotFoundError:
        pass
    else:
        if not require_final:
            return None  # 기존 불변 binding을 보충하거나 교체하지 않는다
        if not reconcile_existing:
            raise PermissionError("existing immutable binding cannot be final-sealed")
    locator = service._locator("claude", Path(body["allowed_root"]) / body["relative_path"])
    if str(locator.allowed_root) != body["allowed_root"]:
        raise PermissionError("file provider root changed")
    raw, root_identity, identity, selected = _snapshot(
        service, locator, body["ancestors"], lambda raw: _range(raw, body["session"], prompt_id))
    original = body["source_identity"]
    if (identity.device, identity.inode) != (original["device"], original["inode"]):
        raise PermissionError("file source identity changed")
    eof = body["captured_eof"]
    if eof != original["size"] or len(raw) < eof or hashlib.sha256(raw[:eof]).hexdigest() != body["prefix_digest"]:
        raise PermissionError("file captured prefix changed")
    if selected is None:
        if existing is not None:
            raise PermissionError("existing immutable binding has no matching native request")
        return {"status": "pending", "binding": None} if require_final else None
    start, end, uuid = selected
    if body["request_uuid"] is None:
        if body["start_offset"] != eof or start < eof:
            raise PermissionError("file request precedes authenticated lower boundary")
    elif (start, uuid) != (body["start_offset"], body["request_uuid"]):
        raise PermissionError("file native boundary changed")
    if require_final and not _has_public_final(raw[start:end]):
        if existing is not None:
            raise PermissionError("existing immutable binding is not final-ready")
        return {"status": "pending", "binding": None}
    if existing is not None:
        from dataclasses import replace
        binding, stored_locator = existing
        service.authority._validate_binding(binding)
        # 발행 시간/서명은 인증된 저장값을 유지하고 나머지 범위는 원본에서 재계산한다.
        # 전체 identity 일치가 필요하므로 이후 append도 복구 근거로 추측하지 않는다.
        expected = replace(
            binding, board=board, task=task, provider="claude", schema_pin=SCHEMA_PIN,
            profile_root_identity=root_identity, locator_ref=service.authority._locator_ref(locator),
            source_identity=identity, session=body["session"],
            turn_start=SourceBoundaryV1(byte_offset=start, ordinal=0),
            turn_end=SourceBoundaryV1(byte_offset=end, ordinal=raw[start:end].count(b"\n")),
            boundary_alignment_proof="producer_jsonl_lines_v1", binding_version=2, generation=1,
            policy_version=policy.version, producer_execution=str(task_receipt["nonce"]),
            created_at_ns=created, source_range_digest=hashlib.sha256(raw[start:end]).hexdigest(),
            issuer_id=service.authority.issuer_id, codex_target_turn_id=None)
        if stored_locator != locator or binding != expected:
            raise PermissionError("existing immutable binding differs from final-ready scope")
        if service._enabled_policy(board, "claude") != policy:
            raise PermissionError("file policy changed before reconciliation")
        if service.bindings.get(board, task) != existing:
            raise PermissionError("immutable binding changed during reconciliation")
        return {"status": "ready", "binding": binding}
    pending = service.authority.begin_binding(
        board=board, task=task, provider="claude", schema_pin=SCHEMA_PIN,
        profile_root_identity=root_identity, locator=locator, source_identity=identity,
        session=body["session"], turn_start=SourceBoundaryV1(byte_offset=start, ordinal=0),
        binding_version=2, generation=1, policy_version=policy.version,
        producer_execution=str(task_receipt["nonce"]), now_ns=created)
    binding = service.authority.seal_binding(
        pending, turn_end=SourceBoundaryV1(byte_offset=end, ordinal=raw[start:end].count(b"\n")),
        source_identity=identity, expected_generation=1, now_ns=service.clock_ns(),
        source_range_digest=hashlib.sha256(raw[start:end]).hexdigest())
    if service._enabled_policy(board, "claude") != policy:
        raise PermissionError("file policy changed before publication")
    service.bindings.put(binding=binding, locator=locator)
    return {"status": "ready", "binding": binding} if require_final else binding
