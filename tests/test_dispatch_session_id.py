"""Tests that handle_function_call forwards session_id into registry.dispatch."""

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_registry(captured: dict):
    """Return a mock registry whose dispatch records the kwargs it receives."""
    registry = MagicMock()

    def _dispatch(name, args, **kwargs):
        captured.update(kwargs)
        return json.dumps({"result": "ok"})

    registry.dispatch.side_effect = _dispatch
    return registry


class TestSessionIdForwarding:

    @pytest.mark.parametrize(
        ("tool_name", "args"),
        [
            ("web_search", {"query": "test"}),
            ("execute_code", {"code": "print(1)"}),
        ],
    )
    def test_call_context_fields_reach_registry_dispatch(self, tool_name, args):
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call

            handle_function_call(
                tool_name,
                args,
                task_id="task-A",
                tool_call_id="call-A",
                session_id="session-A",
                turn_id="turn-A",
                api_request_id="request-A",
                skip_pre_tool_call_hook=True,
            )

        assert captured["task_id"] == "task-A"
        assert captured["tool_call_id"] == "call-A"
        assert captured["session_id"] == "session-A"
        assert captured["turn_id"] == "turn-A"
        assert captured["api_request_id"] == "request-A"

    def test_standard_path_forwards_session_id(self):
        """registry.dispatch receives session_id on the normal tool path."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {"query": "test"},
                task_id="t1",
                session_id="sess-abc",
                skip_pre_tool_call_hook=True,
            )
        assert captured.get("session_id") == "sess-abc"

    def test_execute_code_path_forwards_session_id(self):
        """registry.dispatch receives session_id on the execute_code path."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "execute_code",
                {"code": "print(1)"},
                task_id="t1",
                session_id="sess-xyz",
                skip_pre_tool_call_hook=True,
            )
        assert captured.get("session_id") == "sess-xyz"

    def test_session_id_default_is_none(self):
        """When session_id is omitted, dispatch receives None."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {"query": "test"},
                task_id="t1",
                skip_pre_tool_call_hook=True,
            )
        assert "session_id" in captured
        assert captured["session_id"] is None

    def test_task_id_still_forwarded(self):
        """Existing task_id forwarding is not broken by this change."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {"query": "test"},
                task_id="task-999",
                session_id="sess-1",
                skip_pre_tool_call_hook=True,
            )
        assert captured.get("task_id") == "task-999"
