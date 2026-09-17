"""고정된 배포 commit의 Git 트리로 공식 원본 두 개를 검증한다.

얇은 bundle은 오프라인 해제에 기반 객체가 필요하므로 전체 소스 대신
필수 원본과 commit/tree 증명만 보관한다. 원본은 번역하지 않는 .py.txt 자료이며,
검증한 바이트만 임시 폴더에 .py로 복원한다. 갱신 절차는
fixtures/oauth-native/README.md를 참고한다.
"""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile


_MATERIALIZED = tempfile.TemporaryDirectory(prefix="oauth-native-")


def native_source(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    source = root / 'tests/fixtures/oauth-native'
    proof = json.loads((source / 'provenance.json').read_text())
    spec = importlib.util.spec_from_file_location('oauth_bundle_verifier', root / 'scripts/verify-carried-bundle.py')
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    metadata = verifier.verify(root / 'patches')
    assert metadata['sha256'] == proof['bundle_sha256'], 'refresh native source fixture for bundle'
    tip = (root / 'patches/hermes-agent-carried-commits').read_text().splitlines()[-1]
    assert tip == proof['tip']

    def oid(kind, data):
        return hashlib.sha1(kind.encode() + b' ' + str(len(data)).encode() + b'\0' + data).hexdigest()

    def object_data(key, kind):
        obj = proof['objects'][key]
        data = base64.b64decode(obj['data'], validate=True)
        assert obj['type'] == kind and oid(kind, data) == key
        return data

    def tree_entry(tree, name):
        data = object_data(tree, 'tree')
        while data:
            entry, rest = data.split(b'\0', 1)
            key, data = rest[:20].hex(), rest[20:]
            if entry.split(b' ', 1)[1].decode() == name:
                return key
        raise AssertionError(f'missing pinned tree entry: {name}')

    commit = object_data(tip, 'commit')
    tree = commit.splitlines()[0].removeprefix(b'tree ').decode()
    blobs = {}
    for path in ('hermes_constants.py', 'hermes_cli/config_defaults.py'):
        key = tree
        for name in path.split('/'):
            key = tree_entry(key, name)
        data = (source / (path + '.txt')).read_bytes()
        assert oid('blob', data) == key, f'native blob drift: {path}'
        blobs[path] = data
    # 모든 원본을 검증한 뒤에만 임시 소스를 만들고 프로세스 종료 시 정리한다.
    materialized = Path(tempfile.mkdtemp(dir=_MATERIALIZED.name))
    for path, data in blobs.items():
        target = materialized / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return materialized


def yaml_dependency_root():
    # 개발자 캐시 경로 대신 현재 설치된 테스트 의존성의 위치를 찾는다.
    import yaml
    import dotenv
    return list(dict.fromkeys(str(Path(module.__file__).resolve().parent.parent)
                              for module in (yaml, dotenv)))
