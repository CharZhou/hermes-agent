"""The runtime Ollama check must share the active agent's context policy."""

from types import SimpleNamespace

from agent.conversation_loop import _ollama_context_limit_error


def test_ollama_allows_runtime_at_configured_32k_minimum():
    agent = SimpleNamespace(
        tools=[{}],
        _ollama_num_ctx=32_000,
        minimum_context_length=32_000,
        model="qwen",
        base_url="http://localhost:11434",
        provider="ollama",
        session_id="test-session",
    )

    assert _ollama_context_limit_error(agent, request_tokens=100) is None
