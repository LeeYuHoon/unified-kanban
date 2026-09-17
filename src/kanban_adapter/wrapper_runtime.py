"""3.9 부트스트랩에서 setup release의 3.11+ 런타임으로 진입한다."""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import subprocess
import sys

from .compatibility import check_hermes_compatibility, read_carried_commits
from .release_layout import normalize_agent_repo, release_directory


def _failure(module: str, code: str) -> int:
    """입력이나 예외 본문 없이 훅은 계속하고 명시적 CLI는 실패한다."""
    if module == "claude_hook_entry":
        from .claude_hook_entry import report_failure
        report_failure(sys.argv[2] if len(sys.argv) > 2 else "unknown", code)
        return 0
    detail = "unsupported Hermes; details redacted" if code == "compatibility-rejected" else "details redacted"
    print("unified-kanban: " + code + " (" + detail + ")", file=sys.stderr)
    return 1 if module == "cli" else 0


def main(*, selected_runtime: bool = False) -> int:
    """setup이 검증한 release의 Python만 본문 실행에 사용한다."""
    module = sys.argv[1]
    if module not in {"claude_hook_entry", "codex_hook", "cli"}:
        return 2
    try:
        compatible, _ = check_hermes_compatibility()
        if not compatible:
            return _failure(module, "compatibility-rejected")
        source = Path(__file__).resolve().parent.parent
        agent = normalize_agent_repo(os.environ.get(
            "HERMES_AGENT_REPO", str(Path.home() / ".hermes/hermes-agent")))
        prefix = release_directory(agent, read_carried_commits()[-1]) / "venv"
        selected = prefix / "bin/python"
        if selected_runtime:
            if sys.version_info < (3, 11) or Path(sys.executable) != selected:
                return _failure(module, "collection-failed")
            sys.argv = [module] + sys.argv[2:]
            runpy.run_module("kanban_adapter." + module, run_name="__main__")
            return 0
        # setup은 adapter .venv를 만들지 않는다. 실제 release의 venv가 설치 계약이다.
        candidate = str(selected)
        if os.access(candidate, os.X_OK):
            probe = subprocess.run(
                [candidate, "-I", "-c", "import sys; sys.exit(sys.version_info < (3, 11))"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False)
            if probe.returncode:
                return _failure(module, "collection-failed")
            entry = ('import sys; sys.path.insert(0,sys.argv.pop(1)); '
                     'from kanban_adapter.wrapper_runtime import main; '
                     'sys.exit(main(selected_runtime=True))')
            os.execv(candidate, [candidate, "-I", "-c", entry, str(source)]
                     + sys.argv[1:])
    except Exception:
        return _failure(module, "collection-failed")
    return _failure(module, "collection-failed")


if __name__ == "__main__":
    raise SystemExit(main())