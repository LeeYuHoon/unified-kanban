"""네이티브 정책 인계에 앞서 생산자 권한을 검증해야 한다."""
import json
import os
from pathlib import Path
import pytest
from test_tui_prebuilt_builder import helper


def test_handoff_uses_verified_completion_not_ambient_or_fresh_receipt(tmp_path, monkeypatch):
    h = helper()
    release = tmp_path / ('release-' + 'a' * 40)
    release.mkdir()
    (release / h._COMPLETION_RECEIPT).write_text(json.dumps({'upstream': 'b'*40, 'carried': 'a'*40, 'tui_receipt_sha256': 'c'*64}))
    (release / h._COMPLETION_RECEIPT).chmod(0o600)
    calls = []
    monkeypatch.setattr(h, '_validate_real_directory_ancestry', lambda p: None)
    monkeypatch.setattr(h, '_verify_completed_release', lambda *a: calls.append(a))
    env = h.managed_tui_environment(release)
    assert len(calls) == 1
    assert env == {'HERMES_UNIFIED_KANBAN_TUI': 'prebuilt-v1', 'HERMES_UNIFIED_KANBAN_RELEASE': str(release), 'HERMES_UNIFIED_KANBAN_TUI_RECEIPT_SHA256': 'c'*64}
    def refuse(*args):
        raise RuntimeError('completion mismatch')
    monkeypatch.setattr(h, '_verify_completed_release', refuse)
    with pytest.raises(RuntimeError, match='completion mismatch'):
        h.managed_tui_environment(release)


def test_launcher_handoff_and_exact_legacy_provenance(tmp_path):
    h = helper()
    layout = h.release_layout(tmp_path / 'agent', 'b'*40, 'a'*40)
    current = h.launcher_payload(layout, 'absent')
    assert b'managed_tui_environment' in current
    assert b'DYLD_' in current
    legacy = h.legacy_launcher_payload(layout, 'absent')
    assert h.launcher_baseline(layout, legacy, accept_legacy=True) == 'absent'
    with pytest.raises(h.ForeignLauncher):
        h.launcher_baseline(layout, legacy)
    with pytest.raises(h.ForeignLauncher):
        h.launcher_baseline(layout, legacy + b'# changed', accept_legacy=True)


def test_gateway_parent_forces_verified_handoff_and_scrubs_loaders(tmp_path, monkeypatch):
    import importlib.util
    import plistlib
    script = Path(__file__).resolve().parents[1] / 'scripts/harden-macos-gateway-plist.py'
    spec = importlib.util.spec_from_file_location('harden_tui', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / 'releases'
    release = root / ('release-' + 'a'*40)
    python = release / 'venv/bin/python'
    python.parent.mkdir(parents=True)
    root.chmod(0o700)
    python.write_text('fixture')
    python.chmod(0o700)
    expected = {'HERMES_UNIFIED_KANBAN_TUI': 'prebuilt-v1', 'HERMES_UNIFIED_KANBAN_RELEASE': str(release), 'HERMES_UNIFIED_KANBAN_TUI_RECEIPT_SHA256': 'c'*64}
    calls = []
    def authority(p):
        calls.append(p)
        return expected
    monkeypatch.setattr(module, 'managed_tui_environment', authority, raising=False)
    source, candidate = tmp_path/'source', tmp_path/'candidate'
    source.write_bytes(plistlib.dumps({'Label':'ai.hermes.gateway', 'ProgramArguments':[str(python), '-m', 'hermes_cli.main', 'gateway', 'run'], 'EnvironmentVariables':{'HERMES_UNIFIED_KANBAN_TUI':'evil', 'NODE_OPTIONS':'evil', 'DYLD_INSERT_LIBRARIES':'evil', 'LD_PRELOAD':'evil'}}))
    source.chmod(0o600)
    module.render(source, candidate, root)
    env = plistlib.loads(candidate.read_bytes())['EnvironmentVariables']
    assert calls == [release]
    assert all(env[k] == v for k,v in expected.items())
    assert not {'NODE_OPTIONS', 'DYLD_INSERT_LIBRARIES', 'LD_PRELOAD'} & env.keys()


def test_handoff_rejects_legacy_receipt_without_repair(tmp_path, monkeypatch):
    h = helper()
    release = tmp_path / ('release-' + 'a'*40)
    release.mkdir()
    receipt = release / h._COMPLETION_RECEIPT
    receipt.write_text(json.dumps({'upstream': 'b'*40, 'carried': 'a'*40}))
    receipt.chmod(0o600)
    before = receipt.read_bytes()
    monkeypatch.setattr(h, '_validate_real_directory_ancestry', lambda p: None)
    with pytest.raises(RuntimeError, match='rebuild'):
        h.managed_tui_environment(release)
    assert receipt.read_bytes() == before
