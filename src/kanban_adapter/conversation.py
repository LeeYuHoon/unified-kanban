"""외부 대화 수집을 위한 독립적이고 기본 거부인 보안 core.

제품 인증·kernel receipt와 아직 연결되지 않았으므로 실제 권한 발급기는 없다.
`PrivateFixtureAuthority`는 synthetic fixture 전용이며 production 생성은 명시적으로
비활성화된다. 원문은 verified FD에서 lazy projection하고 durable journal에는
MAC된 content-free metadata만 기록한다. redaction은 best-effort이며 비밀 부재의
증명이 아니다.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import stat
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Self

from .private_files import open_directory, validate_directory

SCHEMA_VERSION = 1
CODEX_SCHEMA_VERSION = "rust-v0.145.0"
JSONL_LINE_BYTES = 1_048_576
JSONL_SCAN_BYTES = 8_388_608
JSONL_RECORD_LIMIT = 5_000
JSONL_SCAN_DEADLINE_MS = 2_000
PRE_REDACTION_TEXT_BYTES = 262_144
POST_REDACTION_TEXT_BYTES = 65_536
SQL_PAGE_ROWS = 500
SQL_PAGE_BYTES = 1_048_576
SQL_QUERY_DEADLINE_MS = 1_000
SQL_BUSY_TIMEOUT_MS = 250
API_REQUEST_DEADLINE_MS = 3_000
API_EVENT_LIMIT = 500
API_RESPONSE_BYTES = 1_048_576
SOURCE_READ_PROCESS_LIMIT = 4
SOURCE_READ_BOARD_LIMIT = 2
SOURCE_READ_TASK_LIMIT = 1
INDEX_GLOBAL_BYTES = 268_435_456
INDEX_CARD_BYTES = 1_048_576
INDEX_RETENTION_DAYS = 30
CHILD_MAX_DEPTH = 4
CHILD_MAX_FANOUT = 32
CHILD_MAX_GRAPH_NODES = 128
CURSOR_TTL_NS = 300_000_000_000
GRANT_MAX_TTL_NS = 60_000_000_000

EventKind = Literal[
    "user_message", "final_assistant", "tool_call", "tool_result", "mcp_call",
    "skill_load", "subagent_start", "subagent_stop",
]
Completeness = Literal[
    "complete", "partial", "unsupported_by_producer", "not_recorded",
    "malformed", "truncated", "unknown",
]
SourceAvailability = Literal["available", "missing", "rotated", "inaccessible", "disabled"]
RedactionState = Literal[
    "not_applicable", "applied", "partially_redacted", "fully_redacted", "failed_closed",
]

_EVENT_KINDS = {
    "user_message", "final_assistant", "tool_call", "tool_result", "mcp_call",
    "skill_load", "subagent_start", "subagent_stop",
}
_TEXT_KINDS = {"user_message", "final_assistant"}
_STATUS_VALUES = {"succeeded", "failed", "aborted", "interrupted", "unknown"}
_COMPLETENESS_VALUES = {
    "complete", "partial", "unsupported_by_producer", "not_recorded",
    "malformed", "truncated", "unknown",
}
_REDACTION_VALUES = {
    "not_applicable", "applied", "partially_redacted", "fully_redacted", "failed_closed",
}
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,62}[A-Za-z0-9])?\Z")
TOOL_NAME_RE = _IDENTIFIER_RE
_OPAQUE_RUN_RE = re.compile(r"[A-Za-z0-9]{16,}")
_OPAQUE_PATTERNS = {
    "replay_id": re.compile(r"rp_[0-9a-f]{64}\Z"),
    "event_id": re.compile(r"ev_[0-9a-f]{64}\Z"),
    "tool_call_ref": re.compile(r"tc_[0-9a-f]{64}\Z"),
    "binding_ref": re.compile(r"br_[0-9a-f]{64}\Z"),
    "child_ref": re.compile(r"ce_[0-9a-f]{64}\Z"),
}
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?82[- .]?)?0?1[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)")
_HOME_RE = re.compile(r"/(?:Users|home)/[^/\s]+(?:/[^\s]*)?")
_SECRET_RE = re.compile(
    r"(?i)(?:sk|ghp|github_pat|xox[baprs]|api[_-]?key|token|password)[-_:= ]?[A-Za-z0-9+/_.=-]{12,}"
)


def _require_int(value: Any, name: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise TypeError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _require_text(value: Any, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{name} is too long")
    return value


def _require_opaque(value: Any, kind: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_PATTERNS[kind].fullmatch(value):
        raise ValueError(f"invalid opaque {kind}")
    return value


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("strict JSON forbids duplicate keys")
        result[key] = value
    return result


def strict_json_loads(value: str) -> Any:
    try:
        return json.loads(
            value,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        raise ValueError("invalid strict JSON") from error


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _mac(prefix: str, secret: bytes, *parts: bytes) -> str:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("MAC secret must contain at least 32 bytes")
    joined = bytes((31,)).join(parts)
    return f"{prefix}_{hmac.new(secret, joined, hashlib.sha256).hexdigest()}"


def normalize_timestamp(value: Any) -> str | None:
    """유한 epoch 또는 timezone이 있는 ISO 값만 UTC ISO 문자열로 정규화한다."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("timestamp bool is forbidden")
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError("timestamp must be finite and non-negative")
        # 1e12 이상은 epoch milliseconds로만 해석한다.
        seconds = numeric / 1000 if numeric >= 1_000_000_000_000 else numeric
        if seconds > 253_402_300_799:
            raise ValueError("timestamp is out of range")
        parsed = datetime.fromtimestamp(seconds, UTC)
    elif isinstance(value, str):
        if not value or len(value.encode("utf-8")) > 64:
            raise ValueError("timestamp string is invalid")
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as error:
            raise ValueError("timestamp must be ISO-8601") from error
        if parsed.tzinfo is None:
            raise ValueError("timestamp timezone is required")
        parsed = parsed.astimezone(UTC)
    else:
        raise TypeError("timestamp must be finite epoch or ISO text")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class CountV1:
    value: int
    exact: bool

    def __post_init__(self) -> None:
        _require_int(self.value, "count")
        if not isinstance(self.exact, bool):
            raise TypeError("exact must be bool")

    def to_public_dict(self) -> dict[str, int | bool]:
        return {"value": self.value, "exact": self.exact}


@dataclass(frozen=True)
class SourceBoundaryV1:
    byte_offset: int
    ordinal: int | None = None

    def __post_init__(self) -> None:
        _require_int(self.byte_offset, "source byte offset")
        if self.ordinal is not None:
            _require_int(self.ordinal, "source ordinal")


@dataclass(frozen=True)
class SourceIdentityV1:
    device: int
    inode: int
    size: int
    mtime_ns: int

    def __post_init__(self) -> None:
        for name in ("device", "inode", "size", "mtime_ns"):
            _require_int(getattr(self, name), f"source {name}")

    @classmethod
    def from_stat(cls, info: os.stat_result) -> SourceIdentityV1:
        return cls(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def inode_tuple(self) -> tuple[int, int]:
        return self.device, self.inode


@dataclass(frozen=True)
class SourceLocator:
    provider: str
    allowed_root: Path
    relative_path: Path
    expected_owner: int

    def __post_init__(self) -> None:
        if sanitize_tool_name(self.provider) != self.provider:
            raise ValueError("invalid provider")
        root = Path(self.allowed_root)
        relative = Path(self.relative_path)
        if not root.is_absolute():
            raise ValueError("allowed root must be absolute")
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("source path must be safe relative path")
        if "\x00" in os.fspath(relative) or len(os.fsencode(relative)) > 4096:
            raise ValueError("source relative path is invalid")
        _require_int(self.expected_owner, "expected owner")
        object.__setattr__(self, "allowed_root", root)
        object.__setattr__(self, "relative_path", relative)

    def private_scope(self) -> bytes:
        return _canonical_json(
            [self.provider, os.fsencode(self.allowed_root).hex(), os.fsencode(self.relative_path).hex(), self.expected_owner]
        )


@dataclass(frozen=True)
class CollectionPolicyV1:
    version: int
    generation: int = 1
    activated_at_ns: int = 0
    expires_at_ns: int = 2**63 - 1
    enabled_boards: Mapping[str, frozenset[str]] = field(default_factory=dict)
    minimum_binding_version: int = 1
    schema_version: int = 1
    pair_activated_at_ns: Mapping[str, Mapping[str, int]] | None = None

    def __post_init__(self) -> None:
        _require_int(self.version, "policy version", minimum=1)
        if type(self.schema_version) is not int or self.schema_version not in {1, 2}:
            raise ValueError("unsupported policy schema")
        _require_int(self.generation, "policy generation", minimum=1)
        _require_int(self.activated_at_ns, "policy activation")
        _require_int(self.expires_at_ns, "policy expiry")
        if self.expires_at_ns <= self.activated_at_ns:
            raise ValueError("policy expiry must follow activation")
        _require_int(self.minimum_binding_version, "minimum binding version", minimum=1)
        copied: dict[str, frozenset[str]] = {}
        if not isinstance(self.enabled_boards, Mapping):
            raise TypeError("enabled boards must be a mapping")
        for board, providers in self.enabled_boards.items():
            _require_text(board, "board", maximum=256)
            if not isinstance(providers, frozenset) or not providers:
                raise TypeError("enabled providers must be a non-empty frozenset")
            copied[board] = frozenset(_require_text(item, "provider", maximum=64) for item in providers)
            # 과거 claude-code 항목은 그대로 보존하되 claude 권한으로 바꾸지 않는다.
            if not copied[board] <= {"claude", "codex", "hermes", "claude-code"}:
                raise ValueError("unsupported policy provider")
        object.__setattr__(self, "enabled_boards", MappingProxyType(copied))
        # 레거시의 인증된 전역 시점은 당시 켜져 있던 쌍에만 상속한다.
        cutoffs = self.pair_activated_at_ns
        if cutoffs is None:
            if self.schema_version != 1:
                raise ValueError("pair activation map required")
            cutoffs = {board: {provider: self.activated_at_ns for provider in providers}
                       for board, providers in copied.items()}
        if not isinstance(cutoffs, Mapping) or set(cutoffs) != set(copied):
            raise ValueError("pair activation boards must match enabled boards")
        for board, values in cutoffs.items():
            if not isinstance(values, Mapping) or set(values) != set(copied[board]):
                raise ValueError("pair activation providers must match enabled providers")
            for cutoff in values.values():
                _require_int(cutoff, "pair activation")
                if not self.activated_at_ns <= cutoff < self.expires_at_ns:
                    raise ValueError("pair activation is outside policy lifetime")
                if self.schema_version == 1 and cutoff != self.activated_at_ns:
                    raise ValueError("legacy policy cannot represent pair activation")
        object.__setattr__(self, "pair_activated_at_ns", MappingProxyType({
            board: MappingProxyType(dict(values)) for board, values in cutoffs.items()
        }))

    def activation_for(self, board: str, provider: str) -> int | None:
        assert self.pair_activated_at_ns is not None
        return self.pair_activated_at_ns.get(board, {}).get(provider)

    @classmethod
    def disabled(cls, *, version: int, generation: int = 1) -> CollectionPolicyV1:
        return cls(version=version, generation=generation)

    def allows(self, board: str, provider: str, binding_version: int, now_ns: int) -> bool:
        activation = self.activation_for(board, provider)
        return (
            activation is not None and activation <= now_ns < self.expires_at_ns
            and binding_version >= self.minimum_binding_version
            and provider in self.enabled_boards.get(board, frozenset())
        )


class PrivatePolicyStore:
    """fixture authority가 소유하는 live policy generation store."""

    def __init__(self, authority_id: str, capability: object) -> None:
        self._authority_id = authority_id
        self._capability = capability
        self._policies: dict[str, CollectionPolicyV1] = {}
        self._lock = threading.RLock()

    def replace(self, policy: CollectionPolicyV1) -> None:
        with self._lock:
            for board in policy.enabled_boards:
                current = self._policies.get(board)
                if current is not None and policy.generation <= current.generation:
                    raise RuntimeError("policy generation CAS failed")
                self._policies[board] = policy

    def get(self, board: str) -> CollectionPolicyV1 | None:
        with self._lock:
            return self._policies.get(board)

    def disable(self, board: str, *, expected_generation: int) -> None:
        with self._lock:
            current = self._policies.get(board)
            if current is None or current.generation != expected_generation:
                raise RuntimeError("policy generation CAS failed")
            self._policies[board] = CollectionPolicyV1.disabled(
                version=current.version, generation=current.generation + 1
            )


# 기존 schema pin과 MAC 직렬화는 유지하고 목표 턴 계약만 별도로 고정한다.
CODEX_TARGET_TURN_SCHEMA_PIN = "openai/codex@rust-v0.154.0:authenticated-target-turn-v1"


def validate_codex_target_turn(provider: str, schema_pin: str, target: str | None) -> None:
    """목표 없는 새 계약이나 기존 계약으로의 목표 주입을 금지한다."""
    if schema_pin == CODEX_TARGET_TURN_SCHEMA_PIN:
        if provider != "codex":
            raise ValueError("Codex target turn requires codex provider")
        _require_text(target, "Codex target turn ID")
    elif target is not None:
        raise ValueError("Codex target turn requires dedicated schema pin")


@dataclass(frozen=True)
class PendingObservationBindingV1:
    board: str
    task: str
    provider: str
    schema_pin: str
    profile_root_identity: SourceIdentityV1
    locator_ref: str
    source_identity: SourceIdentityV1
    session: str
    turn_start: SourceBoundaryV1
    boundary_alignment_proof: Literal["producer_jsonl_lines_v1"]
    binding_version: int
    generation: int
    policy_version: int
    producer_execution: str
    created_at_ns: int
    pending_receipt: str
    issuer_id: str
    codex_target_turn_id: str | None = None

    def __post_init__(self) -> None:
        validate_codex_target_turn(self.provider, self.schema_pin, self.codex_target_turn_id)
        if self.codex_target_turn_id is not None and self.turn_start.ordinal is None:
            raise ValueError("Codex target turn requires native start ordinal")

    @property
    def authorizes_projection(self) -> bool:
        return False


@dataclass(frozen=True)
class TrustedObservationBindingV1:
    board: str
    task: str
    provider: str
    schema_pin: str
    profile_root_identity: SourceIdentityV1
    locator_ref: str
    source_identity: SourceIdentityV1
    session: str
    turn_start: SourceBoundaryV1
    turn_end: SourceBoundaryV1
    boundary_alignment_proof: Literal["producer_jsonl_lines_v1"]
    binding_version: int
    generation: int
    policy_version: int
    producer_execution: str
    created_at_ns: int
    sealed_at_ns: int
    producer_receipt: str
    binding_ref: str
    source_range_digest: str
    auth_tag: str
    issuer_id: str
    codex_target_turn_id: str | None = None

    def __post_init__(self) -> None:
        validate_codex_target_turn(self.provider, self.schema_pin, self.codex_target_turn_id)
        if self.codex_target_turn_id is not None:
            if self.turn_start.ordinal is None or self.turn_end.ordinal is None:
                raise ValueError("Codex target turn requires native ordinal boundaries")
            if (not isinstance(self.source_range_digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", self.source_range_digest) is None
                    or self.source_range_digest == "0" * 64):
                raise ValueError("Codex target turn requires sealed source digest")

    @property
    def authorizes_projection(self) -> bool:
        return True

    def scope_dict(self, *, include_auth: bool = False) -> dict[str, Any]:
        values: dict[str, Any] = {
            "board": self.board,
            "task": self.task,
            "provider": self.provider,
            "schema_pin": self.schema_pin,
            "profile_root_identity": _identity_dict(self.profile_root_identity),
            "locator_ref": self.locator_ref,
            "source_identity": _identity_dict(self.source_identity),
            "session": self.session,
            "turn_start": _boundary_dict(self.turn_start),
            "turn_end": _boundary_dict(self.turn_end),
            "boundary_alignment_proof": self.boundary_alignment_proof,
            "binding_version": self.binding_version,
            "generation": self.generation,
            "policy_version": self.policy_version,
            "producer_execution": self.producer_execution,
            "created_at_ns": self.created_at_ns,
            "sealed_at_ns": self.sealed_at_ns,
            "producer_receipt": self.producer_receipt,
            "binding_ref": self.binding_ref,
            "source_range_digest": self.source_range_digest,
            "issuer_id": self.issuer_id,
        }
        # None을 추가하면 기존 binding/cursor MAC이 바뀌므로 키 자체를 생략한다.
        if self.codex_target_turn_id is not None:
            values["codex_target_turn_id"] = self.codex_target_turn_id
        if include_auth:
            values["auth_tag"] = self.auth_tag
        return values

    def replay_scope(self) -> bytes:
        return _canonical_json(self.scope_dict(include_auth=True))


def _identity_dict(value: SourceIdentityV1) -> dict[str, int]:
    return {"device": value.device, "inode": value.inode, "size": value.size, "mtime_ns": value.mtime_ns}


def _boundary_dict(value: SourceBoundaryV1) -> dict[str, int | None]:
    return {"byte_offset": value.byte_offset, "ordinal": value.ordinal}


@dataclass(frozen=True)
class PrincipalScopeReceiptV1:
    principal_ref: str
    board: str
    task: str
    expires_at_ns: int
    auth_tag: str
    issuer_id: str


@dataclass(frozen=True)
class ProjectionGrantV1:
    grant_ref: str
    principal_ref: str
    binding_ref: str
    board: str
    task: str
    provider: str
    binding_generation: int
    policy_version: int
    policy_generation: int
    expires_at_ns: int
    auth_tag: str
    issuer_id: str


@dataclass(frozen=True)
class _CursorState:
    binding_ref: str
    grant_ref: str
    source_identity: SourceIdentityV1
    offset: int
    event_index: int
    next_seq: int
    last_ordinal: int | None
    expires_at_ns: int
    phase: Literal["inside"] = "inside"


class PrivateFixtureAuthority:
    """synthetic tests에서만 사용하는 private receipt/grant/cursor issuer."""

    def __init__(self, secret: bytes, issuer_id: str, *, fixture: bool) -> None:
        if not fixture:
            raise RuntimeError("production authority is disabled until kernel/auth integration")
        self._initialize_authority(secret, issuer_id)

    def _initialize_authority(self, secret: bytes, issuer_id: str) -> None:
        """검증된 fixture 또는 kernel bootstrap 뒤의 공통 private 상태 초기화."""
        if len(secret) < 32:
            raise ValueError("authority secret must contain at least 32 bytes")
        self._secret = bytes(secret)
        self.issuer_id = _require_text(issuer_id, "issuer id", maximum=64)
        self._policy_capability = object()
        self._pending: dict[str, PendingObservationBindingV1] = {}
        self._sealed: set[str] = set()
        self._cursors: dict[str, _CursorState] = {}
        self._grant_highwater: dict[str, tuple[int, int]] = {}
        self._child_graphs: dict[str, ChildGraphGuard] = {}
        self._execution_graph: dict[str, str] = {}
        self._child_edges: dict[str, str] = {}
        self._graph_sequence = 0
        self._lock = threading.RLock()

    @classmethod
    def create(cls, secret: bytes, *, issuer_id: str) -> PrivateFixtureAuthority:
        return cls(secret, issuer_id, fixture=True)

    @classmethod
    def production(cls) -> PrivateFixtureAuthority:
        raise RuntimeError("production authority is disabled until authoritative integration")

    def create_policy_store(self) -> PrivatePolicyStore:
        return PrivatePolicyStore(self.issuer_id, self._policy_capability)

    def _validate_policy_store(self, policy_store: PrivatePolicyStore) -> None:
        if (
            not isinstance(policy_store, PrivatePolicyStore)
            or policy_store._authority_id != self.issuer_id
            or policy_store._capability is not self._policy_capability
        ):
            raise PermissionError("policy store is not authoritative for this issuer")

    def _locator_ref(self, locator: SourceLocator) -> str:
        return _mac("lr", self._secret, locator.private_scope())

    def begin_binding(
        self,
        *,
        board: str,
        task: str,
        provider: str,
        schema_pin: str,
        profile_root_identity: SourceIdentityV1,
        locator: SourceLocator,
        source_identity: SourceIdentityV1,
        session: str,
        turn_start: SourceBoundaryV1,
        binding_version: int,
        generation: int,
        policy_version: int,
        producer_execution: str,
        now_ns: int,
        codex_target_turn_id: str | None = None,
    ) -> PendingObservationBindingV1:
        validate_codex_target_turn(provider, schema_pin, codex_target_turn_id)
        for value, name in ((board, "board"), (task, "task"), (provider, "provider"), (schema_pin, "schema pin"), (session, "session"), (producer_execution, "producer execution")):
            _require_text(value, name)
        for value, name in ((binding_version, "binding version"), (generation, "generation"), (policy_version, "policy version")):
            _require_int(value, name, minimum=1)
        _require_int(now_ns, "binding creation time")
        if provider != locator.provider or turn_start.byte_offset > source_identity.size:
            raise ValueError("pending binding source scope is invalid")
        body = {
            "board": board, "task": task, "provider": provider, "schema_pin": schema_pin,
            "profile_root_identity": _identity_dict(profile_root_identity),
            "locator_ref": self._locator_ref(locator),
            "source_identity": _identity_dict(source_identity),
            "session": session, "turn_start": _boundary_dict(turn_start),
            "boundary_alignment_proof": "producer_jsonl_lines_v1",
            "binding_version": binding_version, "generation": generation,
            "policy_version": policy_version, "producer_execution": producer_execution,
            "created_at_ns": now_ns, "issuer_id": self.issuer_id,
        }
        if codex_target_turn_id is not None:
            body["codex_target_turn_id"] = codex_target_turn_id
        receipt = _mac("ps", self._secret, _canonical_json(body))
        pending = PendingObservationBindingV1(
            board=board, task=task, provider=provider, schema_pin=schema_pin,
            profile_root_identity=profile_root_identity, locator_ref=body["locator_ref"],
            source_identity=source_identity, session=session, turn_start=turn_start,
            boundary_alignment_proof="producer_jsonl_lines_v1",
            binding_version=binding_version, generation=generation,
            policy_version=policy_version, producer_execution=producer_execution,
            created_at_ns=now_ns, pending_receipt=receipt, issuer_id=self.issuer_id,
            codex_target_turn_id=codex_target_turn_id,
        )
        with self._lock:
            if receipt in self._pending or receipt in self._sealed:
                raise RuntimeError("pending binding replayed")
            self._pending[receipt] = pending
        return pending

    def seal_binding(
        self,
        pending: PendingObservationBindingV1,
        *,
        turn_end: SourceBoundaryV1,
        source_identity: SourceIdentityV1,
        expected_generation: int,
        now_ns: int,
        source_range_digest: str = "0" * 64,
    ) -> TrustedObservationBindingV1:
        with self._lock:
            authoritative = self._pending.get(pending.pending_receipt)
            if authoritative != pending or pending.issuer_id != self.issuer_id:
                raise RuntimeError("pending receipt is not authoritative")
            if pending.pending_receipt in self._sealed:
                raise RuntimeError("binding seal is one-shot")
            if expected_generation != pending.generation:
                raise RuntimeError("binding generation CAS failed")
            if source_identity != pending.source_identity:
                raise RuntimeError("source identity changed before seal")
            if pending.boundary_alignment_proof != "producer_jsonl_lines_v1":
                raise RuntimeError("producer boundary alignment proof is invalid")
            if turn_end.byte_offset <= pending.turn_start.byte_offset or turn_end.byte_offset > source_identity.size:
                raise ValueError("sealed byte range is invalid")
            if (
                pending.turn_start.ordinal is not None
                and turn_end.ordinal is not None
                and turn_end.ordinal <= pending.turn_start.ordinal
            ):
                raise ValueError("sealed ordinal range is invalid")
            _require_int(now_ns, "binding seal time")
            if re.fullmatch(r"[0-9a-f]{64}", source_range_digest) is None:
                raise ValueError("sealed source range digest is invalid")
            base = {
                "pending_receipt": pending.pending_receipt,
                "turn_end": _boundary_dict(turn_end),
                "source_identity": _identity_dict(source_identity),
                "sealed_at_ns": now_ns,
                "source_range_digest": source_range_digest,
            }
            producer_receipt = _mac("pr", self._secret, _canonical_json(base))
            binding_ref = _mac("br", self._secret, pending.pending_receipt.encode(), producer_receipt.encode())
            values = {
                "board": pending.board, "task": pending.task, "provider": pending.provider,
                "schema_pin": pending.schema_pin,
                "profile_root_identity": _identity_dict(pending.profile_root_identity),
                "locator_ref": pending.locator_ref,
                "source_identity": _identity_dict(source_identity), "session": pending.session,
                "turn_start": _boundary_dict(pending.turn_start), "turn_end": _boundary_dict(turn_end),
                "boundary_alignment_proof": pending.boundary_alignment_proof,
                "binding_version": pending.binding_version, "generation": pending.generation,
                "policy_version": pending.policy_version, "producer_execution": pending.producer_execution,
                "created_at_ns": pending.created_at_ns, "sealed_at_ns": now_ns,
                "producer_receipt": producer_receipt, "binding_ref": binding_ref,
                "source_range_digest": source_range_digest,
                "issuer_id": self.issuer_id,
            }
            if pending.codex_target_turn_id is not None:
                values["codex_target_turn_id"] = pending.codex_target_turn_id
            auth_tag = _mac("bd", self._secret, _canonical_json(values))
            binding = TrustedObservationBindingV1(
                board=pending.board, task=pending.task, provider=pending.provider,
                schema_pin=pending.schema_pin,
                profile_root_identity=pending.profile_root_identity,
                locator_ref=pending.locator_ref, source_identity=source_identity,
                session=pending.session, turn_start=pending.turn_start, turn_end=turn_end,
                boundary_alignment_proof=pending.boundary_alignment_proof,
                binding_version=pending.binding_version, generation=pending.generation,
                policy_version=pending.policy_version,
                producer_execution=pending.producer_execution,
                created_at_ns=pending.created_at_ns, sealed_at_ns=now_ns,
                producer_receipt=producer_receipt, binding_ref=binding_ref,
                source_range_digest=source_range_digest,
                auth_tag=auth_tag, issuer_id=self.issuer_id,
                codex_target_turn_id=pending.codex_target_turn_id,
            )
            self._sealed.add(pending.pending_receipt)
            del self._pending[pending.pending_receipt]
            return binding

    def reseal_for_test(self, binding: TrustedObservationBindingV1, _end: SourceBoundaryV1, *, now_ns: int) -> None:
        del now_ns
        if binding.producer_receipt:
            raise RuntimeError("binding seal is one-shot")

    def _validate_binding(self, binding: TrustedObservationBindingV1) -> None:
        try:
            validate_codex_target_turn(binding.provider, binding.schema_pin, binding.codex_target_turn_id)
        except ValueError as exc:
            raise PermissionError("binding target turn contract is invalid") from exc
        if binding.issuer_id != self.issuer_id:
            raise PermissionError("binding issuer is invalid")
        if binding.boundary_alignment_proof != "producer_jsonl_lines_v1":
            raise PermissionError("producer boundary alignment proof is invalid")
        values = binding.scope_dict(include_auth=False)
        expected = _mac("bd", self._secret, _canonical_json(values))
        if not hmac.compare_digest(expected, binding.auth_tag):
            raise PermissionError("binding receipt is invalid")
        _require_opaque(binding.binding_ref, "binding_ref")

    def issue_principal_scope(
        self, *, principal: str, board: str, task: str, expires_at_ns: int
    ) -> PrincipalScopeReceiptV1:
        _require_text(principal, "principal")
        _require_text(board, "board")
        _require_text(task, "task")
        _require_int(expires_at_ns, "principal expiry")
        principal_ref = _mac("id", self._secret, b"principal", principal.encode())
        body = [principal_ref, board, task, expires_at_ns, self.issuer_id]
        return PrincipalScopeReceiptV1(
            principal_ref, board, task, expires_at_ns,
            _mac("pa", self._secret, _canonical_json(body)), self.issuer_id,
        )

    def _validate_principal(self, receipt: PrincipalScopeReceiptV1, now_ns: int) -> None:
        body = [receipt.principal_ref, receipt.board, receipt.task, receipt.expires_at_ns, receipt.issuer_id]
        expected = _mac("pa", self._secret, _canonical_json(body))
        if receipt.issuer_id != self.issuer_id or not hmac.compare_digest(expected, receipt.auth_tag):
            raise PermissionError("principal ACL receipt is invalid")
        if now_ns >= receipt.expires_at_ns:
            raise PermissionError("principal ACL receipt expired")

    def issue_projection_grant(
        self,
        policy_store: PrivatePolicyStore,
        binding: TrustedObservationBindingV1,
        principal: PrincipalScopeReceiptV1,
        *,
        now_ns: int,
        ttl_ns: int,
    ) -> ProjectionGrantV1:
        self._validate_policy_store(policy_store)
        self._validate_binding(binding)
        self._validate_principal(principal, now_ns)
        if principal.board != binding.board or principal.task != binding.task:
            raise PermissionError("principal ACL scope mismatch")
        ttl_ns = _require_int(ttl_ns, "grant TTL", minimum=1, maximum=GRANT_MAX_TTL_NS)
        policy = policy_store.get(binding.board)
        activation = policy.activation_for(binding.board, binding.provider) if policy else None
        if (
            policy is None
            or activation is None
            or policy.version != binding.policy_version
            or not policy.allows(binding.board, binding.provider, binding.binding_version, now_ns)
            or binding.created_at_ns < activation
        ):
            raise PermissionError("live policy denies binding")
        expiry = min(now_ns + ttl_ns, principal.expires_at_ns, policy.expires_at_ns)
        base = [principal.principal_ref, binding.binding_ref, binding.board, binding.task,
                binding.provider, binding.generation, policy.version, policy.generation, expiry,
                self.issuer_id]
        grant_ref = _mac("gr", self._secret, _canonical_json(base))
        auth_tag = _mac("ga", self._secret, grant_ref.encode(), binding.replay_scope())
        grant = ProjectionGrantV1(
            grant_ref, principal.principal_ref, binding.binding_ref, binding.board,
            binding.task, binding.provider, binding.generation, policy.version,
            policy.generation, expiry, auth_tag, self.issuer_id,
        )
        with self._lock:
            self._grant_highwater[grant_ref] = (binding.turn_start.byte_offset, 0)
        return grant

    def validate_projection_access(
        self,
        policy_store: PrivatePolicyStore,
        binding: TrustedObservationBindingV1,
        grant: ProjectionGrantV1,
        locator: SourceLocator,
        *,
        now_ns: int,
    ) -> None:
        self._validate_policy_store(policy_store)
        if now_ns >= grant.expires_at_ns:
            raise PermissionError("projection grant expired")
        self._validate_binding(binding)
        if locator.provider != binding.provider or self._locator_ref(locator) != binding.locator_ref:
            raise PermissionError("binding source scope mismatch")
        if grant.issuer_id != self.issuer_id:
            raise PermissionError("projection grant issuer mismatch")
        expected_ref = _mac(
            "gr",
            self._secret,
            _canonical_json(
                [
                    grant.principal_ref,
                    grant.binding_ref,
                    grant.board,
                    grant.task,
                    grant.provider,
                    grant.binding_generation,
                    grant.policy_version,
                    grant.policy_generation,
                    grant.expires_at_ns,
                    grant.issuer_id,
                ]
            ),
        )
        if not hmac.compare_digest(expected_ref, grant.grant_ref):
            raise PermissionError("projection grant full scope is invalid")
        expected = _mac("ga", self._secret, grant.grant_ref.encode(), binding.replay_scope())
        if not hmac.compare_digest(expected, grant.auth_tag):
            raise PermissionError("projection grant receipt is invalid")
        if (
            grant.binding_ref != binding.binding_ref
            or grant.board != binding.board
            or grant.task != binding.task
            or grant.provider != binding.provider
            or grant.binding_generation != binding.generation
            or grant.policy_version != binding.policy_version
        ):
            raise PermissionError("projection grant scope mismatch")
        policy = policy_store.get(binding.board)
        if (
            policy is None
            or policy.version != grant.policy_version
            or policy.generation != grant.policy_generation
            or not policy.allows(binding.board, binding.provider, binding.binding_version, now_ns)
        ):
            raise PermissionError("live policy revoked projection grant")

    def assert_page_start(
        self, grant: ProjectionGrantV1, *, offset: int, event_index: int = 0
    ) -> None:
        """source access 전에 cursor/highwater rollback을 거부한다."""
        with self._lock:
            if self._grant_highwater.get(grant.grant_ref) != (offset, event_index):
                raise PermissionError("cursor highwater rollback is forbidden")

    def issue_cursor(
        self,
        binding: TrustedObservationBindingV1,
        grant: ProjectionGrantV1,
        *,
        offset: int,
        event_index: int = 0,
        next_seq: int,
        last_ordinal: int | None,
        now_ns: int,
    ) -> str:
        if offset >= binding.turn_end.byte_offset:
            raise RuntimeError("terminal range has no cursor")
        state = _CursorState(
            binding.binding_ref, grant.grant_ref, binding.source_identity, offset,
            event_index,
            next_seq, last_ordinal, min(grant.expires_at_ns, now_ns + CURSOR_TTL_NS),
        )
        token = _mac("cu", self._secret, _canonical_json([
            state.binding_ref, state.grant_ref, _identity_dict(state.source_identity),
            state.offset, state.event_index, state.next_seq, state.last_ordinal,
            state.expires_at_ns, state.phase,
        ]))
        with self._lock:
            self._cursors[token] = state
        return token

    def read_cursor(
        self,
        token: str,
        binding: TrustedObservationBindingV1,
        grant: ProjectionGrantV1,
        *,
        now_ns: int,
    ) -> _CursorState:
        with self._lock:
            state = self._cursors.get(token)
            if state is None:
                raise PermissionError("cursor is invalid")
            if now_ns >= state.expires_at_ns:
                raise PermissionError("cursor expired")
            if state.binding_ref != binding.binding_ref or state.grant_ref != grant.grant_ref:
                raise PermissionError("cursor scope mismatch")
            highwater = self._grant_highwater.get(grant.grant_ref)
            if highwater is None or (state.offset, state.event_index) != highwater:
                raise PermissionError("cursor rollback is forbidden")
            return state

    def commit_cursor(
        self,
        used_token: str | None,
        grant: ProjectionGrantV1,
        *,
        expected_offset: int,
        expected_event_index: int = 0,
        next_offset: int,
        next_event_index: int = 0,
    ) -> None:
        with self._lock:
            highwater = self._grant_highwater.get(grant.grant_ref)
            if highwater != (expected_offset, expected_event_index):
                raise PermissionError("cursor highwater CAS failed")
            self._grant_highwater[grant.grant_ref] = (next_offset, next_event_index)
            if used_token is not None:
                self._cursors.pop(used_token, None)

    def create_child_graph(self, *, max_nodes: int = CHILD_MAX_GRAPH_NODES) -> ChildGraphGuard:
        with self._lock:
            self._graph_sequence += 1
            graph_ref = _mac(
                "cg", self._secret, self.issuer_id.encode(), str(self._graph_sequence).encode()
            )
            graph = ChildGraphGuard(max_nodes=max_nodes, graph_ref=graph_ref)
            self._child_graphs[graph_ref] = graph
        return graph

    def issue_child_edge(
        self,
        parent: TrustedObservationBindingV1,
        child: TrustedObservationBindingV1,
        lifecycle: TrustedChildLifecycleV1,
        graph: ChildGraphGuard,
        *,
        policy_store: PrivatePolicyStore,
        now_ns: int,
        depth: int,
        expires_at_ns: int,
    ) -> TrustedChildEdgeV1:
        self._validate_policy_store(policy_store)
        self._validate_binding(parent)
        self._validate_binding(child)
        lifecycle.validate(parent, child)
        policy = policy_store.get(parent.board)
        if (
            policy is None
            or policy.version != parent.policy_version
            or not policy.allows(parent.board, parent.provider, parent.binding_version, now_ns)
            or expires_at_ns <= now_ns
            or expires_at_ns > policy.expires_at_ns
        ):
            raise PermissionError("child edge live policy denied")
        with self._lock:
            graph_ref = graph.graph_ref
            if graph_ref is None or self._child_graphs.get(graph_ref) is not graph:
                raise PermissionError("child graph is not owned by this issuer")
            parent_graph = self._execution_graph.get(lifecycle.parent_execution)
            child_graph = self._execution_graph.get(lifecycle.child_execution)
            if parent_graph not in {None, graph_ref} or child_graph not in {None, graph_ref}:
                raise PermissionError("execution lineage belongs to another authoritative graph")
            actual_depth = graph.add(
                lifecycle.parent_execution, lifecycle.child_execution, depth=depth
            )
            self._execution_graph[lifecycle.parent_execution] = graph_ref
            self._execution_graph[lifecycle.child_execution] = graph_ref
        body = {
            "parent_binding_ref": parent.binding_ref,
            "child_binding_ref": child.binding_ref,
            "board": parent.board,
            "parent_task": parent.task,
            "child_task": child.task,
            "parent_generation": parent.generation,
            "child_generation": child.generation,
            "parent_range": [_boundary_dict(parent.turn_start), _boundary_dict(parent.turn_end)],
            "child_range": [_boundary_dict(child.turn_start), _boundary_dict(child.turn_end)],
            "policy_version": parent.policy_version,
            "policy_generation": policy.generation,
            "parent_execution": lifecycle.parent_execution,
            "child_execution": lifecycle.child_execution,
            "graph_ref": graph_ref,
            "depth": actual_depth,
            "expires_at_ns": expires_at_ns,
            "issuer_id": self.issuer_id,
        }
        ref = _mac("ce", self._secret, _canonical_json(body))
        tag = _mac("ct", self._secret, ref.encode(), _canonical_json(body))
        edge = TrustedChildEdgeV1(
            parent_binding_ref=parent.binding_ref,
            child_binding_ref=child.binding_ref,
            board=parent.board,
            parent_task=parent.task,
            child_task=child.task,
            parent_generation=parent.generation,
            child_generation=child.generation,
            parent_range=(parent.turn_start, parent.turn_end),
            child_range=(child.turn_start, child.turn_end),
            policy_version=parent.policy_version,
            policy_generation=policy.generation,
            parent_execution=lifecycle.parent_execution,
            child_execution=lifecycle.child_execution,
            graph_ref=graph_ref,
            depth=actual_depth,
            expires_at_ns=expires_at_ns,
            issuer_id=self.issuer_id,
            ref=ref,
            auth_tag=tag,
        )
        with self._lock:
            self._child_edges[ref] = graph_ref
        return edge

    def validate_child_route(
        self,
        edge: TrustedChildEdgeV1,
        *,
        parent_binding: TrustedObservationBindingV1,
        child_binding: TrustedObservationBindingV1,
        requested_board: str,
        requested_parent_task: str,
        requested_child_task: str,
        policy_store: PrivatePolicyStore,
        now_ns: int,
    ) -> None:
        self._validate_policy_store(policy_store)
        if now_ns >= edge.expires_at_ns:
            raise PermissionError("child edge expired")
        self._validate_binding(parent_binding)
        self._validate_binding(child_binding)
        with self._lock:
            if (
                self._child_edges.get(edge.ref) != edge.graph_ref
                or self._child_graphs.get(edge.graph_ref) is None
                or self._execution_graph.get(edge.parent_execution) != edge.graph_ref
                or self._execution_graph.get(edge.child_execution) != edge.graph_ref
            ):
                raise PermissionError("child route lineage is not currently authoritative")
        if (
            requested_board != edge.board
            or requested_parent_task != edge.parent_task
            or requested_child_task != edge.child_task
        ):
            raise PermissionError("child route scope mismatch")
        if (
            edge.parent_binding_ref != parent_binding.binding_ref
            or edge.child_binding_ref != child_binding.binding_ref
            or edge.parent_generation != parent_binding.generation
            or edge.child_generation != child_binding.generation
        ):
            raise PermissionError("child edge binding scope mismatch")
        policy = policy_store.get(edge.board)
        if (
            policy is None
            or policy.version != edge.policy_version
            or policy.generation != edge.policy_generation
            or not policy.allows(
                edge.board, parent_binding.provider, parent_binding.binding_version, now_ns
            )
        ):
            raise PermissionError("child route policy denied")
        body = edge.scope_dict()
        expected = _mac("ct", self._secret, edge.ref.encode(), _canonical_json(body))
        if not hmac.compare_digest(expected, edge.auth_tag):
            raise PermissionError("child edge receipt invalid")


# 구형 직접 발급 함수는 제품 권한처럼 오해될 수 있으므로 항상 차단한다.
def issue_projection_grant(*_args: Any, **_kwargs: Any) -> ProjectionGrantV1:
    raise RuntimeError("direct grant issuance disabled; authoritative private issuer required")


def validate_projection_grant(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("direct grant validation disabled; authoritative private issuer required")


def shared_replay_id(
    binding: TrustedObservationBindingV1,
    *,
    source_event_id: str,
    kind: str,
    secret: bytes,
) -> str:
    if kind not in _EVENT_KINDS:
        raise ValueError("unsupported event kind")
    return _mac("rp", secret, binding.replay_scope(), _require_text(source_event_id, "source event id").encode())


def opaque_identifier(raw: str, *, scope: str, secret: bytes) -> str:
    return _mac("id", secret, _require_text(scope, "scope").encode(), _require_text(raw, "raw id").encode())


def opaque_event_id(binding: TrustedObservationBindingV1, source_event_id: str) -> str:
    """MAC receipt를 namespace로 사용해 raw source ID를 노출하지 않는다."""
    return "ev_" + hashlib.sha256(
        binding.auth_tag.encode("ascii")
        + b"\x00event\x00"
        + _require_text(source_event_id, "source event id").encode()
    ).hexdigest()


def opaque_replay_id(binding: TrustedObservationBindingV1, source_event_id: str) -> str:
    return "rp_" + hashlib.sha256(
        binding.auth_tag.encode("ascii")
        + b"\x00replay\x00"
        + _require_text(source_event_id, "source event id").encode()
    ).hexdigest()


def opaque_tool_call_ref(
    binding: TrustedObservationBindingV1,
    raw_call_id: str,
    *,
    secret: bytes | None = None,
) -> str:
    key = secret if secret is not None else binding.auth_tag.encode("ascii")
    return _mac("tc", key, binding.replay_scope(), _require_text(raw_call_id, "tool call id").encode())


def sanitize_tool_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 64 or ".." in candidate or not _IDENTIFIER_RE.fullmatch(candidate):
        return None
    for run in _OPAQUE_RUN_RE.findall(candidate):
        digits = sum(character.isdigit() for character in run)
        if len(run) >= 32 or digits >= 8:
            return None
    return candidate


def split_mcp_tool_name(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    if value.startswith("mcp__"):
        parts = value[5:].split("__", 1)
    elif value.startswith("mcp_"):
        parts = value[4:].split("_", 1)
    else:
        return None
    if len(parts) != 2:
        return None
    server, tool = map(sanitize_tool_name, parts)
    return (server, tool) if server is not None and tool is not None else None


@dataclass(frozen=True)
class SanitizedText:
    text: str | None
    state: RedactionState
    truncated: bool = False


def _apply_redactions(value: str) -> tuple[str, bool]:
    changed = False
    for pattern, replacement in (
        (_EMAIL_RE, "[redacted-email]"), (_PHONE_RE, "[redacted-phone]"),
        (_HOME_RE, "[redacted-home]"), (_SECRET_RE, "[redacted-secret]"),
    ):
        value, count = pattern.subn(replacement, value)
        changed = changed or count > 0
    return value, changed


def _truncate_utf8(value: str, maximum: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value, False
    clipped = encoded[:maximum]
    while clipped:
        try:
            return clipped.decode("utf-8"), True
        except UnicodeDecodeError as error:
            clipped = clipped[:error.start]
    return "", True


def sanitize_event_text(value: Any) -> SanitizedText:
    if not isinstance(value, str):
        return SanitizedText(None, "failed_closed")
    try:
        if len(value.encode("utf-8")) > PRE_REDACTION_TEXT_BYTES:
            return SanitizedText(None, "failed_closed")
        redacted, changed = _apply_redactions(value)
        redacted, truncated = _truncate_utf8(redacted, POST_REDACTION_TEXT_BYTES)
    except (UnicodeError, RuntimeError, ValueError):
        return SanitizedText(None, "failed_closed")
    if not redacted:
        return SanitizedText(None, "fully_redacted", truncated)
    if truncated:
        return SanitizedText(redacted, "partially_redacted", True)
    return SanitizedText(redacted, "applied" if changed else "not_applicable")


def redact_text(value: Any) -> str:
    """projection helper. 완전 redaction은 빈 문자열로 fail closed한다."""
    sanitized = sanitize_event_text(value)
    return sanitized.text or ""


@dataclass(frozen=True)
class SubagentV1:
    role: str | None = None
    task: str | None = None
    result: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    completeness: Completeness = "partial"
    child_ref: str | None = None

    def __post_init__(self) -> None:
        if self.child_ref is not None:
            _require_opaque(self.child_ref, "child_ref")
        if self.role is not None and sanitize_tool_name(self.role) != self.role:
            raise ValueError("unsafe subagent role")
        for name in ("task", "result"):
            value = getattr(self, name)
            if value is not None and sanitize_event_text(value).text != value:
                raise ValueError(f"subagent {name} must be pre-redacted")
        if self.status is not None and self.status not in _STATUS_VALUES:
            raise ValueError("unsupported subagent status")
        if self.duration_ms is not None:
            _require_int(self.duration_ms, "subagent duration")
        if self.completeness not in _COMPLETENESS_VALUES:
            raise ValueError("unsupported subagent completeness")

    def to_public_dict(self, *, content_free: bool = False) -> dict[str, Any]:
        values: dict[str, Any] = {
            "role": self.role, "status": self.status, "duration_ms": self.duration_ms,
            "completeness": self.completeness, "child_ref": self.child_ref,
        }
        if not content_free:
            values.update(task=self.task, result=self.result)
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class ConversationEventV1:
    replay_id: str
    event_id: str
    seq: int
    kind: EventKind
    timestamp: Any = None
    status: str | None = None
    text: str | None = None
    tool: str | None = None
    mcp: Mapping[str, str] | None = None
    skill: Mapping[str, str] | None = None
    tool_call_ref: str | None = None
    subagent: SubagentV1 | None = None
    redaction: RedactionState = "not_applicable"
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported event schema")
        _require_opaque(self.replay_id, "replay_id")
        _require_opaque(self.event_id, "event_id")
        _require_int(self.seq, "sequence")
        if self.kind not in _EVENT_KINDS:
            raise ValueError("unsupported event kind")
        object.__setattr__(self, "timestamp", normalize_timestamp(self.timestamp))
        if self.text is not None and (
            self.kind not in _TEXT_KINDS or sanitize_event_text(self.text).text != self.text
        ):
            raise ValueError("event text must be public, pre-redacted and bounded")
        if self.redaction not in _REDACTION_VALUES:
            raise ValueError("unsupported redaction state")
        if self.status is not None and self.status not in _STATUS_VALUES:
            raise ValueError("unsupported event status")
        if self.status is not None and self.kind not in {
            "tool_result", "subagent_start", "subagent_stop"
        }:
            raise ValueError("event status is invalid for kind")
        if self.tool is not None and (self.kind != "tool_call" or sanitize_tool_name(self.tool) != self.tool):
            raise ValueError("unsafe ordinary tool metadata")
        if self.mcp is not None:
            copied = dict(self.mcp)
            if self.kind != "mcp_call" or set(copied) != {"server", "tool"}:
                raise ValueError("invalid MCP metadata shape")
            if any(sanitize_tool_name(copied[key]) != copied[key] for key in copied):
                raise ValueError("unsafe MCP metadata")
            object.__setattr__(self, "mcp", MappingProxyType(copied))
        if self.skill is not None:
            copied = dict(self.skill)
            if self.kind != "skill_load" or set(copied) != {"name", "action"}:
                raise ValueError("invalid skill metadata shape")
            if sanitize_tool_name(copied["name"]) != copied["name"] or copied["action"] != "loaded":
                raise ValueError("unsafe skill metadata")
            object.__setattr__(self, "skill", MappingProxyType(copied))
        if self.tool_call_ref is not None:
            _require_opaque(self.tool_call_ref, "tool_call_ref")
            if self.kind not in {"tool_call", "tool_result", "mcp_call"}:
                raise ValueError("tool ref is invalid for event kind")
        if self.kind == "tool_result" and self.tool_call_ref is None:
            raise ValueError("tool result requires opaque tool ref")
        if self.kind == "tool_result" and self.status is None:
            raise ValueError("tool result requires status")
        if self.kind == "tool_call" and (self.tool is None or self.tool_call_ref is None):
            raise ValueError("tool call requires tool metadata and opaque ref")
        if self.kind == "mcp_call" and (self.mcp is None or self.tool_call_ref is None):
            raise ValueError("MCP call requires metadata and opaque ref")
        if self.kind == "skill_load" and self.skill is None:
            raise ValueError("skill load requires metadata")
        if self.subagent is not None and self.kind not in {"subagent_start", "subagent_stop"}:
            raise ValueError("subagent metadata requires lifecycle event")

    def to_public_dict(self, *, content_free: bool = False) -> dict[str, Any]:
        # frozen dataclass의 경계에서도 allowlist를 재검증한다.
        self.__post_init__()
        values: dict[str, Any] = {
            "schema_version": self.schema_version, "replay_id": self.replay_id,
            "event_id": self.event_id, "seq": self.seq, "kind": self.kind,
            "timestamp": self.timestamp, "status": self.status, "tool": self.tool,
            "mcp": dict(self.mcp) if self.mcp is not None else None,
            "skill": dict(self.skill) if self.skill is not None else None,
            "tool_call_ref": self.tool_call_ref,
            "subagent": self.subagent.to_public_dict(content_free=content_free) if self.subagent else None,
        }
        if not content_free:
            values["text"] = self.text
            if self.text is not None:
                values["redaction"] = self.redaction
        return {key: value for key, value in values.items() if value is not None}


def _index_payload(event: ConversationEventV1) -> dict[str, Any]:
    return event.to_public_dict(content_free=True)


def _validate_index_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("index payload schema invalid")  # noqa: TRY004
    allowed = {
        "schema_version", "replay_id", "event_id", "seq", "kind", "timestamp",
        "status", "tool", "mcp", "skill", "tool_call_ref", "subagent",
    }
    if set(payload) - allowed or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("index payload schema invalid")
    base = {"schema_version", "replay_id", "event_id", "seq", "kind"}
    kind = payload.get("kind")
    specific = {
        "user_message": set(),
        "final_assistant": set(),
        "tool_call": {"tool", "tool_call_ref"},
        "tool_result": {"status", "tool_call_ref"},
        "mcp_call": {"mcp", "tool_call_ref"},
        "skill_load": {"skill"},
        "subagent_start": {"subagent"},
        "subagent_stop": {"subagent", "status"},
    }
    if not isinstance(kind, str) or kind not in specific:
        raise ValueError("index payload exact kind schema invalid")
    expected = base | specific[kind]
    if "timestamp" in payload:
        expected.add("timestamp")
    if set(payload) != expected:
        raise ValueError("index payload exact kind schema invalid")
    try:
        event = ConversationEventV1(
            replay_id=payload.get("replay_id"), event_id=payload.get("event_id"),
            seq=payload.get("seq"), kind=payload.get("kind"), timestamp=payload.get("timestamp"),
            status=payload.get("status"), tool=payload.get("tool"), mcp=payload.get("mcp"),
            skill=payload.get("skill"), tool_call_ref=payload.get("tool_call_ref"),
            subagent=SubagentV1(**payload["subagent"]) if isinstance(payload.get("subagent"), dict) else None,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("index payload schema invalid") from error
    return event.replay_id


class ConversationIndex:
    """flock+bounded reload+MAC framing을 사용하는 content-free private JSONL journal."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        secret: bytes,
        scope: str,
        cache_controller: IndexCacheController | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.name in {"", ".", ".."}:
            raise ValueError("index path must name an absolute leaf")
        self.max_bytes = _require_int(max_bytes, "index budget", minimum=1, maximum=INDEX_CARD_BYTES)
        if len(secret) < 32:
            raise ValueError("index secret must contain at least 32 bytes")
        self.secret = bytes(secret)
        self.scope = _require_text(scope, "index scope", maximum=128)
        self.cache_controller = cache_controller
        self._identity: tuple[int, int] | None = None
        self._load_initial()

    def _open_parent(self, *, create: bool) -> int:
        parent = open_directory(self.path.parent, create=create, mode=0o700)
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            os.close(parent)
            raise PermissionError("index parent must be owner-only")
        return parent

    def _open_locked(self, parent: int, *, create: bool) -> tuple[int, bool]:
        flags = (
            os.O_RDWR | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        created = False
        try:
            fd = os.open(self.path.name, flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise
            fd = os.open(self.path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            created = True
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
        ):
            os.close(fd)
            raise PermissionError("index must already be owner 0600 regular single-link file")
        entry = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        if (entry.st_dev, entry.st_ino) != identity:
            os.close(fd)
            raise RuntimeError("index identity changed")
        if self._identity is not None and identity != self._identity:
            os.close(fd)
            raise RuntimeError("index identity CAS failed")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd, created

    def _verify_owner_receipt(
        self, parent: int, identity: tuple[int, int], *, create: bool
    ) -> None:
        name = self.path.name + ".owner"
        payload = {"schema_version": SCHEMA_VERSION, "device": identity[0], "inode": identity[1]}
        receipt = {**payload, "mac": _mac("io", self.secret, self.scope.encode(), _canonical_json(payload))}
        flags = (
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            owner_fd = os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise ValueError("index authenticated ownership receipt is missing") from None
            owner_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
            try:
                raw = _canonical_json(receipt) + b"\n"
                if os.write(owner_fd, raw) != len(raw):
                    raise OSError("index ownership receipt write was partial")
                os.fsync(owner_fd)
                os.fsync(parent)
            finally:
                os.close(owner_fd)
            return
        try:
            info = os.fstat(owner_fd)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise PermissionError("index ownership receipt is not private")
            raw = os.read(owner_fd, 4097)
            if len(raw) > 4096 or not raw.endswith(b"\n"):
                raise ValueError("index ownership receipt schema invalid")
            actual = strict_json_loads(raw.decode("utf-8"))
            if actual != receipt:
                raise ValueError("index authenticated ownership receipt is invalid")
        finally:
            os.close(owner_fd)

    def _record_mac(self, payload: Mapping[str, Any]) -> str:
        return _mac("ix", self.secret, self.scope.encode(), _canonical_json(payload))

    def _reload_locked(self, fd: int, *, recover_partial: bool) -> set[str]:
        size = os.fstat(fd).st_size
        if size > self.max_bytes:
            raise RuntimeError("index budget exceeded")
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                raise OSError("index read ended early")
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        partial_boundary: int | None = None
        if data and not data.endswith(b"\n"):
            partial_boundary = data.rfind(b"\n") + 1
            if not recover_partial:
                raise ValueError("index has partial frame")
            data = data[:partial_boundary]
        seen: set[str] = set()
        for raw_line in data.splitlines():
            try:
                envelope = strict_json_loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise ValueError("index strict JSON schema invalid") from error
            if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "payload", "mac"}:
                raise ValueError("index exact schema invalid")
            if envelope["schema_version"] != SCHEMA_VERSION or not isinstance(envelope["mac"], str):
                raise ValueError("index exact schema invalid")
            replay = _validate_index_payload(envelope["payload"])
            expected = self._record_mac(envelope["payload"])
            if not hmac.compare_digest(expected, envelope["mac"]):
                raise ValueError("index MAC invalid")
            if replay in seen:
                raise ValueError("index duplicate replay frame")
            seen.add(replay)
        if partial_boundary is not None:
            if not seen:
                raise ValueError("index partial recovery requires authenticated journal history")
            os.ftruncate(fd, partial_boundary)
            os.fsync(fd)
        return seen

    def _load_initial(self) -> None:
        try:
            parent = self._open_parent(create=False)
        except FileNotFoundError:
            return
        try:
            try:
                fd, _created = self._open_locked(parent, create=False)
            except FileNotFoundError:
                return
            try:
                info = os.fstat(fd)
                self._verify_owner_receipt(parent, (info.st_dev, info.st_ino), create=False)
                self._reload_locked(fd, recover_partial=True)
                self._identity = (info.st_dev, info.st_ino)
                validate_directory(self.path.parent, parent)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        finally:
            os.close(parent)

    def append_once(self, event: ConversationEventV1) -> bool:
        payload = _index_payload(event)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "payload": payload,
            "mac": self._record_mac(payload),
        }
        frame = _canonical_json(envelope) + b"\n"
        if self.cache_controller is not None:
            self.cache_controller.reserve(len(frame))
        try:
            parent = self._open_parent(create=True)
        except BaseException:
            if self.cache_controller is not None:
                self.cache_controller.release(len(frame))
            raise
        fd = -1
        try:
            fd, created = self._open_locked(parent, create=True)
            info = os.fstat(fd)
            self._verify_owner_receipt(
                parent, (info.st_dev, info.st_ino), create=created
            )
            seen = self._reload_locked(fd, recover_partial=True)
            if event.replay_id in seen:
                os.fsync(fd)
                entry = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
                info = os.fstat(fd)
                if (entry.st_dev, entry.st_ino) != (info.st_dev, info.st_ino):
                    raise RuntimeError("index identity changed during duplicate recovery")
                os.fsync(parent)
                validate_directory(self.path.parent, parent)
                return False
            if info.st_size + len(frame) > self.max_bytes:
                raise RuntimeError("index budget exceeded")
            os.lseek(fd, 0, os.SEEK_END)
            written = 0
            while written < len(frame):
                count = os.write(fd, frame[written:])
                if count <= 0:
                    raise OSError("index append made no progress")
                written += count
            os.fsync(fd)
            entry = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            identity = (info.st_dev, info.st_ino)
            if (entry.st_dev, entry.st_ino) != identity or entry.st_nlink != 1:
                raise RuntimeError("index identity changed after append")
            os.fsync(parent)
            validate_directory(self.path.parent, parent)
            self._identity = identity
            return True
        finally:
            if fd >= 0:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
            os.close(parent)
            if self.cache_controller is not None:
                self.cache_controller.release(len(frame))


def append_event_once(index: ConversationIndex, event: ConversationEventV1) -> bool:
    return index.append_once(event)


class IndexCacheController:
    """dedicated index cache의 global byte/retention upper bound를 집행한다."""

    def __init__(self, root: Path, *, global_bytes: int = INDEX_GLOBAL_BYTES, retention_ns: int) -> None:
        self.root = Path(root)
        self.global_bytes = _require_int(global_bytes, "global index budget", minimum=1, maximum=INDEX_GLOBAL_BYTES)
        self.retention_ns = _require_int(retention_ns, "index retention", minimum=1)
        self._completed: dict[str, tuple[int, tuple[int, int]]] = {}
        self._reserved_bytes = 0
        self._lock = threading.RLock()

    def register(self, index: ConversationIndex, *, completed_at_ns: int) -> None:
        if not isinstance(index, ConversationIndex):
            raise TypeError("retention registration requires an authenticated index")
        if index.path.parent != self.root or index._identity is None:
            raise PermissionError("retention index root or authenticated identity mismatch")
        self._completed[index.path.name] = (
            _require_int(completed_at_ns, "completion time"), index._identity
        )

    def _total(self) -> int:
        total = 0
        count = 0
        with os.scandir(self.root) as entries:
            for entry in entries:
                count += 1
                if count > JSONL_RECORD_LIMIT:
                    raise RuntimeError("global index scan record budget exceeded")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    total += info.st_size
                    if total > self.global_bytes:
                        break
        return total

    def reserve(self, size: int) -> None:
        size = _require_int(size, "index reservation", minimum=1)
        with self._lock:
            if size > self.global_bytes or (
                self.root.exists()
                and self._total() + self._reserved_bytes + size > self.global_bytes
            ):
                raise RuntimeError("global index budget exceeded")
            self._reserved_bytes += size

    def release(self, size: int) -> None:
        size = _require_int(size, "index reservation release", minimum=1)
        with self._lock:
            if size > self._reserved_bytes:
                raise RuntimeError("index reservation accounting underflow")
            self._reserved_bytes -= size

    def prune(self, *, now_ns: int) -> int:
        removed = 0
        with self._lock:
            for name, (completed, registered_identity) in list(self._completed.items()):
                if now_ns - completed < self.retention_ns:
                    continue
                parent = open_directory(self.root)
                try:
                    fd = os.open(
                        name,
                        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent,
                    )
                    try:
                        info = os.fstat(fd)
                        entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if (
                            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.getuid()
                            or (entry.st_dev, entry.st_ino) != (info.st_dev, info.st_ino)
                            or (info.st_dev, info.st_ino) != registered_identity
                        ):
                            continue
                        # macOS에는 열린 FD/identity를 조건으로 하는 atomic unlink가 없다.
                        # stat→unlink는 foreign successor를 지울 수 있으므로 삭제를 생략한다.
                        continue
                    finally:
                        os.close(fd)
                finally:
                    os.close(parent)
        return removed


class SourceReadLimiter:
    """process/board/task source read 동시성 상한."""

    def __init__(
        self,
        *,
        process_limit: int = SOURCE_READ_PROCESS_LIMIT,
        board_limit: int = SOURCE_READ_BOARD_LIMIT,
        task_limit: int = SOURCE_READ_TASK_LIMIT,
    ) -> None:
        self._process = threading.BoundedSemaphore(process_limit)
        self._board_limit = board_limit
        self._task_limit = task_limit
        self._boards: dict[str, threading.BoundedSemaphore] = {}
        self._tasks: dict[tuple[str, str], threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    @contextmanager
    def admit(self, board: str, task: str, *, deadline_ms: int) -> Iterator[None]:
        _require_text(board, "read board", maximum=256)
        _require_text(task, "read task", maximum=256)
        _require_int(deadline_ms, "read deadline", minimum=1, maximum=API_REQUEST_DEADLINE_MS)
        deadline = time.monotonic() + deadline_ms / 1000
        with self._lock:
            if board not in self._boards and len(self._boards) >= JSONL_RECORD_LIMIT:
                raise RuntimeError("source read board scope budget exceeded")
            task_key = (board, task)
            if task_key not in self._tasks and len(self._tasks) >= JSONL_RECORD_LIMIT:
                raise RuntimeError("source read task scope budget exceeded")
            board_sem = self._boards.setdefault(board, threading.BoundedSemaphore(self._board_limit))
            task_sem = self._tasks.setdefault(task_key, threading.BoundedSemaphore(self._task_limit))
        acquired: list[threading.BoundedSemaphore] = []
        try:
            for semaphore in (self._process, board_sem, task_sem):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not semaphore.acquire(timeout=remaining):
                    raise TimeoutError("source read concurrency deadline exceeded")
                acquired.append(semaphore)
            yield
        finally:
            for semaphore in reversed(acquired):
                semaphore.release()


@dataclass
class RootCapability(AbstractContextManager["RootCapability"]):
    fd: int
    identity: SourceIdentityV1
    locator_scope: bytes
    _chain: list[int]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in reversed(self._chain):
            os.close(fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass
class VerifiedSource(AbstractContextManager["VerifiedSource"]):
    fd: int
    identity: SourceIdentityV1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        os.close(self.fd)


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _validate_directory_component(info: os.stat_result, expected_owner: int) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError("source path component must be a directory")
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid not in {0, expected_owner}:
        raise PermissionError("source directory owner is not trusted")
    writable = mode & 0o022
    sticky_root = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if writable and not sticky_root:
        raise PermissionError("source directory permissions are not trusted")


def open_verified_root(locator: SourceLocator) -> RootCapability:
    """filesystem anchor부터 allowed root까지 모든 component를 no-follow로 pin한다."""
    parts = locator.allowed_root.parts
    if not parts or parts[0] != os.sep:
        raise ValueError("allowed root must be absolute")
    chain = [os.open(os.sep, _directory_flags())]
    try:
        _validate_directory_component(os.fstat(chain[0]), locator.expected_owner)
        for component in parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError("unsafe root component")
            child = os.open(component, _directory_flags(), dir_fd=chain[-1])
            try:
                _validate_directory_component(os.fstat(child), locator.expected_owner)
            except BaseException:
                os.close(child)
                raise
            chain.append(child)
        info = os.fstat(chain[-1])
        return RootCapability(
            fd=chain[-1], identity=SourceIdentityV1.from_stat(info),
            locator_scope=locator.private_scope(), _chain=chain,
        )
    except BaseException:
        for fd in reversed(chain):
            os.close(fd)
        raise


def open_verified_jsonl_fd(
    locator: SourceLocator, *, root_capability: RootCapability | None = None
) -> VerifiedSource:
    """retained root capability 아래 leaf를 nonblocking으로 열고 type/owner/inode를 검증한다."""
    owned = root_capability is None
    root = root_capability or open_verified_root(locator)
    if root._closed or root.locator_scope != locator.private_scope():
        if owned:
            root.close()
        raise PermissionError("root capability scope mismatch")
    current = os.dup(root.fd)
    try:
        parts = locator.relative_path.parts
        for component in parts[:-1]:
            child = os.open(component, _directory_flags(), dir_fd=current)
            os.close(current)
            current = child
            info = os.fstat(current)
            if info.st_uid != locator.expected_owner or stat.S_IMODE(info.st_mode) & 0o022:
                raise PermissionError("source descendant directory permissions are not trusted")
        flags = (
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(parts[-1], flags, dir_fd=current)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PermissionError("source must be a regular file")
            if info.st_uid != locator.expected_owner or info.st_nlink != 1:
                raise PermissionError("source owner/single link requirement failed")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError("source mode must be owner-private")
            entry = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
            if (entry.st_dev, entry.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError("source identity changed")
            return VerifiedSource(fd, SourceIdentityV1.from_stat(info))
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(current)
        if owned:
            root.close()


@dataclass(frozen=True)
class DisabledSqliteSource:
    source_availability: Literal["disabled"] = "disabled"
    connection: None = None
    reason: str = "opened DB inode and WAL/SHM snapshot cannot be proven"


def open_verified_sqlite_source(_locator: SourceLocator) -> DisabledSqliteSource:
    return DisabledSqliteSource()


@dataclass(frozen=True)
class TrustedChildLifecycleV1:
    parent_execution: str
    child_execution: str
    child_session: str
    role: str
    started_at_ns: int
    stopped_at_ns: int
    status: str

    def validate(self, parent: TrustedObservationBindingV1, child: TrustedObservationBindingV1) -> None:
        for value, name in (
            (self.parent_execution, "parent execution"), (self.child_execution, "child execution"),
            (self.child_session, "child session"), (self.role, "child role"),
        ):
            _require_text(value, name)
        if self.parent_execution != parent.producer_execution:
            raise ValueError("child lifecycle parent execution mismatch")
        if self.child_execution != child.producer_execution or self.child_session != child.session:
            raise ValueError("child lifecycle identity mismatch")
        if parent.board != child.board or parent.provider != child.provider:
            raise ValueError("child lifecycle provider/board mismatch")
        if self.status not in _STATUS_VALUES:
            raise ValueError("child lifecycle status invalid")
        if self.stopped_at_ns < self.started_at_ns:
            raise ValueError("child lifecycle time range invalid")


class ChildGraphGuard:
    def __init__(
        self, *, max_nodes: int = CHILD_MAX_GRAPH_NODES, graph_ref: str | None = None
    ) -> None:
        self.max_nodes = _require_int(max_nodes, "child graph node limit", minimum=2, maximum=CHILD_MAX_GRAPH_NODES)
        if graph_ref is not None and re.fullmatch(r"cg_[0-9a-f]{64}", graph_ref) is None:
            raise ValueError("child graph ref is invalid")
        self.graph_ref = graph_ref
        self._edges: dict[str, set[str]] = {}
        self._nodes: set[str] = set()
        self._depths: dict[str, int] = {}
        self._lock = threading.RLock()

    def _reachable(self, start: str, target: str) -> bool:
        pending = [start]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(self._edges.get(current, ()))
        return False

    def add(self, parent: str, child: str, *, depth: int) -> int:
        supplied = _require_int(depth, "child depth", minimum=1, maximum=CHILD_MAX_DEPTH)
        with self._lock:
            if parent == child or self._reachable(child, parent):
                raise ValueError("child graph cycle detected")
            children = self._edges.setdefault(parent, set())
            if child in children:
                raise RuntimeError("child edge already exists")
            if child in self._depths:
                raise RuntimeError("child already has an authoritative graph position")
            if len(children) >= CHILD_MAX_FANOUT:
                raise ValueError("child graph fanout exceeded")
            prospective = self._nodes | {parent, child}
            if len(prospective) > self.max_nodes:
                raise ValueError("child graph node limit exceeded")
            actual = self._depths.get(parent, 0) + 1
            if supplied != actual:
                raise ValueError("child depth does not match authoritative graph")
            if actual > CHILD_MAX_DEPTH:
                raise ValueError("child graph depth exceeded")
            children.add(child)
            self._nodes = prospective
            self._depths.setdefault(parent, actual - 1)
            self._depths[child] = actual
            return actual


@dataclass(frozen=True)
class TrustedChildEdgeV1:
    parent_binding_ref: str
    child_binding_ref: str
    board: str
    parent_task: str
    child_task: str
    parent_generation: int
    child_generation: int
    parent_range: tuple[SourceBoundaryV1, SourceBoundaryV1]
    child_range: tuple[SourceBoundaryV1, SourceBoundaryV1]
    policy_version: int
    policy_generation: int
    parent_execution: str
    child_execution: str
    graph_ref: str
    depth: int
    expires_at_ns: int
    issuer_id: str
    ref: str
    auth_tag: str

    def __post_init__(self) -> None:
        _require_opaque(self.parent_binding_ref, "binding_ref")
        _require_opaque(self.child_binding_ref, "binding_ref")
        _require_opaque(self.ref, "child_ref")
        if re.fullmatch(r"cg_[0-9a-f]{64}", self.graph_ref) is None:
            raise ValueError("child edge graph ref is invalid")
        _require_text(self.issuer_id, "child edge issuer")
        if len(self.parent_range) != 2 or len(self.child_range) != 2:
            raise ValueError("child edge range invalid")

    def scope_dict(self) -> dict[str, Any]:
        return {
            "parent_binding_ref": self.parent_binding_ref, "child_binding_ref": self.child_binding_ref,
            "board": self.board, "parent_task": self.parent_task, "child_task": self.child_task,
            "parent_generation": self.parent_generation, "child_generation": self.child_generation,
            "parent_range": [_boundary_dict(item) for item in self.parent_range],
            "child_range": [_boundary_dict(item) for item in self.child_range],
            "policy_version": self.policy_version, "policy_generation": self.policy_generation,
            "parent_execution": self.parent_execution,
            "child_execution": self.child_execution, "graph_ref": self.graph_ref,
            "depth": self.depth,
            "expires_at_ns": self.expires_at_ns, "issuer_id": self.issuer_id,
        }


@dataclass(frozen=True)
class ChildPair:
    child_ref: None = None
    child_session_ref: None = None
    role: str | None = None
    task: str | None = None
    result: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    completeness: Completeness = "partial"


def pair_child_events(start: Mapping[str, Any], stop: Mapping[str, Any], *, secret: bytes) -> ChildPair:
    """권위 lifecycle receipt가 없는 raw mappings에는 navigation을 발급하지 않는다."""
    del secret
    role = sanitize_tool_name(start.get("role"))
    status = stop.get("status") if stop.get("status") in _STATUS_VALUES else None
    return ChildPair(role=role, status=status, completeness="partial")


def make_child_edge(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("raw child edge issuance disabled; authoritative typed lifecycle required")
