"""Opt-in Hermes correlation metadata captured in the calling session's context."""

from typing import Any

from gateway.session_context import get_session_env
from tools.mcp_tool_common import _parse_boolish

_HERMES_CONTEXT_FIELDS = (
    "run_id", "task_id", "session_id", "session_key", "tool_call_id",
    "turn_id", "api_request_id", "platform",
)


def _build_hermes_context_meta(**fields: Any) -> dict[str, dict[str, str]]:
    context = {"version": "1"}
    context.update({name: fields[name] for name in _HERMES_CONTEXT_FIELDS
                    if isinstance(fields.get(name), str) and fields[name]})
    return {"io.nous.hermes/context": context}


def capture_hermes_context_meta(server: Any, **kwargs: Any) -> dict | None:
    config = getattr(server, "_config", {}) or {}
    if not _parse_boolish(config.get("forward_hermes_context", False), default=False):
        return None
    return _build_hermes_context_meta(
        run_id=get_session_env("HERMES_RUN_ID"),
        task_id=kwargs.get("task_id"),
        session_id=kwargs.get("session_id") or get_session_env("HERMES_SESSION_ID"),
        session_key=get_session_env("HERMES_SESSION_KEY"),
        tool_call_id=kwargs.get("tool_call_id"),
        turn_id=kwargs.get("turn_id"),
        api_request_id=kwargs.get("api_request_id"),
        platform=get_session_env("HERMES_SESSION_PLATFORM"),
    )
