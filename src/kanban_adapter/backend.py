"""모든 observation 어댑터가 사용하는 검증된 Hermes Kanban CLI 경계."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


Runner = Callable[[list[str]], str]
_FD_FILE_RE = re.compile(r"--(?:title|result)-file=/dev/fd/([0-9]+)\Z")
_RECEIPT_FD_RE = re.compile(r"--conversation-receipt-fd=([0-9]+)\Z")


class BoardNotMappedError(RuntimeError):
    """현재 디렉터리를 포함하는 dashboard 보드 프로젝트 디렉터리가 없다."""


def run_command(argv: list[str]) -> str:
    """Hermes 명령을 실행해 stdout을 반환하고, 실패 시 정제된 진단과 함께 예외를 던진다."""
    pass_fds = tuple(sorted({
        int(match.group(1))
        for argument in argv
        if (match := (_FD_FILE_RE.fullmatch(argument) or _RECEIPT_FD_RE.fullmatch(argument))) is not None
    }))
    completed = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
        pass_fds=pass_fds,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {detail}")
    return completed.stdout


_SOURCE = {
    "claude-code": ("claude-code-external", "claude"),
    "codex": ("codex-external", "codex"),
    "manual": (None, "manual"),
}
_BOARD_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_TASK_RE = re.compile(r"t_[A-Za-z0-9]+\Z")
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")


def _git_metadata_line(path: Path) -> str:
    """실행/설정 로딩 없이 작은 Git 포인터 파일만 읽는다."""
    if not path.is_file() or path.is_symlink():
        raise ValueError("not a regular Git metadata file")
    with path.open("rb") as handle:
        raw = handle.read(8193)
    if len(raw) > 8192:
        raise ValueError("oversized Git metadata")
    value = os.fsdecode(raw).removesuffix("\n")
    if not value or "\n" in value or "\x00" in value:
        raise ValueError("invalid Git metadata")
    return value


def _registered_git_root(current: Path) -> tuple[Path, Path] | None:
    """표준 Git 루트와 공통 디렉터리를 양방향 등록으로 검증한다.

    Git 프로세스를 실행하지 않아 PATH/GIT_* 및 include/fsmonitor/hooks 설정을
    신뢰하지 않는다. 별도 git-dir, submodule, 손상된 등록은 보수적으로 거절한다.
    """
    try:
        if not current.is_dir():
            return None
        for root in (current, *current.parents):
            marker = root / ".git"
            if not marker.exists() and not marker.is_symlink():
                continue
            if marker.is_symlink():
                return None
            if marker.is_dir():
                common = marker.resolve(strict=True)
            else:
                pointer = _git_metadata_line(marker)
                if not pointer.startswith("gitdir: "):
                    return None
                gitdir = (root / pointer[8:]).resolve(strict=True)
                common = (gitdir / _git_metadata_line(gitdir / "commondir")).resolve(strict=True)
                # common/worktrees/<id> 등록과 worktree .git의 역참조가 모두 필요하다.
                if gitdir.parent != common / "worktrees":
                    return None
                backlink = _git_metadata_line(gitdir / "gitdir")
                if not Path(backlink).is_absolute() or Path(backlink).resolve(strict=True) != marker:
                    return None
                if not (gitdir / "HEAD").is_file():
                    return None
            if not ((common / "HEAD").is_file() and (common / "objects").is_dir()
                    and (common / "refs").is_dir()):
                return None
            return root, common
    except (OSError, ValueError, RuntimeError):
        return None
    return None


@dataclass
class HermesCliBackend:
    """Hermes CLI를 통해 검증된 observation 카드를 생성하고 변경한다."""

    runner: Runner = run_command

    def resolve_board(self, *, cwd: Path) -> str:
        """최장 명시 매핑 우선, 없으면 검증된 동일 Git 저장소의 유일한 보드를 선택한다."""
        raw = self.runner(["hermes", "kanban", "boards", "list", "--json"])
        try:
            boards = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Hermes board list returned non-JSON output") from exc
        if not isinstance(boards, list):
            raise RuntimeError("Hermes board list was not a JSON array")

        current = cwd.resolve()
        matches: list[tuple[int, str]] = []
        mappings: list[tuple[Path, str]] = []
        for board in boards:
            if not isinstance(board, dict):
                raise RuntimeError("Hermes board list contained a non-object entry")
            if "default_workdir" not in board:
                raise RuntimeError(
                    "Hermes board list entry was missing default_workdir"
                )
            slug = board.get("slug")
            workdir = board["default_workdir"]
            if not isinstance(slug, str) or not _BOARD_RE.fullmatch(slug):
                raise RuntimeError("Hermes board list contained an invalid slug")
            if workdir is None:
                continue
            if not isinstance(workdir, str) or not workdir:
                raise RuntimeError("Hermes board list contained an invalid project directory")
            requested = Path(workdir).expanduser()
            if not requested.is_absolute():
                raise RuntimeError(
                    "Hermes board list contained a non-absolute project directory"
                )
            root = requested.resolve()
            mappings.append((root, slug))
            if current == root or root in current.parents:
                matches.append((len(root.parts), slug))

        if not matches:
            # 명시적 최장 경로가 없을 때만 동일 저장소 등록을 비교한다.
            # 하위 폴더 매핑을 저장소 전체로 확장하거나 없는 루트를 사용하지 않는다.
            identity = _registered_git_root(current)
            if identity is not None:
                candidates = []
                for root, slug in mappings:
                    mapped = _registered_git_root(root)
                    if mapped is not None and mapped[0] == root and mapped[1] == identity[1]:
                        candidates.append(slug)
                if len(candidates) > 1:
                    raise RuntimeError(
                        "multiple Kanban boards map to this Git repository: "
                        + ", ".join(sorted(candidates))
                    )
                if candidates:
                    return candidates[0]
            raise BoardNotMappedError(
                "no Kanban board is mapped to this directory; set Project directory "
                "when creating the board in Hermes Dashboard"
            )
        matches.sort(reverse=True)
        longest = matches[0][0]
        winners = sorted(slug for depth, slug in matches if depth == longest)
        if len(winners) > 1:
            raise RuntimeError(
                "multiple Kanban boards map to this directory: " + ", ".join(winners)
            )
        return winners[0]

    def start(
        self,
        *,
        board: str,
        source: str,
        title: str | None = None,
        title_file: Path | None = None,
        idempotency_key: str | None = None,
        receipt_fd: int | None = None,
    ) -> str:
        """running 상태의 observation 카드를 생성하고 검증된 task id를 반환한다."""
        if source not in _SOURCE:
            raise ValueError(f"unsupported source: {source}")
        if (title is None) == (title_file is None):
            raise ValueError("title and title_file are mutually exclusive")
        owned_title_file: Path | None = None
        if title_file is None:
            fd, name = tempfile.mkstemp(prefix="unified-kanban-title-")
            owned_title_file = Path(name)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(title or "")
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                owned_title_file.unlink(missing_ok=True)
                raise
            title_file = owned_title_file
        assignee, tenant = _SOURCE[source]
        argv = ["hermes", "kanban", "--board", board, "create"]
        if idempotency_key is not None:
            if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
                raise ValueError("invalid idempotency key")
            argv.append(f"--idempotency-key={idempotency_key}")
        if receipt_fd is not None:
            if receipt_fd < 3:
                raise ValueError("conversation receipt FD must not alias stdio")
            argv.append(f"--conversation-receipt-fd={receipt_fd}")
        if assignee:
            argv.extend(["--assignee", assignee])
        argv.extend([
            "--tenant", tenant,
            "--created-by", "kanban-adapter",
            "--observation",
            "--json",
            f"--title-file={title_file}",
        ])
        try:
            raw = self.runner(argv)
        finally:
            if owned_title_file is not None:
                owned_title_file.unlink(missing_ok=True)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            preview = raw.strip().replace("\n", " ")[:200]
            raise RuntimeError(f"Hermes returned non-JSON output: {preview}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Hermes create response was not a JSON object")
        task_id = payload.get("id")
        if not isinstance(task_id, str) or not _TASK_RE.fullmatch(task_id):
            raise RuntimeError("Hermes create response did not contain a valid task id")
        if payload.get("status") != "running" or payload.get("observation") is not True:
            # 멱등(idempotent) create는 기존에 있던 행을 반환할 수 있다. 생성
            # 토큰이 없으면 이 호출이 반환된 task를 소유한다고 증명할 수 없으므로,
            # ``complete``로 보상(compensate)하면 무관한 작업을 종료시킬 수 있다.
            raise RuntimeError(
                "Hermes create did not return a running observation card"
            )
        return task_id

    def update(
        self,
        *,
        board: str,
        task_id: str,
        message: str,
        idempotency_key: str | None = None,
    ) -> None:
        """댓글을 추가하되, ``idempotency_key``당 원자적으로 최대 한 번만 추가한다."""
        argv = [
            "hermes", "kanban", "--board", board, "comment",
            "--author", "kanban-adapter",
        ]
        if idempotency_key is not None:
            if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
                raise ValueError("invalid idempotency key")
            argv.append(f"--idempotency-key={idempotency_key}")
        argv.extend(["--", task_id, message])
        self.runner(argv)

    def publish_codex_usage(self, *, board: str, task_id: str, messages: list[str]) -> None:
        """확정된 요청 배치를 게시하기 전에 기존 완료 키를 예약한다.

        훅의 로컬 완료 상태가 아니라 원격 댓글을 권위 있는 기준으로 삼는다.
        모든 재시도는 먼저 확정된 바이트를 읽는다. 구형 완료 기록기도 같은 키를
        사용하므로 요청 모드로 예약된 뒤에는 합계를 삽입할 수 없다.
        """
        from .usage import usage_event_id
        slot = usage_event_id("codex", task_id)
        marker = "Codex request batch v1\n"

        def validate(batch):
            if not isinstance(batch, list) or len(batch) > 128:
                raise ValueError("invalid Codex usage batch")
            if len(json.dumps(batch).encode()) > 48000:
                raise ValueError("Codex usage batch exceeds bound")
            events = []
            for body in batch:
                if not isinstance(body, str) or not body.startswith("Codex tool usage\n"):
                    raise ValueError("invalid Codex usage body")
                event = json.loads(body.partition("\n")[2])
                request = event.get("request_hash")
                version = event.get('schema_version')
                if (event.get("source") != "codex" or version not in (1, 2)
                        or (request is not None and version != 2)
                        or event.get("event_id") != usage_event_id("codex", task_id, request)
                        or (request is not None and (not isinstance(request, str)
                            or not re.fullmatch(r"[0-9a-f]{16}", request)))
                        or (version == 2 and event.get("usage_timing") != ("request" if request else "completion"))):
                    raise ValueError("invalid Codex usage identity")
                events.append(event)
            if len({e['event_id'] for e in events}) != len(events):
                raise ValueError("duplicate Codex usage identity")
            if any(e.get('request_hash') is None for e in events) and len(events) != 1:
                raise ValueError("mixed Codex usage modes")
            return events

        def read():
            payload = json.loads(self.runner([
                "hermes", "kanban", "--board", board, "show", "--json", "--", task_id,
            ]))
            if payload.get("task", {}).get("id") != task_id or not isinstance(payload.get("comments"), list):
                raise RuntimeError("Codex usage readback scope mismatch")
            return [c['body'] for c in payload['comments']
                    if c.get('author') == 'kanban-adapter' and isinstance(c.get('body'), str)]

        bodies = read()
        # 키 도입 전의 기존 댓글도 영구히 기존 방식으로 유지하며 소급 보완하지 않는다.
        legacy = [b for b in bodies if b.startswith("Codex tool usage\n")
                  and json.loads(b.partition('\n')[2]).get('event_id') == slot]
        frozen = [b for b in bodies if b.startswith(marker)]
        if not frozen and any(b.startswith('Codex tool usage\n') and b not in legacy for b in bodies):
            raise RuntimeError('unreserved historical Codex usage')
        if legacy:
            if frozen:
                raise RuntimeError("conflicting Codex publication modes")
            return
        if not frozen:
            # 재시도는 새로 계산한 후보가 아니라 원격에 확정된 상태에서 재개한다.
            events = validate(messages)
            if not messages:
                # Stop 이후에도 네이티브 증거가 추가될 수 있다. 사용량 자료가 없는데도
                # 완료 처리하지 말고 연관된 재시도를 위해 수명 주기의 권위 있는 상태를 유지한다.
                raise RuntimeError("Codex usage evidence not ready")
            candidate = (marker + json.dumps(messages, separators=(',', ':'))
                         if events[0].get('request_hash') else messages[0])
            self.update(board=board, task_id=task_id, message=candidate, idempotency_key=slot)
            bodies = read()
            frozen = [b for b in bodies if b.startswith(marker)]
            legacy = [b for b in bodies if b.startswith("Codex tool usage\n")
                      and json.loads(b.partition('\n')[2]).get('event_id') == slot]
            if legacy and not frozen:
                return
        if len(frozen) != 1 or legacy:
            raise RuntimeError("Codex usage reservation readback failed")
        batch = json.loads(frozen[0][len(marker):])
        frozen_events = validate(batch)
        if not frozen_events or any(not e.get('request_hash') for e in frozen_events):
            raise RuntimeError("invalid frozen request mode")
        for body, event in zip(batch, frozen_events):
            self.update(board=board, task_id=task_id, message=body, idempotency_key=event['event_id'])
        observed = read()
        if any(body not in observed for body in batch):
            raise RuntimeError("Codex usage publication readback failed")

    def done(
        self,
        *,
        board: str,
        task_id: str,
        result: str | None = None,
        result_file: Path | None = None,
        summary: str | None = None,
    ) -> None:
        """전체 result와 간결한 summary를 분리해 유지하면서 카드를 완료한다."""
        argv = ["hermes", "kanban", "--board", board, "complete"]
        if result is not None and result_file is not None:
            raise ValueError("result and result_file are mutually exclusive")
        if result is None:
            if result_file is not None:
                argv.append(f"--result-file={result_file}")
            else:
                result = summary
        if result is not None:
            argv.append(f"--result={result}")
        if summary:
            # 간결한 handoff를 어시스턴트의 전체 result와 분리해 유지한다.
            # `--opt=<text>` 형식은 하이픈으로 시작하는 사용자 데이터를 안전하게 바인딩한다.
            argv.append(f"--summary={summary}")
        argv.extend(["--", task_id])
        self.runner(argv)

    def block(self, *, board: str, task_id: str, reason: str) -> None:
        """명시적인 사용자 입력이 필요할 때 observation 카드를 block 상태로 만든다."""
        self.runner([
            "hermes", "kanban", "--board", board, "block",
            "--kind", "needs_input", "--", task_id, reason,
        ])
