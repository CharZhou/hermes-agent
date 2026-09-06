"""Chat-scoped native mentions, persisted observations and trusted outbound rendering."""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, TYPE_CHECKING

from hermes_constants import get_hermes_home
from utils import atomic_json_write

if TYPE_CHECKING:
    from plugins.platforms.feishu.adapter import FeishuMentionRef

logger = logging.getLogger(__name__)
_FEISHU_MENTION_TARGETS_METADATA_KEY = "feishu_mention_targets"
_FEISHU_MENTION_REGISTRY_FILENAME = "feishu_mention_targets.json"
_FEISHU_MENTION_REGISTRY_VERSION = 2
_FEISHU_MENTION_REGISTRY_MAX_CHATS = 1000
_FEISHU_MENTION_REGISTRY_MAX_TARGETS_PER_CHAT = 200
_FEISHU_MENTION_REGISTRY_TTL_SECONDS = 30 * 24 * 60 * 60
_LITERAL_NATIVE_AT_TAG_RE = re.compile(r'<at\s+user_id="([^"]+)"></at>')
_NATIVE_AT_TOKEN_NONCE = uuid.uuid4().hex
_NATIVE_AT_TOKEN_RE = re.compile(rf"__HERMES_FEISHU_AT_{_NATIVE_AT_TOKEN_NONCE}_([A-Za-z0-9_-]+)__")
_MARKDOWN_FENCE_OPEN_RE = re.compile(r"^```([^\n`]*)\s*$")
_MARKDOWN_FENCE_CLOSE_RE = re.compile(r"^```\s*$")


def _encode_native_at_token(open_id: str) -> str:
    encoded = base64.urlsafe_b64encode(str(open_id).encode("utf-8")).decode("ascii").rstrip("=")
    return f"__HERMES_FEISHU_AT_{_NATIVE_AT_TOKEN_NONCE}_{encoded}__"


def _decode_native_at_token(encoded: str) -> str:
    try:
        padded = encoded + ("=" * (-len(encoded) % 4))
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def _escape_literal_native_at_tags(content: str) -> str:
    if not content:
        return content
    return _LITERAL_NATIVE_AT_TAG_RE.sub(
        lambda match: match.group(0).replace("<", "&lt;").replace(">", "&gt;"),
        content,
    )


def _render_native_at_tokens_as_text(content: str) -> str:
    if not content:
        return content

    def _replace(match: re.Match[str]) -> str:
        open_id = _decode_native_at_token(match.group(1))
        return f'<at user_id="{open_id}"></at>' if open_id else match.group(0)

    return _NATIVE_AT_TOKEN_RE.sub(_replace, content)


def _render_plain_text_payload_content(content: str) -> str:
    return _render_native_at_tokens_as_text(_escape_literal_native_at_tags(content))


def _build_markdown_post_row(content: str) -> List[Dict[str, str]]:
    row: List[Dict[str, str]] = []
    position = 0
    for match in _NATIVE_AT_TOKEN_RE.finditer(content):
        if match.start() > position:
            row.append(
                {
                    "tag": "md",
                    "text": _escape_literal_native_at_tags(content[position:match.start()]),
                }
            )
        open_id = _decode_native_at_token(match.group(1))
        if open_id:
            row.append({"tag": "at", "user_id": open_id})
        else:
            row.append({"tag": "md", "text": match.group(0)})
        position = match.end()
    if position < len(content):
        row.append({"tag": "md", "text": _escape_literal_native_at_tags(content[position:])})
    return row or [{"tag": "md", "text": ""}]


def _build_mention_targets(mentions: Sequence[FeishuMentionRef]) -> Dict[str, set[str]]:
    targets: Dict[str, set[str]] = {}
    for ref in mentions:
        if ref.is_self or ref.is_all:
            continue
        name = (ref.name or "").strip()
        open_id = (ref.open_id or "").strip()
        if name and open_id:
            targets.setdefault(name, set()).add(open_id)
    return targets


def _compile_feishu_mentions(
    content: str,
    *,
    mention_targets: Optional[Dict[str, str]] = None,
) -> str:
    if not content or not mention_targets:
        return content

    sorted_targets = sorted(
        (
            (str(name).strip(), str(open_id).strip())
            for name, open_id in mention_targets.items()
            if str(name or "").strip() and str(open_id or "").strip()
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    if not sorted_targets:
        return content

    def _compile_line(line: str) -> str:
        compiled_line = line
        for name, open_id in sorted_targets:
            boundary = r"(?=$|[\s.,;:!?、，。；：！？(){}\[\]<>\"'`])"
            pattern = re.compile(rf"^(?P<indent>[ \t]*)@{re.escape(name)}{boundary}")
            compiled_line = pattern.sub(
                lambda match, open_id=open_id: (
                    f"{match.group('indent')}{_encode_native_at_token(open_id)}"
                ),
                compiled_line,
            )
        return compiled_line

    parts: List[str] = []
    in_code_block = False
    for line in content.splitlines(keepends=True):
        stripped_line = line.strip()
        is_fence = bool(
            _MARKDOWN_FENCE_CLOSE_RE.match(stripped_line)
            if in_code_block
            else _MARKDOWN_FENCE_OPEN_RE.match(stripped_line)
        )
        if is_fence:
            parts.append(line)
            in_code_block = not in_code_block
            continue
        parts.append(line if in_code_block else _compile_line(line))
    return "".join(parts)


def _merge_delivery_mention_targets(
    targets: Dict[str, str],
    raw_targets: Any,
) -> Dict[str, str]:
    merged = dict(targets)
    if not isinstance(raw_targets, dict):
        return merged
    for raw_name, raw_value in raw_targets.items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        if isinstance(raw_value, str):
            observed_ids = {raw_value.strip()} if raw_value.strip() else set()
        elif isinstance(raw_value, (list, tuple, set, frozenset)):
            observed_ids = {
                str(item).strip() for item in raw_value if str(item or "").strip()
            }
        else:
            observed_ids = set()
        if len(observed_ids) == 1:
            merged[name] = next(iter(observed_ids))
        else:
            merged.pop(name, None)
    return merged


class FeishuMentionMixin:
    """Maintain per-chat observations without treating ambiguous names as identities."""

    def _init_mention_registry(self) -> None:
        self._mention_registry_path = get_hermes_home() / _FEISHU_MENTION_REGISTRY_FILENAME
        self._mention_registry_lock = threading.Lock()
        self._mention_registry = {"version": _FEISHU_MENTION_REGISTRY_VERSION, "chats": {}}
        self._load_mention_registry()

    def _compile_outbound_mentions(self, chat_id: str, content: str, metadata=None) -> str:
        targets = _merge_delivery_mention_targets(
            self._mention_targets_for_chat(chat_id),
            (metadata or {}).get(_FEISHU_MENTION_TARGETS_METADATA_KEY),
        )
        for name in self._ambiguous_mention_names_for_chat(chat_id):
            targets.pop(name, None)
        return _compile_feishu_mentions(content, mention_targets=targets)

    def _ensure_mention_registry_state(self) -> None:
        if not hasattr(self, "_mention_registry_lock"):
            self._mention_registry_lock = threading.Lock()
        if not hasattr(self, "_mention_registry_path"):
            self._mention_registry_path = None
        if not hasattr(self, "_mention_registry"):
            self._mention_registry = {
                "version": _FEISHU_MENTION_REGISTRY_VERSION,
                "chats": {},
            }

    @staticmethod
    def _normalize_mention_registry(raw: Any) -> Dict[str, Any]:
        registry: Dict[str, Any] = {
            "version": _FEISHU_MENTION_REGISTRY_VERSION,
            "chats": {},
        }
        if not isinstance(raw, dict) or not isinstance(raw.get("chats"), dict):
            return registry
        migration_time = time.time()

        for raw_chat_id, raw_chat in raw["chats"].items():
            chat_id = str(raw_chat_id or "").strip()
            if not chat_id or not isinstance(raw_chat, dict):
                continue
            raw_targets = raw_chat.get("targets", raw_chat)
            if not isinstance(raw_targets, dict):
                continue
            try:
                chat_updated_at = float(raw_chat.get("updated_at") or 0.0)
            except (TypeError, ValueError):
                chat_updated_at = 0.0
            migration_timestamp = chat_updated_at if chat_updated_at > 0 else migration_time
            targets: Dict[str, Dict[str, Any]] = {}
            for raw_name, raw_entry in raw_targets.items():
                name = str(raw_name or "").strip()
                if not name:
                    continue
                observations: Dict[str, float] = {}
                if isinstance(raw_entry, str):
                    open_id = raw_entry.strip()
                    if open_id:
                        observations[open_id] = migration_timestamp
                elif isinstance(raw_entry, dict):
                    try:
                        updated_at = float(raw_entry.get("updated_at") or 0.0)
                    except (TypeError, ValueError):
                        updated_at = 0.0
                    entry_timestamp = updated_at if updated_at > 0 else migration_timestamp
                    raw_observations = raw_entry.get("observations")
                    if isinstance(raw_observations, dict):
                        for raw_open_id, raw_seen_at in raw_observations.items():
                            open_id = str(raw_open_id or "").strip()
                            if not open_id:
                                continue
                            try:
                                seen_at = float(raw_seen_at or 0.0)
                            except (TypeError, ValueError):
                                continue
                            observations[open_id] = seen_at if seen_at > 0 else entry_timestamp
                    else:
                        open_id = str(raw_entry.get("open_id") or "").strip()
                        if open_id:
                            observations[open_id] = entry_timestamp
                        for item in raw_entry.get("open_ids", []) or []:
                            candidate = str(item or "").strip()
                            if candidate:
                                observations[candidate] = entry_timestamp
                if observations:
                    targets[name] = {"observations": observations}
            if targets:
                registry["chats"][chat_id] = {
                    "targets": targets,
                    "updated_at": chat_updated_at or migration_time,
                }
        return registry

    def _load_mention_registry(self) -> None:
        self._ensure_mention_registry_state()
        path = self._mention_registry_path
        if path is None:
            return
        try:
            if not path.exists():
                return
            with open(path, "r", encoding="utf-8") as handle:
                self._mention_registry = self._normalize_mention_registry(json.load(handle))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("[Feishu] Failed to load mention registry from %s", path, exc_info=True)
            self._mention_registry = {
                "version": _FEISHU_MENTION_REGISTRY_VERSION,
                "chats": {},
            }

    def _persist_mention_registry_locked(self) -> None:
        path = self._mention_registry_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(path, self._mention_registry, indent=None)
        except OSError:
            logger.warning("[Feishu] Failed to persist mention registry to %s", path, exc_info=True)

    @staticmethod
    def _prune_mention_observations(entry: Dict[str, Any], now: float) -> bool:
        observations = entry.get("observations")
        if not isinstance(observations, dict):
            entry["observations"] = {}
            return True
        stale = []
        for open_id, seen_at in observations.items():
            try:
                timestamp = float(seen_at or 0.0)
            except (TypeError, ValueError):
                stale.append(open_id)
                continue
            if (
                timestamp > 0
                and _FEISHU_MENTION_REGISTRY_TTL_SECONDS > 0
                and now - timestamp >= _FEISHU_MENTION_REGISTRY_TTL_SECONDS
            ):
                stale.append(open_id)
        for open_id in stale:
            observations.pop(open_id, None)
        return bool(stale)

    def _prune_mention_registry_locked(self, now: float) -> bool:
        changed = False
        chats = self._mention_registry.setdefault("chats", {})
        for chat_id, chat in list(chats.items()):
            if not isinstance(chat, dict):
                chats.pop(chat_id, None)
                changed = True
                continue
            targets = chat.get("targets")
            if not isinstance(targets, dict):
                chats.pop(chat_id, None)
                changed = True
                continue
            for name, entry in list(targets.items()):
                if not isinstance(entry, dict) or self._prune_mention_observations(entry, now):
                    changed = True
                if not isinstance(entry, dict) or not entry.get("observations"):
                    targets.pop(name, None)
                    changed = True
            if len(targets) > _FEISHU_MENTION_REGISTRY_MAX_TARGETS_PER_CHAT:
                stale_names = sorted(
                    targets,
                    key=lambda name: max(
                        (targets[name].get("observations") or {"": 0.0}).values()
                    ),
                )[: len(targets) - _FEISHU_MENTION_REGISTRY_MAX_TARGETS_PER_CHAT]
                for name in stale_names:
                    targets.pop(name, None)
                changed = True
            if not targets:
                chats.pop(chat_id, None)
                changed = True
        if len(chats) > _FEISHU_MENTION_REGISTRY_MAX_CHATS:
            stale_chat_ids = sorted(
                chats,
                key=lambda chat_id: float((chats.get(chat_id) or {}).get("updated_at") or 0.0),
            )[: len(chats) - _FEISHU_MENTION_REGISTRY_MAX_CHATS]
            for chat_id in stale_chat_ids:
                chats.pop(chat_id, None)
            changed = True
        return changed

    def _update_mention_registry(
        self,
        chat_id: str,
        targets: Dict[str, set[str]],
    ) -> None:
        chat_key = str(chat_id or "").strip()
        if not chat_key or not targets:
            return
        self._ensure_mention_registry_state()
        now = time.time()
        with self._mention_registry_lock:
            self._prune_mention_registry_locked(now)
            chats = self._mention_registry.setdefault("chats", {})
            chat = chats.setdefault(chat_key, {"targets": {}, "updated_at": now})
            chat["updated_at"] = now
            chat_targets = chat.setdefault("targets", {})

            for raw_name, raw_open_ids in targets.items():
                name = str(raw_name or "").strip()
                open_ids = {
                    str(item).strip() for item in raw_open_ids if str(item or "").strip()
                }
                if not name or not open_ids:
                    continue
                entry = chat_targets.setdefault(name, {"observations": {}})
                observations = entry.setdefault("observations", {})
                for open_id in open_ids:
                    observations[open_id] = now
                if len(observations) > 1:
                    logger.warning(
                        "[Feishu] Ambiguous mention name in chat %s for %r: %s",
                        chat_key,
                        name,
                        sorted(observations),
                    )

            self._prune_mention_registry_locked(now)
            self._persist_mention_registry_locked()

    def _mention_targets_for_chat(self, chat_id: str) -> Dict[str, str]:
        chat_key = str(chat_id or "").strip()
        if not chat_key:
            return {}
        self._ensure_mention_registry_state()
        now = time.time()
        with self._mention_registry_lock:
            changed = self._prune_mention_registry_locked(now)
            chat = self._mention_registry.get("chats", {}).get(chat_key, {})
            targets = chat.get("targets", {}) if isinstance(chat, dict) else {}
            result: Dict[str, str] = {}
            if isinstance(targets, dict):
                for raw_name, raw_entry in targets.items():
                    if not isinstance(raw_entry, dict):
                        continue
                    observations = raw_entry.get("observations")
                    if not isinstance(observations, dict) or len(observations) != 1:
                        continue
                    name = str(raw_name or "").strip()
                    open_id = str(next(iter(observations)) or "").strip()
                    if name and open_id:
                        result[name] = open_id
            if changed:
                self._persist_mention_registry_locked()
            return result

    def _ambiguous_mention_names_for_chat(self, chat_id: str) -> set[str]:
        chat_key = str(chat_id or "").strip()
        if not chat_key:
            return set()
        self._ensure_mention_registry_state()
        now = time.time()
        with self._mention_registry_lock:
            changed = self._prune_mention_registry_locked(now)
            chat = self._mention_registry.get("chats", {}).get(chat_key, {})
            targets = chat.get("targets", {}) if isinstance(chat, dict) else {}
            result = {
                str(name)
                for name, entry in targets.items()
                if isinstance(entry, dict)
                and isinstance(entry.get("observations"), dict)
                and len(entry["observations"]) > 1
            }
            if changed:
                self._persist_mention_registry_locked()
            return result
