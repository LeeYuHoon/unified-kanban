from __future__ import annotations

import ast
import io
from pathlib import Path
import re
import subprocess
import tokenize


REPO = Path(__file__).resolve().parents[1]
HANGUL_RE = re.compile(r"[가-힣]")
ENGLISH_WORD_RE = re.compile(r"[A-Za-z]{3,}")
DIRECTIVE_RE = re.compile(
    r"^#\s*(?:!|noqa\b|type:\s*ignore\b|pragma:\s*no cover\b|fmt:|ruff:|shellcheck\b|pyright:|pylint:|nosec\b)",
    re.IGNORECASE,
)


def tracked_files() -> list[Path]:
    """Git이 관리하는 파일만 검사 대상으로 돌려준다."""
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        check=True,
        capture_output=True,
    ).stdout
    return [REPO / raw.decode("utf-8") for raw in output.split(b"\0") if raw]


def needs_korean(text: str) -> bool:
    """사람이 읽는 영문 설명인지 판단한다."""
    stripped = text.strip()
    return bool(ENGLISH_WORD_RE.search(stripped)) and not DIRECTIVE_RE.match(stripped)


def shell_comment(line: str) -> str | None:
    """인용부호 안의 `#`은 건드리지 않고 셸 주석만 찾는다."""
    single_quoted = False
    double_quoted = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and not single_quoted:
            escaped = True
            continue
        if character == "'" and not double_quoted:
            single_quoted = not single_quoted
            continue
        if character == '"' and not single_quoted:
            double_quoted = not double_quoted
            continue
        if (
            character == "#"
            and not single_quoted
            and not double_quoted
            and (index == 0 or line[index - 1].isspace())
        ):
            return line[index:]
    return None


def test_readme_is_short_and_written_for_new_users() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    assert len(readme.splitlines()) <= 220
    assert len(readme.encode("utf-8")) <= 24_000
    for heading in ("## 무엇을 하는 프로젝트인가요?", "## 설치", "## 사용 방법", "## 삭제"):
        assert heading in readme
    for difficult_term in (
        "descriptor-relative",
        "TOCTOU",
        "same-inode",
        "intermediate ancestry",
        "containing parent",
        "writable ancestor",
        "fsync",
    ):
        assert difficult_term not in readme


def test_readme_hermes_release_matches_manifests() -> None:
    """README의 Hermes 버전과 commit이 배포 manifest와 같은지 확인한다."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    version = (REPO / "patches/hermes-agent-version").read_text(encoding="utf-8").strip()
    supported_upstream = (
        REPO / "patches/hermes-agent-supported-upstream"
    ).read_text(encoding="utf-8").strip()
    carried_commits = [
        line
        for line in (REPO / "patches/hermes-agent-carried-commits").read_text(
            encoding="utf-8"
        ).splitlines()
        if line and not line.startswith("#")
    ]
    carried_head = carried_commits[-1]

    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
    assert f"Hermes Agent: `{version}`" in readme
    assert f"공식 기반 commit: `{supported_upstream}`" in readme
    assert f"Unified Kanban release commit: `{carried_head}`" in readme


def test_python_comments_and_docstrings_include_korean() -> None:
    failures: list[str] = []
    for path in tracked_files():
        if path.suffix != ".py":
            continue
        source = path.read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT or not needs_korean(token.string):
                continue
            if not HANGUL_RE.search(token.string):
                failures.append(f"{path.relative_to(REPO)}:{token.start[0]} 주석")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            docstring = ast.get_docstring(node, clean=False)
            if docstring and ENGLISH_WORD_RE.search(docstring) and not HANGUL_RE.search(docstring):
                failures.append(f"{path.relative_to(REPO)}:{getattr(node, 'lineno', 1)} docstring")

    assert not failures, "한글 설명이 없는 Python 주석/docstring:\n" + "\n".join(failures)


def test_shell_comments_include_korean() -> None:
    failures: list[str] = []
    for path in tracked_files():
        if path.suffix not in {".sh", ".bash"} and path.parent.name != "bin":
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            comment = shell_comment(line)
            if comment is None or not needs_korean(comment):
                continue
            if not HANGUL_RE.search(comment):
                failures.append(f"{path.relative_to(REPO)}:{line_number}")

    assert not failures, "한글 설명이 없는 셸 주석:\n" + "\n".join(failures)


def test_config_comments_include_korean() -> None:
    failures: list[str] = []
    for path in tracked_files():
        if path.suffix not in {".yml", ".yaml", ".toml"}:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            comment = shell_comment(line)
            if comment is None or not needs_korean(comment):
                continue
            if not HANGUL_RE.search(comment):
                failures.append(f"{path.relative_to(REPO)}:{line_number}")

    assert not failures, "한글 설명이 없는 설정 파일 주석:\n" + "\n".join(failures)
