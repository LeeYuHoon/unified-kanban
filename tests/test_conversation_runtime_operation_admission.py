"""runtime에서 얻은 객체를 API가 보유해도 작업 admission을 다시 확인한다."""
import json

import pytest

from kanban_adapter import conversation_runtime as runtime
from test_conversation_grant_existing_blockers import existing, setup  # noqa: F401


@pytest.mark.parametrize("change", ["disable", "replace-identical", "policy"])
def test_retained_service_denies_before_membership(existing, monkeypatch, change):
    path = existing / "runtime.json"
    service = runtime.get_conversation_service()
    assert service is not None
    touched = []
    monkeypatch.setattr(service, "task_membership", lambda *_: touched.append(True))
    if change == "disable":
        value = json.loads(path.read_bytes())
        value["enabled"] = False
        path.write_text(json.dumps(value))
    elif change == "replace-identical":
        raw = path.read_bytes()
        path.rename(path.with_suffix(".old"))
        path.write_bytes(raw)
        path.chmod(0o600)
    else:
        from dataclasses import replace
        policy = service.policies.load()
        service.policies.replace(replace(policy, generation=policy.generation + 1), expected_generation=policy.generation)
    assert not service.authorizes_principal("github:fixture-user", "test-board")
    with pytest.raises(PermissionError, match="runtime|policy"):
        service.get_parent_page(principal_id="github:fixture-user", board="test-board", task="fixture", cursor=None, limit=10)
    assert touched == []
