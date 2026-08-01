"""Tests for Feishu reply_to_mode behavior."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from gateway.config import GatewayConfig, Platform, PlatformConfig, load_gateway_config
from plugins.platforms.feishu.adapter import FeishuAdapter


def test_platform_config_normalizes_yaml_false_reply_to_mode():
    config = PlatformConfig.from_dict({"enabled": True, "reply_to_mode": False})

    assert config.reply_to_mode == "off"


def test_platform_config_reads_extra_reply_to_mode_when_top_level_absent():
    config = PlatformConfig.from_dict(
        {
            "enabled": True,
            "extra": {"reply_to_mode": "off"},
        }
    )

    assert config.reply_to_mode == "off"


def test_platform_config_top_level_none_does_not_read_extra_reply_to_mode():
    config = PlatformConfig.from_dict(
        {
            "enabled": True,
            "reply_to_mode": None,
            "extra": {"reply_to_mode": "off"},
        }
    )

    assert config.reply_to_mode == "first"


def test_gateway_config_from_dict_keeps_platforms_feishu_reply_to_mode():
    config = GatewayConfig.from_dict(
        {
            "platforms": {
                "feishu": {
                    "enabled": True,
                    "reply_to_mode": False,
                }
            }
        }
    )

    assert config.platforms[Platform.FEISHU].reply_to_mode == "off"


def test_platforms_feishu_reply_to_mode_overrides_gateway_platforms(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "\n".join(
            [
                "gateway:",
                "  platforms:",
                "    feishu:",
                "      reply_to_mode: first",
                "platforms:",
                "  feishu:",
                "    reply_to_mode: off",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    config = load_gateway_config()

    assert config.platforms[Platform.FEISHU].reply_to_mode == "off"


def test_top_level_feishu_reply_to_mode_loads_from_config_yaml(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "feishu:\n  reply_to_mode: off\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    config = load_gateway_config()

    assert config.platforms[Platform.FEISHU].reply_to_mode == "off"


@patch.dict(os.environ, {}, clear=True)
def test_adapter_normalizes_false_reply_to_mode_to_off():
    adapter = FeishuAdapter(PlatformConfig(reply_to_mode=False))

    assert adapter._reply_to_mode == "off"


@patch.dict(os.environ, {}, clear=True)
def test_inbound_reply_to_mode_off_drops_feishu_thread_and_reply_metadata():
    adapter = FeishuAdapter(PlatformConfig(reply_to_mode="off"))
    adapter._dispatch_inbound_event = AsyncMock()
    adapter.get_chat_info = AsyncMock(
        return_value={"chat_id": "oc_chat", "name": "Feishu Group", "type": "group"}
    )
    adapter._resolve_sender_profile = AsyncMock(
        return_value={"user_id": "ou_user", "user_name": "Alice", "user_id_alt": None}
    )
    adapter._fetch_message_text = AsyncMock(return_value="parent text")
    adapter._resolve_channel_prompt = Mock(return_value=None)
    message = SimpleNamespace(
        chat_id="oc_chat",
        thread_id="omt_thread",
        root_id="om_root",
        parent_id="om_parent",
        upper_message_id=None,
        message_type="text",
        content='{"text":"hello"}',
        message_id="om_child",
    )

    asyncio.run(
        adapter._process_inbound_message(
            data=SimpleNamespace(event=SimpleNamespace(message=message)),
            message=message,
            sender_id=SimpleNamespace(open_id="ou_user", user_id=None, union_id=None),
            is_bot=False,
            chat_type="group",
            message_id="om_child",
        )
    )

    event = adapter._dispatch_inbound_event.await_args.args[0]
    assert event.source.thread_id is None
    assert event.reply_to_message_id is None
    assert event.reply_to_text is None
    adapter._fetch_message_text.assert_not_awaited()
    adapter._resolve_channel_prompt.assert_called_once_with("oc_chat", None)


@patch.dict(os.environ, {}, clear=True)
def test_send_reply_to_mode_off_ignores_reply_and_thread_metadata():
    adapter = FeishuAdapter(PlatformConfig(reply_to_mode="off"))
    captured = {"create": [], "reply": []}

    class _MessageAPI:
        def create(self, request):
            captured["create"].append(request)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om_flat"),
            )

        def reply(self, request):
            captured["reply"].append(request)
            raise AssertionError("reply API should not be used when reply_to_mode is off")

    adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=_MessageAPI())))

    async def _direct(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch("plugins.platforms.feishu.adapter.asyncio.to_thread", side_effect=_direct):
        result = asyncio.run(
            adapter.send(
                chat_id="oc_chat",
                content="hello",
                reply_to="om_parent",
                metadata={
                    "thread_id": "omt_thread",
                    "reply_to_message_id": "om_trigger",
                    "notify": True,
                },
            )
        )

    assert result.success
    assert result.message_id == "om_flat"
    assert captured["reply"] == []
    request = captured["create"][0]
    assert request.receive_id_type == "chat_id"
    assert request.request_body.receive_id == "oc_chat"


@patch.dict(os.environ, {}, clear=True)
def test_send_default_mode_still_replies_in_feishu_thread():
    adapter = FeishuAdapter(PlatformConfig(reply_to_mode="first"))
    captured = {}

    class _MessageAPI:
        def reply(self, request):
            captured["request"] = request
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om_thread_reply"),
            )

    adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=_MessageAPI())))

    async def _direct(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch("plugins.platforms.feishu.adapter.asyncio.to_thread", side_effect=_direct):
        result = asyncio.run(
            adapter.send(
                chat_id="oc_chat",
                content="hello",
                reply_to="om_parent",
                metadata={"thread_id": "omt_thread"},
            )
        )

    assert result.success
    assert captured["request"].message_id == "om_parent"
    assert captured["request"].request_body.reply_in_thread is True
