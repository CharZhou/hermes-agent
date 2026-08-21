"""Conversation-loop boundaries for post-send Responses WebSocket failures."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("dotenv")
pytest.importorskip("requests")
pytest.importorskip("httpx")
pytest.importorskip("openai")

from agent.agent_runtime_helpers import (
    codex_responses_ws_runtime_identity,
    restore_primary_runtime,
    switch_model,
    try_recover_primary_transport,
)
from agent.codex_responses_ws_transport import (
    GenericWsRejectedError,
    GenericWsStartedError,
)
from agent.codex_runtime import run_codex_stream
from run_agent import AIAgent


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
            responses_ws_state=True,
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


def _prepare_runtime_restore_agent(agent):
    agent._create_openai_client = MagicMock(return_value=SimpleNamespace())
    agent._is_openrouter_url = MagicMock(return_value=False)
    agent.context_compressor = SimpleNamespace(
        model=agent.model,
        base_url=agent.base_url,
        api_key=agent.api_key,
        provider=agent.provider,
        context_length=128000,
        api_mode=agent.api_mode,
        threshold_tokens=96000,
        update_model=MagicMock(),
    )
    agent._credential_pool = None
    agent._credential_pool_entry_id = None
    agent._cache_disabled = False


def test_primary_runtime_snapshot_restores_responses_ws_state(ws_agent):
    """Primary runtime snapshots must preserve stateful WS capability."""
    _prepare_runtime_restore_agent(ws_agent)
    assert ws_agent._primary_runtime["responses_ws_state"] is True

    ws_agent.responses_ws_state = False
    ws_agent._fallback_activated = True
    ws_agent._rate_limited_until = 0

    assert restore_primary_runtime(ws_agent) is True
    assert ws_agent.responses_ws_state is True


def test_primary_transport_recovery_restores_responses_ws_state(monkeypatch, ws_agent):
    """Primary connection recovery must rebuild from the stateful WS snapshot."""
    _prepare_runtime_restore_agent(ws_agent)
    ws_agent.responses_ws_state = False
    monkeypatch.setattr("agent.agent_runtime_helpers.time.sleep", lambda _seconds: None)

    class ConnectError(Exception):
        pass

    assert try_recover_primary_transport(
        ws_agent,
        ConnectError("reset"),
        retry_count=0,
        max_retries=1,
    ) is True
    assert ws_agent.responses_ws_state is True


def test_provider_fallback_closes_responses_ws_session_before_runtime_mutation(
    monkeypatch,
    ws_agent,
):
    """Provider fallback must retire session state before changing identity."""
    from agent.chat_completion_helpers import try_activate_fallback

    _prepare_runtime_restore_agent(ws_agent)
    ws_agent._fallback_chain = [{"provider": "custom:fallback", "model": "gpt-5"}]
    ws_agent._fallback_index = 0
    ws_agent._fallback_activated = False
    ws_agent._unavailable_fallback_keys = set()
    ws_agent._replace_primary_openai_client = MagicMock()
    ws_agent._anthropic_prompt_cache_policy = MagicMock(return_value=(False, False))
    ws_agent._ensure_lmstudio_runtime_loaded = MagicMock()
    ws_agent._buffer_status = MagicMock()
    ws_agent.reasoning_config = None
    ws_agent._pending_fallback_notice = None
    ws_agent._generic_ws_auto_disabled_for = "old"
    ws_agent._transport_cache = {"old": object()}

    close_identity = []

    def close_old_session():
        close_identity.append(
            (ws_agent.requested_provider, ws_agent.base_url)
        )

    old_session = SimpleNamespace(close=MagicMock(side_effect=close_old_session))
    ws_agent._codex_responses_ws_session = old_session
    ws_agent._codex_responses_ws_session_identity = codex_responses_ws_runtime_identity(ws_agent)
    fallback_client = SimpleNamespace(
        base_url="https://fallback.example.com/v1",
        api_key="fallback-key",
        _custom_headers={},
    )
    fallback_runtime = {
        "provider": "custom",
        "requested_provider": "custom:fallback",
        "base_url": "https://fallback.example.com/v1",
        "api_mode": "codex_responses",
        "responses_transport": "auto",
        "responses_ws_url": None,
        "responses_ws_state": True,
        "responses_transport_provider": "custom:fallback",
        "request_overrides": {},
        "api_key": "fallback-key",
    }

    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_args, **_kwargs: (fallback_client, "gpt-5"),
    )
    monkeypatch.setattr(
        "hermes_cli.fallback_config.resolve_entry_api_key",
        lambda _entry: "fallback-key",
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: fallback_runtime,
    )
    monkeypatch.setattr(
        "hermes_cli.model_normalize.normalize_model_for_provider",
        lambda model, _provider: model,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: None)
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 128000,
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(
        "hermes_constants.resolve_reasoning_config",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "agent.chat_completion_helpers.rewrite_prompt_model_identity",
        lambda *_args, **_kwargs: None,
    )

    assert try_activate_fallback(ws_agent) is True
    old_session.close.assert_called_once()
    assert close_identity == [("custom:relay", "https://relay.example.com/v1")]
    assert ws_agent._codex_responses_ws_session is None
    assert ws_agent.provider == "custom"
    assert ws_agent.requested_provider == "custom:fallback"


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
    ws_agent,
):
    """Stateful WS post-send failures are terminal for the turn."""
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
