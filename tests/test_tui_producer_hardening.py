import subprocess
from pathlib import Path
import pytest
from test_tui_prebuilt_builder import helper


def test_darwin_node_rejects_external_and_relative_dylibs(monkeypatch, tmp_path):
    h = helper()
    monkeypatch.setattr(h.sys, 'platform', 'darwin')
    node = tmp_path / 'node'
    for dependency in ('/opt/homebrew/lib/libfoo.dylib', '@rpath/libfoo.dylib', '/usr/lib/../local/libfoo.dylib'):
        monkeypatch.setattr(h.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 0, f'{node}:\n\t{dependency} (compatibility version 1.0.0)\n', ''))
        with pytest.raises(RuntimeError, match='system'):
            h.verify_node_loader_closure(node, {})
    monkeypatch.setattr(h.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 0, f'{node}:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n', ''))
    h.verify_node_loader_closure(node, {})


def test_builder_detects_tool_replacement_after_version(tmp_path, monkeypatch):
    h = helper()
    release = tmp_path / ('release-' + '1'*40)
    (release / 'ui-tui/scripts').mkdir(parents=True)
    for name in ('package.json', 'package-lock.json', 'ui-tui/package.json', 'ui-tui/scripts/build.mjs'):
        (release / name).write_text('{}')
    node, npm = tmp_path / 'node', tmp_path / 'npm'
    for p in (node, npm):
        p.write_text('#!/bin/sh\nexit 0\n')
        p.chmod(0o700)
    monkeypatch.setattr(h, 'verify_node_loader_closure', lambda *a: None, raising=False)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if '--version' in args:
            node.write_text('#!/bin/sh\nexit 7\n')
            return subprocess.CompletedProcess(args, 0, 'v24.12.0', '')
        pytest.fail('changed tool reached npm construction')
    monkeypatch.setattr(h.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='identity'):
        h.build_release_tui(release, npm=npm, node=node, base_env={})
    assert len(calls) == 1
