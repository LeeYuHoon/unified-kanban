from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SMOKE = REPO / "scripts/kanban-smoke.sh"


def make_fakes(
    tmp_path: Path,
    *,
    boards: list[str],
    fail_archive: bool = False,
    fail_update: bool = False,
) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "hermes.log"
    state = tmp_path / "state"

    adapter = fake_bin / "adapter"
    adapter.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == start ]]; then echo t_fake1234; fi\n"
        "if [[ \"$1\" == update && \"$FAIL_UPDATE\" == 1 ]]; then exit 9; fi\n",
        encoding="utf-8",
    )
    adapter.chmod(0o755)

    hermes = fake_bin / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
        "if [[ \"$*\" == 'kanban boards list --json' ]]; then printf '%s\\n' \"$FAKE_BOARDS\"; exit 0; fi\n"
        "if [[ \"$*\" == kanban\\ boards\\ create* ]]; then exit 0; fi\n"
        "if [[ \"$*\" == kanban\\ --board*archive* ]]; then\n"
        "  if [[ \"$FAIL_ARCHIVE\" == 1 ]]; then exit 7; fi\n"
        "  touch \"$FAKE_STATE\"; exit 0\n"
        "fi\n"
        "if [[ \"$*\" == kanban\\ --board*show* ]]; then\n"
        "  if [[ -f \"$FAKE_STATE\" ]]; then echo '  status:    archived';\n"
        "  else printf '%s\\n' '  status:    done' 'smoke-update-ok' 'smoke-done-ok'; fi\n"
        "fi\n",
        encoding="utf-8",
    )
    hermes.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "KANBAN_ADAPTER": str(adapter),
        "KANBAN_SMOKE_BOARD": "exact-smoke",
        "FAKE_LOG": str(log),
        "FAKE_STATE": str(state),
        "FAKE_BOARDS": json.dumps([{"slug": slug} for slug in boards]),
        "FAIL_ARCHIVE": "1" if fail_archive else "0",
        "FAIL_UPDATE": "1" if fail_update else "0",
    }


def run_smoke(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SMOKE)], cwd=REPO, env=env,
        text=True, capture_output=True, check=False,
    )


def test_smoke_uses_existing_exact_board_slug(tmp_path: Path) -> None:
    env = make_fakes(tmp_path, boards=["prefix-exact-smoke-suffix", "exact-smoke"])
    result = run_smoke(env)
    assert result.returncode == 0, result.stderr
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    assert "kanban boards create" not in log
    assert "SMOKE PASS" in result.stdout


def test_smoke_does_not_create_missing_board(tmp_path: Path) -> None:
    env = make_fakes(tmp_path, boards=["prefix-exact-smoke-suffix"])

    result = run_smoke(env)

    assert result.returncode != 0
    assert "board does not exist: exact-smoke" in result.stderr
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    assert "kanban boards create" not in log


def test_smoke_fails_closed_on_invalid_board_json_schema(tmp_path: Path) -> None:
    env = make_fakes(tmp_path, boards=[])
    env["FAKE_BOARDS"] = json.dumps({"slug": "exact-smoke"})

    result = run_smoke(env)

    assert result.returncode != 0
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    assert "kanban boards create" not in log
    assert "SMOKE PASS" not in result.stdout


def test_smoke_fails_when_archive_cleanup_fails(tmp_path: Path) -> None:
    env = make_fakes(tmp_path, boards=["exact-smoke"], fail_archive=True)
    result = run_smoke(env)
    assert result.returncode != 0
    assert "SMOKE PASS" not in result.stdout


def test_smoke_archives_created_card_after_update_failure(tmp_path: Path) -> None:
    env = make_fakes(tmp_path, boards=["exact-smoke"], fail_update=True)

    result = run_smoke(env)

    assert result.returncode != 0
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    assert "kanban --board exact-smoke archive t_fake1234" in log
    assert "SMOKE PASS" not in result.stdout


def test_spec_documents_options_before_board_slug() -> None:
    spec = (REPO / "docs/unified-kanban-spec.md").read_text(encoding="utf-8")

    assert 'boards create --name "Project A" --icon 🚀 project-a' in spec
    assert 'boards create --name "Project B" --icon 🔧 project-b' in spec
    assert "boards create project-a --name" not in spec
    assert "boards create project-b --name" not in spec
