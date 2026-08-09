"""Conversation-loop boundaries for post-send Responses WebSocket failures."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.codex_responses_ws_transport import (
    GenericWsRejectedError,
    GenericWsStartedError,
)
from run_agent import AIAgent


@pytest.fixture
def ws_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            model="gpt-5",
            api_key="test-key",
            base_url="https://relay.example.com/v1",
            provider="custom",
            requested_provider="custom:relay",
            api_mode="codex_responses",
            responses_transport="auto",
            responses_transport_provider="custom:relay",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._fallback_chain = [{"provider": "openrouter", "model": "fallback/model"}]
    agent._fallback_index = 0
    return agent


@pytest.mark.parametrize(
    "error",
    [
        GenericWsStartedError("failed after send", retryable=True),
        GenericWsRejectedError("relay rejected after send", status_code=400),
    ],
)
def test_post_send_ws_errors_never_activate_conversation_fallback(ws_agent, error):
    """A post-send WS failure must terminate without changing providers."""
    ws_agent._try_activate_fallback = MagicMock(return_value=False)
    with (
        patch.object(ws_agent, "_run_codex_stream", side_effect=error) as run_stream,
        patch.object(ws_agent, "_persist_session"),
        patch.object(ws_agent, "_save_trajectory"),
        patch.object(ws_agent, "_cleanup_task_resources"),
    ):
        result = ws_agent.run_conversation("hello")

    assert run_stream.call_count == 1
    ws_agent._try_activate_fallback.assert_not_called()
    assert result["completed"] is False
    assert result["failed"] is True


def test_post_send_image_rejection_does_not_strip_images_or_replay(ws_agent):
    """A 400 image rejection after WS send cannot enter image recovery."""
    ws_agent._vision_supported = True
    image = {
        "type": "image_url",
        "image_url": {
            "url": (
                "data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        },
    }
    ws_agent._try_activate_fallback = MagicMock(return_value=False)
    error = GenericWsRejectedError(
        "Only 'text' content type is supported",
        status_code=400,
        body={"error": {"message": "Only 'text' content type is supported"}},
    )
    with (
        patch.object(ws_agent, "_run_codex_stream", side_effect=error) as run_stream,
        patch.object(ws_agent, "_persist_session"),
        patch.object(ws_agent, "_save_trajectory"),
        patch.object(ws_agent, "_cleanup_task_resources"),
    ):
        result = ws_agent.run_conversation(
            [{"type": "text", "text": "describe"}, image]
        )

    assert run_stream.call_count == 1
    ws_agent._try_activate_fallback.assert_not_called()
    assert ws_agent._vision_supported is True
    assert result["completed"] is False
