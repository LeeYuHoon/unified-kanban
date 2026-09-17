from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_user_documentation_does_not_publish_dashboard_cost_or_pricing_guidance():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/token-cost.md" not in readme
    assert "토큰 옆 비용" not in readme
    assert not (ROOT / "docs/token-cost.md").exists()