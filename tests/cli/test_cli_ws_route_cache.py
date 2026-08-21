"""Classic CLI route identity must preserve warm agents across turns."""

import sys
from types import ModuleType
from types import SimpleNamespace


def _cli_agent_setup_mixin():
    if "rich.markup" not in sys.modules:
        rich_module = ModuleType("rich")
        rich_markup = ModuleType("rich.markup")
        rich_markup.escape = lambda value: value
        rich_module.markup = rich_markup
        sys.modules.setdefault("rich", rich_module)
        sys.modules["rich.markup"] = rich_markup

    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    return CLIAgentSetupMixin


class _RouteCLI(_cli_agent_setup_mixin()):
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


def test_identical_cli_transport_route_keeps_same_agent_signature(monkeypatch):
    cli_stub = ModuleType("cli")

    class _Agent:
        def __init__(self, **_kwargs):
            self._session_messages = []

    class _ChatConsole:
        def print(self, *_args, **_kwargs):
            return None

    cli_stub.AIAgent = _Agent
    cli_stub.ChatConsole = _ChatConsole
    cli_stub._DIM = ""
    cli_stub._RST = ""
    cli_stub._accent_hex = lambda: "white"
    cli_stub._cprint = lambda *_args, **_kwargs: None
    cli_stub._prepare_deferred_agent_startup = lambda: None
    cli_stub.logger = SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    cli_stub._active_agent_ref = None
    monkeypatch.setitem(sys.modules, "cli", cli_stub)

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
    if "dotenv" not in sys.modules:
        dotenv = ModuleType("dotenv")
        dotenv.load_dotenv = lambda *args, **kwargs: None
        sys.modules["dotenv"] = dotenv

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
