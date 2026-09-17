import json
import os
import subprocess
import sys
import pytest
from test_tui_prebuilt_builder import helper


def closure(tmp_path):
    h = helper()
    root = tmp_path / 'release'
    (root/'tui-runtime/app').mkdir(parents=True)
    for name in ('node', 'app/entry.js', 'app/package.json'):
        p = root/'tui-runtime'/name
        p.write_text('fixture')
        p.chmod(0o700 if name == 'node' else 0o600)
    record = dict(schema=1, kind='unified-kanban-prebuilt-tui', node='tui-runtime/node', entry='tui-runtime/app/entry.js', cwd='tui-runtime/app', files=h._tui_closure_inventory(root))
    receipt = root/'.hermes-tui-runtime.json'
    receipt.write_text(json.dumps(record)); receipt.chmod(0o600)
    return h, root, receipt, record


def test_walk_errors_fail_closed(tmp_path, monkeypatch):
    h, root, _, _ = closure(tmp_path)
    def walk(*a, **kw):
        if kw.get('onerror'):
            kw['onerror'](PermissionError('denied'))
        return iter([])
    monkeypatch.setattr(h.os, 'walk', walk)
    with pytest.raises((OSError, RuntimeError)):
        h._tui_closure_inventory(root)


@pytest.mark.parametrize('mutation', ['boolean-schema', 'duplicate-key'])
def test_receipt_parser_is_strict(tmp_path, mutation):
    h, root, receipt, record = closure(tmp_path)
    text = json.dumps(record)
    if mutation == 'boolean-schema':
        text = text.replace('"schema": 1', '"schema": true')
    else:
        text = text.replace('{', '{"schema": 0, ', 1)
    receipt.write_text(text)
    with pytest.raises(RuntimeError):
        h.verify_release_tui(root)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS ACL')
@pytest.mark.parametrize('target', ['tui-runtime', 'tui-runtime/app/entry.js', '.hermes-tui-runtime.json'])
def test_acl_is_rejected(tmp_path, target):
    h, root, _, _ = closure(tmp_path)
    path = root/target
    subprocess.run(['/bin/chmod', '+a', 'everyone allow write', str(path)], check=True)
    try:
        with pytest.raises(RuntimeError, match='ACL'):
            h.verify_release_tui(root)
    finally:
        subprocess.run(['/bin/chmod', '-N', str(path)], check=True)


@pytest.mark.parametrize('payload', [b'{"schema":1,"x":NaN}', b'{"schema":1,"x":Infinity}', b'{"schema":1,"x":'+b'['*2000+b'0'+b']'*2000+b'}', b'{"schema":1}'+b' '*1048576])
def test_bounded_strict_json(tmp_path, payload):
    h = helper()
    with pytest.raises(ValueError):
        h._strict_tui_json(payload)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS ACL')
def test_deny_only_acl_allowed(tmp_path):
    h, root, _, _ = closure(tmp_path)
    path = root/'tui-runtime/app/entry.js'
    subprocess.run(['/bin/chmod', '+a', 'everyone deny write', str(path)], check=True)
    try:
        h.verify_release_tui(root)
    finally:
        subprocess.run(['/bin/chmod', '-N', str(path)], check=True)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS ACL')
def test_release_root_acl_rejected(tmp_path):
    h, root, _, _ = closure(tmp_path)
    subprocess.run(['/bin/chmod', '+a', 'everyone allow write', str(root)], check=True)
    try:
        with pytest.raises(RuntimeError):
            h.verify_release_tui(root)
    finally:
        subprocess.run(['/bin/chmod', '-N', str(root)], check=True)


def test_exact_size_receipt_and_escaped_names(tmp_path):
    h, root, receipt, record = closure(tmp_path)
    strange = root/'tui-runtime/app'/'quoted"\\[name].js'
    strange.write_text('fixture')
    record['files'] = h._tui_closure_inventory(root)
    raw = json.dumps(record).encode()
    receipt.write_bytes(raw + b' ' * (1048576 - len(raw)))
    assert h.verify_release_tui(root) == record
    receipt.write_bytes(receipt.read_bytes() + b' ')
    with pytest.raises(RuntimeError):
        h.verify_release_tui(root)


def test_acl_lookup_failure_denied(tmp_path, monkeypatch):
    h, root, _, _ = closure(tmp_path)
    def fail(fd):
        raise OSError('ACL query failed')
    monkeypatch.setattr(h, '_tui_acl_text', fail)
    with pytest.raises(RuntimeError):
        h.verify_release_tui(root)


def test_acl_and_content_use_same_open_fd(tmp_path, monkeypatch):
    h, root, _, _ = closure(tmp_path)
    target = root/'tui-runtime/app/entry.js'
    real = h._tui_acl_text
    identity = target.stat().st_ino
    def swap(fd):
        if os.fstat(fd).st_ino == identity:
            replacement = target.with_name('replacement')
            replacement.write_text('attacker')
            replacement.replace(target)
        return real(fd)
    monkeypatch.setattr(h, '_tui_acl_text', swap)
    # 검사한 inode의 이름 변경은 ctime을 바꾸므로 새 경로를 읽지 않고 실패한다.
    with pytest.raises(RuntimeError):
        h.verify_release_tui(root)


def test_unreadable_subtree_denied(tmp_path):
    h, root, _, _ = closure(tmp_path)
    hidden = root/'tui-runtime/hidden'
    hidden.mkdir(); hidden.chmod(0)
    try:
        with pytest.raises(RuntimeError):
            h.verify_release_tui(root)
    finally:
        hidden.chmod(0o700)
