"""잘못된 필드 집합은 정책 잠금이나 비밀 파일 접근 전에 거부한다."""
import json
import pytest
from kanban_adapter import conversation_runtime as runtime


@pytest.mark.parametrize("change", ["minimal", "policy_file", "authority_secret_file", "kernel_secret_file", "kernel_receipt", "binding_root", "principal_board_grants", "provider_roots", "extra"])
def test_invalid_fields_rejected_before_policy_access(tmp_path, monkeypatch, change):
    root = tmp_path.resolve()
    root.chmod(0o700)
    payload = dict(schema_version=1, enabled=True,
                   policy_file=str(root / "policy.json"),
                   authority_secret_file=str(root / "authority.key"),
                   kernel_secret_file=str(root / "kernel.key"), kernel_receipt={},
                   binding_root=str(root / "bindings"), principal_board_grants={}, provider_roots={})
    if change == "minimal":
        payload = dict(schema_version=1, enabled=True)
    elif change == "extra":
        payload["unexpected"] = True
    else:
        del payload[change]
    path = root / "runtime.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    def forbidden(*args, **kwargs):
        pytest.fail("필드 검증 전에 정책 잠금 또는 비밀 파일에 접근함")
    monkeypatch.setattr(runtime, "policy_lease", forbidden)
    monkeypatch.setattr(runtime, "_private_bytes", forbidden)
    with pytest.raises(ValueError, match="conversation runtime config fields are invalid"):
        runtime._build(path)
    assert sorted(p.name for p in root.iterdir()) == ["runtime.json"]
