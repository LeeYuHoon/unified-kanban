"""모의 검증 대신 실제 완료된 TUI 권한을 갖도록 테스트 소스 픽스처를 구성한다."""
import json
from test_hermes_release_manager import build_completed_release


def complete_tui(h, layout):
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
    h._publish_completion_receipt(layout, old['upstream'], old['carried'])
    return layout


def completed_layout(h, checkout, work):
    return build_completed_release(h, checkout, work)
