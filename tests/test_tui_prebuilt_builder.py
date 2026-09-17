"""불변 TUI 빌더 계약 회귀 검증."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pytest


def helper():
    spec = importlib.util.spec_from_file_location('release_tui_builder', Path(__file__).resolve().parents[1] / 'scripts/hermes-release-manager.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_builder_seals_bundle_and_node_without_runtime_modules(tmp_path, monkeypatch):
    h = helper()
    monkeypatch.setattr(h, "verify_node_loader_closure", lambda *a: None)
    release = tmp_path / ('release-' + '1' * 40)
    (release / 'ui-tui/scripts').mkdir(parents=True)
    for name in ('package.json', 'package-lock.json', 'ui-tui/package.json'):
        (release / name).write_text('{}')
    (release / 'ui-tui/scripts/build.mjs').write_text('build')
    node = tmp_path / 'node'
    node.write_text('#!/bin/sh\nprintf "v24.12.0\\n"\n')
    node.chmod(0o700)
    npm = tmp_path / 'npm'
    npm.write_text('#!/bin/sh\nmkdir -p ui-tui/dist node_modules\nprintf "console.log(42)" > ui-tui/dist/entry.js\n')
    npm.chmod(0o700)
    manifest = h.build_release_tui(release, npm=npm, node=node, base_env={'HOME': str(tmp_path)})
    assert manifest == release / '.hermes-tui-runtime.json'
    assert not (release / 'node_modules').exists()
    record = h.verify_release_tui(release)
    assert record['kind'] == 'unified-kanban-prebuilt-tui'
    assert record['files']['tui-runtime/node'] == hashlib.sha256(node.read_bytes()).hexdigest()
    before = h._tree_digest(release)
    assert h.verify_release_tui(release) == record
    assert h._tree_digest(release) == before
    extra = release / 'tui-runtime/app/unreviewed.js'
    extra.write_text('extra')
    with pytest.raises(RuntimeError, match='TUI'):
        h.verify_release_tui(release)
    extra.unlink()
    extra.symlink_to(release / 'tui-runtime/app/entry.js')
    with pytest.raises(RuntimeError, match='TUI'):
        h.verify_release_tui(release)
    extra.unlink()
    os.link(release / 'tui-runtime/app/entry.js', extra)
    with pytest.raises(RuntimeError, match='TUI'):
        h.verify_release_tui(release)
    extra.unlink()
    (release / 'tui-runtime/node').chmod(0o722)
    with pytest.raises(RuntimeError, match='TUI'):
        h.verify_release_tui(release)
    (release / 'tui-runtime/node').chmod(0o700)
    assert h._tree_digest(release) == before
    (release / 'tui-runtime/app/entry.js').write_text('changed')
    with pytest.raises(RuntimeError, match='TUI'):
        h.verify_release_tui(release)
    (release / 'tui-runtime/app/entry.js').unlink()
    with pytest.raises(RuntimeError, match='TUI'):
        h.verify_release_tui(release)
