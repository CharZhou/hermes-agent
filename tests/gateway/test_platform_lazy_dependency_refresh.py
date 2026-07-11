"""Regression coverage for stale, lazy-installed platform SDKs."""

import importlib
import sys
import types
from contextlib import ExitStack
from unittest.mock import patch

import pytest


@pytest.mark.parametrize(
    ("module_name", "available_name", "check_name", "validator_name", "feature"),
    [
        (
            "plugins.platforms.discord.adapter",
            "DISCORD_AVAILABLE",
            "check_discord_requirements",
            "ensure",
            "platform.discord",
        ),
        (
            "plugins.platforms.feishu.adapter",
            "FEISHU_AVAILABLE",
            "check_feishu_requirements",
            "ensure_and_bind",
            "platform.feishu",
        ),
        (
            "plugins.platforms.slack.adapter",
            "SLACK_AVAILABLE",
            "check_slack_requirements",
            "ensure_and_bind",
            "platform.slack",
        ),
        (
            "plugins.platforms.telegram.adapter",
            "TELEGRAM_AVAILABLE",
            "check_telegram_requirements",
            "ensure",
            "platform.telegram",
        ),
    ],
)
def test_importable_platform_sdk_still_validates_pinned_dependencies(
    monkeypatch,
    module_name,
    available_name,
    check_name,
    validator_name,
    feature,
):
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, available_name, True)

    with ExitStack() as stack:
        if validator_name == "ensure_and_bind":
            validator = stack.enter_context(
                patch("tools.lazy_deps.ensure_and_bind", return_value=True)
            )
        else:
            validator = stack.enter_context(patch("tools.lazy_deps.ensure"))
        stack.enter_context(patch("tools.lazy_deps.feature_missing", return_value=()))

        assert getattr(module, check_name)() is True

    validator.assert_called_once()
    assert validator.call_args.args[0] == feature
    assert validator.call_args.kwargs["prompt"] is False


def test_feishu_refresh_evicts_stale_sdk_modules(monkeypatch):
    adapter = importlib.import_module("plugins.platforms.feishu.adapter")
    stale_root = types.ModuleType("lark_oapi")
    stale_ws = types.ModuleType("lark_oapi.ws")
    monkeypatch.setitem(sys.modules, "lark_oapi", stale_root)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws", stale_ws)
    monkeypatch.setattr(adapter, "FEISHU_AVAILABLE", True)

    with (
        patch(
            "tools.lazy_deps.feature_missing",
            return_value=("lark-oapi==1.6.8",),
        ),
        patch("tools.lazy_deps.ensure_and_bind", return_value=True),
    ):
        assert adapter.check_feishu_requirements() is True

    assert "lark_oapi" not in sys.modules
    assert "lark_oapi.ws" not in sys.modules
