"""Feishu flat-chat reply policy shared by text, media and retry routes."""


def normalize_reply_to_mode(value):
    if value is False:
        return "off"
    if value is None:
        return "first"
    return str(value).strip().lower() or "first"


class FeishuReplyMixin:
    def _reply_to_disabled(self) -> bool:
        return normalize_reply_to_mode(getattr(self, "_reply_to_mode", "first")) == "off"

    def _send_routing_metadata(self, metadata):
        if not metadata or not self._reply_to_disabled():
            return metadata
        routing = dict(metadata)
        routing.pop("thread_id", None)
        routing.pop("reply_to_message_id", None)
        return routing or None
