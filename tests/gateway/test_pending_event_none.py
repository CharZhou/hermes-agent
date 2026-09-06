"""Tests for pending follow-up extraction in recursive _run_agent calls.

When pending_event is None (Path B: pending comes from interrupt_message),
accessing pending_event.channel_prompt previously raised AttributeError.
This verifies the fix: channel_prompt is captured inside the
`if pending_event is not None:` block and falls back to None otherwise.

Also verifies that internal control interrupt reasons like "Stop requested"
do not get recycled into the pending-user-message follow-up path.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run import GatewayRunner, _is_control_interrupt_message
from gateway.turn_context import TurnContext


def _extract_channel_prompt(pending_event):
    """Reproduce the fixed logic from gateway/run.py.

    Mirrors the variable-capture pattern used before the recursive
    _run_agent call so we can test both paths without a full runner.
    """
    next_channel_prompt = None
    if pending_event is not None:
        next_channel_prompt = getattr(pending_event, "channel_prompt", None)
    return next_channel_prompt


def _extract_pending_text(interrupted, pending_event, interrupt_message):
    """Reproduce the fixed pending-text selection from gateway/run.py."""
    if interrupted and pending_event is None and interrupt_message:
        if _is_control_interrupt_message(interrupt_message):
            return None
        return interrupt_message
    return None


class TestPendingEventNoneChannelPrompt:
    """Guard against AttributeError when pending_event is None."""


    def test_pending_event_with_channel_prompt_passes_through(self):
        """Path A: pending_event present — channel_prompt is forwarded."""
        event = SimpleNamespace(channel_prompt="You are a helpful bot.")
        result = _extract_channel_prompt(event)
        assert result == "You are a helpful bot."

    @pytest.mark.asyncio
    @pytest.mark.parametrize("has_event", [False, True])
    async def test_recursive_followup_carries_only_its_own_delivery_metadata(self, has_event):
        source = SimpleNamespace(chat_id="chat")
        event = SimpleNamespace(
            source=source, channel_prompt="prompt", message_type="text",
            metadata={"delivery_metadata": {
                "feishu_mention_targets": {"Alex": "ou_alex"}, "thread_id": "untrusted",
            }},
        ) if has_event else None
        runner = SimpleNamespace(
            _MAX_INTERRUPT_DEPTH=5, _adapter_for_source=lambda source: None,
            _is_goal_continuation_event=lambda event: False,
            _session_key_for_source=lambda source: "session",
            _prepare_profile_scoped_inbound_message_text=AsyncMock(return_value="next"),
            _reply_anchor_for_event=lambda event: "anchor",
            _refresh_agent_cache_message_count=AsyncMock(),
            _run_agent=AsyncMock(return_value={"final_response": "done"}),
        )
        ctx = TurnContext(source=source, session_key="session", session_id="id", history=[],
                          delivery_metadata={"feishu_mention_targets": {"Old": "ou_old"}})
        await GatewayRunner._run_agent_queued_followup(
            runner, ctx, None, "next", event, {}, {"interrupted": True}, None,
        )
        forwarded = runner._run_agent.await_args.kwargs
        assert forwarded["delivery_metadata"] == (
            {"feishu_mention_targets": {"Alex": "ou_alex"}} if has_event else None
        )
        assert forwarded["channel_prompt"] == ("prompt" if has_event else None)


class TestControlInterruptMessages:
    """Control interrupt reasons must not become follow-up user input."""

    def test_stop_requested_is_not_treated_as_pending_user_message(self):
        result = _extract_pending_text(True, None, "Stop requested")
        assert result is None
