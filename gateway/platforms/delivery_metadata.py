"""Adapter delivery context, kept separate from core-owned routing metadata."""

_DELIVERY_METADATA_KEYS = frozenset({"feishu_mention_targets"})


def merge_delivery_metadata(delivery_metadata, core_metadata=None) -> dict | None:
    """Carry approved adapter context without letting it replace the core route."""
    merged = {
        key: value for key, value in delivery_metadata.items()
        if key in _DELIVERY_METADATA_KEYS
    } if isinstance(delivery_metadata, dict) else {}
    merged.update(core_metadata or {})
    return merged or None


def delivery_metadata_for_event(event, core_metadata=None) -> dict | None:
    metadata = getattr(event, "metadata", None)
    delivery = metadata.get("delivery_metadata") if isinstance(metadata, dict) else None
    return merge_delivery_metadata(delivery, core_metadata)
