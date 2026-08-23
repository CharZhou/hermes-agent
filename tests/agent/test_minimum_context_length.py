"""Configuration policy for Hermes' supported model context floor."""

from agent.model_metadata import MINIMUM_CONTEXT_LENGTH, resolve_minimum_context_length


def test_resolve_minimum_context_length_accepts_explicit_32k():
    assert resolve_minimum_context_length(32_000) == 32_000


def test_resolve_minimum_context_length_preserves_default_for_invalid_values():
    assert resolve_minimum_context_length(31_999) == MINIMUM_CONTEXT_LENGTH
    assert resolve_minimum_context_length(True) == MINIMUM_CONTEXT_LENGTH
    assert resolve_minimum_context_length("32000") == MINIMUM_CONTEXT_LENGTH
