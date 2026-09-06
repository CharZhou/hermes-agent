"""Adapter context survives the real turn, stream and final-delivery wiring."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, StreamingConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult, _thread_metadata_for_event
from gateway.platforms.delivery_metadata import delivery_metadata_for_event
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


@pytest.mark.asyncio
@pytest.mark.parametrize("final_lane", ["transformed", "queued"])
async def test_turn_stream_and_final_edit_keep_event_targets_and_core_route(final_lane):
    source = SessionSource(platform=Platform.FEISHU, chat_id="chat", thread_id="topic", chat_type="group")
    event = MessageEvent(text="hello", source=source, message_id="anchor", metadata={
        "delivery_metadata": {
            "feishu_mention_targets": {"Alex": "ou_alex"},
            "thread_id": "foreign-topic", "reply_to_message_id": "foreign-anchor",
        },
    })
    adapter = SimpleNamespace(
        name="feishu", MAX_MESSAGE_LENGTH=8000, SUPPORTS_NATIVE_STREAMING=False,
        extract_media=BasePlatformAdapter.extract_media,
        send=AsyncMock(return_value=SendResult(success=True, message_id="sent")),
        edit_message=AsyncMock(return_value=SendResult(success=True, message_id="sent")),
    )
    runner = object.__new__(GatewayRunner)
    runner._adapter_for_source = lambda source: adapter
    runner.hooks = SimpleNamespace()
    runner.config = SimpleNamespace(streaming=StreamingConfig(enabled=True, transport="edit"))
    ctx = TurnContext(
        source=source, event_message_id=event.message_id,
        delivery_metadata=delivery_metadata_for_event(event),
        _run_still_current=lambda: True, resolve_display_setting=lambda *args: None,
        user_config={},
    )
    turn_runner = TurnRunner(runner, ctx)
    runner._run_agent_bind_turn_wiring(ctx, turn_runner, source, event.message_id, False)
    metadata = {
        "thread_id": "topic", "reply_to_message_id": "anchor",
        "feishu_mention_targets": {"Alex": "ou_alex"},
    }
    assert _thread_metadata_for_event(event)["feishu_mention_targets"] == metadata["feishu_mention_targets"]
    assert _thread_metadata_for_event(event)["thread_id"] == "topic"
    assert ctx._progress_metadata["feishu_mention_targets"] == metadata["feishu_mention_targets"]
    assert ctx._status_thread_metadata == metadata

    consumer, *_ = turn_runner._setup_stream_consumer("feishu")
    assert consumer is not None
    await consumer._send_or_edit("@Alex draft")
    sent_metadata = adapter.send.await_args.kwargs["metadata"]
    assert {key: sent_metadata[key] for key in metadata} == metadata
    await consumer._edit_message(message_id="sent", content="@Alex streamed", finalize=True)
    assert adapter.edit_message.await_args.kwargs["metadata"] == metadata

    if final_lane == "transformed":
        response = {"final_response": "@Alex final", "response_transformed": True}
        await runner._run_agent_mark_streamed_delivery(response, ctx)
        assert response["already_sent"] is True
    else:
        await runner._deliver_queued_first_response(
            "@Alex final", source=source, adapter=adapter, metadata=metadata,
            deliver_media=False, stream_consumer=consumer,
        )
    assert adapter.edit_message.await_args.kwargs["metadata"] == metadata
    assert adapter.edit_message.await_args.kwargs["content"] == "@Alex final"
    assert adapter.send.await_count == 1
