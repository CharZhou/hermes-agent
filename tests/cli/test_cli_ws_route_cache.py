"""Classic CLI route identity must preserve warm agents across turns."""

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


def test_identical_cli_transport_route_keeps_same_agent_signature(monkeypatch):
    import cli

    class _Agent:
        def __init__(self, **_kwargs):
            self._session_messages = []

    monkeypatch.setattr(cli, "AIAgent", _Agent)
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
