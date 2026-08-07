from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "scripts" / "setup.sh"
UNINSTALL = REPO / "scripts" / "uninstall.sh"
SUPPORTED_SHA = (REPO / "patches/hermes-agent-supported-upstream").read_text(
    encoding="utf-8"
).strip()
CARRIED_HEAD = [
    line for line in (REPO / "patches/hermes-agent-carried-commits").read_text(
        encoding="utf-8"
    ).splitlines()
    if line and not line.startswith("#")
][-1]


def install_fake_hermes(
    root: Path,
    *,
    boards: list[str] | None = None,
    compatible: bool = True,
    missing_option: str | None = None,
    option_replacements: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    log = root / "hermes.log"
    script = fake_bin / "hermes"
    def options(text: str) -> str:
        if not compatible:
            return ""
        if missing_option:
            text = text.replace(missing_option, "")
        for old, new in (option_replacements or {}).items():
            text = text.replace(old, new)
        return text

    compatibility_cases = (
        f"  'kanban --help') printf '%s\\n' '{options('--board')}';;\n"
        f"  'kanban create --help') printf '%s\\n' "
        f"'{options('--assignee --tenant --created-by --initial-status --observation --json')}';;\n"
        f"  'kanban boards list --help') printf '%s\\n' '{options('--json')}';;\n"
        f"  'kanban boards create --help') printf '%s\\n' '{options('--name')}';;\n"
        f"  'kanban comment --help') printf '%s\\n' '{options('--author')}';;\n"
        f"  'kanban complete --help') printf '%s\\n' '{options('--summary')}';;\n"
        f"  'kanban block --help') printf '%s\\n' '{options('--kind')}';;\n"
        "  'kanban show --help') echo 'task_id';;\n"
        "  'kanban archive --help') echo 'task_ids';;\n"
    )
    script.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_HERMES_LOG\"\n"
        "case \"$*\" in\n"
        f"  version) echo \"Hermes Agent v0.18.2 upstream ${{FAKE_HERMES_UPSTREAM:-{SUPPORTED_SHA[:8]}}}\";;\n"
        + compatibility_cases +
        "  'kanban boards list --json')\n"
        "    if [[ -n \"${FAKE_SWAP_ENVRC:-}\" ]]; then\n"
        "      rm -f -- \"$FAKE_SWAP_ENVRC\"\n"
        "      ln -s -- \"$FAKE_SWAP_TARGET\" \"$FAKE_SWAP_ENVRC\"\n"
        "    fi\n"
        "    printf '%s\\n' \"$FAKE_BOARDS_JSON\";;\n"
        "  'kanban boards create '*) echo 'created';;\n"
        "  'gateway restart') echo 'restarted';;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    git = fake_bin / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == '-C' ]]; then shift 2; fi\n"
        "case \"$*\" in\n"
        f"  'rev-parse origin/main') echo \"${{FAKE_GIT_UPSTREAM:-{SUPPORTED_SHA}}}\";;\n"
        f"  'rev-parse HEAD') echo \"${{FAKE_GIT_HEAD:-{CARRIED_HEAD}}}\";;\n"
        "  'cat-file -e '*'{commit}')\n"
        "    if [[ -n \"${FAKE_REQUIRE_BUNDLE:-}\" && ! -f \"$FAKE_BUNDLE_IMPORTED\" ]]; then exit 1; fi;;\n"
        "  'fetch '*'+refs/heads/*:refs/unified-kanban/carried/*') touch \"$FAKE_BUNDLE_IMPORTED\";;\n"
        "  'cherry HEAD '*) echo \"- ${*: -1}\";;\n"
        "  *) echo \"unexpected git call: $*\" >&2; exit 1;;\n"
        "esac\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    return fake_bin, log


def run_script(
    script: Path,
    home: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_STATE_HOME"] = str(home / ".local/state")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    if script == SETUP and env_extra is None:
        env_extra, _ = fake_env(home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script), *args], cwd=REPO, env=env,
        text=True, capture_output=True, check=False,
    )


def fake_env(
    tmp_path: Path,
    *,
    boards: list[str] | None = None,
    compatible: bool = True,
    missing_option: str | None = None,
    option_replacements: dict[str, str] | None = None,
) -> tuple[dict[str, str], Path]:
    fake_bin, log = install_fake_hermes(
        tmp_path,
        boards=boards,
        compatible=compatible,
        missing_option=missing_option,
        option_replacements=option_replacements,
    )
    agent_repo = tmp_path / "fake-hermes-agent"
    agent_repo.mkdir(exist_ok=True)
    carried = tmp_path / "empty-carried-commits"
    carried.write_text("# no carried commits in generic setup fixtures\n", encoding="utf-8")
    return {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_HERMES_LOG": str(log),
        "FAKE_BOARDS_JSON": json.dumps([{"slug": x} for x in (boards or [])]),
        "HERMES_AGENT_REPO": str(agent_repo),
        "HERMES_CARRIED_COMMITS_FILE": str(carried),
    }, log


def test_setup_dry_run_does_not_write(tmp_path: Path) -> None:
    result = run_script(SETUP, tmp_path, "--dry-run", "--no-restart", "--skip-smoke")
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_imports_missing_carried_objects_from_repository_bundle(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path)
    carried = tmp_path / "carried-commits"
    carried.write_text("deadbeef\n", encoding="utf-8")
    bundle = tmp_path / "hermes-agent-carried.bundle"
    bundle.write_bytes(b"synthetic bundle")
    imported = tmp_path / "bundle-imported"
    env.update(
        {
            "HERMES_CARRIED_COMMITS_FILE": str(carried),
            "HERMES_CARRIED_BUNDLE": str(bundle),
            "FAKE_REQUIRE_BUNDLE": "1",
            "FAKE_BUNDLE_IMPORTED": str(imported),
        }
    )

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert result.returncode == 0, result.stderr
    assert imported.exists()


def test_setup_dry_run_simulates_legacy_plugin_migration(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy/hermes-kanban"
    legacy.mkdir(parents=True)
    plugin_link = tmp_path / ".hermes/plugins/hermes-kanban"
    plugin_link.parent.mkdir(parents=True)
    plugin_link.symlink_to(legacy)
    env, _ = fake_env(tmp_path)
    env["UNIFIED_KANBAN_LEGACY_HERMES_PLUGIN_SOURCE"] = str(legacy)

    result = run_script(
        SETUP,
        tmp_path,
        "--dry-run",
        "--no-restart",
        "--skip-smoke",
        env_extra=env,
    )

    assert result.returncode == 0, result.stderr
    assert "Migrating legacy hermes-kanban plugin symlink" in result.stdout
    assert plugin_link.is_symlink()
    assert plugin_link.resolve() == legacy


def test_setup_is_idempotent_and_uses_repo_symlinks(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path)
    first = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env,
    )
    second = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env,
    )
    assert first.returncode == second.returncode == 0
    adapter_link = tmp_path / ".local/bin/kanban-adapter"
    assert adapter_link.is_symlink()
    assert adapter_link.resolve() == REPO / "bin/kanban-adapter"
    linked_cli = subprocess.run(
        ["bash", str(adapter_link), "--help"],
        env={**os.environ, **env, "HOME": str(tmp_path)},
        text=True, capture_output=True, check=False,
    )
    assert linked_cli.returncode == 0, linked_cli.stderr
    viewer_link = tmp_path / ".local/bin/ai-session-viewer"
    assert viewer_link.is_symlink()
    assert viewer_link.resolve() == REPO / "bin/ai-session-viewer"
    linked_viewer = subprocess.run(
        ["bash", str(viewer_link), "--help"],
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert linked_viewer.returncode == 0, linked_viewer.stderr
    assert "AI Session Viewer" in linked_viewer.stdout
    hook_link = tmp_path / ".local/bin/claude-kanban-hook"
    linked_hook = subprocess.run(
        ["bash", str(hook_link)],
        env={**os.environ, **env, "HOME": str(tmp_path)},
        text=True, capture_output=True, check=False,
    )
    assert linked_hook.returncode == 2
    assert "usage: claude-kanban-hook" in linked_hook.stderr
    codex_hook_link = tmp_path / ".local/bin/codex-kanban-hook"
    linked_codex_hook = subprocess.run(
        ["bash", str(codex_hook_link)],
        env={**os.environ, **env, "HOME": str(tmp_path)},
        text=True, capture_output=True, check=False,
    )
    assert linked_codex_hook.returncode == 2
    assert "usage: codex-kanban-hook" in linked_codex_hook.stderr
    assert "Hermes Dashboard" in first.stdout
    assert "Project directory" in first.stdout


@pytest.mark.parametrize("restart_args", [(), ("--no-restart",)])
def test_setup_warns_that_running_hermes_clients_keep_stale_plugin_code(
    tmp_path: Path, restart_args: tuple[str, ...],
) -> None:
    result = run_script(SETUP, tmp_path, *restart_args, "--skip-smoke")

    assert result.returncode == 0, result.stderr
    assert "running Hermes CLI, TUI, and Desktop chat processes" in result.stderr
    assert "version already loaded in memory" in result.stderr
    assert "must be quit and relaunched" in result.stderr
    assert "before testing a new prompt" in result.stderr
    assert "restarting the gateway does not reload those clients" in result.stderr


def test_setup_prints_stale_client_warning_after_gateway_restart(tmp_path: Path) -> None:
    env_extra, _ = fake_env(tmp_path)
    env = os.environ.copy()
    env.update(env_extra)
    env["HOME"] = str(tmp_path)
    env["XDG_STATE_HOME"] = str(tmp_path / ".local/state")
    env["XDG_CACHE_HOME"] = str(tmp_path / ".cache")

    result = subprocess.run(
        ["bash", str(SETUP), "--skip-smoke"],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode == 0, result.stdout
    assert result.stdout.index("restarted") < result.stdout.index("WARNING: running Hermes")


def test_setup_quotes_hook_commands_for_shell_paths(tmp_path: Path) -> None:
    home = tmp_path / "home with space;touch SHOULD_NOT_EXIST"
    result = run_script(SETUP, home, "--no-restart", "--skip-smoke")
    assert result.returncode == 0, result.stderr
    settings = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
    command = next(
        hook["command"]
        for group in settings["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
        if "unified-kanban-managed-v1" in hook.get("command", "")
    )

    executed = subprocess.run(
        command,
        shell=True,
        input="{}",
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache")},
        check=False,
    )

    assert executed.returncode == 0, executed.stderr
    assert not (REPO / "SHOULD_NOT_EXIST").exists()


def test_setup_merges_claude_hooks_and_uninstall_preserves_existing_hooks(
    tmp_path: Path,
) -> None:
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    existing = {
        "model": "opus",
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [
                    {"type": "command", "command": "/keep/existing-hook"},
                    {
                        "type": "command",
                        "command": f"{tmp_path}/.local/bin/claude-kanban-hook prompt",
                        "timeout": 30,
                    },
                ],
            }],
        },
    }
    settings.write_text(json.dumps(existing), encoding="utf-8")

    first = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke")
    second = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke")
    assert first.returncode == second.returncode == 0, second.stderr

    hook_link = tmp_path / ".local/bin/claude-kanban-hook"
    assert hook_link.is_symlink()
    assert hook_link.resolve() == REPO / "bin/claude-kanban-hook"
    installed = json.loads(settings.read_text(encoding="utf-8"))
    assert installed["model"] == "opus"
    commands = {
        event: [
            hook["command"]
            for group in installed["hooks"][event]
            for hook in group.get("hooks", [])
        ]
        for event in ("UserPromptSubmit", "Stop", "SessionEnd")
    }
    prefix = shlex.quote(str(hook_link))
    marker = "# unified-kanban-managed-v1"
    assert commands["UserPromptSubmit"].count(f"{prefix} prompt {marker}") == 1
    assert commands["Stop"].count(f"{prefix} stop {marker}") == 1
    assert commands["SessionEnd"].count(f"{prefix} session-end {marker}") == 1
    assert "/keep/existing-hook" in commands["UserPromptSubmit"]

    removed = run_script(UNINSTALL, tmp_path, "--no-restart")
    assert removed.returncode == 0, removed.stderr
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["model"] == "opus"
    assert after["hooks"]["UserPromptSubmit"] == existing["hooks"]["UserPromptSubmit"]
    assert "Stop" not in after["hooks"]
    assert "SessionEnd" not in after["hooks"]
    assert not hook_link.exists()
    assert not (tmp_path / ".local/bin/ai-session-viewer").exists()


def test_setup_merges_codex_hooks_and_uninstall_preserves_existing_hooks(
    tmp_path: Path,
) -> None:
    settings = tmp_path / ".codex/hooks.json"
    settings.parent.mkdir(parents=True)
    existing = {
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [{"type": "command", "command": "/keep/codex-hook"}],
            }],
        },
    }
    settings.write_text(json.dumps(existing), encoding="utf-8")

    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0

    hook_link = tmp_path / ".local/bin/codex-kanban-hook"
    installed = json.loads(settings.read_text(encoding="utf-8"))
    commands = {
        event: [
            hook["command"]
            for group in installed["hooks"][event]
            for hook in group.get("hooks", [])
        ]
        for event in ("UserPromptSubmit", "Stop")
    }
    prefix = shlex.quote(str(hook_link))
    marker = "# unified-kanban-managed-v1"
    assert commands["UserPromptSubmit"].count(f"{prefix} prompt {marker}") == 1
    assert commands["Stop"].count(f"{prefix} stop {marker}") == 1
    assert "/keep/codex-hook" in commands["UserPromptSubmit"]

    removed = run_script(UNINSTALL, tmp_path, "--no-restart")
    assert removed.returncode == 0, removed.stderr
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["hooks"]["UserPromptSubmit"] == existing["hooks"]["UserPromptSubmit"]
    assert "Stop" not in after["hooks"]
    assert not hook_link.exists()


def test_setup_preflights_malformed_codex_before_installing_anything(
    tmp_path: Path,
) -> None:
    claude = tmp_path / ".claude/settings.json"
    codex = tmp_path / ".codex/hooks.json"
    claude.parent.mkdir(parents=True)
    codex.parent.mkdir(parents=True)
    original = {"model": "keep", "hooks": {}}
    claude.write_text(json.dumps(original), encoding="utf-8")
    codex.write_text('{"hooks":{"Stop":"not-a-list"}}', encoding="utf-8")

    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke")

    assert result.returncode != 0
    assert json.loads(claude.read_text(encoding="utf-8")) == original
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert not (tmp_path / ".local/state/unified-kanban").exists()


def test_setup_refuses_foreign_symlink(tmp_path: Path) -> None:
    target = tmp_path / ".local/bin/kanban-adapter"
    target.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.write_text("keep", encoding="utf-8")
    target.symlink_to(foreign)
    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke")
    assert result.returncode != 0
    assert target.resolve() == foreign


def test_setup_refuses_foreign_session_viewer_symlink(tmp_path: Path) -> None:
    target = tmp_path / ".local/bin/ai-session-viewer"
    target.parent.mkdir(parents=True, exist_ok=True)
    foreign = tmp_path / "foreign-viewer"
    foreign.write_text("keep", encoding="utf-8")
    target.symlink_to(foreign)

    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke")

    assert result.returncode != 0
    assert target.is_symlink()
    assert target.resolve() == foreign
    assert foreign.read_text(encoding="utf-8") == "keep"


def test_setup_rejects_unsafe_board_slug(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "safe;$(touch PWNED)",
    )
    assert result.returncode == 2
    assert not (project / ".envrc").exists()


@pytest.mark.parametrize("option", ["--project-dir", "--board"])
def test_setup_rejects_missing_option_value_without_installing(
    tmp_path: Path, option: str
) -> None:
    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", option)
    assert result.returncode == 2
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_refuses_project_envrc_symlink_without_changing_target(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    shared = tmp_path / "shared.envrc"
    shared.write_text("export KEEP_ME=yes\n", encoding="utf-8")
    envrc = project / ".envrc"
    envrc.symlink_to(shared)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert envrc.is_symlink()
    assert envrc.resolve() == shared
    assert shared.read_text(encoding="utf-8") == "export KEEP_ME=yes\n"


def test_setup_rejects_incompatible_hermes_before_writing(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, compatible=False)
    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)
    assert result.returncode != 0
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_rejects_unsupported_hermes_upstream_before_writing(
    tmp_path: Path,
) -> None:
    env, _ = fake_env(tmp_path)
    unsupported = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    env["FAKE_GIT_UPSTREAM"] = unsupported
    env["FAKE_HERMES_UPSTREAM"] = unsupported[:8]

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert result.returncode != 0
    assert "Setup blocked: unified-kanban supports Hermes upstream" in result.stderr
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_ignores_environment_pin_override(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path)
    expected = tmp_path / "invalid-hermes-upstream"
    expected.write_text("not-a-commit\n", encoding="utf-8")
    env["HERMES_EXPECTED_UPSTREAM_FILE"] = str(expected)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


@pytest.mark.parametrize(
    "missing_option",
    [
        "--board", "--assignee", "--tenant", "--created-by",
        "--initial-status", "--observation", "--json", "--name", "--author",
        "--summary", "--kind",
    ],
)
def test_setup_rejects_each_missing_hermes_option(
    tmp_path: Path, missing_option: str
) -> None:
    env, _ = fake_env(tmp_path, missing_option=missing_option)
    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)
    assert result.returncode != 0
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


@pytest.mark.parametrize(
    ("required", "lookalike"),
    [
        ("--board", "--board-id"),
        ("--json", "--json-output"),
        ("--summary", "--summary-file"),
        ("--name", "--name-prefix"),
    ],
)
def test_setup_rejects_lookalike_hermes_options(
    tmp_path: Path, required: str, lookalike: str
) -> None:
    env, _ = fake_env(tmp_path, option_replacements={required: lookalike})
    result = run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke", env_extra=env)
    assert result.returncode != 0
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


@pytest.mark.parametrize(
    "invalid_boards",
    [
        {"slug": "shop-bridge"},
        [1],
        [{"slug": 1}],
        [{"name": "missing-slug"}],
    ],
)
def test_setup_fails_closed_on_invalid_board_json_schema(
    tmp_path: Path, invalid_boards: object
) -> None:
    env, log = fake_env(tmp_path)
    env["FAKE_BOARDS_JSON"] = json.dumps(invalid_boards)
    project = tmp_path / "project"
    project.mkdir()

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    log_lines = log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("kanban boards create shop-bridge ") for line in log_lines)
    assert not (project / ".envrc").exists()


def test_project_routing_creates_board_and_uninstall_removes_managed_line(tmp_path: Path) -> None:
    env, log = fake_env(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text("export KEEP_ME=yes\n", encoding="utf-8")
    args = (
        "--no-restart", "--skip-smoke", "--project-dir", str(project),
        "--board", "shop-bridge",
    )
    assert run_script(SETUP, tmp_path, *args, env_extra=env).returncode == 0
    assert run_script(SETUP, tmp_path, *args, env_extra=env).returncode == 0
    lines = envrc.read_text(encoding="utf-8").splitlines()
    assert "export KEEP_ME=yes" in lines
    assert lines.count("export HERMES_KANBAN_BOARD=shop-bridge # unified-kanban") == 1
    assert "kanban boards create shop-bridge" in log.read_text(encoding="utf-8")

    result = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)
    assert result.returncode == 0, result.stderr
    assert envrc.read_text(encoding="utf-8") == "export KEEP_ME=yes\n"


def test_corrupt_state_does_not_modify_envrc(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text("export KEEP_ME=yes\n", encoding="utf-8")
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True)
    state.write_text("not-json", encoding="utf-8")

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert envrc.read_text(encoding="utf-8") == "export KEEP_ME=yes\n"


def test_corrupt_state_fails_before_board_or_cache_side_effects(tmp_path: Path) -> None:
    env, log = fake_env(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True)
    state.write_text("not-json", encoding="utf-8")

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    log_lines = log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("kanban boards create shop-bridge ") for line in log_lines)
    assert not (tmp_path / ".cache/unified-kanban").exists()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


@pytest.mark.parametrize("invalid_state", [{}, [1], ["relative"], ["/tmp/x", "/tmp/x"]])
def test_setup_rejects_invalid_state_schema_without_modifying_envrc(
    tmp_path: Path, invalid_state: object
) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text("export KEEP_ME=yes\n", encoding="utf-8")
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps(invalid_state), encoding="utf-8")

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert envrc.read_text(encoding="utf-8") == "export KEEP_ME=yes\n"


def test_setup_refuses_managed_state_symlink(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign-state.json"
    foreign.write_text("[]\n", encoding="utf-8")
    state.symlink_to(foreign)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert state.is_symlink()
    assert foreign.read_text(encoding="utf-8") == "[]\n"
    assert not (project / ".envrc").exists()


@pytest.mark.parametrize("target_kind", ["state", "envrc"])
@pytest.mark.parametrize("object_kind", ["directory", "fifo"])
def test_setup_rejects_non_regular_object_before_board_creation(
    tmp_path: Path, target_kind: str, object_kind: str
) -> None:
    env, log = fake_env(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    if target_kind == "state":
        target = tmp_path / ".local/state/unified-kanban/managed-projects.json"
        target.parent.mkdir(parents=True)
    else:
        target = project / ".envrc"
    if object_kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    log_lines = log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("kanban boards create shop-bridge ") for line in log_lines)
    assert target.exists()
    assert not (tmp_path / ".cache/unified-kanban").exists()


def test_setup_rejects_envrc_symlink_swap_during_board_lookup(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text("export KEEP_ME=yes\n", encoding="utf-8")
    external = tmp_path / "external.envrc"
    external.write_text("export EXTERNAL=yes\n", encoding="utf-8")
    env["FAKE_SWAP_ENVRC"] = str(envrc)
    env["FAKE_SWAP_TARGET"] = str(external)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert envrc.is_symlink()
    assert envrc.resolve() == external
    assert external.read_text(encoding="utf-8") == "export EXTERNAL=yes\n"
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    assert not state.exists()
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_setup_rolls_back_state_when_envrc_replace_fails(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc_original = b"export KEEP_ME=yes\n"
    envrc.write_bytes(envrc_original)
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True)
    state_original = b"[]\n"
    state.write_bytes(state_original)
    injection = tmp_path / "failure-injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os\n"
        "_real_replace = os.replace\n"
        "_calls = 0\n"
        "def _replace(src, dst):\n"
        "    global _calls\n"
        "    _calls += 1\n"
        "    if _calls == 2:\n"
        "        raise OSError('injected envrc replace failure')\n"
        "    return _real_replace(src, dst)\n"
        "os.replace = _replace\n",
        encoding="utf-8",
    )
    env["PYTHONPATH"] = str(injection)

    result = run_script(
        SETUP, tmp_path, "--no-restart", "--skip-smoke",
        "--project-dir", str(project), "--board", "shop-bridge", env_extra=env,
    )

    assert result.returncode != 0
    assert state.read_bytes() == state_original
    assert envrc.read_bytes() == envrc_original
    assert sorted(path.name for path in project.iterdir()) == [".envrc"]
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()


def test_uninstall_does_not_replace_envrc_changed_to_symlink(tmp_path: Path) -> None:
    env, _ = fake_env(tmp_path, boards=["shop-bridge"])
    project = tmp_path / "project"
    project.mkdir()
    args = (
        "--no-restart", "--skip-smoke", "--project-dir", str(project),
        "--board", "shop-bridge",
    )
    assert run_script(SETUP, tmp_path, *args, env_extra=env).returncode == 0
    envrc = project / ".envrc"
    envrc.unlink()
    shared = tmp_path / "shared.envrc"
    shared.write_text("export USER_MANAGED=yes\n", encoding="utf-8")
    envrc.symlink_to(shared)

    result = run_script(UNINSTALL, tmp_path, "--no-restart", env_extra=env)

    assert result.returncode == 0, result.stderr
    assert envrc.is_symlink()
    assert envrc.resolve() == shared
    assert shared.read_text(encoding="utf-8") == "export USER_MANAGED=yes\n"


def test_uninstall_validates_state_before_removing_adapter_link(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    adapter = tmp_path / ".local/bin/kanban-adapter"
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("not-json", encoding="utf-8")

    result = run_script(UNINSTALL, tmp_path, "--no-restart")

    assert result.returncode != 0
    assert adapter.is_symlink()


@pytest.mark.parametrize(
    "invalid_state",
    [
        {},
        {"project": "/tmp/project"},
        [1],
        ["relative/project"],
        ["/absolute/project", "/absolute/project"],
    ],
)
def test_uninstall_rejects_invalid_state_schema_before_changes(
    tmp_path: Path, invalid_state: object
) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    adapter = tmp_path / ".local/bin/kanban-adapter"
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(invalid_state), encoding="utf-8")

    result = run_script(UNINSTALL, tmp_path, "--no-restart")

    assert result.returncode != 0
    assert adapter.is_symlink()
    assert state.exists()


@pytest.mark.parametrize("state_kind", ["directory", "fifo"])
def test_uninstall_rejects_non_regular_state_object_before_changes(
    tmp_path: Path, state_kind: str
) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    adapter = tmp_path / ".local/bin/kanban-adapter"
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    if state_kind == "directory":
        state.mkdir()
    else:
        os.mkfifo(state)

    result = run_script(UNINSTALL, tmp_path, "--no-restart")

    assert result.returncode != 0
    assert adapter.is_symlink()
    assert state.exists()


def test_uninstall_preflights_all_envrc_files_before_any_change(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_envrc = first / ".envrc"
    second_envrc = second / ".envrc"
    managed = "export HERMES_KANBAN_BOARD=board # unified-kanban\n"
    first_envrc.write_text("export KEEP=yes\n" + managed, encoding="utf-8")
    second_envrc.write_bytes(b"\xff\xfe" + managed.encode())
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.write_text(json.dumps([str(first), str(second)]), encoding="utf-8")
    first_before = first_envrc.read_bytes()

    result = run_script(UNINSTALL, tmp_path, "--no-restart")

    assert result.returncode != 0
    assert first_envrc.read_bytes() == first_before
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()
    assert state.exists()


def test_uninstall_rolls_back_when_second_replace_fails(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    projects = [tmp_path / "first", tmp_path / "second"]
    managed = "export HERMES_KANBAN_BOARD=board # unified-kanban\n"
    originals: dict[Path, bytes] = {}
    for index, project in enumerate(projects):
        project.mkdir()
        envrc = project / ".envrc"
        envrc.write_text(f"export KEEP_{index}=yes\n" + managed, encoding="utf-8")
        originals[envrc] = envrc.read_bytes()
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.write_text(json.dumps([str(project) for project in projects]), encoding="utf-8")

    injection = tmp_path / "failure-injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os\n"
        "_real_replace = os.replace\n"
        "_calls = 0\n"
        "def _replace(src, dst):\n"
        "    global _calls\n"
        "    _calls += 1\n"
        "    if _calls == 2:\n"
        "        raise OSError('injected replace failure')\n"
        "    return _real_replace(src, dst)\n"
        "os.replace = _replace\n",
        encoding="utf-8",
    )

    result = run_script(
        UNINSTALL, tmp_path, "--no-restart", env_extra={"PYTHONPATH": str(injection)}
    )

    assert result.returncode != 0
    for envrc, original in originals.items():
        assert envrc.read_bytes() == original
        assert sorted(path.name for path in envrc.parent.iterdir()) == [".envrc"]
    assert state.exists()
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_preserves_envrc_replaced_during_temp_preparation(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text(
        "export ORIGINAL=yes\nexport HERMES_KANBAN_BOARD=board # unified-kanban\n",
        encoding="utf-8",
    )
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state_original = json.dumps([str(project)]).encode()
    state.write_bytes(state_original)
    injection = tmp_path / "identity-injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os, tempfile\n"
        "from pathlib import Path\n"
        "_real_mkstemp = tempfile.mkstemp\n"
        "_done = False\n"
        "def _mkstemp(*args, **kwargs):\n"
        "    global _done\n"
        "    result = _real_mkstemp(*args, **kwargs)\n"
        "    if not _done:\n"
        "        _done = True\n"
        "        target = Path(os.environ['SWAP_ENVRC'])\n"
        "        replacement = target.with_name('.envrc.concurrent')\n"
        "        replacement.write_text('export CONCURRENT=yes\\n')\n"
        "        os.replace(replacement, target)\n"
        "    return result\n"
        "tempfile.mkstemp = _mkstemp\n",
        encoding="utf-8",
    )

    result = run_script(
        UNINSTALL, tmp_path, "--no-restart",
        env_extra={"PYTHONPATH": str(injection), "SWAP_ENVRC": str(envrc)},
    )

    assert result.returncode != 0
    assert envrc.read_text(encoding="utf-8") == "export CONCURRENT=yes\n"
    assert state.read_bytes() == state_original
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_does_not_claim_envrc_replaced_immediately_after_commit(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc.write_text(
        "export ORIGINAL=yes\nexport HERMES_KANBAN_BOARD=board # unified-kanban\n",
        encoding="utf-8",
    )
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state_original = json.dumps([str(project)]).encode()
    state.write_bytes(state_original)
    injection = tmp_path / "post-commit-race-injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "_real_replace = os.replace\n"
        "_done = False\n"
        "def _replace(src, dst):\n"
        "    global _done\n"
        "    result = _real_replace(src, dst)\n"
        "    if not _done and Path(dst).name == '.envrc':\n"
        "        _done = True\n"
        "        target = Path(dst)\n"
        "        concurrent = target.with_name('.envrc.concurrent')\n"
        "        concurrent.write_text('export CONCURRENT=yes\\n')\n"
        "        _real_replace(concurrent, target)\n"
        "    return result\n"
        "os.replace = _replace\n",
        encoding="utf-8",
    )

    result = run_script(
        UNINSTALL, tmp_path, "--no-restart",
        env_extra={"PYTHONPATH": str(injection)},
    )

    assert result.returncode != 0
    assert envrc.read_text(encoding="utf-8") == "export CONCURRENT=yes\n"
    assert state.read_bytes() == state_original
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_preserves_state_replaced_after_envrc_commit(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    envrc_original = b"export KEEP=yes\nexport HERMES_KANBAN_BOARD=board # unified-kanban\n"
    envrc.write_bytes(envrc_original)
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.write_text(json.dumps([str(project)]), encoding="utf-8")
    injection = tmp_path / "state-race-injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "_real_replace = os.replace\n"
        "_done = False\n"
        "def _replace(src, dst):\n"
        "    global _done\n"
        "    result = _real_replace(src, dst)\n"
        "    if not _done and Path(dst).name == '.envrc':\n"
        "        _done = True\n"
        "        state = Path(os.environ['SWAP_STATE'])\n"
        "        replacement = state.with_suffix('.concurrent')\n"
        "        replacement.write_text('[\\\"/concurrent/project\\\"]\\n')\n"
        "        _real_replace(replacement, state)\n"
        "    return result\n"
        "os.replace = _replace\n",
        encoding="utf-8",
    )

    result = run_script(
        UNINSTALL, tmp_path, "--no-restart",
        env_extra={"PYTHONPATH": str(injection), "SWAP_STATE": str(state)},
    )

    assert result.returncode != 0
    assert envrc.read_bytes() == envrc_original
    assert state.read_text(encoding="utf-8") == '["/concurrent/project"]\n'
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_does_not_overwrite_concurrent_change_during_rollback(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    projects = [tmp_path / "first", tmp_path / "second"]
    envrcs = []
    managed = "export HERMES_KANBAN_BOARD=board # unified-kanban\n"
    for index, project in enumerate(projects):
        project.mkdir()
        envrc = project / ".envrc"
        envrc.write_text(f"export KEEP_{index}=yes\n" + managed, encoding="utf-8")
        envrcs.append(envrc)
    second_original = envrcs[1].read_bytes()
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state_original = json.dumps([str(project) for project in projects]).encode()
    state.write_bytes(state_original)
    injection = tmp_path / "rollback-race-injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "_real_replace = os.replace\n"
        "_calls = 0\n"
        "def _replace(src, dst):\n"
        "    global _calls\n"
        "    _calls += 1\n"
        "    if _calls == 2:\n"
        "        first = Path(os.environ['FIRST_ENVRC'])\n"
        "        concurrent = first.with_name('.envrc.concurrent')\n"
        "        concurrent.write_text('export CONCURRENT=yes\\n')\n"
        "        _real_replace(concurrent, first)\n"
        "        raise OSError('injected second replace failure')\n"
        "    return _real_replace(src, dst)\n"
        "os.replace = _replace\n",
        encoding="utf-8",
    )

    result = run_script(
        UNINSTALL, tmp_path, "--no-restart",
        env_extra={"PYTHONPATH": str(injection), "FIRST_ENVRC": str(envrcs[0])},
    )

    assert result.returncode != 0
    assert envrcs[0].read_text(encoding="utf-8") == "export CONCURRENT=yes\n"
    assert envrcs[1].read_bytes() == second_original
    assert state.read_bytes() == state_original
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_rolls_back_when_state_removal_fails(tmp_path: Path) -> None:
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    project = tmp_path / "project"
    project.mkdir()
    envrc = project / ".envrc"
    original = b"export KEEP=yes\nexport HERMES_KANBAN_BOARD=board # unified-kanban\n"
    envrc.write_bytes(original)
    state = tmp_path / ".local/state/unified-kanban/managed-projects.json"
    state.write_text(json.dumps([str(project)]), encoding="utf-8")

    injection = tmp_path / "failure-injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "_real_unlink = Path.unlink\n"
        "def _unlink(self, *args, **kwargs):\n"
        "    if self.name == 'managed-projects.json':\n"
        "        raise OSError('injected state unlink failure')\n"
        "    return _real_unlink(self, *args, **kwargs)\n"
        "Path.unlink = _unlink\n",
        encoding="utf-8",
    )

    result = run_script(
        UNINSTALL, tmp_path, "--no-restart", env_extra={"PYTHONPATH": str(injection)}
    )

    assert result.returncode != 0
    assert envrc.read_bytes() == original
    assert sorted(path.name for path in project.iterdir()) == [".envrc"]
    assert state.exists()
    assert (tmp_path / ".local/bin/kanban-adapter").is_symlink()


def test_uninstall_removes_only_managed_link(tmp_path: Path) -> None:
    unrelated = tmp_path / ".local/bin/unrelated"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep", encoding="utf-8")
    assert run_script(SETUP, tmp_path, "--no-restart", "--skip-smoke").returncode == 0
    result = run_script(UNINSTALL, tmp_path, "--no-restart")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".local/bin/kanban-adapter").exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
