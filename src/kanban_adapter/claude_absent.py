"""Claude 2.1.268 absent-start는 신뢰하는 로컬 작성자의 최초 열기만 허용하며 완전 수집이 아니다.

새 영수증에 연결된 관측만 허용하며 캡처 시점에는 생산자 inode가 없다.
최초 열기 전 동일 UID의 교체 공격은 명시적으로 이 계약 범위 밖이다.
"""
from __future__ import annotations

from .conversation_transaction import collection_operation

import hashlib
import hmac
import json
import os
import re
import stat
import time
from pathlib import Path

from .conversation import (
    JSONL_LINE_BYTES, JSONL_RECORD_LIMIT, JSONL_SCAN_BYTES, JSONL_SCAN_DEADLINE_MS,
    SourceBoundaryV1, SourceIdentityV1, _directory_flags,
    open_verified_jsonl_fd, open_verified_root,
)
from .transcript_projection import ClaudeProjector
from .claude_native_ancestry import add_attachment

SCHEMA_PIN = ClaudeProjector.schema_pin + ":claude-2.1.268-absent-limited-partial-v1"
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


def _mac(service, value):
    wire = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(service.bindings.secret, b"claude-absent-v1\0" + wire, hashlib.sha256).hexdigest()


def _ancestry(locator, *, absent=False, expected=None):
    """ENOENT 또는 식별자를 입증하는 동안 기존 디렉터리 FD를 모두 유지한다."""
    with open_verified_root(locator) as root:
        chain = list(root._chain)
        owned = []
        try:
            for part in locator.relative_path.parts[:-1]:
                try:
                    child = os.open(part, _directory_flags(), dir_fd=chain[-1])
                except FileNotFoundError:
                    if absent:
                        break
                    raise
                owned.append(child)
                info = os.fstat(child)
                if info.st_uid != locator.expected_owner or stat.S_IMODE(info.st_mode) & 0o022:
                    raise PermissionError("untrusted absent-source ancestry")
                chain.append(child)
            else:
                if absent:
                    try:
                        os.stat(locator.relative_path.name, dir_fd=chain[-1], follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise PermissionError("source absence was not verified")
            identities = [[os.fstat(fd).st_dev, os.fstat(fd).st_ino] for fd in chain]
            if expected is not None and identities[:len(expected)] != expected:
                raise PermissionError("absent-source ancestry identity changed")
            # 모든 접근 권한을 유지한 채 동일한 정규 네임스페이스를 다시 연다.
            with open_verified_root(locator) as current:
                if [(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in current._chain] != [tuple(v) for v in identities[:len(root._chain)]]:
                    raise PermissionError("absent-source root changed during proof")
            return identities
        finally:
            for fd in reversed(owned):
                os.close(fd)


@collection_operation
def capture(service, *, board, task, session, source_path, task_receipt, prompt_id):
    if not isinstance(prompt_id, str) or not _UUID.fullmatch(prompt_id):
        return None
    if not isinstance(session, str) or not session:
        return None
    policy = service._enabled_policy(board, "claude")
    created = service._verify_task_receipt(task_receipt, board=board, task=task, policy=policy, provider="claude")
    if not service.task_membership(board, task):
        raise PermissionError("observation membership required")
    locator = service._locator("claude", Path(source_path))
    ancestors = _ancestry(locator, absent=True)
    # 동일한 식별자를 유지한 네임스페이스에서 부재를 재확인하며 오프셋으로 우회하지 않는다.
    if _ancestry(locator, absent=True) != ancestors:
        raise PermissionError("source ancestry changed during absence capture")
    body = dict(mode="claude-absent-v1", board=board, task=task, provider="claude",
                session=session, prompt_id=prompt_id, allowed_root=str(locator.allowed_root),
                relative_path=str(locator.relative_path), ancestors=ancestors,
                receipt_nonce=task_receipt["nonce"], receipt_created_at_ns=created,
                policy_generation=policy.generation, policy_version=policy.version)
    return {**body, "mac": _mac(service, body)}


def _range(raw, session, prompt_id):
    """네이티브 promptId와 parentUuid 연결만 사용하며 제목이나 본문으로 매칭하지 않는다."""
    start = end = None
    position = 0
    seen = set()
    identities = set()  # 알 수 없는 레코드는 조상 관계를 연결하거나 UUID를 가리지 않는다.
    turn_seen = set()
    records = 0
    deadline = time.monotonic() + JSONL_SCAN_DEADLINE_MS / 1000
    for line in raw.splitlines(keepends=True):
        if time.monotonic() > deadline:
            raise TimeoutError("absent snapshot deadline exceeded")
        if not line.endswith(b"\n"):
            break  # 비동기 기록 중인 꼬리는 봉인된 레코드가 아니다
        if len(line) > JSONL_LINE_BYTES:
            raise PermissionError("absent snapshot line budget exceeded")
        records += 1
        if records > JSONL_RECORD_LIMIT:
            raise PermissionError("absent snapshot record budget exceeded")
        record = json.loads(line)
        if not isinstance(record, dict):
            raise PermissionError("invalid absent snapshot record")
        kind = record.get("type")
        if not isinstance(kind, str) or not kind:
            raise PermissionError("invalid absent snapshot type")
        for name in ("sessionId", "promptId", "uuid"):
            if name in record and (not isinstance(record[name], str) or not record[name]):
                raise PermissionError("invalid absent snapshot identity field")
        parent = record.get("parentUuid")

        if parent is not None and (not isinstance(parent, str) or not parent):
            raise PermissionError("invalid absent snapshot parent")
        if "sessionId" in record and record["sessionId"] != session:
            raise PermissionError("absent snapshot session mismatch")
        if kind == "attachment":
            add_attachment(record, session=session, active_prompt=prompt_id if start is not None else None,
                           seen=seen, turn_seen=turn_seen)
        if kind in {"user", "assistant"}:
            if record.get("sessionId") != session or not ClaudeProjector()._public_entry(record):
                raise PermissionError("ambiguous absent snapshot conversation")
            message = record.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("role"), str) or message["role"] != kind:
                raise PermissionError("invalid absent snapshot message")
            content = message.get("content")
            if "stop_reason" in message and message["stop_reason"] is not None and not isinstance(message["stop_reason"], str):
                raise PermissionError("invalid absent snapshot stop reason")
            if isinstance(content, str) and kind == "user":
                pass
            elif isinstance(content, list) and content:
                for block in content:
                    if not isinstance(block, dict) or not isinstance(block.get("type"), str) or not block["type"]:
                        raise PermissionError("invalid absent snapshot content block")
                    if block["type"] == "text" and not isinstance(block.get("text"), str):
                        raise PermissionError("invalid absent snapshot text")
                    for name in ("id", "name", "tool_use_id"):
                        if name in block and (not isinstance(block[name], str) or not block[name]):
                            raise PermissionError("invalid absent snapshot tool identity")
                    if "is_error" in block and not isinstance(block["is_error"], bool):
                        raise PermissionError("invalid absent snapshot tool status")
            else:
                raise PermissionError("invalid absent snapshot content")
            tool_result = kind == "user" and isinstance(content, list) and bool(content) and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            if kind == "user" and not tool_result:
                if start is not None:
                    if record.get("promptId") == prompt_id:
                        raise PermissionError("duplicate prompt boundary")
                    break  # 다음 턴은 이 스냅샷 범위 밖이다
                if record.get("promptId") != prompt_id:
                    raise PermissionError("first prompt boundary mismatch")
                start = position
            elif start is None:
                raise PermissionError("conversation precedes prompt boundary")
            elif parent not in turn_seen:
                raise PermissionError("unbound message parent")
            if record.get("promptId", prompt_id) != prompt_id:
                raise PermissionError("cross-prompt conversation")
            uuid = record.get("uuid")
            if not isinstance(uuid, str) or not uuid or uuid in seen:
                raise PermissionError("ambiguous message identity")
            if seen and (not isinstance(parent, str) or parent not in seen):
                raise PermissionError("unbound message parent")
            if not seen and parent is not None:
                raise PermissionError("first prompt has preexisting parent")
            seen.add(uuid)
            turn_seen.add(uuid)
        if "uuid" in record:
            if record["uuid"] in identities:
                raise PermissionError("duplicate native record identity")
            identities.add(record["uuid"])
        position += len(line)
        if start is not None:
            end = position
    if start is None or end is None:
        return None
    return start, end


@collection_operation
def seal(service, *, board, task, prepared, task_receipt, prompt_id):
    body = {k: v for k, v in prepared.items() if k != "mac"}
    if not hmac.compare_digest(str(prepared.get("mac", "")), _mac(service, body)):
        raise PermissionError("absent receipt authentication failed")
    if body.get("mode") != "claude-absent-v1":
        raise PermissionError("absent receipt mode mismatch")
    if (body["board"], body["task"], body["prompt_id"], body["receipt_nonce"]) != (board, task, prompt_id, task_receipt.get("nonce")):
        raise PermissionError("absent receipt prompt/task replay")
    policy = service._enabled_policy(board, "claude")
    created = service._verify_task_receipt(task_receipt, board=board, task=task, policy=policy, provider="claude")
    if (created, policy.generation, policy.version) != (body["receipt_created_at_ns"], body["policy_generation"], body["policy_version"]):
        raise PermissionError("absent receipt policy changed")
    if not service.task_membership(board, task):
        raise PermissionError("observation membership required")
    try:
        service.bindings.get(board, task)
    except FileNotFoundError:
        pass
    else:
        return None  # 불변 스냅샷이므로 재실행 갱신이나 소급 수집하지 않는다
    locator = service._locator("claude", Path(body["allowed_root"]) / body["relative_path"])
    if str(locator.allowed_root) != body["allowed_root"]:
        raise PermissionError("absent source root configuration changed")
    ancestors = _ancestry(locator, expected=body["ancestors"])
    with open_verified_root(locator) as root, open_verified_jsonl_fd(locator, root_capability=root) as source:
        if source.identity.size > JSONL_SCAN_BYTES:
            raise PermissionError("absent snapshot byte budget exceeded")
        raw = os.pread(source.fd, source.identity.size, 0)
        if len(raw) != source.identity.size:
            raise PermissionError("absent snapshot short read")
        selected = _range(raw, body["session"], prompt_id)
        if selected is None:
            return None
        start, end = selected
        digest = hashlib.sha256(raw[start:end]).hexdigest()
        if service._digest_range(source.fd, start, end) != digest:
            raise PermissionError("absent snapshot bytes changed")
        if SourceIdentityV1.from_stat(os.fstat(source.fd)) != source.identity:
            raise PermissionError("absent snapshot identity changed")
        if _ancestry(locator, expected=ancestors) != ancestors:
            raise PermissionError("absent snapshot ancestry changed")
        with open_verified_jsonl_fd(locator) as current:
            if current.identity != source.identity:
                raise PermissionError("absent snapshot source replaced after pin")
        pending = service.authority.begin_binding(
            board=board, task=task, provider="claude", schema_pin=SCHEMA_PIN,
            profile_root_identity=root.identity, locator=locator, source_identity=source.identity,
            session=body["session"], turn_start=SourceBoundaryV1(byte_offset=start, ordinal=0),
            binding_version=1, generation=1, policy_version=policy.version,
            producer_execution=str(task_receipt["nonce"]), now_ns=created)
        binding = service.authority.seal_binding(
            pending, turn_end=SourceBoundaryV1(byte_offset=end, ordinal=raw[start:end].count(b"\n")),
            source_identity=source.identity, expected_generation=1,
            now_ns=service.clock_ns(), source_range_digest=digest)
    if service._enabled_policy(board, "claude") != policy:
        raise PermissionError("absent snapshot policy changed before publication")
    service.bindings.put(binding=binding, locator=locator)
    return binding


FINAL_MODE = "claude-absent-first-open-final-v1"


def _final_authorize(service, prepared, *, board, task, task_receipt, prompt_id, mode,
                     reconcile_existing=False):
    body = {k: v for k, v in prepared.items() if k != "mac"}
    if not hmac.compare_digest(str(prepared.get("mac", "")), _mac(service, body)):
        raise PermissionError("absent preparation authentication failed")
    if (body.get("mode"), body.get("provider"), body.get("board"), body.get("task"),
            body.get("prompt_id"), body.get("receipt_nonce")) != (
            mode, "claude", board, task, prompt_id, task_receipt.get("nonce")):
        raise PermissionError("absent preparation scope mismatch")
    if not isinstance(prompt_id, str) or not _UUID.fullmatch(prompt_id):
        raise PermissionError("native prompt UUID required")
    policy = service._enabled_policy(board, "claude")
    created = service._verify_task_receipt(task_receipt, board=board, task=task, policy=policy, provider="claude")
    if (created, policy.generation, policy.version) != (
            body["receipt_created_at_ns"], body["policy_generation"], body["policy_version"]):
        raise PermissionError("absent preparation policy changed")
    if not service.task_membership(board, task):
        raise PermissionError("observation membership required")
    if not reconcile_existing:
        try:
            service.bindings.get(board, task)
        except FileNotFoundError:
            pass
        else:
            raise PermissionError("existing immutable binding cannot be final-sealed")
    locator = service._locator("claude", Path(body["allowed_root"]) / body["relative_path"])
    if str(locator.allowed_root) != body["allowed_root"]:
        raise PermissionError("absent source root configuration changed")
    return body, policy, created, locator


@collection_operation
def prepare_final(service, *, board, task, prepared, task_receipt, prompt_id):
    """최초 안전한 open을 인증 준비로 고정한다. 반환값을 재시도 전에 영속 저장한다.

    이 함수는 최초 부재 증거에 한 번만 적용한다. 재시도는 반드시 seal_final에
    반환된 준비를 전달한다. 실패한 준비를 버리고 원래 부재 증거로 재고정하지 않는다.
    """
    from .claude_file_provenance import _snapshot
    body, policy, _, locator = _final_authorize(
        service, prepared, board=board, task=task, task_receipt=task_receipt,
        prompt_id=prompt_id, mode="claude-absent-v1")
    ancestors = _ancestry(locator, expected=body["ancestors"])
    raw, _, identity, selected = _snapshot(
        service, locator, ancestors, lambda raw: _range(raw, body["session"], prompt_id))
    # 미완성 첫 줄도 그대로 해시하여 다음 open에서 다른 파일로 갈아타지 못하게 한다.
    pin = dict(body, mode=FINAL_MODE, absent_ancestors=body["ancestors"], ancestors=ancestors,
               source_identity=service._identity_payload(identity), captured_eof=len(raw),
               prefix_digest=hashlib.sha256(raw).hexdigest(),
               start_offset=selected[0] if selected else None)
    if service._enabled_policy(board, "claude") != policy:
        raise PermissionError("absent preparation policy changed before pin")
    return {**pin, "mac": _mac(service, pin)}


@collection_operation
def seal_final(service, *, board, task, prepared, task_receipt, prompt_id,
               reconcile_existing=False):
    """고정된 최초 inode에서만 final을 기다린다. pending은 공개 binding이 없다."""
    from .claude_file_provenance import _snapshot, _has_public_final, SCHEMA_PIN as FILE_SCHEMA_PIN
    body, policy, created, locator = _final_authorize(
        service, prepared, board=board, task=task, task_receipt=task_receipt,
        prompt_id=prompt_id, mode=FINAL_MODE, reconcile_existing=reconcile_existing)
    existing = None
    try:
        existing = service.bindings.get(board, task)
    except FileNotFoundError:
        pass
    _ancestry(locator, expected=body["absent_ancestors"])
    # 부재 시작은 이전 공개 대화 없이 검증된 attachment preamble만 허용한다.
    raw, root_identity, identity, selected = _snapshot(
        service, locator, body["ancestors"], lambda raw: _range(raw, body["session"], prompt_id))
    original = body["source_identity"]
    if (identity.device, identity.inode) != (original["device"], original["inode"]):
        raise PermissionError("absent first-open source identity changed")
    eof = body["captured_eof"]
    if eof != original["size"] or len(raw) < eof or hashlib.sha256(raw[:eof]).hexdigest() != body["prefix_digest"]:
        raise PermissionError("absent first-open prefix changed")
    if selected is not None and body["start_offset"] is not None and selected[0] != body["start_offset"]:
        raise PermissionError("absent first-open request boundary changed")
    if service._enabled_policy(board, "claude") != policy:
        raise PermissionError("absent final policy changed during snapshot")
    if selected is None or not _has_public_final(raw[selected[0]:selected[1]]):
        if existing is not None:
            raise PermissionError("existing immutable binding is not final-ready")
        return {"status": "pending", "binding": None}
    start, end = selected
    if existing is not None:
        from dataclasses import replace
        binding, stored_locator = existing
        service.authority._validate_binding(binding)
        # 최초 pin과 현재 범위로 전 필드를 비교한다. 발행/범위 확장은 하지 않는다.
        expected = replace(
            binding, board=board, task=task, provider="claude", schema_pin=FILE_SCHEMA_PIN,
            profile_root_identity=root_identity, locator_ref=service.authority._locator_ref(locator),
            source_identity=identity, session=body["session"],
            turn_start=SourceBoundaryV1(byte_offset=start, ordinal=0),
            turn_end=SourceBoundaryV1(byte_offset=end, ordinal=raw[start:end].count(b"\n")),
            boundary_alignment_proof="producer_jsonl_lines_v1", binding_version=2, generation=1,
            policy_version=policy.version, producer_execution=str(task_receipt["nonce"]),
            created_at_ns=created, source_range_digest=hashlib.sha256(raw[start:end]).hexdigest(),
            issuer_id=service.authority.issuer_id, codex_target_turn_id=None)
        if stored_locator != locator or binding != expected:
            raise PermissionError("existing immutable binding differs from absent final scope")
        if service._enabled_policy(board, "claude") != policy or not service.task_membership(board, task):
            raise PermissionError("absent final authority changed during reconciliation")
        if service.bindings.get(board, task) != existing:
            raise PermissionError("immutable binding changed during reconciliation")
        return {"status": "ready", "binding": binding}
    # 이미 검증한 네이티브 범위에는 별도 v2 권한만 쓰며 공통 EOF 계약은 그대로 둔다.
    pending = service.authority.begin_binding(
        board=board, task=task, provider="claude", schema_pin=FILE_SCHEMA_PIN,
        profile_root_identity=root_identity, locator=locator, source_identity=identity,
        session=body["session"], turn_start=SourceBoundaryV1(byte_offset=start, ordinal=0),
        binding_version=2, generation=1, policy_version=policy.version,
        producer_execution=str(task_receipt["nonce"]), now_ns=created)
    binding = service.authority.seal_binding(
        pending, turn_end=SourceBoundaryV1(byte_offset=end, ordinal=raw[start:end].count(b"\n")),
        source_identity=identity, expected_generation=1, now_ns=service.clock_ns(),
        source_range_digest=hashlib.sha256(raw[start:end]).hexdigest())
    if service._enabled_policy(board, "claude") != policy:
        raise PermissionError("absent final policy changed before publication")
    service.bindings.put(binding=binding, locator=locator)
    return {"status": "ready", "binding": binding}
