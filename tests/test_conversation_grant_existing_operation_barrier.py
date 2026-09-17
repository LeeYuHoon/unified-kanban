"""직접 provenance 호출의 작업 수명 잠금 누락을 재현하는 RED 인수 테스트.

임시 합성 원본/authority만 사용한다. 마지막 정책 재검증의 예외는 이미 수행한
원본 읽기를 취소하지 못한다. 운영 파일이나 다른 lane의 소스는 수정하지 않는다.
"""
from contextlib import contextmanager
from dataclasses import replace

import pytest

from kanban_adapter import codex_file_provenance as native
from test_codex_authenticated_prepare import setup


def test_direct_prepare_must_not_read_source_after_writer_commits(tmp_path, monkeypatch):
    source, service, args, _ = setup(tmp_path)
    original_snapshot = native._snapshot
    original_open = native.open_verified_jsonl_fd
    observations = []

    def interleave_writer(*values, **kwargs):
        # 정책/locator 검증이 끝난 뒤, 직접 module이 원본을 열기 직전에 게시한다.
        # 짧은 _enabled_policy/_locator 잠금으로는 이 창을 직렬화할 수 없다.
        old = service.policies.load()
        service.policies.replace(
            replace(old, generation=old.generation + 1, enabled_boards={}, pair_activated_at_ns={}),
            expected_generation=old.generation,
        )
        assert not service.policies.load().enabled_boards
        return original_snapshot(*values, **kwargs)

    @contextmanager
    def observe_source_open(*values, **kwargs):
        with original_open(*values, **kwargs) as opened:
            observations.append(bool(service.policies.load().enabled_boards))
            yield opened

    monkeypatch.setattr(native, "_snapshot", interleave_writer)
    monkeypatch.setattr(native, "open_verified_jsonl_fd", observe_source_open)
    with pytest.raises(PermissionError, match="policy"):
        native.prepare(service, session="native", source_path=source, **args)
    with pytest.raises(FileNotFoundError):
        service.bindings.get("demo", "task")
    assert not observations, (
        "direct provenance read source after policy writer committed; "
        "final revalidation denies preparation but is not an operation lease"
    )
