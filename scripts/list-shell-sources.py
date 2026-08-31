#!/usr/bin/env python3
"""이 저장소가 배포하는 모든 셸 소스를 각각의 문법 검사기와 함께 나열한다.

CI 셸 게이트는 예전에 파일을 수작업으로 지정했는데, 그 때문에 배포된 스크립트
세 개가 조용히 검사 대상에서 빠졌다. 따라서 발견은 저장소 자체에서 이루어진다:
Git이 추적하는 모든 것과, 존재하며 무시되지 않은 모든 것을 대상으로 하므로,
새 스크립트는 누군가 목록에 추가하는 것을 기억하는 순간이 아니라 존재하는
순간부터 게이트된다.

출력은 한 줄에 하나의 ``<checker>\\t<path>`` 레코드이며 정렬되어 있으므로, CI
bash에서 배열 없이 평범한 ``while IFS=$'\\t' read -r`` 루프로 소비할 수 있다.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL_SUFFIXES = (".sh", ".bash")
# 지원 플랫폼에서 검사기를 사용할 수 있는 방언만 포함한다. 그 외에는 반드시
# 요란하게 실패해야 한다: 아무도 파싱할 수 없는 스크립트야말로 이 열거기가
# 메우기 위해 존재하는 바로 그 공백이다.
SYNTAX_CHECKERS = {"sh": "sh", "bash": "bash"}
UNCHECKABLE_SHELLS = ("dash", "ksh", "zsh", "csh", "tcsh", "fish")


def syntax_checker(name: str, head: bytes) -> str | None:
    """후보 파일 하나에 대한 검사기를 반환하고, 셸이 아니면 ``None``을 반환한다."""
    command = ""
    if head.startswith(b"#!"):
        words = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split()
        if words:
            command = Path(words[0]).name
            if command == "env" and len(words) > 1:
                command = Path(words[1]).name
    if command in UNCHECKABLE_SHELLS:
        raise SystemExit(f"no shell syntax checker is configured for {command}: {name}")
    if command in SYNTAX_CHECKERS:
        return SYNTAX_CHECKERS[command]
    if command:
        return None
    return "bash" if name.endswith(SHELL_SUFFIXES) else None


def candidate_paths() -> list[str]:
    """체크아웃 또는 정규화된 소스 아카이브에서 배포되는 모든 파일을 반환한다."""
    if not (ROOT / ".git").exists():
        return _archive_candidate_paths()
    paths: set[str] = set()
    for arguments in (
        ["ls-files", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        listed = subprocess.run(
            ["git", "-C", str(ROOT), *arguments],
            capture_output=True,
            check=True,
        ).stdout
        paths.update(record.decode("utf-8", "surrogateescape") for record in listed.split(b"\0") if record)
    return sorted(paths)


def _archive_ignore_rules() -> list[tuple[bool, bool, str]]:
    """릴리스에서 사용하는, 의도적으로 작게 유지된 루트 ``.gitignore`` 부분집합을 파싱한다."""
    ignore = ROOT / ".gitignore"
    try:
        lines = ignore.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"source archive has no readable .gitignore: {exc}") from exc
    rules = []
    for number, raw in enumerate(lines, 1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        negated = value.startswith("!")
        if negated:
            value = value[1:]
        directory_only = value.endswith("/")
        if directory_only:
            value = value[:-1]
        if not value or "/" in value or "\\" in value or "**" in value:
            raise SystemExit(
                f"unsupported .gitignore rule for archive discovery at line {number}: {raw!r}"
            )
        rules.append((negated, directory_only, value))
    return rules


def _archive_ignored(relative: Path, *, directory: bool, rules: list[tuple[bool, bool, str]]) -> bool:
    ignored = False
    components = relative.parts
    for negated, directory_only, pattern in rules:
        if directory_only:
            candidates = components if directory else components[:-1]
            matched = any(fnmatch.fnmatchcase(component, pattern) for component in candidates)
        else:
            matched = not directory and fnmatch.fnmatchcase(relative.name, pattern)
        if matched:
            ignored = not negated
    return ignored


def _archive_candidate_paths() -> list[str]:
    """생성되어 무시되는 출력물을 제외하면서 정규화된 아카이브를 순회한다."""
    rules = _archive_ignore_rules()
    paths = []
    for directory, names, files in os.walk(ROOT):
        base = Path(directory).relative_to(ROOT)
        kept = []
        for name in names:
            relative = base / name
            if name == ".git" or _archive_ignored(relative, directory=True, rules=rules):
                continue
            kept.append(name)
        names[:] = kept
        for name in files:
            relative = base / name
            if not _archive_ignored(relative, directory=False, rules=rules):
                paths.append(str(relative))
    return sorted(paths)


def shell_sources() -> list[tuple[str, str]]:
    discovered = []
    for relative in candidate_paths():
        path = ROOT / relative
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                head = handle.read(256)
        except OSError as exc:
            raise SystemExit(f"could not read candidate shell source {relative}: {exc}") from exc
        checker = syntax_checker(relative, head)
        if checker is not None:
            discovered.append((checker, relative))
    return discovered


def main() -> int:
    discovered = shell_sources()
    if not discovered:
        print("no shell sources discovered; the gate would check nothing", file=sys.stderr)
        return 1
    for checker, relative in discovered:
        print(f"{checker}\t{relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
