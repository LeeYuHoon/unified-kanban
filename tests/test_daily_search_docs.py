"""검색·날짜 기능의 사용자 안내와 날짜 계약을 유지한다."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_daily_search_documentation_contract():
    readme = (ROOT / "README.md").read_text()
    assert "전체기간" in readme
    assert "오늘" in readme
    assert "docs/daily-search.md" in readme
    doc = (ROOT / "docs/daily-search.md").read_text()
    for term in ("/api/plugins/kanban/daily", "Asia/Seoul", "[start,end)",
                 "usage_at", "생성일 추정", "undated_events", "HERMES_KANBAN_DB",
                 "보관", "0", "N/A", "시연 데이터", "운영 미적용"):
        assert term in doc


def test_daily_public_docs_keep_machine_paths_private_and_date_candidate_claims():
    for name in ("README.md", "docs/daily-search.md", "docs/daily-search-verification.md"):
        text = (ROOT / name).read_text()
        assert "/Users/" not in text
    doc = (ROOT / "docs/daily-search.md").read_text()
    assert "2026-09-09T08:21:37Z" in doc
    assert "로컬 CLI" in doc
    assert "암호학적" in doc
