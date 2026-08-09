"""Focused tests for Feishu native mention delivery metadata."""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, _delivery_metadata_for_event
from gateway.session import SessionSource
from plugins.platforms.feishu.adapter import (
    _FEISHU_MENTION_REGISTRY_TTL_SECONDS,
    FeishuAdapter,
    FeishuMentionRef,
    _build_mention_targets,
)


def _adapter(home: Path) -> FeishuAdapter:
    with patch.dict("os.environ", {"HERMES_HOME": str(home)}, clear=False):
        return FeishuAdapter(PlatformConfig())


def test_same_event_duplicate_names_retain_all_open_ids() -> None:
    refs = [
        FeishuMentionRef(name="Alex", open_id="ou_one"),
        FeishuMentionRef(name="Alex", open_id="ou_two"),
        FeishuMentionRef(name="Hermes", open_id="ou_self", is_self=True),
        FeishuMentionRef(is_all=True),
    ]

    assert _build_mention_targets(refs) == {"Alex": {"ou_one", "ou_two"}}


def test_registry_suppresses_cross_event_name_conflicts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = _adapter(Path(tmp))

        with patch("plugins.platforms.feishu.adapter.time.time", return_value=100.0):
            adapter._update_mention_registry("oc_chat", {"Alex": {"ou_one"}})
        with patch("plugins.platforms.feishu.adapter.time.time", return_value=101.0):
            adapter._update_mention_registry("oc_chat", {"Alex": {"ou_two"}})
            assert adapter._mention_targets_for_chat("oc_chat") == {}


def test_registry_conflict_recovers_when_stale_observation_expires() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = _adapter(Path(tmp))

        with patch("plugins.platforms.feishu.adapter.time.time", return_value=100.0):
            adapter._update_mention_registry("oc_chat", {"Alex": {"ou_old"}})
        with patch(
            "plugins.platforms.feishu.adapter.time.time",
            return_value=100.0 + (_FEISHU_MENTION_REGISTRY_TTL_SECONDS / 2),
        ):
            adapter._update_mention_registry("oc_chat", {"Alex": {"ou_new"}})
            assert adapter._mention_targets_for_chat("oc_chat") == {}
        with patch(
            "plugins.platforms.feishu.adapter.time.time",
            return_value=101.0 + _FEISHU_MENTION_REGISTRY_TTL_SECONDS,
        ):
            assert adapter._mention_targets_for_chat("oc_chat") == {"Alex": "ou_new"}


def test_same_event_conflict_expires_and_later_unique_observation_recovers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = _adapter(Path(tmp))

        with patch("plugins.platforms.feishu.adapter.time.time", return_value=100.0):
            adapter._update_mention_registry(
                "oc_chat",
                {"Alex": {"ou_one", "ou_two"}},
            )
            assert adapter._mention_targets_for_chat("oc_chat") == {}
        with patch(
            "plugins.platforms.feishu.adapter.time.time",
            return_value=101.0 + _FEISHU_MENTION_REGISTRY_TTL_SECONDS,
        ):
            adapter._update_mention_registry("oc_chat", {"Alex": {"ou_three"}})
            assert adapter._mention_targets_for_chat("oc_chat") == {"Alex": "ou_three"}


def test_send_renders_unique_target_as_native_mention() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = _adapter(Path(tmp))
        captured = {}

        class _Messages:
            def create(self, request):
                captured["content"] = request.request_body.content
                return SimpleNamespace(
                    success=lambda: True,
                    data=SimpleNamespace(message_id="om_reply"),
                )

        adapter._client = SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=_Messages()))
        )

        result = asyncio.run(
            adapter.send(
                "oc_chat",
                "@Alex received",
                metadata={"feishu_mention_targets": {"Alex": "ou_alex"}},
            )
        )

        assert result.success
        assert captured["content"] == json.dumps(
            {"text": '<at user_id="ou_alex"></at> received'},
            ensure_ascii=False,
        )


def test_ambiguous_metadata_does_not_render_native_mention() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = _adapter(Path(tmp))
        captured = {}

        class _Messages:
            def create(self, request):
                captured["content"] = request.request_body.content
                return SimpleNamespace(
                    success=lambda: True,
                    data=SimpleNamespace(message_id="om_reply"),
                )

        adapter._client = SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=_Messages()))
        )

        result = asyncio.run(
            adapter.send(
                "oc_chat",
                "@Alex received",
                metadata={"feishu_mention_targets": {"Alex": ["ou_one", "ou_two"]}},
            )
        )

        assert result.success
        assert captured["content"] == json.dumps(
            {"text": "@Alex received"},
            ensure_ascii=False,
        )


def test_inbound_event_publishes_only_unambiguous_mention_targets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = _adapter(Path(tmp))
        adapter._extract_message_content = AsyncMock(
            return_value=(
                "ping",
                MessageType.TEXT,
                [],
                [],
                [
                    FeishuMentionRef(name="Alex", open_id="ou_one"),
                    FeishuMentionRef(name="Alex", open_id="ou_two"),
                    FeishuMentionRef(name="Blair", open_id="ou_blair"),
                ],
            )
        )
        adapter._fetch_message_text = AsyncMock(return_value=None)
        adapter.get_chat_info = AsyncMock(return_value={"name": "Chat"})
        adapter._resolve_sender_profile = AsyncMock(
            return_value={
                "user_id": "ou_sender",
                "user_name": "Sender",
                "user_id_alt": "",
            }
        )
        adapter._dispatch_inbound_event = AsyncMock()
        message = SimpleNamespace(chat_id="oc_chat")

        asyncio.run(
            adapter._process_inbound_message(
                data=message,
                message=message,
                sender_id=SimpleNamespace(open_id="ou_sender"),
                chat_type="group",
                message_id="om_event",
            )
        )

        event = adapter._dispatch_inbound_event.await_args.args[0]
        assert event.metadata == {
            "delivery_metadata": {
                "feishu_mention_targets": {"Blair": "ou_blair"},
            }
        }


def test_delivery_metadata_cannot_override_reserved_route_keys() -> None:
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_core",
        user_id="ou_core",
        thread_id="omt_core",
    )
    event = MessageEvent(
        text="ping",
        message_type=MessageType.TEXT,
        source=source,
        message_id="om_core",
        metadata={
            "delivery_metadata": {
                "thread_id": "omt_attacker",
                "reply_to_message_id": "om_attacker",
                "session_id": "session_attacker",
                "message_id": "om_attacker",
                "chat_id": "oc_attacker",
                "user_id": "ou_attacker",
                "platform": "telegram",
                "feishu_mention_targets": {"Alex": "ou_alex"},
            }
        },
    )

    assert _delivery_metadata_for_event(
        event,
        {"thread_id": "omt_core", "reply_to_message_id": "om_core"},
    ) == {
        "thread_id": "omt_core",
        "reply_to_message_id": "om_core",
        "feishu_mention_targets": {"Alex": "ou_alex"},
    }
