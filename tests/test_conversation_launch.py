"""일반 실행 후보를 실제 공급자 대신 임시 실행 파일로 검증한다."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "collected-native-agent"


def fixture_launch(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state = home / ".hermes" / "unified-kanban-conversation"
    state.mkdir(parents=True, mode=0o700)
    native = tmp_path / "native"
    native.write_text(f"#!{sys.executable}\n" + '''import json,os,sys
from pathlib import Path
Path("new-dir").mkdir()
Path("new-dir/source").write_text("fixture")
print(json.dumps({"args":sys.argv[1:],"cwd":os.getcwd(),"home":os.environ["HOME"],"codex":os.environ.get("CODEX_HOME"),"config":os.environ.get("UNIFIED_KANBAN_CONVERSATION_CONFIG"),"mask":os.umask(0o077),"extra":os.environ.get("EXTRA")}))
sys.exit(23)
''')
    native.chmod(0o700)
    profile = state / "codex.json"
    payload = dict(schema_version=1, provider="codex", executable=str(native),
                   sha256=hashlib.sha256(native.read_bytes()).hexdigest())
    profile.write_text(json.dumps(payload))
    profile.chmod(0o600)
    selector = state / "activation.json"
    selector.write_text(json.dumps(dict(schema_version=1, enabled=True,
        runtime_config=str(state / "runtime.json"), launcher_profiles={"codex":str(profile)})))
    selector.chmod(0o600)
    env = {"HOME": str(home), "HERMES_HOME":str(home / ".hermes"),
           "CODEX_HOME":str(home / "orca account"), "PATH":"/usr/bin:/bin", "EXTRA":"unchanged"}
    return native, profile, selector, env


def invoke(tmp_path, env, args=(), entry=BIN):
    return subprocess.run([str(entry), "codex", *args], cwd=tmp_path,
        env=env, text=True, capture_output=True)


def test_persistent_selector_symlink_exact_native_context(tmp_path):
    _, _, selector, env = fixture_launch(tmp_path)
    link = tmp_path / "installed"
    link.symlink_to(BIN)
    alias = tmp_path / "alias"
    alias.symlink_to(link)
    args = ["", "--help", "--", "space value", "$(not-a-command)", "한글\n값"]
    before = os.umask(0o022)
    try:
        result = invoke(tmp_path, env, args, alias)
        assert result.returncode == 23, result.stderr
        data = json.loads(result.stdout)
        assert data == dict(args=args, cwd=str(tmp_path.resolve()), home=env["HOME"],
            codex=env["CODEX_HOME"], config=json.loads(selector.read_text())["runtime_config"],
            mask=0o077, extra="unchanged")
        assert os.umask(0o022) == 0o022
        assert (tmp_path / "new-dir").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "new-dir/source").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(before)


@pytest.mark.parametrize("guard", ["HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK", "UNIFIED_KANBAN_NATIVE_LAUNCH_ACTIVE"])
@pytest.mark.parametrize("value", ["", "present"])
def test_presence_guards_refuse_without_child(tmp_path, guard, value):
    _, _, _, env = fixture_launch(tmp_path)
    env[guard] = value
    result = invoke(tmp_path, env)
    assert result.returncode == 2
    assert not (tmp_path / "new-dir").exists()


@pytest.mark.parametrize("damage", ["hash", "relative", "missing", "nonexec", "writable", "setuid", "setgid", "symlink", "directory", "self", "self-hardlink", "provider", "schema", "extra"])
def test_unapproved_target_never_executes(tmp_path, damage):
    native, profile, _, env = fixture_launch(tmp_path)
    value = json.loads(profile.read_text())
    if damage == "hash":
        native.write_text(native.read_text() + "\n")
    elif damage == "relative":
        value["executable"] = "native"
    elif damage == "missing":
        native.unlink()
    elif damage == "nonexec":
        native.chmod(0o600)
    elif damage == "writable":
        native.chmod(0o777)
    elif damage in {"setuid", "setgid"}:
        native.chmod(0o4700 if damage == "setuid" else 0o2700)
    elif damage == "symlink":
        alias = tmp_path / "native-link"
        alias.symlink_to(native)
        value["executable"] = str(alias)
    elif damage == "directory":
        value["executable"] = str(tmp_path)
    elif damage in {"self", "self-hardlink"}:
        target = BIN
        if damage == "self-hardlink":
            target = tmp_path / "self"
            os.link(BIN, target)
        value["executable"] = str(target)
        value["sha256"] = hashlib.sha256(BIN.read_bytes()).hexdigest()
    elif damage == "provider":
        value["provider"] = "claude"
    elif damage == "schema":
        value["schema_version"] = True
    else:
        value["unexpected"] = True
    profile.write_text(json.dumps(value))
    result = invoke(tmp_path, env)
    assert result.returncode == 2
    assert not (tmp_path / "new-dir").exists()


@pytest.mark.parametrize("damage", ["absent", "disabled", "malformed", "mode", "hardlink", "symlink", "parent-mode", "duplicate", "fifo"])
def test_selector_or_metadata_failure_never_executes(tmp_path, damage):
    _, profile, selector, env = fixture_launch(tmp_path)
    if damage == "absent":
        selector.unlink()
    elif damage == "disabled":
        value = json.loads(selector.read_text())
        value["enabled"] = False
        selector.write_text(json.dumps(value))
    elif damage == "malformed":
        profile.write_text("private-canary:not-json")
    elif damage == "mode":
        profile.chmod(0o644)
    elif damage == "hardlink":
        os.link(profile, profile.with_name("alias"))
    elif damage == "symlink":
        target = profile.with_name("real")
        profile.rename(target)
        profile.symlink_to(target)
    elif damage == "parent-mode":
        profile.parent.chmod(0o755)
    elif damage == "duplicate":
        profile.write_text('{"schema_version":1,"schema_version":2}')
    else:
        profile.unlink()
        os.mkfifo(profile, 0o600)
    result = invoke(tmp_path, env)
    assert result.returncode == 2
    assert "private-canary" not in result.stderr
    assert not (tmp_path / "new-dir").exists()


def test_selector_changed_between_reads_is_refused(tmp_path, monkeypatch):
    from kanban_adapter import conversation_launch as launch
    _, _, selector, env = fixture_launch(tmp_path)
    monkeypatch.setattr(launch.os, "environ", env)
    resolve = launch.activation.resolve_activation
    def changed(home):
        selected = resolve(home)
        value = json.loads(selector.read_text())
        value["launcher_profiles"]["claude"] = str(tmp_path / "other-profile.json")
        selector.write_text(json.dumps(value))
        return selected
    monkeypatch.setattr(launch.activation, "resolve_activation", changed)
    monkeypatch.setattr(launch.os, "umask", lambda _: None)
    monkeypatch.setattr(launch.os, "execve", lambda *args: pytest.fail("changed selector reached exec"))
    assert launch.main(["codex"]) == 2


@pytest.mark.parametrize("value", ["", "relative", "/tmp/../runtime"])
def test_invalid_override_is_not_fallback(tmp_path, value):
    _, _, _, env = fixture_launch(tmp_path)
    env["UNIFIED_KANBAN_CONVERSATION_CONFIG"] = value
    assert invoke(tmp_path, env).returncode == 2
    assert not (tmp_path / "new-dir").exists()


def test_explicit_override_and_existing_source_are_preserved(tmp_path):
    _, _, _, env = fixture_launch(tmp_path)
    source = tmp_path / "existing-source"
    source.write_text("unchanged")
    source.chmod(0o644)
    before = source.stat()
    env["UNIFIED_KANBAN_CONVERSATION_CONFIG"] = str(tmp_path / "explicit.json")
    result = invoke(tmp_path, env)
    assert result.returncode == 23
    assert json.loads(result.stdout)["config"] == env["UNIFIED_KANBAN_CONVERSATION_CONFIG"]
    after = source.stat()
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (before.st_ino, before.st_mode, before.st_mtime_ns)


def test_signal_is_not_converted_to_success(tmp_path):
    native, profile, _, env = fixture_launch(tmp_path)
    native.write_text(f"#!{sys.executable}\nimport os,signal\nos.kill(os.getpid(),signal.SIGTERM)\n")
    value = json.loads(profile.read_text())
    value["sha256"] = hashlib.sha256(native.read_bytes()).hexdigest()
    profile.write_text(json.dumps(value))
    assert invoke(tmp_path, env).returncode == -15


def test_claude_uses_same_context_contract(tmp_path):
    _, profile, selector, env = fixture_launch(tmp_path)
    value = json.loads(profile.read_text())
    value["provider"] = "claude"
    profile.write_text(json.dumps(value))
    value = json.loads(selector.read_text())
    value["launcher_profiles"] = {"claude": str(profile)}
    selector.write_text(json.dumps(value))
    result = subprocess.run([str(BIN), "claude", "--resume", "space value"],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 23, result.stderr
    data = json.loads(result.stdout)
    assert data["args"] == ["--resume", "space value"]
    assert data["codex"] == env["CODEX_HOME"]
    assert data["cwd"] == str(tmp_path.resolve())
    assert data["mask"] == 0o077


def test_override_does_not_replace_explicit_persistent_consent(tmp_path):
    _, _, selector, env = fixture_launch(tmp_path)
    selector.unlink()
    env["UNIFIED_KANBAN_CONVERSATION_CONFIG"] = str(tmp_path / "override.json")
    assert invoke(tmp_path, env).returncode == 2
    assert not (tmp_path / "new-dir").exists()


def test_path_decoy_is_not_used_for_native_resolution(tmp_path):
    _, _, _, env = fixture_launch(tmp_path)
    decoy = tmp_path / "codex"
    decoy.write_text("#!/bin/sh\nexit 91\n")
    decoy.chmod(0o700)
    env["PATH"] = str(tmp_path) + ":/usr/bin:/bin"
    assert invoke(tmp_path, env).returncode == 23


def test_sourcing_preserves_shell_umask_and_environment(tmp_path):
    _, _, _, env = fixture_launch(tmp_path)
    result = subprocess.run(["/bin/bash", "-c",
        'umask 022; source "$1"; rc=$?; printf "%s|%s|%s" "$rc" "$(umask)" "${UNIFIED_KANBAN_NATIVE_LAUNCH_ACTIVE-unset}"',
        "fixture", str(BIN)], cwd=tmp_path, env=env, text=True,
        capture_output=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout == "2|0022|unset"
    assert not (tmp_path / "new-dir").exists()


def test_relative_installed_symlink_chain(tmp_path):
    _, _, _, env = fixture_launch(tmp_path)
    link = tmp_path / "installed"
    link.symlink_to(BIN)
    alias = tmp_path / "alias"
    alias.symlink_to("installed")
    assert invoke(tmp_path, env, entry=alias).returncode == 23
