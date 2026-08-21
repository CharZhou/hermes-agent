"""Conversation-loop boundaries for post-send Responses WebSocket failures."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault(
    "dotenv",
    SimpleNamespace(load_dotenv=lambda *_args, **_kwargs: False),
)
sys.modules.setdefault(
    "requests",
    SimpleNamespace(
        Session=type("Session", (), {}),
        get=lambda *_args, **_kwargs: None,
        post=lambda *_args, **_kwargs: None,
        exceptions=SimpleNamespace(RequestException=Exception),
    ),
)
sys.modules.setdefault(
    "httpx",
    SimpleNamespace(
        RemoteProtocolError=type("RemoteProtocolError", (Exception,), {}),
        ReadTimeout=type("ReadTimeout", (Exception,), {}),
        ConnectError=type("ConnectError", (Exception,), {}),
    ),
)
sys.modules.setdefault(
    "openai",
    SimpleNamespace(APIConnectionError=type("APIConnectionError", (Exception,), {})),
)

from agent.agent_runtime_helpers import codex_responses_ws_runtime_identity, switch_model
from agent.codex_responses_ws_transport import (
    GenericWsRejectedError,
    GenericWsStartedError,
)
from agent.codex_runtime import run_codex_stream
from run_agent import AIAgent


def _stub_httpx(monkeypatch):
    module = SimpleNamespace(
        RemoteProtocolError=type("RemoteProtocolError", (Exception,), {}),
        ReadTimeout=type("ReadTimeout", (Exception,), {}),
        ConnectError=type("ConnectError", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "httpx", module)
    return module


def _stub_openai(monkeypatch):
    module = SimpleNamespace(
        APIConnectionError=type("APIConnectionError", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "openai", module)
    return module


@pytest.fixture
def ws_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.ssl_guard.verify_ca_bundle_with_fallback"),
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


def _prepare_ws_runtime_agent(agent):
    agent.responses_transport = "auto"
    agent.responses_transport_provider = "custom:relay"
    agent.responses_ws_url = None
    agent.responses_ws_state = True
    agent.api_key = "test-key"
    agent._client_kwargs = {"timeout": 5.0, "default_headers": {}}
    agent._interrupt_requested = False
    agent._active_request_abort = None
    agent._generic_ws_auto_disabled_for = None
    agent._codex_stream_last_event_ts = 0
    agent._ensure_primary_openai_client = MagicMock(return_value=agent.client)
    agent._fire_stream_delta = MagicMock()
    agent._fire_reasoning_delta = MagicMock()
    agent._fire_streamed_codex_commentary = MagicMock()
    agent._touch_activity = MagicMock()
    agent._client_log_context = MagicMock(return_value="ctx")


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


def test_post_send_session_error_does_not_activate_provider_fallback(
    monkeypatch,
    ws_agent,
):
    """Stateful WS post-send failures are terminal for the turn."""
    _stub_httpx(monkeypatch)
    _stub_openai(monkeypatch)
    _prepare_ws_runtime_agent(ws_agent)
    ws_agent._try_activate_fallback = MagicMock(return_value=False)
    error = GenericWsStartedError("failed after session send", retryable=True)
    session = SimpleNamespace(stream_request=MagicMock(side_effect=error))
    ws_agent._codex_responses_ws_session = session
    ws_agent._codex_responses_ws_session_identity = codex_responses_ws_runtime_identity(ws_agent)

    with pytest.raises(GenericWsStartedError):
        run_codex_stream(ws_agent, {"model": "gpt-5", "input": "hello"}, client=ws_agent.client)

    session.stream_request.assert_called_once()
    ws_agent._try_activate_fallback.assert_not_called()


def test_provider_change_closes_old_responses_ws_session(ws_agent):
    """Changing runtime identity must retire stale Responses WS state."""
    _prepare_ws_runtime_agent(ws_agent)
    old_session = SimpleNamespace(close=MagicMock())
    ws_agent._codex_responses_ws_session = old_session
    ws_agent._create_openai_client = MagicMock(return_value=SimpleNamespace())
    ws_agent.context_compressor = SimpleNamespace(
        model=ws_agent.model,
        base_url=ws_agent.base_url,
        api_key=ws_agent.api_key,
        provider=ws_agent.provider,
        context_length=128000,
        api_mode=ws_agent.api_mode,
        threshold_tokens=96000,
        update_model=MagicMock(),
    )
    ws_agent._read_reasoning_echo_from_config = MagicMock(return_value=False)
    ws_agent._ensure_lmstudio_runtime_loaded = MagicMock()

    with patch("hermes_cli.providers.determine_api_mode", return_value="codex_responses"):
        switch_model(
            ws_agent,
            "gpt-5",
            "custom:other",
            api_key="next-key",
            base_url="https://other.example.com/v1",
            api_mode="codex_responses",
            responses_transport="auto",
            responses_ws_state=True,
            responses_transport_provider="custom:other",
        )

    old_session.close.assert_called_once()
    assert ws_agent._codex_responses_ws_session is None
