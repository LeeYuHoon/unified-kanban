"""승인된 새 observation을 보안 core와 제품 경계에 연결하는 계층.

정책과 binding은 owner-only sidecar에 서명해 저장한다. 대화 본문은 저장하지
않으며 loopback process token은 interactive principal로 승격하지 않는다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .conversation import (
    JSONL_LINE_BYTES,
    JSONL_RECORD_LIMIT,
    JSONL_SCAN_BYTES,
    JSONL_SCAN_DEADLINE_MS,
    CollectionPolicyV1,
    PrivateFixtureAuthority,
    SourceBoundaryV1,
    SourceIdentityV1,
    SourceLocator,
    TrustedObservationBindingV1,
    open_verified_jsonl_fd,
    open_verified_root,
)
from .private_files import atomic_publish, read_bytes
from .transcript_projection import (
    ClaudeProjector,
    CodexProjector,
    ProjectionLimits,
    project_page,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _tag(prefix: str, secret: bytes, value: object) -> str:
    if len(secret) < 32:
        raise ValueError("private secret must contain at least 32 bytes")
    return prefix + "_" + hmac.new(secret, _canonical(value), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class KernelAuthorityReceiptV1:
    schema_version: int
    issuer_id: str
    key_id: str
    issued_at_ns: int
    auth_tag: str


class ProductionConversationAuthority(PrivateFixtureAuthority):
    """Hermes kernel 서명 receipt가 있을 때만 생성되는 production issuer."""

    @classmethod
    def issue_kernel_receipt(
        cls, *, kernel_secret: bytes, issuer_id: str, key_id: str, now_ns: int
    ) -> KernelAuthorityReceiptV1:
        body = [1, issuer_id, key_id, now_ns]
        return KernelAuthorityReceiptV1(1, issuer_id, key_id, now_ns, _tag("kr", kernel_secret, body))

    @classmethod
    def bootstrap(
        cls,
        *,
        authority_secret: bytes,
        kernel_secret: bytes,
        receipt: KernelAuthorityReceiptV1,
    ) -> ProductionConversationAuthority:
        body = [receipt.schema_version, receipt.issuer_id, receipt.key_id, receipt.issued_at_ns]
        expected = _tag("kr", kernel_secret, body)
        if receipt.schema_version != 1 or not hmac.compare_digest(expected, receipt.auth_tag):
            raise PermissionError("kernel authority receipt is invalid")
        authority = cls.__new__(cls)
        authority._initialize_authority(authority_secret, receipt.issuer_id)
        return authority

    @classmethod
    def for_test(cls, *, kernel_secret: bytes) -> ProductionConversationAuthority:
        receipt = cls.issue_kernel_receipt(
            kernel_secret=kernel_secret,
            issuer_id="test-hermes-kanban-kernel",
            key_id="test-only",
            now_ns=1,
        )
        return cls.bootstrap(
            authority_secret=b"test-authority-secret-material!!",
            kernel_secret=kernel_secret,
            receipt=receipt,
        )


@dataclass(frozen=True)
class VerifiedInteractivePrincipal:
    principal_id: str
    auth_method: Literal["oauth_session"]
    board_grants: frozenset[str]
    authenticated_at_ns: int

    @classmethod
    def create(
        cls,
        *,
        principal_id: str,
        auth_method: str,
        board_grants: frozenset[str],
        authenticated_at_ns: int,
    ) -> VerifiedInteractivePrincipal:
        if auth_method != "oauth_session":
            raise PermissionError("verified interactive session is required")
        if not principal_id or not board_grants or any(not value for value in board_grants):
            raise ValueError("interactive principal scope is invalid")
        return cls(principal_id, "oauth_session", frozenset(board_grants), authenticated_at_ns)

    def authorizes(self, board: str) -> bool:
        return board in self.board_grants


class OwnerPolicyFile:
    """명시적 generation CAS를 쓰는 owner-managed policy 파일."""

    def __init__(self, path: Path, *, secret: bytes) -> None:
        self.path = Path(path)
        self.secret = bytes(secret)
        if not self.path.is_absolute():
            raise ValueError("policy path must be absolute")

    @staticmethod
    def _payload(policy: CollectionPolicyV1) -> dict[str, object]:
        return {
            "schema_version": 1,
            "version": policy.version,
            "generation": policy.generation,
            "activated_at_ns": policy.activated_at_ns,
            "expires_at_ns": policy.expires_at_ns,
            "minimum_binding_version": policy.minimum_binding_version,
            "enabled_boards": {key: sorted(value) for key, value in policy.enabled_boards.items()},
        }

    def load(self) -> CollectionPolicyV1:
        try:
            raw, receipt = read_bytes(self.path)
        except FileNotFoundError:
            return CollectionPolicyV1.disabled(version=1, generation=1)
        try:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
                raise ValueError("policy envelope is invalid")
            payload = envelope["payload"]
            if not isinstance(payload, dict) or not hmac.compare_digest(
                str(envelope["mac"]), _tag("po", self.secret, payload)
            ):
                raise PermissionError("policy MAC is invalid")
            return CollectionPolicyV1(
                version=payload["version"], generation=payload["generation"],
                activated_at_ns=payload["activated_at_ns"], expires_at_ns=payload["expires_at_ns"],
                minimum_binding_version=payload["minimum_binding_version"],
                enabled_boards={key: frozenset(value) for key, value in payload["enabled_boards"].items()},
            )
        finally:
            receipt.close()

    def replace(self, policy: CollectionPolicyV1, *, expected_generation: int) -> None:
        current = self.load()
        if current.generation != expected_generation or policy.generation != expected_generation + 1:
            raise RuntimeError("policy generation CAS failed")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise PermissionError("policy parent must be owner-only")
        payload = self._payload(policy)
        expected_identity = None
        if self.path.exists():
            old_raw, expected_identity = read_bytes(self.path)
            old_payload = self._payload(current)
            expected_raw = _canonical({
                "payload": old_payload,
                "mac": _tag("po", self.secret, old_payload),
            }) + b"\n"
            if old_raw != expected_raw:
                expected_identity.close()
                raise RuntimeError("policy changed before generation CAS")
        try:
            atomic_publish(
                self.path,
                _canonical({"payload": payload, "mac": _tag("po", self.secret, payload)}) + b"\n",
                expected_identity=expected_identity,
            ).close()
        except BaseException:
            if expected_identity is not None:
                expected_identity.close()
            raise


def _identity(value: dict[str, int]) -> SourceIdentityV1:
    return SourceIdentityV1(value["device"], value["inode"], value["size"], value["mtime_ns"])


def _boundary(value: dict[str, int | None]) -> SourceBoundaryV1:
    return SourceBoundaryV1(value["byte_offset"], value.get("ordinal"))


class BindingStore:
    """raw identity를 private하게 유지하는 content-free signed sidecar."""

    def __init__(self, root: Path, *, secret: bytes) -> None:
        self.root = Path(root)
        self.secret = bytes(secret)

    def _path(self, board: str, task: str) -> Path:
        digest = hashlib.sha256(f"{board}\0{task}".encode()).hexdigest()
        return self.root / f"{digest}.json"

    def put(self, *, binding: TrustedObservationBindingV1, locator: SourceLocator) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "binding": binding.scope_dict(include_auth=True),
            "locator": {
                "provider": locator.provider,
                "allowed_root": os.fsencode(locator.allowed_root).hex(),
                "relative_path": os.fsencode(locator.relative_path).hex(),
                "expected_owner": locator.expected_owner,
            },
        }
        atomic_publish(
            self._path(binding.board, binding.task),
            _canonical({"payload": payload, "mac": _tag("bs", self.secret, payload)}) + b"\n",
        ).close()

    def get(self, board: str, task: str) -> tuple[TrustedObservationBindingV1, SourceLocator]:
        raw, receipt = read_bytes(self._path(board, task))
        try:
            envelope = json.loads(raw)
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            if not isinstance(payload, dict) or not hmac.compare_digest(
                str(envelope.get("mac", "")), _tag("bs", self.secret, payload)
            ):
                raise PermissionError("binding sidecar MAC is invalid")
            values = payload["binding"]
            locator_values = payload["locator"]
            locator = SourceLocator(
                locator_values["provider"], Path(os.fsdecode(bytes.fromhex(locator_values["allowed_root"]))),
                Path(os.fsdecode(bytes.fromhex(locator_values["relative_path"]))),
                locator_values["expected_owner"],
            )
            binding = TrustedObservationBindingV1(
                board=values["board"], task=values["task"], provider=values["provider"],
                schema_pin=values["schema_pin"], profile_root_identity=_identity(values["profile_root_identity"]),
                locator_ref=values["locator_ref"], source_identity=_identity(values["source_identity"]),
                session=values["session"], turn_start=_boundary(values["turn_start"]),
                turn_end=_boundary(values["turn_end"]), boundary_alignment_proof=values["boundary_alignment_proof"],
                binding_version=values["binding_version"], generation=values["generation"],
                policy_version=values["policy_version"], producer_execution=values["producer_execution"],
                created_at_ns=values["created_at_ns"], sealed_at_ns=values["sealed_at_ns"],
                producer_receipt=values["producer_receipt"], binding_ref=values["binding_ref"],
                source_range_digest=values["source_range_digest"],
                auth_tag=values["auth_tag"], issuer_id=values["issuer_id"],
            )
            if binding.board != board or binding.task != task:
                raise PermissionError("binding sidecar scope mismatch")
            return binding, locator
        finally:
            receipt.close()


class ConversationService:
    """membership → policy → binding 검증 뒤 verified FD projection만 수행한다."""

    def __init__(
        self,
        *,
        authority: ProductionConversationAuthority,
        policies: OwnerPolicyFile,
        bindings: BindingStore,
        task_membership: Callable[[str, str], bool],
        principal_board_grants: dict[str, frozenset[str]] | None = None,
        kernel_secret: bytes | None = None,
        provider_roots: dict[str, Path] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.authority = authority
        self.policies = policies
        self.bindings = bindings
        self.task_membership = task_membership
        self.principal_board_grants = {
            principal: frozenset(boards)
            for principal, boards in (principal_board_grants or {}).items()
        }
        self.clock_ns = clock_ns
        self.kernel_secret = kernel_secret
        self.provider_roots = {
            provider: Path(root)
            for provider, root in (provider_roots or {}).items()
        }
        if any(not root.is_absolute() for root in self.provider_roots.values()):
            raise ValueError("configured provider roots must be absolute")
        self._cursor_grants: dict[str, object] = {}
        self._lock = threading.RLock()

    def authorizes_principal(self, principal_id: str, board: str) -> bool:
        return board in self.principal_board_grants.get(principal_id, frozenset())

    def _verify_task_receipt(
        self,
        receipt: dict[str, object],
        *,
        board: str,
        task: str,
        policy: CollectionPolicyV1 | None = None,
    ) -> int:
        if self.kernel_secret is None or set(receipt) != {
            "schema", "issuer", "key_id", "board", "task", "observation",
            "created_at_ns", "nonce", "auth_tag",
        }:
            raise PermissionError("typed observation receipt is unavailable")
        body = {key: value for key, value in receipt.items() if key != "auth_tag"}
        if (
            body.get("schema") != "hermes-kanban-observation-receipt-v1"
            or body.get("issuer") != "hermes-kanban-kernel"
            or body.get("board") != board
            or body.get("task") != task
            or body.get("observation") is not True
        ):
            raise PermissionError("typed observation receipt scope mismatch")
        wire = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(
            self.kernel_secret, b"observation-receipt\0" + wire, hashlib.sha256
        ).hexdigest()
        tag = receipt.get("auth_tag")
        if not isinstance(tag, str) or not hmac.compare_digest(tag, expected):
            raise PermissionError("typed observation receipt authentication failed")
        created_at_ns = receipt.get("created_at_ns")
        if isinstance(created_at_ns, bool) or not isinstance(created_at_ns, int) or created_at_ns <= 0:
            raise PermissionError("typed observation receipt creation time is invalid")
        if policy is not None and created_at_ns < policy.activated_at_ns:
            raise PermissionError("observation card predates policy activation")
        return created_at_ns

    def _enabled_policy(self, board: str, provider: str) -> CollectionPolicyV1:
        try:
            policy = self.policies.load()
        except (OSError, TypeError, ValueError) as exc:
            raise PermissionError("conversation policy is invalid") from exc
        now = self.clock_ns()
        if not policy.allows(board, provider, 1, now):
            raise PermissionError("conversation collection policy is disabled")
        return policy

    def _locator(self, provider: str, source_path: Path) -> SourceLocator:
        configured_root = self.provider_roots.get(provider)
        if configured_root is None:
            raise PermissionError("configured provider root is unavailable")
        if not source_path.is_absolute():
            raise ValueError("source path must be absolute")
        try:
            relative = source_path.relative_to(configured_root)
        except ValueError as exc:
            raise PermissionError("source is outside configured provider root") from exc
        if relative == Path("."):
            raise PermissionError("source must be below configured provider root")
        return SourceLocator(provider, configured_root, relative, os.getuid())

    @staticmethod
    def _identity_payload(identity: SourceIdentityV1) -> dict[str, int]:
        return {
            "device": identity.device,
            "inode": identity.inode,
            "size": identity.size,
            "mtime_ns": identity.mtime_ns,
        }

    @staticmethod
    def _scan_sealed_range(
        fd: int,
        *,
        start_offset: int,
        end_offset: int,
        provider: str,
        session: str,
        require_session: bool = True,
    ) -> tuple[int, int | None, int | None]:
        # 명시된 네이티브 순번은 원본 기준을 유지하며 새 기준으로 바꾸지 않는다.
        # 순번이 없는 기존 JSONL은 종전의 로컬 행 번호 기준을 유지한다.
        if (type(start_offset) is not int or type(end_offset) is not int
                or start_offset < 0 or end_offset < start_offset):
            raise ValueError("invalid source scan offsets")
        first_ordinal = next_ordinal = None
        ordinal_mode = None
        if end_offset - start_offset > JSONL_SCAN_BYTES:
            raise PermissionError("sealed source range exceeds scan byte budget")
        position = start_offset
        pending = bytearray()
        records = 0
        blank_records = 0
        session_seen = False
        deadline = time.monotonic() + JSONL_SCAN_DEADLINE_MS / 1_000
        while position < end_offset:
            if time.monotonic() > deadline:
                raise TimeoutError("sealed source scan deadline exceeded")
            chunk = os.pread(fd, min(65_536, end_offset - position), position)
            if not chunk:
                raise OSError("sealed source scan made no progress")
            position += len(chunk)
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    if len(pending) > JSONL_LINE_BYTES:
                        raise PermissionError("sealed source line exceeds byte budget")
                    break
                if newline > JSONL_LINE_BYTES:
                    raise PermissionError("sealed source line exceeds byte budget")
                line = bytes(pending[:newline])
                del pending[: newline + 1]
                records += 1
                if records > JSONL_RECORD_LIMIT:
                    raise PermissionError("sealed source range exceeds record budget")
                if not line.strip():
                    blank_records += 1
                    continue
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PermissionError("sealed source contains malformed JSONL") from exc
                if not isinstance(record, dict):
                    raise PermissionError("sealed source record must be an object")
                if provider == "codex":
                    present = "ordinal" in record
                    if ordinal_mode is not None and ordinal_mode != present:
                        raise PermissionError("mixed source ordinal domains")
                    ordinal_mode = present
                    if present:
                        ordinal = record["ordinal"]
                        if type(ordinal) is not int or ordinal < 0:
                            raise PermissionError("invalid native ordinal")
                        if next_ordinal is not None and ordinal != next_ordinal:
                            raise PermissionError("native ordinal gap or order mismatch")
                        if first_ordinal is None:
                            first_ordinal = ordinal
                        next_ordinal = ordinal + 1
                if provider == "codex" and record.get("type") == "session_meta":
                    payload = record.get("payload")
                    if not isinstance(payload, dict) or payload.get("id") != session:
                        raise PermissionError("Codex source session identity mismatch")
                    session_seen = True
                elif provider == "claude" and "sessionId" in record:
                    if record.get("sessionId") != session:
                        raise PermissionError("Claude source session identity mismatch")
                    session_seen = True
        if pending:
            raise PermissionError("sealed source does not end at a JSONL boundary")
        if ordinal_mode and blank_records:
            raise PermissionError("native ordinal record count mismatch")
        if require_session and not session_seen:
            raise PermissionError("sealed source lacks authoritative session identity")
        return records, first_ordinal, next_ordinal

    @staticmethod
    def _digest_range(fd: int, start_offset: int, end_offset: int) -> str:
        if end_offset - start_offset > JSONL_SCAN_BYTES:
            raise PermissionError("sealed source digest exceeds scan byte budget")
        digest = hashlib.sha256()
        position = start_offset
        deadline = time.monotonic() + JSONL_SCAN_DEADLINE_MS / 1_000
        while position < end_offset:
            if time.monotonic() > deadline:
                raise TimeoutError("sealed source digest deadline exceeded")
            chunk = os.pread(fd, min(65_536, end_offset - position), position)
            if not chunk:
                raise OSError("sealed source digest made no progress")
            digest.update(chunk)
            position += len(chunk)
        return digest.hexdigest()

    def capture_hook_start(
        self,
        *,
        board: str,
        task: str,
        provider: str,
        session: str,
        source_path: Path,
        task_receipt: dict[str, object],
    ) -> dict[str, object]:
        """인증된 새 카드 뒤에만 source 시작 위치를 private hook state로 반환한다."""
        policy = self._enabled_policy(board, provider)
        created_at_ns = self._verify_task_receipt(
            task_receipt,
            board=board,
            task=task,
            policy=policy,
        )
        if not self.task_membership(board, task):
            raise PermissionError("task is not an observation member of this board")
        locator = self._locator(provider, source_path)
        with open_verified_root(locator) as root, open_verified_jsonl_fd(
            locator,
            root_capability=root,
        ) as source:
            start_offset = source.identity.size
            self._scan_sealed_range(
                source.fd,
                start_offset=0,
                end_offset=start_offset,
                provider=provider,
                session=session,
            )
            if SourceIdentityV1.from_stat(os.fstat(source.fd)) != source.identity:
                raise PermissionError("source changed during hook-start identity proof")
            if SourceIdentityV1.from_stat(os.fstat(root.fd)) != root.identity:
                raise PermissionError("provider root changed during hook-start identity proof")
            root_identity = self._identity_payload(root.identity)
            source_identity = self._identity_payload(source.identity)
        return {
            "provider": provider,
            "session": session,
            "allowed_root": str(locator.allowed_root),
            "relative_path": str(locator.relative_path),
            "start_offset": start_offset,
            "root_identity": root_identity,
            "source_identity": source_identity,
            "receipt_created_at_ns": created_at_ns,
            "receipt_nonce": task_receipt["nonce"],
        }

    def seal_hook_binding(
        self,
        *,
        board: str,
        task: str,
        prepared: dict[str, object],
        task_receipt: dict[str, object],
    ) -> TrustedObservationBindingV1 | None:
        """Stop 시점의 안정된 JSONL inode와 line range만 signed binding으로 저장한다."""
        if not isinstance(prepared, dict) or not isinstance(task_receipt, dict):
            raise ValueError("prepared source and task receipt must be objects")
        if prepared.get("receipt_nonce") != task_receipt.get("nonce"):
            raise PermissionError("prepared source belongs to another task receipt")
        provider_value = prepared.get("provider")
        session = prepared.get("session")
        root_text = prepared.get("allowed_root")
        relative_text = prepared.get("relative_path")
        start_offset = prepared.get("start_offset")
        root_identity_value = prepared.get("root_identity")
        source_identity_value = prepared.get("source_identity")
        receipt_created_at_ns = prepared.get("receipt_created_at_ns")
        if (
            not isinstance(provider_value, str)
            or provider_value not in {"claude", "codex"}
            or not isinstance(session, str)
            or not isinstance(root_text, str)
            or not isinstance(relative_text, str)
            or isinstance(start_offset, bool)
            or not isinstance(start_offset, int)
            or start_offset < 0
            or not isinstance(root_identity_value, dict)
            or not isinstance(source_identity_value, dict)
            or isinstance(receipt_created_at_ns, bool)
            or not isinstance(receipt_created_at_ns, int)
        ):
            raise ValueError("prepared source binding is invalid")
        provider = provider_value
        if not self.task_membership(board, task):
            raise PermissionError("task is not an observation member of this board")
        policy = self._enabled_policy(board, provider)
        verified_created_at_ns = self._verify_task_receipt(
            task_receipt,
            board=board,
            task=task,
            policy=policy,
        )
        if receipt_created_at_ns != verified_created_at_ns:
            raise PermissionError("prepared receipt creation time changed")
        locator = self._locator(provider, Path(root_text) / relative_text)
        if locator.allowed_root != Path(root_text):
            raise PermissionError("prepared provider root changed")
        try:
            existing, existing_locator = self.bindings.get(board, task)
        except FileNotFoundError:
            existing = None
        else:
            if (
                existing.provider != provider
                or existing.session != session
                or existing.turn_start.byte_offset != start_offset
                or existing_locator != locator
                or existing.producer_execution != str(task_receipt["nonce"])
            ):
                raise PermissionError("existing binding belongs to another source range")
            return existing
        schema_pin = (
            "anthropics/claude-agent-sdk-python@"
            "a8b1e285f97f8dbcb7b10226d74ba0d551b493f4:sessions.py"
            if provider == "claude"
            else "openai/codex@rust-v0.145.0"
        )
        with open_verified_root(locator) as root, open_verified_jsonl_fd(
            locator, root_capability=root
        ) as source:
            if root.identity != _identity(root_identity_value):
                raise PermissionError("configured provider root identity changed")
            start_identity = _identity(source_identity_value)
            if start_offset != start_identity.size:
                raise PermissionError("prepared offset differs from captured source metadata")
            if (source.identity.device, source.identity.inode) != (
                start_identity.device,
                start_identity.inode,
            ):
                raise PermissionError("source identity changed after hook start")
            end_offset = source.identity.size
            if end_offset <= start_offset:
                return None
            if start_offset and os.pread(source.fd, 1, start_offset - 1) != b"\n":
                raise PermissionError("prepared start is not a JSONL line boundary")
            if os.pread(source.fd, 1, end_offset - 1) != b"\n":
                raise PermissionError("source end is not a sealed JSONL line boundary")
            self._scan_sealed_range(
                source.fd,
                start_offset=0,
                end_offset=end_offset,
                provider=provider,
                session=session,
            )
            line_count, first_ordinal, next_ordinal = self._scan_sealed_range(
                source.fd,
                start_offset=start_offset,
                end_offset=end_offset,
                provider=provider,
                session=session,
                require_session=False,
            )
            source_range_digest = self._digest_range(source.fd, start_offset, end_offset)
            after = os.fstat(source.fd)
            if (
                after.st_dev != source.identity.device
                or after.st_ino != source.identity.inode
                or after.st_size != source.identity.size
                or after.st_mtime_ns != source.identity.mtime_ns
            ):
                raise PermissionError("source identity changed during sealing")
            pending = self.authority.begin_binding(
                board=board,
                task=task,
                provider=provider,
                schema_pin=schema_pin,
                profile_root_identity=root.identity,
                locator=locator,
                source_identity=source.identity,
                session=session,
                turn_start=SourceBoundaryV1(
                    byte_offset=start_offset, ordinal=first_ordinal if first_ordinal is not None else 0
                ),
                binding_version=1,
                generation=1,
                policy_version=policy.version,
                producer_execution=str(task_receipt["nonce"]),
                now_ns=verified_created_at_ns,
            )
            binding = self.authority.seal_binding(
                pending,
                turn_end=SourceBoundaryV1(
                    byte_offset=end_offset, ordinal=next_ordinal if next_ordinal is not None else line_count
                ),
                source_identity=source.identity,
                expected_generation=1,
                now_ns=self.clock_ns(),
                source_range_digest=source_range_digest,
            )
        self.bindings.put(binding=binding, locator=locator)
        return binding

    def get_parent_page(
        self,
        *,
        principal_id: str,
        board: str,
        task: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        board_grants = self.principal_board_grants.get(principal_id, frozenset())
        if board not in board_grants:
            raise PermissionError("explicit board grant required")
        principal = VerifiedInteractivePrincipal.create(
            principal_id=principal_id,
            auth_method="oauth_session",
            board_grants=board_grants,
            authenticated_at_ns=self.clock_ns(),
        )
        if not principal.authorizes(board):
            raise PermissionError("explicit board grant required")
        if not self.task_membership(board, task):
            raise PermissionError("task membership denied before collection access")
        policy = self.policies.load()
        if board not in policy.enabled_boards:
            raise PermissionError("collection policy denies board")
        binding, locator = self.bindings.get(board, task)
        now_ns = self.clock_ns()
        if not policy.allows(board, binding.provider, binding.binding_version, now_ns):
            raise PermissionError("collection policy denies binding")
        policy_store = self.authority.create_policy_store()
        policy_store.replace(policy)
        if binding.provider == "codex":
            projector = CodexProjector()
        elif binding.provider == "claude":
            projector = ClaudeProjector()
            from .claude_absent import SCHEMA_PIN
            if binding.schema_pin == SCHEMA_PIN:
                projector.schema_pin = SCHEMA_PIN
        else:
            raise PermissionError("provider source projection remains disabled")
        with self._lock:
            grant = self._cursor_grants.get(cursor) if cursor is not None else None
            if grant is None:
                principal_receipt = self.authority.issue_principal_scope(
                    principal=principal.principal_id,
                    board=board,
                    task=task,
                    expires_at_ns=min(policy.expires_at_ns, now_ns + 60_000_000_000),
                )
                grant = self.authority.issue_projection_grant(
                    policy_store,
                    binding,
                    principal_receipt,
                    now_ns=now_ns,
                    ttl_ns=min(60_000_000_000, policy.expires_at_ns - now_ns),
                )
            page = project_page(
                locator,
                projector,
                binding=binding,
                grant=grant,
                authority=self.authority,
                policy_store=policy_store,
                cursor=cursor,
                limits=ProjectionLimits(max_events=limit),
                now_ns=now_ns,
            )
            if cursor is not None:
                self._cursor_grants.pop(cursor, None)
            if page.next_cursor is not None:
                self._cursor_grants[page.next_cursor] = grant
        result = page.to_public_dict()
        if binding.provider == "claude":
            # 대화 기록 쓰기는 Stop보다 늦으므로 최종 메시지도 쓰기 완료 증명이 아니다.
            result["completeness"] = "partial"
        return result

    def get_child_links(self, *, principal_id: str, board: str, task: str) -> dict[str, object]:
        if not self.authorizes_principal(principal_id, board):
            raise PermissionError("explicit board grant required")
        if not self.task_membership(board, task):
            raise FileNotFoundError(task)
        return {
            "completeness": "partial",
            "children": [],
            "reason": "child_source_identity_unavailable",
        }