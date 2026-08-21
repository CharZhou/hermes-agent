"""Classic CLI route identity must preserve warm agents across turns."""

import sys
from types import SimpleNamespace

import pytest


def test_identical_cli_transport_route_keeps_same_agent_signature(monkeypatch):
    pytest.importorskip("rich.markup", reason="rich required for cli import")

    import cli as cli_mod
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    class _RouteCLI(CLIAgentSetupMixin):
        def __init__(self):
            self.model = "gpt-5"
            self.provider = "custom"
            self.requested_provider = "custom:relay"
            self.api_key = "test-key"
            self.base_url = "https://relay.example.com/v1"
            self.api_mode = "codex_responses"
            self.responses_transport = "auto"
            self.responses_ws_url = "wss://relay.example.com/v1/responses"
            self.responses_transport_provider = "custom:relay"
            self.acp_command = None
            self.acp_args = []
            self.service_tier = None

        def __getattr__(self, _name):
            return None

        def _install_tool_callbacks(self):
            return None

        def _ensure_tirith_security(self):
            return None

        def _ensure_runtime_credentials(self):
            return True

        def finalize_preloaded_skills(self):
            return None

        def _current_reasoning_callback(self):
            return None

    class _Agent:
        def __init__(self, **_kwargs):
            self._session_messages = []

    class _ChatConsole:
        def print(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(cli_mod, "AIAgent", _Agent)
    monkeypatch.setattr(cli_mod, "ChatConsole", _ChatConsole)
    monkeypatch.setattr(cli_mod, "_DIM", "")
    monkeypatch.setattr(cli_mod, "_RST", "")
    monkeypatch.setattr(cli_mod, "_accent_hex", lambda: "white")
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_mod, "_prepare_deferred_agent_startup", lambda: None)
    monkeypatch.setattr(cli_mod, "logger", SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    ))
    monkeypatch.setattr(cli_mod, "_active_agent_ref", None, raising=False)

    cli = _RouteCLI()
    cli._session_db = object()
    cli._resumed = False
    cli.conversation_history = []
    cli.agent = None
    route = cli._resolve_turn_agent_config("first turn")
    assert cli._init_agent(
        model_override=route["model"],
        runtime_override=route["runtime"],
    )

    # The classic chat path compares this tuple before every turn. The
    # signature produced during initialization must include the same transport
    # identity fields as the route computed on the next turn.
    second_route = cli._resolve_turn_agent_config("second turn")

    assert second_route["signature"] == cli._active_agent_route_signature


def test_tui_runtime_snapshot_and_restore_preserve_responses_ws_state():
    pytest.importorskip("rich", reason="rich required for tui gateway import")
    pytest.importorskip("dotenv", reason="python-dotenv required for tui gateway import")
    pytest.importorskip("httpx", reason="httpx required for tui gateway import")

    import tui_gateway.server as server

    class _SnapshotAgent:
        model = "gpt-5"
        provider = "custom:relay"
        api_key = "test-key"
        base_url = "https://relay.example.com/v1"
        api_mode = "codex_responses"
        responses_transport = "auto"
        responses_ws_url = "wss://relay.example.com/v1/responses"
        responses_ws_state = True
        responses_transport_provider = "custom:relay"
        request_overrides = {"reasoning": {"effort": "low"}}
        _primary_runtime = None

    snapshot = server._snapshot_agent_model_runtime(_SnapshotAgent())

    assert snapshot["responses_ws_state"] is True

    class _RestoreAgent:
        def __init__(self):
            self.calls = []

        def switch_model(self, **kwargs):
            self.calls.append(kwargs)

    restore_agent = _RestoreAgent()
    server._restore_agent_model_runtime(restore_agent, snapshot)

    assert restore_agent.calls[0]["responses_ws_state"] is True


def test_module_import_adds_no_synthetic_rich_or_dotenv_modules():
    baseline_rich = sys.modules.get("rich")
    baseline_dotenv = sys.modules.get("dotenv")

    import tests.cli.test_cli_ws_route_cache as test_module

    assert test_module is sys.modules["tests.cli.test_cli_ws_route_cache"]
    assert sys.modules.get("rich") is baseline_rich
    assert sys.modules.get("dotenv") is baseline_dotenv
