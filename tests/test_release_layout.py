"""릴리스 네임스페이스는 shell 호출자와 Python 호출자에서 동일해야 한다."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from kanban_adapter.release_layout import (
    normalize_agent_repo,
    previous_release_selector,
    release_directory,
    release_root,
    release_selector,
)

ROOT = Path(__file__).resolve().parents[1]
CARRIED = "2" * 40


def load_release_manager():
    helper = ROOT / "scripts" / "hermes-release-manager.py"
    spec = importlib.util.spec_from_file_location("hermes_release_manager", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "value",
    [
        "/home/user/.hermes/hermes-agent",
        "/home/user/.hermes/hermes-agent/",
        "/home/user/.hermes/hermes-agent//",
        "/home/user//.hermes/hermes-agent",
        "/home/user/.hermes/./hermes-agent",
        "/home/user/./.hermes/hermes-agent/./",
    ],
)
def test_equivalent_spellings_share_one_normal_form(value: str) -> None:
    assert str(normalize_agent_repo(value)) == "/home/user/.hermes/hermes-agent"


@pytest.mark.parametrize(
    "value",
    [
        "relative/hermes-agent",
        "",
        "/",
        "//",
        "/home/user/.hermes/hermes-agent/..",
        "/home/user/.hermes/../hermes-agent",
    ],
)
def test_unnormalizable_checkout_paths_fail_closed(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_agent_repo(value)


@pytest.mark.parametrize(
    "value",
    [
        "/home/user/.hermes/hermes-agent",
        "/home/user/.hermes/hermes-agent/",
        "/home/user/.hermes//hermes-agent",
        "/home/user/.hermes/./hermes-agent",
    ],
)
def test_release_root_is_the_sibling_a_shell_would_concatenate(value: str) -> None:
    """정규형에 ``.releases``를 더한 값은 shell이 계산하는 값과 정확히 같다."""
    repo = normalize_agent_repo(value)

    assert str(release_root(value)) == f"{repo}.releases"
    assert str(release_selector(value)) == f"{repo}.releases/current"
    assert str(previous_release_selector(value)) == f"{repo}.releases/previous"
    assert str(release_directory(value, CARRIED)) == f"{repo}.releases/release-{CARRIED}"
    assert release_root(value) != repo
    assert repo not in release_root(value).parents


def test_release_directory_requires_a_full_carried_sha() -> None:
    with pytest.raises(ValueError):
        release_directory("/home/user/hermes-agent", "abc")


@pytest.mark.parametrize(
    "value",
    ["/home/user/hermes-agent", "/home/user/hermes-agent/", "/home/user//hermes-agent"],
)
def test_release_manager_layout_agrees_with_the_shared_normal_form(value: str) -> None:
    helper = load_release_manager()

    layout = helper.release_layout(value, "1" * 40, CARRIED)

    assert layout.root == release_root(value)
    assert layout.selector == release_selector(value)
    assert layout.release == release_directory(value, CARRIED)


def test_release_manager_layout_refuses_traversal_checkout_paths() -> None:
    helper = load_release_manager()

    with pytest.raises(ValueError):
        helper.release_layout("/home/user/hermes-agent/..", "1" * 40, CARRIED)
