"""설치기 전용 임시 경로에서 실제 링크 수명주기를 검증한다."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_conversation_launch import fixture_launch, invoke
from kanban_adapter import conversation_launch as launch
from kanban_adapter import conversation_namespace as ns

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/install-collected-launcher.py'
RECEIPT = '.collected-native-agent.receipt.json'


def run(prefix, env, action='install', *extra):
    return subprocess.run([sys.executable, '-I', '-S', '-B', str(SCRIPT), action,
        '--prefix', str(prefix), '--profile-home', env['HERMES_HOME'], '--provider', 'codex', *extra],
        env=env, text=True, capture_output=True)


def test_installed_link_lifecycle(tmp_path):
    _, _, selector, env = fixture_launch(tmp_path)
    prefix = tmp_path / 'bin'
    prefix.mkdir()
    before = selector.read_bytes()
    result = run(prefix, env, 'install', '--dry-run')
    assert result.returncode == 0, result.stderr
    assert list(prefix.iterdir()) == []
    result = run(prefix, env)
    assert result.returncode == 0, result.stderr
    link = prefix / 'collected-native-agent'
    assert os.readlink(link) == str(ROOT / 'bin/collected-native-agent')
    identities = {p.name: p.lstat().st_ino for p in prefix.iterdir()}
    assert run(prefix, env).returncode == 0
    assert identities == {p.name: p.lstat().st_ino for p in prefix.iterdir()}
    args = ['', '--help', '--', 'space value', '$(no)', '한글\n값']
    child = invoke(tmp_path, env, args, link)
    assert child.returncode == 23, child.stderr
    data = json.loads(child.stdout)
    assert data['args'] == args
    assert data['cwd'] == str(tmp_path.resolve())
    assert data['codex'] == env['CODEX_HOME']
    assert run(prefix, env, 'uninstall').returncode == 0
    assert list(prefix.iterdir()) == []
    assert run(prefix, env, 'uninstall').returncode == 0
    assert run(prefix, env).returncode == 0
    assert run(prefix, env, 'uninstall').returncode == 0
    assert selector.read_bytes() == before


@pytest.mark.parametrize('guard', ['HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_KANBAN_TASK', 'UNIFIED_KANBAN_NATIVE_LAUNCH_ACTIVE'])
def test_installed_guards_and_required_provider(tmp_path, guard):
    _, _, _, env = fixture_launch(tmp_path)
    prefix = tmp_path / 'bin'
    prefix.mkdir()
    assert run(prefix, env).returncode == 0
    link = prefix / 'collected-native-agent'
    assert subprocess.run([str(link)], env=env, capture_output=True).returncode == 2
    assert invoke(tmp_path, dict(env, **{guard: ''}), entry=link).returncode == 2
    assert not (tmp_path / 'new-dir').exists()


@pytest.mark.parametrize('damage', ['file', 'foreign', 'broken', 'same-unowned', 'successor', 'same-successor', 'receipt-symlink', 'receipt-acl', 'receipt-mode', 'prefix-mode', 'ancestor-mode', 'prefix-acl', 'ancestor-acl', 'missing-activation', 'disabled', 'native-hash', 'native-acl'])
def test_refusals_preserve_foreign_state(tmp_path, damage):
    native, profile, selector, env = fixture_launch(tmp_path)
    parent = tmp_path / 'parent'
    parent.mkdir()
    prefix = parent / 'bin'
    prefix.mkdir()
    link = prefix / 'collected-native-agent'
    receipt = prefix / RECEIPT
    if damage in {'successor', 'same-successor', 'receipt-symlink', 'receipt-acl', 'receipt-mode'}:
        assert run(prefix, env).returncode == 0
    action = 'install'
    if damage == 'file':
        link.write_text('foreign')
    elif damage in {'foreign', 'broken'}:
        link.symlink_to(native if damage == 'foreign' else tmp_path / 'absent')
    elif damage == 'same-unowned':
        link.symlink_to(ROOT / 'bin/collected-native-agent')
        assert run(prefix, env).returncode == 0
        assert not receipt.exists()
        action = 'uninstall'
    elif damage in {'successor', 'same-successor'}:
        link.rename(prefix / 'original')
        link.symlink_to(native if damage == 'successor' else ROOT / 'bin/collected-native-agent')
        action = 'uninstall'
    elif damage == 'receipt-symlink':
        receipt.rename(prefix / 'original-receipt')
        receipt.symlink_to(prefix / 'original-receipt')
    elif damage.endswith('-mode'):
        {'receipt-mode': receipt, 'prefix-mode': prefix, 'ancestor-mode': parent}[damage].chmod(0o777)
    elif damage.endswith('-acl'):
        target = {'receipt-acl': receipt, 'prefix-acl': prefix, 'ancestor-acl': parent, 'native-acl': native}[damage]
        subprocess.run(['/bin/chmod', '+a', 'everyone allow write', str(target)], check=True)
        fd = os.open(target, os.O_RDONLY)
        try:
            assert ':allow:' in ns.acl_text(fd)
        finally:
            os.close(fd)
    elif damage == 'missing-activation':
        selector.unlink()
    elif damage == 'disabled':
        data = json.loads(selector.read_text())
        data['enabled'] = False
        selector.write_text(json.dumps(data))
    elif damage == 'native-hash':
        native.write_text(native.read_text() + '\n')
    before = {p.name: (p.lstat().st_ino, p.lstat().st_mode) for p in prefix.iterdir()}
    result = run(prefix, env, action)
    assert result.returncode == 2, result.stdout
    assert before == {p.name: (p.lstat().st_ino, p.lstat().st_mode) for p in prefix.iterdir()}
    assert not (tmp_path / 'new-dir').exists()


def modules():
    spec = importlib.util.spec_from_file_location('terminal_installer_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    links = module.load('terminal_links_test', ROOT / 'scripts/manage_repo_link.py')
    tx = module.load('terminal_tx_test', ROOT / 'scripts/path-transaction.py')
    return module, links, tx


@pytest.mark.parametrize('successor', [False, True])
def test_receipt_failure_rolls_back_only_produced_link(tmp_path, monkeypatch, successor):
    native, _, _, env = fixture_launch(tmp_path)
    prefix = tmp_path / 'bin'
    prefix.mkdir()
    module, links, tx = modules()
    link = prefix / 'collected-native-agent'
    def fail(*args):
        if successor:
            link.rename(prefix / 'retained-original')
            link.symlink_to(native)
        raise OSError('injected receipt failure')
    monkeypatch.setattr(module, '_write_receipt', fail)
    args = SimpleNamespace(prefix=str(prefix), profile_home=env['HERMES_HOME'], provider='codex', action='install', dry_run=False)
    with pytest.raises(OSError, match='injected'):
        module.operate(args, ns, launch, links, tx)
    if successor:
        assert os.readlink(link) == str(native)
    else:
        assert list(prefix.iterdir()) == []


@pytest.mark.parametrize('prefix', ['/', '//', '/./', 'relative', '/tmp/../bad'])
def test_unsafe_prefix_refused(tmp_path, prefix):
    _, _, _, env = fixture_launch(tmp_path)
    assert run(prefix, env).returncode == 2


def test_missing_prefix_is_not_created(tmp_path):
    _, _, _, env = fixture_launch(tmp_path)
    prefix = tmp_path / 'missing'
    assert run(prefix, env).returncode == 2
    assert not prefix.exists()


@pytest.mark.parametrize('successor', [False, True])
def test_uninstall_receipt_failure_restores_owned_inode(tmp_path, monkeypatch, successor):
    native, _, _, env = fixture_launch(tmp_path)
    prefix = tmp_path / 'bin'
    prefix.mkdir()
    assert run(prefix, env).returncode == 0
    link = prefix / 'collected-native-agent'
    identity = link.lstat().st_ino
    module, links, tx = modules()
    def fail(*args):
        if successor:
            link.symlink_to(native)
        raise OSError('injected retirement failure')
    monkeypatch.setattr(tx, '_discard_operation_receipt', fail)
    args = SimpleNamespace(prefix=str(prefix), profile_home=env['HERMES_HOME'], provider='codex', action='uninstall', dry_run=False)
    with pytest.raises((OSError, RuntimeError)):
        module.operate(args, ns, launch, links, tx)
    if successor:
        assert os.readlink(link) == str(native)
    else:
        assert link.lstat().st_ino == identity
        assert sorted(p.name for p in prefix.iterdir()) == sorted(['collected-native-agent', RECEIPT])
        assert run(prefix, env, 'uninstall').returncode == 0


def test_recovery_residue_refuses_without_cleanup(tmp_path):
    _, _, _, env = fixture_launch(tmp_path)
    prefix = tmp_path / 'bin'
    prefix.mkdir()
    residue = prefix / '.collected-native-recovery-interrupted'
    residue.write_text('preserve')
    assert run(prefix, env).returncode == 2
    assert run(prefix, env, 'uninstall').returncode == 2
    assert residue.read_text() == 'preserve'
    assert list(prefix.iterdir()) == [residue]
