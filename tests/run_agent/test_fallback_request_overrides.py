"""Provider fallback must replace request-scoped provider body overrides."""

import importlib
from unittest.mock import MagicMock, patch

from agent.transports import get_transport
from run_agent import AIAgent


def _make_agent(*, fallback_model, request_overrides=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.model_metadata.get_model_context_length", return_value=128_000),
    ):
        agent = AIAgent(
            model="primary-model",
            provider="custom",
            api_key="primary-key",
            base_url="https://primary.example/v1",
            request_overrides=request_overrides or {},
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
    agent.client = MagicMock()
    return agent


def _client(base_url, api_key="fallback-key"):
    client = MagicMock()
    client.base_url = base_url
    client.api_key = api_key
    client._custom_headers = None
    return client


def _request_kwargs(agent):
    importlib.import_module("agent.transports.chat_completions")

    return get_transport("chat_completions").build_kwargs(
        model=agent.model,
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        request_overrides=agent.request_overrides,
    )


def test_primary_custom_extra_body_does_not_reach_ordinary_fallback_request():
    agent = _make_agent(
        fallback_model={"provider": "openrouter", "model": "fallback-model"},
        request_overrides={"extra_body": {"primary_only": True}},
    )
    runtime = {
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "fallback-key",
    }

    with (
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=runtime,
        ),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_client(runtime["base_url"]), "fallback-model"),
        ),
    ):
        assert agent._try_activate_fallback() is True

    assert "primary_only" not in _request_kwargs(agent).get("extra_body", {})


def test_named_custom_fallback_installs_its_own_extra_body_on_request():
    agent = _make_agent(
        fallback_model={"provider": "custom:edge", "model": "edge-model"},
    )
    config = {
        "providers": {
            "edge": {
                "name": "Edge",
                "base_url": "https://edge.example/v1",
                "api_key": "edge-key",
                "model": "edge-model",
                "extra_body": {"edge_only": True},
            }
        }
    }

    with (
        patch("hermes_cli.runtime_provider.load_config", return_value=config),
        patch("hermes_cli.config.load_config", return_value=config),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_client("https://edge.example/v1", "edge-key"), "edge-model"),
        ),
    ):
        assert agent._try_activate_fallback() is True

    assert _request_kwargs(agent)["extra_body"]["edge_only"] is True


def test_restore_primary_runtime_replaces_fallback_request_overrides():
    primary_overrides = {"extra_body": {"primary_only": True}}
    agent = _make_agent(
        fallback_model={"provider": "openrouter", "model": "fallback-model"},
        request_overrides=primary_overrides,
    )
    agent.request_overrides = {"extra_body": {"fallback_only": True}}
    agent._fallback_activated = True
    agent._rate_limited_until = 0

    with (
        patch.object(agent, "_create_openai_client", return_value=MagicMock()),
        patch("agent.credential_pool.load_pool", return_value=None),
    ):
        assert agent._restore_primary_runtime() is True

    kwargs = _request_kwargs(agent)
    assert kwargs["extra_body"]["primary_only"] is True
    assert "fallback_only" not in kwargs["extra_body"]
