"""생성된 실행기로 실제 완료 검증을 수행하며 운영 환경에는 설치하지 않는다."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from test_hermes_release_manager import load_helper, build_completed_release


@pytest.mark.parametrize('command', ['chat', 'dashboard', 'gateway'])
def test_completed_authority_reaches_direct_and_service_parents(tmp_path, command):
    h = load_helper()
    checkout = tmp_path / 'agent'
    checkout.mkdir()
    layout = build_completed_release(h, checkout, tmp_path/'source')
    completion = layout.release / h._COMPLETION_RECEIPT
    old = json.loads(completion.read_bytes())
    completion.unlink()
    (layout.release/'ui-tui').mkdir()
    (layout.release/'ui-tui/package.json').write_text('{}')
    runtime = layout.release/'tui-runtime'
    (runtime/'app').mkdir(parents=True)
    for path, data in [(runtime/'node', b'#!/bin/sh\nexit 0\n'),(runtime/'app/entry.js', b'fixture'),(runtime/'app/package.json', b'{"type":"module"}')]:
        path.write_bytes(data)
        path.chmod(0o700 if path.name == 'node' else 0o600)
    receipt = layout.release/'.hermes-tui-runtime.json'
    receipt.write_text(json.dumps({'schema':1,'kind':'unified-kanban-prebuilt-tui','node':'tui-runtime/node','entry':'tui-runtime/app/entry.js','cwd':'tui-runtime/app','files':h._tui_closure_inventory(layout.release)}))
    receipt.chmod(0o600)
    selected = layout.release/'venv/bin/hermes'
    selected.write_text('#!/bin/sh\n/usr/bin/env\n')
    selected.chmod(0o700)
    h._publish_completion_receipt(layout, old['upstream'], old['carried'])
    expected = h.managed_tui_environment(layout.release)
    layout.selector.write_bytes(h.selector_payload(layout))
    launcher = tmp_path/'hermes'
    launcher.write_bytes(h.launcher_payload(layout, 'absent'))
    launcher.chmod(0o700)
    env = {'HOME':str(tmp_path), 'PATH':'/usr/bin:/bin', 'HERMES_UNIFIED_KANBAN_TUI':'evil', 'HERMES_UNIFIED_KANBAN_RELEASE':'/evil', 'HERMES_UNIFIED_KANBAN_TUI_RECEIPT_SHA256':'0'*64, 'NODE_OPTIONS':'evil', 'LD_PRELOAD':'evil'}
    result = subprocess.run([str(launcher), command], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    observed = dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    assert all(observed[k] == v for k,v in expected.items())
    assert 'NODE_OPTIONS' not in observed and 'LD_PRELOAD' not in observed
    (runtime/'app/entry.js').write_text('changed')
    refused = subprocess.run([str(launcher), command], env=env, capture_output=True, text=True)
    assert refused.returncode == 126 and not refused.stdout
