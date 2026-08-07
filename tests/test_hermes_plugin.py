from __future__ import annotations

import importlib.util
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "integrations/hermes/hermes-kanban"
PLUGIN = PLUGIN_DIR / "__init__.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("hermes_kanban_user_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_manifest_keeps_hermes_kanban_id() -> None:
    manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    lines = [line.strip() for line in manifest.splitlines()]
    assert "name: hermes-kanban" in lines
    for hook in (
        "pre_llm_call",
        "post_tool_call",
        "post_llm_call",
        "post_api_request",
        "subagent_start",
        "on_session_end",
    ):
        assert f"- {hook}" in lines


def test_plugin_registers_turn_hooks_and_forwards_payload() -> None:
    plugin = load_plugin()
    registered = {}

    class Context:
        def register_hook(self, name, callback):
            registered[name] = callback

    calls = []

    class Tracker:
        def __getattr__(self, name):
            def record(**kwargs):
                calls.append((name, kwargs))

            return record

    plugin._tracker = Tracker()
    plugin.register(Context())

    registered["pre_llm_call"](
        session_id="s1", turn_id="u1", user_message="hello", platform="cli",
        model="claude-fable-5", conversation_history=["secret"], ignored="value",
    )
    registered["on_session_end"](
        session_id="s1", turn_id="u1", completed=True, interrupted=False,
        ignored="value",
    )

    assert calls == [
        ("start", {
            "session_id": "s1", "turn_id": "u1",
            "user_message": "hello", "platform": "cli",
            "model": "claude-fable-5",
        }),
        ("finish", {
            "session_id": "s1", "turn_id": "u1",
            "completed": True, "interrupted": False,
        }),
    ]


def test_plugin_forwards_usage_hooks_without_raw_payloads() -> None:
    plugin = load_plugin()
    registered = {}
    calls = []

    class Context:
        def register_hook(self, name, callback):
            registered[name] = callback

    class Tracker:
        def __getattr__(self, name):
            def record(**kwargs):
                calls.append((name, kwargs))

            return record

    plugin._tracker = Tracker()
    plugin.register(Context())

    registered["post_tool_call"](
        tool_name="skill_view", args={"name": "axolotl"}, result="secret-result",
        session_id="s1", turn_id="u1", status="ok", duration_ms=42,
        middleware_trace=[{"secret": 1}],
    )
    registered["subagent_start"](
        parent_session_id="s1", parent_turn_id="u1", child_role="leaf",
        child_goal="secret goal", child_session_id="c1",
    )
    registered["post_llm_call"](
        session_id="s1", turn_id="u1", model="gpt-5.6-sol",
        assistant_response="all done", user_message="secret prompt",
        conversation_history=["secret"],
    )
    registered["post_api_request"](
        session_id="s1", turn_id="u1", api_request_id="req-1",
        usage={"input_tokens": 12, "output_tokens": 7},
        messages=["secret prompt"], response={"secret": "payload"},
    )

    assert calls == [
        ("record_tool", {
            "session_id": "s1", "turn_id": "u1",
            "tool_name": "skill_view", "args": {"name": "axolotl"},
        }),
        ("record_subagent", {
            "parent_session_id": "s1", "parent_turn_id": "u1",
            "child_role": "leaf",
        }),
        ("record_turn_result", {
            "session_id": "s1", "turn_id": "u1",
            "assistant_response": "all done", "model": "gpt-5.6-sol",
        }),
        ("record_api_usage", {
            "session_id": "s1", "turn_id": "u1", "api_request_id": "req-1",
            "usage": {"input_tokens": 12, "output_tokens": 7},
        }),
    ]


def test_usage_hooks_fail_open_and_return_none() -> None:
    plugin = load_plugin()
    registered = {}

    class Context:
        def register_hook(self, name, callback):
            registered[name] = callback

    class ExplodingTracker:
        def __getattr__(self, name):
            def boom(**kwargs):
                raise RuntimeError("hermes backend down")

            return boom

    plugin._tracker = ExplodingTracker()
    plugin.register(Context())

    assert registered["post_tool_call"](tool_name="skill_view", args={}) is None
    assert registered["subagent_start"](parent_session_id="s1") is None
    assert registered["post_llm_call"](session_id="s1") is None


def test_plugin_hooks_fail_open_when_tracker_raises() -> None:
    plugin = load_plugin()
    registered = {}

    class Context:
        def register_hook(self, name, callback):
            registered[name] = callback

    class ExplodingTracker:
        def start(self, **kwargs):
            raise RuntimeError("hermes backend down")

        def finish(self, **kwargs):
            raise RuntimeError("hermes backend down")

    plugin._tracker = ExplodingTracker()
    plugin.register(Context())

    assert registered["pre_llm_call"](
        session_id="s1", turn_id="u1", user_message="hi", platform="cli",
    ) is None
    assert registered["on_session_end"](
        session_id="s1", turn_id="u1", completed=True, interrupted=False,
    ) is None


def test_plugin_uses_unified_turn_tracker() -> None:
    plugin = load_plugin()
    from kanban_adapter.hermes_hook import TurnTracker

    assert isinstance(plugin._tracker, TurnTracker)


def test_plugin_registers_no_hooks_when_hermes_upstream_is_unsupported() -> None:
    plugin = load_plugin()
    assert hasattr(plugin, "_compatibility_check")
    setattr(plugin, "_compatibility_check", lambda: (False, "unsupported Hermes upstream"))
    registered = {}

    class Context:
        def register_hook(self, name, callback):
            registered[name] = callback

    plugin.register(Context())

    assert registered == {}


def test_plugin_stops_recording_when_hermes_changes_after_registration() -> None:
    plugin = load_plugin()
    assert hasattr(plugin, "_compatibility_check")
    compatibility = iter([(True, ""), (False, "Hermes changed")])
    setattr(plugin, "_compatibility_check", lambda: next(compatibility))
    registered = {}
    calls = []

    class Context:
        def register_hook(self, name, callback):
            registered[name] = callback

    class Tracker:
        def start(self, **kwargs):
            calls.append(kwargs)

    setattr(plugin, "_tracker", Tracker())
    plugin.register(Context())
    registered["pre_llm_call"](
        session_id="s1", turn_id="u1", user_message="do not record",
    )

    assert calls == []
