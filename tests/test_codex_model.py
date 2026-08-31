from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from kanban_adapter import codex_model
from kanban_adapter.codex_hook import enrich_payload
from kanban_adapter.codex_model import (
    default_state_db,
    resolve_model,
    resolve_rollout_path,
)


def make_state_db(path: Path, rows: list[tuple[str, str | None]]) -> Path:
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "CREATE TABLE threads ("
            "  id TEXT PRIMARY KEY,"
            "  cwd TEXT,"
            "  first_user_message TEXT,"
            "  model TEXT"
            ")"
        )
        connection.executemany(
            "INSERT INTO threads (id, cwd, first_user_message, model)"
            " VALUES (?, ?, ?, ?)",
            [(tid, "/secret/cwd", "secret prompt body", model) for tid, model in rows],
        )
    connection.close()
    return path


def make_config(path: Path, body: str) -> Path:
    """무시된다는 사실만 입증하기 위해 데이터베이스 옆에 둔 ``config.toml``."""
    path.write_text(body, encoding="utf-8")
    return path


def test_session_model_comes_from_the_exact_session_row(tmp_path: Path) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])
    make_config(tmp_path / "config.toml", 'model = "gpt-5.6-terra"\n')

    assert resolve_model("sess-a", state_db=db) == "gpt-5.6-sol"


def test_missing_session_row_resolves_to_none_not_a_machine_default(
    tmp_path: Path,
) -> None:
    """설정 기본값은 이 세션의 모델이 아니므로 절대 사용하지 않는다."""
    db = make_state_db(tmp_path / "state_5.sqlite", [("other", "gpt-5.6-sol")])
    make_config(tmp_path / "config.toml", 'model = "gpt-5.6-terra"\n')

    assert resolve_model("sess-a", state_db=db) is None


def test_null_session_model_resolves_to_none(tmp_path: Path) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", None)])
    make_config(tmp_path / "config.toml", 'model = "gpt-5.6-terra"\n')

    assert resolve_model("sess-a", state_db=db) is None


def test_config_toml_is_never_opened(tmp_path: Path, monkeypatch) -> None:
    """API든 뜻하지 않은 읽기든 어떤 TOML 대체 경로도 남지 않는다."""
    db = make_state_db(tmp_path / "state_5.sqlite", [("other", "gpt-5.6-sol")])
    config = make_config(
        tmp_path / "config.toml",
        'model = "gpt-5.6-terra"\nsecret_token = "sk-do-not-read"\n',
    )

    opened: list[str] = []
    real_open = open

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy_open)
    assert resolve_model("sess-a", state_db=db) is None
    assert str(config) not in opened

    monkeypatch.undo()
    assert not hasattr(codex_model, "default_config_path")
    assert not hasattr(codex_model, "_model_from_config")
    with pytest.raises(TypeError):
        resolve_model("sess-a", state_db=db, config_path=config)


def spy_on_queries(monkeypatch) -> list[tuple]:
    """실제 sqlite3에 위임하면서 codex_model이 실행하는 모든 문을 기록한다."""
    seen: list[tuple] = []
    real_connect = sqlite3.connect

    class SpyConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, parameters=(), *args, **kwargs):
            seen.append((sql, parameters))
            return self._inner.execute(sql, parameters, *args, **kwargs)

        def close(self):
            self._inner.close()

    monkeypatch.setattr(
        codex_model.sqlite3,
        "connect",
        lambda *a, **kw: SpyConnection(real_connect(*a, **kw)),
    )
    return seen


def test_resolution_reads_only_the_model_column(tmp_path: Path, monkeypatch) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])
    seen = spy_on_queries(monkeypatch)

    assert resolve_model("sess-a", state_db=db) == "gpt-5.6-sol"

    assert seen
    for sql, _params in seen:
        assert "cwd" not in sql
        assert "first_user_message" not in sql
        assert "*" not in sql
        assert sql.startswith("SELECT model FROM threads")


def test_the_database_is_opened_read_only(tmp_path: Path, monkeypatch) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])
    opens: list[tuple] = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        opens.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(codex_model.sqlite3, "connect", recording_connect)
    assert resolve_model("sess-a", state_db=db) == "gpt-5.6-sol"

    (args, kwargs) = opens[-1]
    assert kwargs["uri"] is True
    assert args[0].startswith("file:")
    assert args[0].endswith("?mode=ro")


def test_a_path_with_uri_metacharacters_cannot_redirect_the_open(
    tmp_path: Path,
) -> None:
    """디렉터리 이름의 ``?``를 URI 쿼리로 해석해서는 안 된다."""
    home = tmp_path / "co?dex#home"
    home.mkdir()
    db = make_state_db(home / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])

    assert resolve_model("sess-a", state_db=db) == "gpt-5.6-sol"


def test_session_id_is_passed_as_a_bound_parameter(tmp_path: Path, monkeypatch) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])
    seen = spy_on_queries(monkeypatch)

    hostile = "x'; DROP TABLE threads; --"
    assert resolve_model(hostile, state_db=db) is None

    sql, parameters = seen[-1]
    assert "?" in sql
    assert hostile not in sql
    assert tuple(parameters) == (hostile,)
    # 테이블이 그대로이므로 문에는 어떤 값도 보간되지 않았다.
    monkeypatch.undo()
    connection = sqlite3.connect(db)
    assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
    connection.close()


def test_malformed_sources_fail_open(tmp_path: Path) -> None:
    missing_db = tmp_path / "nope.sqlite"
    bad_db = tmp_path / "bad.sqlite"
    bad_db.write_text("not a database", encoding="utf-8")

    assert resolve_model("s", state_db=missing_db) is None
    assert resolve_model("s", state_db=bad_db) is None
    assert resolve_model("s", state_db=None) is None
    assert resolve_model("", state_db=bad_db) is None


def test_symlinked_state_db_is_refused(tmp_path: Path) -> None:
    real = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])
    link = tmp_path / "link.sqlite"
    link.symlink_to(real)
    assert resolve_model("sess-a", state_db=link) is None


def test_non_regular_state_db_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "state_5.sqlite"
    directory.mkdir()
    fifo = tmp_path / "fifo.sqlite"
    os.mkfifo(fifo)

    assert resolve_model("sess-a", state_db=directory) is None
    assert resolve_model("sess-a", state_db=fifo) is None


def swap_on_connect(monkeypatch, replace: "callable") -> None:
    """열기와 쿼리 사이에 ``replace``가 경로를 바꾸게 한다."""
    real_connect = sqlite3.connect

    def swapping_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        replace()
        return connection

    monkeypatch.setattr(codex_model.sqlite3, "connect", swapping_connect)


def test_state_db_replaced_after_the_open_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """TOCTOU 구간: 해당 경로의 다른 파일에는 절대 쿼리하지 않는다."""
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])
    impostor = make_state_db(tmp_path / "impostor.sqlite", [("sess-a", "planted")])
    swap_on_connect(monkeypatch, lambda: os.replace(impostor, db))

    assert resolve_model("sess-a", state_db=db) is None


def test_state_db_swapped_for_a_symlink_after_the_open_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])
    elsewhere = make_state_db(tmp_path / "elsewhere.sqlite", [("sess-a", "planted")])

    def swap() -> None:
        db.unlink()
        db.symlink_to(elsewhere)

    swap_on_connect(monkeypatch, swap)
    assert resolve_model("sess-a", state_db=db) is None


def test_state_db_mode_change_after_the_open_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])
    db.chmod(0o600)
    swap_on_connect(monkeypatch, lambda: db.chmod(0o666))

    assert resolve_model("sess-a", state_db=db) is None


def test_state_db_removed_after_the_open_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])
    swap_on_connect(monkeypatch, db.unlink)

    assert resolve_model("sess-a", state_db=db) is None


def test_an_unchanged_database_still_resolves_under_the_guard(
    tmp_path: Path, monkeypatch
) -> None:
    """보호 장치는 자신이 감싸는 일반적인 경우를 거부해서는 안 된다."""
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-sol")])
    swap_on_connect(monkeypatch, lambda: None)

    assert resolve_model("sess-a", state_db=db) == "gpt-5.6-sol"


def test_junk_model_values_are_rejected(tmp_path: Path) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "a model with spaces")])
    assert resolve_model("sess-a", state_db=db) is None


def test_default_state_db_prefers_the_highest_schema_version(tmp_path: Path) -> None:
    for name in ("state_2.sqlite", "state_10.sqlite", "state_5.sqlite", "other.sqlite"):
        (tmp_path / name).touch()
    assert default_state_db(tmp_path) == tmp_path / "state_10.sqlite"
    assert default_state_db(tmp_path / "missing") is None


def test_enrich_payload_prefers_the_explicit_payload_model(tmp_path: Path) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-terra")])

    enriched = enrich_payload(
        {"session_id": "sess-a", "model": "gpt-5.6-sol", "prompt": "hi"},
        state_db=db,
    )
    assert enriched["model"] == "gpt-5.6-sol"


def test_enrich_payload_fills_in_a_missing_model(tmp_path: Path) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-terra")])

    enriched = enrich_payload({"session_id": "sess-a", "prompt": "hi"}, state_db=db)
    assert enriched["model"] == "gpt-5.6-terra"
    assert enriched["prompt"] == "hi"


def test_enrich_payload_leaves_an_unresolvable_model_unset(tmp_path: Path) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("other", "gpt-5.6-terra")])
    make_config(tmp_path / "config.toml", 'model = "gpt-5.6-mini"\n')

    enriched = enrich_payload({"session_id": "sess-a", "prompt": "hi"}, state_db=db)
    assert enriched["model"] is None


def test_enrich_payload_without_a_session_id_stays_model_free(tmp_path: Path) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-terra")])
    enriched = enrich_payload({"prompt": "hi"}, state_db=db)
    assert enriched["model"] is None


def test_enrich_payload_no_longer_accepts_a_config_path(tmp_path: Path) -> None:
    db = make_state_db(tmp_path / "state_5.sqlite", [("sess-a", "gpt-5.6-terra")])
    with pytest.raises(TypeError):
        enrich_payload(
            {"session_id": "sess-a"}, state_db=db, config_path=tmp_path / "config.toml"
        )


def test_resolve_rollout_path_reads_only_the_matching_thread(tmp_path: Path) -> None:
    db = tmp_path / "state_5.sqlite"
    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT, model TEXT, first_user_message TEXT)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?)",
            ("session-1", str(tmp_path / "rollout-session-1.jsonl"), "gpt-5.6-sol", "secret"),
        )
    connection.close()

    assert resolve_rollout_path("session-1", state_db=db) == str(
        tmp_path / "rollout-session-1.jsonl"
    )
    assert resolve_rollout_path("missing", state_db=db) is None


def test_enrich_payload_falls_back_to_codex_state_rollout_path(tmp_path: Path) -> None:
    db = tmp_path / "state_5.sqlite"
    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, model TEXT)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?)",
            ("session-1", str(tmp_path / "rollout.jsonl"), "gpt-5.6-sol"),
        )
    connection.close()

    enriched = enrich_payload({"session_id": "session-1", "prompt": "hi"}, state_db=db)
    assert enriched["transcript_path"] == str(tmp_path / "rollout.jsonl")
