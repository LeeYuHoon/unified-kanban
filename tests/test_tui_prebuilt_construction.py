"""준비 순서와 완료 영수증의 TUI 결속을 검증한다."""
from test_tui_prebuilt_builder import helper


def test_prepare_builds_tui_before_seal(tmp_path, monkeypatch):
    h = helper()
    release = tmp_path / ('release-' + '1' * 40)
    layout = h.ReleaseLayout(tmp_path, release, tmp_path / 'current')
    events = []
    def source(*args, **kwargs):
        (release / 'ui-tui').mkdir(parents=True)
        (release / 'ui-tui/package.json').write_text('{}')
        (release / 'venv/bin').mkdir(parents=True)
        launcher = release / 'venv/bin/hermes'
        launcher.write_text('fixture')
        launcher.chmod(0o700)
    monkeypatch.setattr(h, 'build_source_release', source)
    monkeypatch.setattr(h, 'build_release_tui', lambda *a, **k: events.append('tui'))
    monkeypatch.setattr(h, '_precompile_release_bytecode', lambda *a: events.append('bytecode'))
    monkeypatch.setattr(h, '_publish_bytecode_fingerprint', lambda *a: events.append('fingerprint'))
    monkeypatch.setattr(h, '_publish_completion_receipt', lambda *a: events.append('seal'))
    h.prepare_release(layout, upstream='2'*40, carried='1'*40, bundle=tmp_path/'bundle', npm=tmp_path/'npm')
    assert events == ['tui', 'bytecode', 'fingerprint', 'seal']


def test_completion_requires_and_binds_tui_receipt(tmp_path, monkeypatch):
    import hashlib
    import pytest
    h = helper()
    release = tmp_path / ('release-' + '1' * 40)
    (release / '.git').mkdir(parents=True)
    (release / 'ui-tui').mkdir()
    (release / 'ui-tui/package.json').write_text('{}')
    for name in ('config', 'HEAD'):
        (release / '.git' / name).write_text('fixture')
    layout = h.ReleaseLayout(tmp_path, release, tmp_path/'current')
    monkeypatch.setattr(h, '_run_git', lambda *a: '')
    monkeypatch.setattr(h, 'case_collisions', lambda *a: ())
    monkeypatch.setattr(h, '_materialized_case_collisions', lambda *a, **k: [])
    monkeypatch.setattr(h, '_bytecode_inventory', lambda *a: {})
    with pytest.raises(RuntimeError, match='TUI'):
        h._completion_payload(layout, '2'*40, '1'*40)
    receipt = release / '.hermes-tui-runtime.json'
    receipt.write_text('{}')
    monkeypatch.setattr(h, 'verify_release_tui', lambda *a: {})
    payload = h._completion_payload(layout, '2'*40, '1'*40)
    assert payload['tui_receipt_sha256'] == hashlib.sha256(receipt.read_bytes()).hexdigest()
