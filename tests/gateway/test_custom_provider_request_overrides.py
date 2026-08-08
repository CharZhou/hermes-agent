"""Gateway request-override propagation for named custom providers."""

from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import gateway.run as gateway_run
import gateway.platforms.api_server as api_server
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


_PROVIDER_OVERRIDES = {
    "extra_body": {
        "text": {"verbosity": "low"},
        "metadata": {"provider": "custom", "shared": "session"},
    }
}


class _CapturingAgent:
    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        persist_user_message=None,
        persist_user_timestamp=None,
    ):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.FEISHU,
        chat_id="ou_test",
        chat_type="dm",
        user_id="user-1",
        user_name="tester",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
    )


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda _source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda _session_id: [],
    )
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner._get_or_create_gateway_honcho = lambda _session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def test_runtime_resolvers_preserve_provider_request_overrides(monkeypatch):
    runtime = {
        "api_key": "test-key",
        "base_url": "https://example.test/v1",
        "provider": "custom:edge",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
        "request_overrides": _PROVIDER_OVERRIDES,
    }
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: runtime,
    )

    assert gateway_run._resolve_runtime_agent_kwargs()["request_overrides"] == _PROVIDER_OVERRIDES
    assert gateway_run._resolve_runtime_agent_kwargs_for_provider("custom:edge")[
        "request_overrides"
    ] == _PROVIDER_OVERRIDES


def test_api_runtime_resolver_preserves_provider_request_overrides(monkeypatch):
    runtime = {
        "api_key": "test-key",
        "base_url": "https://example.test/v1",
        "provider": "custom:edge",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
        "request_overrides": _PROVIDER_OVERRIDES,
    }
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: runtime,
    )

    resolved = api_server._resolve_request_runtime_agent_kwargs("custom:edge", "edge-model")

    assert resolved["request_overrides"] == _PROVIDER_OVERRIDES


def test_api_runtime_overrides_apply_provider_request_overrides():
    runtime_kwargs = {}

    api_server._apply_runtime_agent_overrides(
        runtime_kwargs,
        {"request_overrides": _PROVIDER_OVERRIDES},
    )

    assert runtime_kwargs["request_overrides"] == _PROVIDER_OVERRIDES


def test_fallback_runtime_preserves_provider_request_overrides(monkeypatch):
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {
            "fallback_providers": [
                {"provider": "custom:edge", "model": "edge-model"}
            ]
        },
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "api_key": "test-key",
            "base_url": "https://example.test/v1",
            "provider": "custom:edge",
            "api_mode": "chat_completions",
            "request_overrides": _PROVIDER_OVERRIDES,
        },
    )

    runtime = gateway_run._try_resolve_fallback_provider()

    assert runtime["request_overrides"] == _PROVIDER_OVERRIDES


def test_credentialed_session_fast_path_preserves_request_overrides():
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    runner._session_model_overrides[session_key] = {
        "model": "edge-model",
        "provider": "custom:edge",
        "api_key": "test-key",
        "base_url": "https://example.test/v1",
        "api_mode": "chat_completions",
        "request_overrides": _PROVIDER_OVERRIDES,
    }

    model, runtime = runner._resolve_session_agent_runtime(
        source=source,
        session_key=session_key,
        user_config={"model": {"default": "global-model"}},
    )

    assert model == "edge-model"
    assert runtime["request_overrides"] == _PROVIDER_OVERRIDES


def test_apply_session_override_deep_merges_with_runtime_provider_values():
    runner = _make_runner()
    session_key = runner._session_key_for_source(_make_source())
    runner._session_model_overrides[session_key] = {
        "model": "edge-model",
        "request_overrides": _PROVIDER_OVERRIDES,
    }

    _, runtime = runner._apply_session_model_override(
        session_key,
        "global-model",
        {
            "request_overrides": {
                "extra_body": {
                    "metadata": {"runtime": True, "shared": "runtime"},
                    "runtime_only": True,
                },
                "top_level_runtime": True,
            }
        },
    )

    assert runtime["request_overrides"] == {
        "extra_body": {
            "text": {"verbosity": "low"},
            "metadata": {
                "runtime": True,
                "provider": "custom",
                "shared": "session",
            },
            "runtime_only": True,
        },
        "top_level_runtime": True,
    }


@pytest.mark.parametrize("service_tier", [None, "priority"])
def test_turn_route_preserves_and_deep_merges_provider_overrides(service_tier):
    runner = _make_runner()
    runner._service_tier = service_tier
    runtime = {
        "provider": "custom:edge",
        "request_overrides": _PROVIDER_OVERRIDES,
    }
    fast = {
        "extra_body": {
            "metadata": {"fast": True, "shared": "fast"},
        },
        "service_tier": "priority",
    }

    with patch(
        "hermes_cli.models.resolve_fast_mode_overrides",
        return_value=fast,
    ):
        route = runner._resolve_turn_agent_config("hi", "edge-model", runtime)

    expected = _PROVIDER_OVERRIDES
    if service_tier:
        expected = {
            "extra_body": {
                "text": {"verbosity": "low"},
                "metadata": {
                    "provider": "custom",
                    "fast": True,
                    "shared": "fast",
                },
            },
            "service_tier": "priority",
        }
    assert route["request_overrides"] == expected


@pytest.mark.asyncio
async def test_model_command_next_real_turn_exposes_request_overrides_in_agent_kwargs(
    tmp_path,
    monkeypatch,
):
    """The value resolved by /model must reach the next AIAgent constructor."""
    from hermes_cli.model_switch import ModelSwitchResult

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        """
model:
  default: global-model
  provider: openrouter
providers: {}
custom_providers:
  - name: Edge
    base_url: https://example.test/v1
    model: edge-model
    extra_body:
      text:
        verbosity: low
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *args, **kwargs: 128_000,
    )

    switch_result = ModelSwitchResult(
        success=True,
        new_model="edge-model",
        target_provider="custom:edge",
        provider_changed=True,
        api_key="test-key",
        base_url="https://example.test/v1",
        api_mode="chat_completions",
        request_overrides=_PROVIDER_OVERRIDES,
        provider_label="Edge",
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kwargs: switch_result,
    )

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda _config, _platform: {"core"},
    )

    runner = _make_runner()
    event = _make_event("/model edge-model --provider custom:edge --session")
    switched = await runner._handle_model_command(event)
    assert switched is not None and "edge-model" in switched

    session_key = runner._session_key_for_source(event.source)
    _CapturingAgent.last_init = None
    result = await runner._run_agent(
        message="next turn",
        context_prompt="",
        history=[],
        source=event.source,
        session_id="session-1",
        session_key=session_key,
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init["request_overrides"] == _PROVIDER_OVERRIDES
