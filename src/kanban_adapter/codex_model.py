"""``model``이 누락된 hook 이벤트를 위한 Codex 모델 결정.

Codex 0.145의 hook 입력 스키마는 UserPromptSubmit, PostToolUse,
SubagentStart, Stop에서 ``model``을 요구하지만, SessionEnd는 이를 담지
않으며 더 오래되거나 미래의 빌드는 이를 생략할 수 있다. 복구되는 유일한
값은 Codex의 state 데이터베이스에 *바로 이 세션*에 대해 기록된 모델이다.

의도적으로 두 번째 출처는 없다. ``config.toml``의 최상위 ``model`` 같은
머신 전역 기본값은 이 세션의 모델이 아니다; 이를 보고하면 그럴듯하지만
검증되지 않은 식별자가 카드에 기록되는데, 이는 정직한 공백보다 나쁘다.
세션 행을 읽을 수 없으면 모델은 그냥 기록되지 않은 채로 남는다.

읽는 것은 오직 모델 식별자뿐이다: SQL 프로젝션이 단일 ``model`` 컬럼만
지정하므로 prompt, cwd, title이 실수로 반환될 수 없다. 데이터베이스는
읽기 전용으로 열리며, 그 경로는 연결을 열기 전과 후 모두 심볼릭 링크가
아닌 일반 파일인지 검사되므로, 호출 도중 바꿔치기된 파일은 읽히는 대신
거부된다.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote

from .usage import sanitize_model

_STATE_DB_RE = re.compile(r"state_(\d+)\.sqlite\Z")
_QUERY_TIMEOUT_SECONDS = 2.0


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def default_state_db(codex_home: Path | None = None) -> Path | None:
    """버전이 가장 높은 최신 Codex state 데이터베이스, 없으면 None."""
    home = codex_home or default_codex_home()
    best: tuple[int, Path] | None = None
    try:
        entries = list(home.iterdir())
    except OSError:
        return None
    for entry in entries:
        match = _STATE_DB_RE.fullmatch(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        if best is None or version > best[0]:
            best = (version, entry)
    return None if best is None else best[1]


def _regular_file_stat(path: Path | None) -> os.stat_result | None:
    """순수 일반 파일의 ``lstat`` 결과, 그 외에는 None.

    ``stat`` 대신 ``lstat``을 쓰는 이유는 심볼릭 링크가 가리키는 대상으로
    따라가는 대신 심볼릭 링크를 심볼릭 링크로 보고 거부하기 위해서다.
    """
    if path is None:
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return info


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    """두 stat이 권한 변경 없이 동일한 파일을 나타내는지 여부."""
    return (before.st_dev, before.st_ino, before.st_mode) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
    )


def _read_only_uri(path: Path) -> str:
    """``path``에 대한 SQLite 읽기 전용 URI.

    경로를 퍼센트 인코딩하는 이유는 SQLite가 URI의 ``?``와 ``#``을 쿼리와
    프래그먼트 구분자로 파싱하기 때문이다; 둘 중 하나를 포함한 Codex home은
    그렇지 않으면 조용히 다른 데이터베이스를 열게 된다.
    """
    return f"file:{quote(str(path))}?mode=ro"


def _model_from_state_db(session_id: str, state_db: Path | None) -> str | None:
    if not session_id or state_db is None:
        return None
    before = _regular_file_stat(state_db)
    if before is None:
        return None
    connection = None
    try:
        connection = sqlite3.connect(
            _read_only_uri(state_db),
            uri=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
        # 첫 lstat과 open 사이에 경로가 심볼릭 링크, FIFO, 또는 다른
        # 사용자의 데이터베이스로 교체되었을 수 있다.
        # 어떤 문(statement)이든 실행하기 전에 다시 검사하고 차이가 있으면
        # fail closed 하여, 최악의 경우에도 모델이 기록되지 않는 데 그친다.
        after = _regular_file_stat(state_db)
        if after is None or not _same_file(before, after):
            return None
        row = connection.execute(
            "SELECT model FROM threads WHERE id = ? LIMIT 1", (session_id,)
        ).fetchone()
    except Exception:
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    if not row:
        return None
    return sanitize_model(row[0])


def resolve_model(session_id: str, *, state_db: Path | None) -> str | None:
    """이 세션의 Codex 모델, 정확히 읽을 수 없으면 None."""
    return _model_from_state_db(session_id, state_db)


def resolve_rollout_path(session_id: str, *, state_db: Path | None) -> str | None:
    """Codex state에서 이 세션의 rollout 경로를 읽기 전용·fail-closed로 반환한다."""
    if not session_id or state_db is None:
        return None
    before = _regular_file_stat(state_db)
    if before is None:
        return None
    connection = None
    try:
        connection = sqlite3.connect(
            _read_only_uri(state_db),
            uri=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
        after = _regular_file_stat(state_db)
        if after is None or not _same_file(before, after):
            return None
        row = connection.execute(
            "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1", (session_id,)
        ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
    if not row or not isinstance(row[0], str) or not row[0] or "\x00" in row[0]:
        return None
    path = Path(row[0])
    if not path.is_absolute() or len(row[0]) > 4096:
        return None
    return row[0]
