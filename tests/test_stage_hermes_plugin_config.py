from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/stage-hermes-plugin-config.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("stage_hermes_plugin_config", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_remains_bound_after_transaction_root_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    transaction = tmp_path / "transaction"
    moved = tmp_path / "moved-transaction"
    transaction.mkdir(mode=0o700)
    baseline = transaction / "baseline"
    output = transaction / "candidate"
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    hermes_cli = tmp_path / "reviewed-release/venv/bin/hermes"
    baseline.write_text("plugins: []\n", encoding="utf-8")
    baseline.chmod(0o600)
    root_fd = os.open(transaction, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    transaction.rename(moved)
    transaction.mkdir(mode=0o700)
    (transaction / "foreign").write_text("preserve", encoding="utf-8")
    monkeypatch.setenv("UNIFIED_KANBAN_TRANSACTION_DIR", str(transaction))

    def fake_hermes(command, *, env, check, close_fds, cwd):
        assert command == [
            str(hermes_cli),
            "plugins",
            "enable",
            "--no-allow-tool-override",
            "hermes-kanban",
        ]
        assert env["HERMES_HOME"] == "."
        assert "UNIFIED_KANBAN_TRANSACTION_DIR" not in env
        assert "UNIFIED_KANBAN_TRANSACTION_FD" not in env
        assert "UNIFIED_KANBAN_TOKEN_LEDGER_FD" not in env
        assert check is True
        assert close_fds is True
        assert transaction not in Path(cwd).parents
        assert moved not in Path(cwd).parents
        (Path(cwd) / "config.yaml").write_text(
            "plugins: [hermes-kanban]\n", encoding="utf-8"
        )

    monkeypatch.setattr(helper.subprocess, "run", fake_hermes)
    monkeypatch.setenv("UNIFIED_KANBAN_TRANSACTION_FD", str(root_fd))
    monkeypatch.setenv("UNIFIED_KANBAN_TOKEN_LEDGER_FD", "10")
    try:
        helper.stage(root_fd, baseline, output, plugin, "enable", hermes_cli)
    finally:
        os.close(root_fd)

    assert (moved / "candidate").read_text(encoding="utf-8") == "plugins: [hermes-kanban]\n"
    assert stat.S_IMODE((moved / "candidate").stat().st_mode) == 0o600
    assert not (transaction / "candidate").exists()
    assert (transaction / "foreign").read_text(encoding="utf-8") == "preserve"
