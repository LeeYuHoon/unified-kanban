"""검증된 JSONL range를 provider 공개 schema로만 projection한다.

Codex schema pin: openai/codex rust-v0.145.0의 RolloutLine/EventMsg/ResponseItem.
Claude 필터 pin: claude-agent-sdk-python@a8b1e285f97f8dbcb7b10226d74ba0d551b493f4
sessions.py의 isSidechain/isMeta/
isCompactSummary/teamName 검사. 지원 근거가 없는 SQLite와 Hermes transcript는 비활성화한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from .conversation import (
    API_EVENT_LIMIT,
    API_RESPONSE_BYTES,
    JSONL_LINE_BYTES,
    JSONL_RECORD_LIMIT,
    JSONL_SCAN_BYTES,
    TOOL_NAME_RE,
    ConversationEventV1,
    PrivateFixtureAuthority,
    PrivatePolicyStore,
    ProjectionGrantV1,
    SanitizedText,
    SourceBoundaryV1,
    SourceLocator,
    SourceReadLimiter,
    TrustedObservationBindingV1,
    CODEX_TARGET_TURN_SCHEMA_PIN,
    validate_codex_target_turn,
    normalize_timestamp,
    opaque_event_id,
    opaque_replay_id,
    opaque_tool_call_ref,
    open_verified_jsonl_fd,
    open_verified_root,
    sanitize_event_text,
    strict_json_loads,
)


class ProjectionError(ValueError):
    pass


class SourceRotatedError(ProjectionError):
    pass


_DEFAULT_READ_LIMITER = SourceReadLimiter()


@dataclass(frozen=True, slots=True)
class ProjectionLimits:
    max_events: int = API_EVENT_LIMIT
    max_response_bytes: int = API_RESPONSE_BYTES
    max_line_bytes: int = JSONL_LINE_BYTES
    max_records: int = JSONL_RECORD_LIMIT
    max_scan_bytes: int = JSONL_SCAN_BYTES
    deadline_ms: int = 2_000
    malformed_limit: int = 8

    def __post_init__(self) -> None:
        for name in (
            "max_events",
            "max_response_bytes",
            "max_line_bytes",
            "max_records",
            "max_scan_bytes",
            "deadline_ms",
            "malformed_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_events > API_EVENT_LIMIT:
            raise ValueError("max_events exceeds the API cap")
        if self.max_response_bytes > API_RESPONSE_BYTES:
            raise ValueError("max_response_bytes exceeds the API cap")
        if self.max_line_bytes > JSONL_LINE_BYTES:
            raise ValueError("max_line_bytes exceeds the source line cap")
        if self.max_records > JSONL_RECORD_LIMIT:
            raise ValueError("max_records exceeds the record cap")
        if self.max_scan_bytes > JSONL_SCAN_BYTES:
            raise ValueError("max_scan_bytes exceeds the scan cap")
        if self.deadline_ms > 3_000:
            raise ValueError("deadline exceeds the API cap")
        if self.malformed_limit > 64:
            raise ValueError("malformed_limit exceeds the cap")


@dataclass(frozen=True, slots=True)
class ProjectionPage:
    events: tuple[ConversationEventV1, ...]
    next_cursor: str | None
    completeness: str
    counts: Mapping[str, Mapping[str, int | bool]]
    capabilities: Mapping[str, str]
    malformed_count: int
    dropped_count: int
    truncated: bool
    source_availability: str

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple) or len(self.events) > API_EVENT_LIMIT:
            raise TypeError("events must be a bounded tuple")
        if not all(isinstance(event, ConversationEventV1) for event in self.events):
            raise TypeError("events contain an invalid DTO")
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str)
            or re.fullmatch(r"cu_[0-9a-f]{64}", self.next_cursor) is None
        ):
            raise ValueError("next cursor is not opaque")
        if self.completeness not in {"complete", "partial"}:
            raise ValueError("invalid completeness")
        if self.source_availability not in {"available", "rotated", "unavailable"}:
            raise ValueError("invalid source availability")
        for name in ("malformed_count", "dropped_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{name} must be a nonnegative integer")
        if not isinstance(self.truncated, bool):
            raise TypeError("truncated must be boolean")
        frozen_counts: dict[str, Mapping[str, int | bool]] = {}
        for kind, value in self.counts.items():
            copied = dict(value)
            if set(copied) != {"value", "exact"}:
                raise ValueError("count must have exact schema")
            count = copied["value"]
            exact = copied["exact"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise TypeError("count value must be a nonnegative integer")
            if not isinstance(exact, bool):
                raise TypeError("count exact must be boolean")
            frozen_counts[str(kind)] = MappingProxyType(copied)
        copied_capabilities = dict(self.capabilities)
        if any(
            status not in {"supported", "metadata_only", "unsupported"}
            for status in copied_capabilities.values()
        ):
            raise ValueError("capability status is invalid")
        object.__setattr__(self, "counts", MappingProxyType(frozen_counts))
        object.__setattr__(self, "capabilities", MappingProxyType(copied_capabilities))

    def to_public_dict(self) -> dict[str, Any]:
        self.__post_init__()
        return {
            "schema_version": 1,
            "events": [event.to_public_dict() for event in self.events],
            "next_cursor": self.next_cursor,
            "completeness": self.completeness,
            "counts": {kind: dict(value) for kind, value in self.counts.items()},
            "capabilities": dict(self.capabilities),
            "malformed_count": self.malformed_count,
            "dropped_count": self.dropped_count,
            "truncated": self.truncated,
            "source_availability": self.source_availability,
        }


@dataclass(frozen=True, slots=True)
class ProjectOutcome:
    events: tuple[ConversationEventV1, ...] = ()
    dropped_count: int = 0
    unsupported_count: int = 0


class StrictProjector(Protocol):
    provider: str
    schema_pin: str
    capabilities: Mapping[str, str]

    def project(
        self,
        record: Mapping[str, Any],
        *,
        binding: TrustedObservationBindingV1,
        source_event_id: str,
        seq: int,
    ) -> ProjectOutcome: ...


def _strict_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProjectionError("record must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ProjectionError("record keys must be strings")
    return value


def _text(value: Any, *, maximum: int = 64_000) -> SanitizedText:
    if not isinstance(value, str) or len(value) > maximum:
        raise ProjectionError("invalid text")
    sanitized = sanitize_event_text(value)
    if sanitized.text is None:
        return sanitized
    encoded = sanitized.text.encode("utf-8")
    if len(encoded) <= 8_192:
        return sanitized
    clipped = encoded[:8_160].decode("utf-8", errors="ignore") + "…[truncated]"
    return SanitizedText(clipped, "partially_redacted", True)


def _combine_text(parts: Sequence[SanitizedText]) -> SanitizedText:
    if any(part.text is None for part in parts):
        return SanitizedText(None, "failed_closed", any(part.truncated for part in parts))
    combined = sanitize_event_text("".join(part.text or "" for part in parts))
    if any(part.truncated for part in parts):
        return SanitizedText(combined.text, "partially_redacted", True)
    if any(part.state != "not_applicable" for part in parts):
        return SanitizedText(combined.text, "applied", combined.truncated)
    return combined


def _timestamp(record: Mapping[str, Any]) -> str | None:
    try:
        return normalize_timestamp(record.get("timestamp"))
    except (TypeError, ValueError) as exc:
        raise ProjectionError("invalid timestamp") from exc


def _tool_name(value: Any) -> str:
    if not isinstance(value, str) or not TOOL_NAME_RE.fullmatch(value):
        raise ProjectionError("invalid tool name")
    return value


def _mcp_metadata(tool_name: str) -> Mapping[str, str] | None:
    if not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__")
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    if not TOOL_NAME_RE.fullmatch(parts[1]) or not TOOL_NAME_RE.fullmatch(parts[2]):
        return None
    return {"server": parts[1], "tool": parts[2]}


def _event(
    binding: TrustedObservationBindingV1,
    source_event_id: str,
    seq: int,
    kind: str,
    *,
    timestamp: Any = None,
    text: str | SanitizedText | None = None,
    tool: str | None = None,
    tool_call_ref: str | None = None,
    status: str | None = None,
    mcp: Mapping[str, str] | None = None,
) -> ConversationEventV1:
    sanitized = text if isinstance(text, SanitizedText) else (
        sanitize_event_text(text) if text is not None else None
    )
    actual_kind = "mcp_call" if kind == "tool_call" and mcp is not None else kind
    return ConversationEventV1(
        replay_id=opaque_replay_id(binding, source_event_id),
        event_id=opaque_event_id(binding, source_event_id),
        seq=seq,
        kind=actual_kind,
        timestamp=timestamp,
        text=sanitized.text if sanitized is not None else None,
        redaction=sanitized.state if sanitized is not None else "not_applicable",
        tool=tool if actual_kind == "tool_call" else None,
        mcp=mcp if actual_kind == "mcp_call" else None,
        tool_call_ref=tool_call_ref,
        status=status,
    )


class CodexProjector:
    """0.145 공개 투영에 검토된 0.154 메시지 봉투 형식을 추가한다.

    바인딩 고정 버전은 기존의 봉인된 생산자 영수증과 호환성을 유지한다.
    0.154 전체를 지원하는 것은 아니다. item_completed 미러와 usage
    레코드는 계속 제외하며 response_item의 user/input_text와 명시적인
    assistant/final_answer/output_text만 공개 이벤트로 승격한다. 비공개 메타데이터는
    구조만 검증하며 공개 이벤트에 복사하지 않는다.
    """

    provider = "codex"
    schema_pin = "openai/codex@rust-v0.145.0"
    message_format = "openai/codex@rust-v0.154.0:reviewed-message-envelope-v1"
    _envelope_fields = frozenset({"timestamp", "ordinal", "type", "payload"})
    _message_fields = frozenset({"type", "id", "role", "content", "phase", "end_turn"})
    _native_metadata_field = "internal_chat_message_metadata_passthrough"

    @classmethod
    def _validate_message(cls, record: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        native = cls._native_metadata_field in payload
        allowed = cls._message_fields | ({cls._native_metadata_field} if native else set())
        if set(payload) - allowed:
            raise ProjectionError("unknown message field")
        if "id" in payload and payload["id"] is not None and not isinstance(payload["id"], str):
            raise ProjectionError("invalid message id")
        if "end_turn" in payload and payload["end_turn"] is not None and not isinstance(payload["end_turn"], bool):
            raise ProjectionError("invalid end_turn")
        if native:
            if _ordinal(record) is None or not isinstance(payload.get("id"), str):
                raise ProjectionError("native message requires ordinal and id")
            metadata = _strict_mapping(payload[cls._native_metadata_field])
            if set(metadata) != {"turn_id", "create_time", "content_item_kinds"}:
                raise ProjectionError("unknown native metadata shape")
            if not isinstance(metadata["turn_id"], str) or type(metadata["create_time"]) not in (int, float):
                raise ProjectionError("invalid native metadata types")
            kinds = metadata["content_item_kinds"]
            if not isinstance(kinds, list) or not all(isinstance(kind, str) for kind in kinds):
                raise ProjectionError("invalid content item kinds")
    capabilities = MappingProxyType(
        {
            "user_message": "supported",
            "assistant_final": "supported",
            "tool_call": "supported",
            "tool_result": "metadata_only",
            "skill": "unsupported",
            "mcp": "unsupported",
            "child": "unsupported",
        }
    )

    def project(
        self,
        record: Mapping[str, Any],
        *,
        binding: TrustedObservationBindingV1,
        source_event_id: str,
        seq: int,
    ) -> ProjectOutcome:
        record = _strict_mapping(record)
        if set(record) - self._envelope_fields:
            raise ProjectionError("unknown rollout envelope field")
        if "ordinal" in record:
            if _ordinal(record) is None:
                raise ProjectionError("ordinal must not be null")
        timestamp = _timestamp(record)
        line_type = record.get("type")
        payload = record.get("payload")
        if line_type not in {"event_msg", "response_item"}:
            return ProjectOutcome(dropped_count=1)
        payload = _strict_mapping(payload)
        payload_type = payload.get("type")
        if line_type == "event_msg" and payload_type == "user_message":
            message = _text(payload.get("message"))
            return ProjectOutcome(
                (_event(binding, source_event_id, seq, "user_message", timestamp=timestamp, text=message),)
            )
        if line_type == "event_msg" and payload_type == "agent_message":
            if payload.get("phase") != "final_answer":
                return ProjectOutcome(dropped_count=1)
            message = _text(payload.get("message"))
            return ProjectOutcome(
                (_event(binding, source_event_id, seq, "final_assistant", timestamp=timestamp, text=message),)
            )
        if line_type == "response_item" and payload_type == "message":
            self._validate_message(record, payload)
            role = payload.get("role")
            public_user = role == "user" and payload.get("phase") is None
            # rust-v0.154.0의 context/world_state/environment.rs는 주입된 환경 조각을
            # role=user로 내보내지만 별도의 네이티브 종류로 구분한다.
            # 혼합 조각을 포함한 메시지 전체를 제외하며 text/XML로 판별하지 않는다.
            # 실제 user.text 요청에도 같은 봉투 형식이 포함될 수 있기 때문이다.
            metadata = payload.get(self._native_metadata_field)
            if public_user and metadata is not None and (
                "environments.environment_context" in metadata["content_item_kinds"]
            ):
                return ProjectOutcome(dropped_count=1)
            if not public_user and not (role == "assistant" and payload.get("phase") == "final_answer"):
                return ProjectOutcome(dropped_count=1)
            content = payload.get("content")
            if not isinstance(content, list):
                raise ProjectionError("message content must be a list")
            chunks: list[SanitizedText] = []
            for block in content:
                block = _strict_mapping(block)
                if set(block) != {"type", "text"} or block.get("type") != ("input_text" if public_user else "output_text"):
                    raise ProjectionError("non-public response item block")
                chunks.append(_text(block.get("text")))
            return ProjectOutcome(
                (
                    _event(
                        binding,
                        source_event_id,
                        seq,
                        "user_message" if public_user else "final_assistant",
                        timestamp=timestamp,
                        text=_combine_text(chunks),
                    ),
                )
            )
        if line_type == "response_item" and payload_type in {"function_call", "custom_tool_call"}:
            name = _tool_name(payload.get("name"))
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ProjectionError("tool call_id is required")
            return ProjectOutcome(
                (
                    _event(
                        binding,
                        source_event_id,
                        seq,
                        "tool_call",
                        timestamp=timestamp,
                        tool=name,
                        tool_call_ref=opaque_tool_call_ref(binding, call_id),
                    ),
                )
            )
        if line_type == "response_item" and payload_type in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ProjectionError("tool result call_id is required")
            return ProjectOutcome(
                (
                    _event(
                        binding,
                        source_event_id,
                        seq,
                        "tool_result",
                        timestamp=timestamp,
                        tool_call_ref=opaque_tool_call_ref(binding, call_id),
                        status="unknown",
                    ),
                )
            )
        return ProjectOutcome(dropped_count=1)


class CodexTargetTurnProjector(CodexProjector):
    """인증된 네이티브 목표 ID의 공개 response_item만 투영한다.

    호출자는 project_page의 binding/grant 검증을 통과해야 한다. 이 필터나
    parser 준비 상태만으로 생산자 권한 또는 원본 경계를 만들지 않는다.
    """

    schema_pin = CODEX_TARGET_TURN_SCHEMA_PIN

    capabilities = MappingProxyType({
        **CodexProjector.capabilities,
        "tool_call": "unsupported", "tool_result": "unsupported",
    })

    def project(
        self,
        record: Mapping[str, Any],
        *,
        binding: TrustedObservationBindingV1,
        source_event_id: str,
        seq: int,
    ) -> ProjectOutcome:
        validate_codex_target_turn(binding.provider, binding.schema_pin, binding.codex_target_turn_id)
        if binding.schema_pin != self.schema_pin:
            raise PermissionError("Codex target projector requires dedicated binding")
        record = _strict_mapping(record)
        if record.get("type") != "response_item":
            return ProjectOutcome(dropped_count=1)
        payload = _strict_mapping(record.get("payload"))
        if payload.get("type") != "message":
            return ProjectOutcome(dropped_count=1)
        metadata = payload.get(self._native_metadata_field)
        if not isinstance(metadata, Mapping) or metadata.get("turn_id") != binding.codex_target_turn_id:
            return ProjectOutcome(dropped_count=1)
        return super().project(record, binding=binding, source_event_id=source_event_id, seq=seq)


class ClaudeProjector:
    """Claude SDK transcript filter의 명시적 negative marker와 role을 적용한다."""

    provider = "claude"
    schema_pin = (
        "anthropics/claude-agent-sdk-python@"
        "a8b1e285f97f8dbcb7b10226d74ba0d551b493f4:sessions.py"
    )
    capabilities = MappingProxyType(
        {
            "user_message": "supported",
            "assistant_final": "supported",
            "tool_call": "metadata_only",
            "tool_result": "metadata_only",
            "skill": "unsupported",
            "mcp": "metadata_only",
            "child": "unsupported",
        }
    )

    def _public_entry(self, record: Mapping[str, Any]) -> bool:
        for name in ("isMeta", "isCompactSummary", "isSidechain"):
            value = record.get(name, False)
            if not isinstance(value, bool):
                raise ProjectionError(f"{name} must be boolean")
            if value:
                return False
        if "teamName" in record:
            team = record["teamName"]
            if team is not None and team != "":
                if not isinstance(team, str):
                    raise ProjectionError("teamName must be a string")
                return False
        return True

    def project(
        self,
        record: Mapping[str, Any],
        *,
        binding: TrustedObservationBindingV1,
        source_event_id: str,
        seq: int,
    ) -> ProjectOutcome:
        record = _strict_mapping(record)
        if not self._public_entry(record):
            return ProjectOutcome(dropped_count=1)
        outer = record.get("type")
        if outer not in {"user", "assistant"}:
            return ProjectOutcome(dropped_count=1)
        if record.get("sessionId") != binding.session:
            return ProjectOutcome(dropped_count=1)
        message = _strict_mapping(record.get("message"))
        if message.get("role") != outer:
            return ProjectOutcome(dropped_count=1)
        content = message.get("content")
        timestamp = _timestamp(record)
        if outer == "assistant":
            if not isinstance(content, list):
                raise ProjectionError("assistant content must be a list")
            outcomes: list[ConversationEventV1] = []
            next_seq = seq
            for index, block_value in enumerate(content):
                block = _strict_mapping(block_value)
                block_type = block.get("type")
                block_source_id = f"{source_event_id}:{index}"
                if block_type == "text":
                    stop_reason = message.get("stop_reason")
                    if stop_reason not in {"end_turn", "stop_sequence"}:
                        continue
                    outcomes.append(
                        _event(
                            binding,
                            block_source_id,
                            next_seq,
                            "final_assistant",
                            timestamp=timestamp,
                            text=_text(block.get("text")),
                        )
                    )
                    next_seq += 1
                elif block_type == "tool_use":
                    name = _tool_name(block.get("name"))
                    call_id = block.get("id")
                    if not isinstance(call_id, str) or not call_id:
                        raise ProjectionError("tool id is required")
                    outcomes.append(
                        _event(
                            binding,
                            block_source_id,
                            next_seq,
                            "tool_call",
                            timestamp=timestamp,
                            tool=name,
                            tool_call_ref=opaque_tool_call_ref(binding, call_id),
                            mcp=_mcp_metadata(name),
                        )
                    )
                    next_seq += 1
                else:
                    return ProjectOutcome(tuple(outcomes), dropped_count=1)
            return ProjectOutcome(tuple(outcomes), dropped_count=0 if outcomes else 1)

        if isinstance(content, str):
            return ProjectOutcome(
                (_event(binding, source_event_id, seq, "user_message", timestamp=timestamp, text=_text(content)),)
            )
        if not isinstance(content, list):
            raise ProjectionError("user content must be text or list")
        text_blocks: list[SanitizedText] = []
        result_events: list[ConversationEventV1] = []
        saw_result = False
        for index, block_value in enumerate(content):
            block = _strict_mapping(block_value)
            block_type = block.get("type")
            if block_type == "text" and not saw_result:
                text_blocks.append(_text(block.get("text")))
                continue
            if block_type == "tool_result" and not text_blocks:
                saw_result = True
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ProjectionError("tool result id is required")
                is_error = block.get("is_error", False)
                if not isinstance(is_error, bool):
                    raise ProjectionError("is_error must be boolean")
                result_events.append(
                    _event(
                        binding,
                        f"{source_event_id}:{index}",
                        seq + len(result_events),
                        "tool_result",
                        timestamp=timestamp,
                        tool_call_ref=opaque_tool_call_ref(binding, call_id),
                        status="failed" if is_error else "succeeded",
                    )
                )
                continue
            return ProjectOutcome(dropped_count=1)
        if text_blocks:
            return ProjectOutcome(
                (
                    _event(
                        binding,
                        source_event_id,
                        seq,
                        "user_message",
                        timestamp=timestamp,
                        text=_combine_text(text_blocks),
                    ),
                )
            )
        return ProjectOutcome(tuple(result_events))


class HermesProjector:
    provider = "hermes"
    schema_pin = "disabled-until-authoritative-public-schema"
    capabilities = MappingProxyType({"all": "unsupported"})

    def project(self, *args: Any, **kwargs: Any) -> ProjectOutcome:
        raise ProjectionError("Hermes projection is disabled until authoritative proof exists")


class LegacyObservationResolver:
    """Legacy 행은 본문이나 경로를 읽지 않고 연결 불가 상태만 반환한다."""

    @staticmethod
    def classify(*, has_trusted_binding: bool) -> str:
        return "unavailable" if not has_trusted_binding else "trusted_binding_required"


def open_verified_sqlite_source(*args: Any, **kwargs: Any) -> int:
    raise PermissionError("SQLite source projection is disabled until safety is proven")


def _line_at(
    fd: int,
    *,
    offset: int,
    end: int,
    max_line_bytes: int,
    remaining_scan: int,
) -> tuple[bytes, int, int]:
    if offset >= end:
        return b"", offset, 0
    collected = bytearray()
    position = offset
    scanned = 0
    while position < end:
        budget = min(4_096, end - position, remaining_scan - scanned)
        if budget <= 0:
            raise ProjectionError("scan byte budget exceeded")
        chunk = os.pread(fd, budget, position)
        if not chunk:
            raise SourceRotatedError("source truncated inside sealed range")
        scanned += len(chunk)
        newline = chunk.find(b"\n")
        if newline >= 0:
            collected.extend(chunk[:newline])
            position += newline + 1
            if len(collected) > max_line_bytes:
                raise ProjectionError("JSONL line exceeds byte cap")
            return bytes(collected), position, scanned
        collected.extend(chunk)
        position += len(chunk)
        if len(collected) > max_line_bytes:
            raise ProjectionError("JSONL line exceeds byte cap")
    if collected:
        raise ProjectionError("sealed range does not end on a JSONL boundary")
    return b"", position, scanned


def _validate_boundary_alignment(fd: int, boundary: SourceBoundaryV1, *, is_end: bool) -> None:
    if boundary.byte_offset == 0:
        return
    previous = os.pread(fd, 1, boundary.byte_offset - 1)
    if previous != b"\n":
        which = "end" if is_end else "start"
        raise ProjectionError(f"sealed {which} is not a line boundary")


def _ordinal(record: Mapping[str, Any]) -> int | None:
    value = record.get("ordinal")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectionError("ordinal must be a nonnegative integer")
    return value


def _event_wire_size(event: ConversationEventV1) -> int:
    return len(json.dumps(event.to_public_dict(), ensure_ascii=True, sort_keys=True).encode("utf-8"))


def _page_wire_size(
    events: Sequence[ConversationEventV1],
    *,
    next_cursor: str | None,
    counts: Mapping[str, Any],
    capabilities: Mapping[str, str],
    malformed: int,
    dropped: int,
    truncated: bool,
    completeness: str = "partial",
    source_availability: str = "available",
) -> int:
    envelope = {
        "schema_version": 1,
        "events": [event.to_public_dict() for event in events],
        "next_cursor": next_cursor,
        "completeness": completeness,
        "counts": {
            kind: dict(value) if isinstance(value, Mapping) else value
            for kind, value in counts.items()
        },
        "capabilities": dict(capabilities),
        "malformed_count": malformed,
        "dropped_count": dropped,
        "truncated": truncated,
        "source_availability": source_availability,
    }
    return len(json.dumps(envelope, ensure_ascii=True, sort_keys=True).encode("utf-8"))


def _checked_page(page: ProjectionPage, *, max_response_bytes: int) -> ProjectionPage:
    wire_size = len(
        json.dumps(page.to_public_dict(), ensure_ascii=True, sort_keys=True).encode("utf-8")
    )
    if wire_size > max_response_bytes:
        raise ProjectionError("full response envelope exceeds byte cap")
    return page


def _availability_page(
    projector: StrictProjector, *, availability: str, max_response_bytes: int
) -> ProjectionPage:
    page = ProjectionPage(
        events=(),
        next_cursor=None,
        completeness="partial",
        counts=MappingProxyType({}),
        capabilities=projector.capabilities,
        malformed_count=0,
        dropped_count=0,
        truncated=False,
        source_availability=availability,
    )
    return _checked_page(page, max_response_bytes=max_response_bytes)


def project_page(
    locator: SourceLocator,
    projector: StrictProjector,
    *,
    binding: TrustedObservationBindingV1,
    grant: ProjectionGrantV1,
    authority: PrivateFixtureAuthority,
    policy_store: PrivatePolicyStore,
    cursor: str | None = None,
    limits: ProjectionLimits | None = None,
    now_ns: int | None = None,
    read_limiter: SourceReadLimiter | None = None,
) -> ProjectionPage:
    """권한·scope를 먼저 검증한 뒤 sealed byte range만 bounded projection한다."""
    limits = limits or ProjectionLimits()
    now = time.time_ns() if now_ns is None else now_ns

    # Source path에 접근하기 전 검증 가능한 모든 authorization/scope를 검사한다.
    authority.validate_projection_access(
        policy_store,
        binding,
        grant,
        locator,
        now_ns=now,
    )
    if projector.provider != binding.provider:
        raise PermissionError("projector provider mismatch")
    if projector.schema_pin != binding.schema_pin:
        raise PermissionError("projector schema pin mismatch")

    # Digest 검증 역시 source scan이다. 요청 budget 안에서 sealed range 전체를
    # 검증할 수 없다면 unmetered full-range read 대신 안전한 partial 응답을 낸다.
    sealed_bytes = binding.turn_end.byte_offset - binding.turn_start.byte_offset
    if sealed_bytes > limits.max_scan_bytes:
        return ProjectionPage(
            events=(), next_cursor=None, completeness="partial",
            counts={}, capabilities=projector.capabilities,
            malformed_count=0, dropped_count=0, truncated=True,
            source_availability="available",
        )

    if cursor is None:
        state_offset = binding.turn_start.byte_offset
        state_event_index = 0
        state_next_seq = 0
        state_last_ordinal = None
    else:
        cursor_state = authority.read_cursor(
            cursor,
            binding,
            grant,
            now_ns=now,
        )
        state_offset = cursor_state.offset
        state_event_index = cursor_state.event_index
        state_next_seq = cursor_state.next_seq
        state_last_ordinal = cursor_state.last_ordinal
    authority.assert_page_start(
        grant, offset=state_offset, event_index=state_event_index
    )

    started = time.monotonic_ns()
    deadline_ns = started + limits.deadline_ms * 1_000_000
    events: list[ConversationEventV1] = []
    counts: dict[str, int] = {}
    malformed = 0
    dropped = 0
    truncated = False
    record_count = 0
    scan_bytes = 0
    offset = state_offset
    event_index = state_event_index
    next_seq = state_next_seq
    last_ordinal = state_last_ordinal

    admission = (read_limiter or _DEFAULT_READ_LIMITER).admit(
        binding.board, binding.task, deadline_ms=limits.deadline_ms
    )
    admission.__enter__()
    try:
        root = open_verified_root(locator)
    except BaseException:
        admission.__exit__(None, None, None)
        raise
    try:
        if root.identity.inode_tuple() != binding.profile_root_identity.inode_tuple():
            return _availability_page(
                projector, availability="rotated",
                max_response_bytes=limits.max_response_bytes,
            )
        try:
            source = open_verified_jsonl_fd(locator, root_capability=root)
        except (FileNotFoundError, PermissionError, OSError):
            return _availability_page(
                projector, availability="unavailable",
                max_response_bytes=limits.max_response_bytes,
            )
        try:
            fd = source.fd
            source_identity = source.identity
            legacy_identity = binding.source_range_digest == "0" * 64
            if (
                source_identity.inode_tuple() != binding.source_identity.inode_tuple()
                or (legacy_identity and source_identity != binding.source_identity)
            ):
                return _availability_page(
                    projector, availability="rotated",
                    max_response_bytes=limits.max_response_bytes,
                )
            if binding.turn_end.byte_offset > source_identity.size:
                return _availability_page(
                    projector, availability="rotated",
                    max_response_bytes=limits.max_response_bytes,
                )

            digest = hashlib.sha256()
            digest_offset = binding.turn_start.byte_offset
            while digest_offset < binding.turn_end.byte_offset:
                if time.monotonic_ns() >= deadline_ns:
                    return ProjectionPage(
                        events=(), next_cursor=None, completeness="partial",
                        counts={}, capabilities=projector.capabilities,
                        malformed_count=0, dropped_count=0, truncated=True,
                        source_availability="available",
                    )
                chunk = os.pread(
                    fd,
                    min(65_536, binding.turn_end.byte_offset - digest_offset),
                    digest_offset,
                )
                if not chunk:
                    return _availability_page(
                        projector, availability="rotated",
                        max_response_bytes=limits.max_response_bytes,
                    )
                digest.update(chunk)
                digest_offset += len(chunk)
            if not legacy_identity and digest.hexdigest() != binding.source_range_digest:
                return _availability_page(
                    projector, availability="rotated",
                    max_response_bytes=limits.max_response_bytes,
                )
            scan_bytes = digest_offset - binding.turn_start.byte_offset

            while offset < binding.turn_end.byte_offset:
                if len(events) >= limits.max_events:
                    truncated = True
                    break
                if record_count >= limits.max_records or scan_bytes >= limits.max_scan_bytes:
                    truncated = True
                    break
                if time.monotonic_ns() >= deadline_ns:
                    truncated = True
                    break
                line_start = offset
                line, line_end, consumed = _line_at(
                    fd,
                    offset=offset,
                    end=binding.turn_end.byte_offset,
                    max_line_bytes=limits.max_line_bytes,
                    remaining_scan=limits.max_scan_bytes - scan_bytes,
                )
                scan_bytes += consumed
                record_count += 1
                if not line:
                    malformed += 1
                    if malformed > limits.malformed_limit:
                        raise ProjectionError("malformed record budget exceeded")
                    offset = line_end
                    event_index = 0
                    continue
                try:
                    record = strict_json_loads(line.decode("utf-8", errors="strict"))
                    record = _strict_mapping(record)
                    ordinal = _ordinal(record)
                    if ordinal is not None:
                        if binding.turn_start.ordinal is not None and ordinal < binding.turn_start.ordinal:
                            raise ProjectionError("ordinal precedes sealed start")
                        if binding.turn_end.ordinal is not None and ordinal >= binding.turn_end.ordinal:
                            raise ProjectionError("ordinal escapes sealed end")
                        if last_ordinal is not None and ordinal <= last_ordinal:
                            raise ProjectionError("ordinal is not strictly increasing")
                    outcome = projector.project(
                        record,
                        binding=binding,
                        source_event_id=f"{line_start}:{ordinal if ordinal is not None else '-'}",
                        seq=next_seq - event_index,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ProjectionError, TypeError, ValueError):
                    malformed += 1
                    if malformed > limits.malformed_limit:
                        raise ProjectionError("malformed record budget exceeded")
                    offset = line_end
                    event_index = 0
                    continue

                line_fully_admitted = True
                for outcome_index, event in enumerate(outcome.events):
                    if outcome_index < event_index:
                        continue
                    if len(events) >= limits.max_events:
                        truncated = True
                        offset = line_start
                        event_index = outcome_index
                        line_fully_admitted = False
                        break
                    candidate = [*events, event]
                    candidate_counts = dict(counts)
                    candidate_counts[event.kind] = candidate_counts.get(event.kind, 0) + 1
                    candidate_count_payload = {
                        kind: {"value": value, "exact": False}
                        for kind, value in sorted(candidate_counts.items())
                    }
                    if (
                        _page_wire_size(
                            candidate,
                            next_cursor="cu_" + "0" * 64,
                            counts=candidate_count_payload,
                            capabilities=projector.capabilities,
                            malformed=malformed,
                            dropped=dropped,
                            truncated=True,
                            completeness="partial",
                        )
                        > limits.max_response_bytes
                    ):
                        if not events:
                            raise ProjectionError("minimum event envelope exceeds byte cap")
                        truncated = True
                        offset = line_start
                        event_index = outcome_index
                        line_fully_admitted = False
                        break
                    events.append(event)
                    counts = candidate_counts
                    next_seq += 1
                if not line_fully_admitted:
                    break
                dropped += outcome.dropped_count + outcome.unsupported_count
                offset = line_end
                event_index = 0
                if ordinal is not None:
                    last_ordinal = ordinal
        finally:
            source.__exit__(None, None, None)
    finally:
        root.close()
        admission.__exit__(None, None, None)

    terminal = offset >= binding.turn_end.byte_offset and event_index == 0
    next_cursor = None
    if not terminal:
        next_cursor = authority.issue_cursor(
            binding,
            grant,
            offset=offset,
            event_index=event_index,
            next_seq=next_seq,
            last_ordinal=last_ordinal,
            now_ns=now,
        )

    capability_partial = any(value == "unsupported" for value in projector.capabilities.values())
    text_clipped = any(event.redaction == "partially_redacted" for event in events)
    truncated = truncated or text_clipped
    partial = (
        not terminal
        or malformed > 0
        or dropped > 0
        or truncated
        or capability_partial
    )
    count_payload = {
        kind: {"value": value, "exact": not partial} for kind, value in sorted(counts.items())
    }
    page = ProjectionPage(
        events=tuple(events),
        next_cursor=next_cursor,
        completeness="partial" if partial else "complete",
        counts=count_payload,
        capabilities=projector.capabilities,
        malformed_count=malformed,
        dropped_count=dropped,
        truncated=truncated,
        source_availability="available",
    )
    page = _checked_page(page, max_response_bytes=limits.max_response_bytes)
    authority.commit_cursor(
        cursor,
        grant,
        expected_offset=state_offset,
        expected_event_index=state_event_index,
        next_offset=offset,
        next_event_index=event_index,
    )
    return page
