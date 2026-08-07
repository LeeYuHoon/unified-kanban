from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kanban_adapter.claude_hook import handle_event
from kanban_adapter.codex_hook import normalize_payload


@dataclass
class FakeAdapter:
    calls: list[tuple[list[str], Path]] = field(default_factory=list)

    def __call__(self, argv: list[str], cwd: Path) -> str:
        self.calls.append((argv, cwd))
        return "t_abcdef12\n" if argv[0] == "start" else ""


def test_codex_alias_payload_creates_and_completes_card(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    adapter = FakeAdapter()

    prompt = normalize_payload({
        "sessionID": "codex-session",
        "directory": str(project),
        "input": "Codex 테스트 작업",
    })
    handle_event(
        "prompt", prompt, adapter=adapter, cache_dir=cache, source="codex"
    )
    assert adapter.calls[0] == (
        ["start", "--title", "Codex 테스트 작업", "--source", "codex"],
        project.resolve(),
    )

    stop = normalize_payload({
        "sessionId": "codex-session",
        "result": "작업 완료",
    })
    handle_event("stop", stop, adapter=adapter, cache_dir=cache, source="codex")
    assert adapter.calls[-1] == (
        [
            "done", "--task", "t_abcdef12",
            "--result=작업 완료", "--summary=작업 완료",
        ],
        project.resolve(),
    )
    assert list(cache.glob("*.json")) == []
