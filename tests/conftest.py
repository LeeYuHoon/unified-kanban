from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def clear_hermes_auxiliary_process_context(monkeypatch) -> None:
    """Keep the parent-process test baseline stable inside delegated CI workers."""
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
