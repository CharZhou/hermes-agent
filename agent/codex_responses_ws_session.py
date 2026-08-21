from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


def _copy_value(value: Any) -> Any:
    try:
        return deepcopy(value)
    except Exception as exc:
        raise TypeError(f"Unsupported non-copyable request value of type {type(value).__name__}") from exc


def _copy_request_kwargs(api_kwargs: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _copy_value(value) for key, value in dict(api_kwargs).items()}


def _input_items(value: Any) -> tuple[Any, ...]:
    if isinstance(value, list):
        return tuple(_copy_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_copy_value(item) for item in value)
    if value is None:
        return ()
    return (_copy_value(value),)


def _terminal_response_id(terminal_event: Any) -> str | None:
    candidates: list[Any] = []
    if isinstance(terminal_event, Mapping):
        candidates.extend([terminal_event.get("response_id"), terminal_event.get("id")])
        response = terminal_event.get("response")
        if isinstance(response, Mapping):
            candidates.extend([response.get("id"), response.get("response_id")])
    else:
        candidates.extend([getattr(terminal_event, "response_id", None), getattr(terminal_event, "id", None)])
        response = getattr(terminal_event, "response", None)
        if response is not None:
            candidates.extend([getattr(response, "id", None), getattr(response, "response_id", None)])

    for candidate in candidates:
        if isinstance(candidate, str):
            candidate = candidate.strip()
            if candidate:
                return candidate
    return None


def _normalize_request(api_kwargs: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[Any, ...]]:
    copied = _copy_request_kwargs(api_kwargs)
    input_value = copied.pop("input", [])
    return copied, _input_items(input_value)


@dataclass(frozen=True, slots=True)
class ResponsesRequestSnapshot:
    request_body: Mapping[str, Any]
    input_items: tuple[Any, ...]
    response_id: str | None
    turn_state: Any

    @classmethod
    def from_api_kwargs(
        cls,
        api_kwargs: Mapping[str, Any],
        response_id: str | None,
        turn_state: Any,
    ) -> ResponsesRequestSnapshot:
        request_body, input_items = _normalize_request(api_kwargs)
        return cls(
            request_body=MappingProxyType(request_body),
            input_items=input_items,
            response_id=response_id,
            turn_state=_copy_value(turn_state),
        )

    def can_increment(
        self,
        next_api_kwargs: Mapping[str, Any],
        *,
        state_enabled: bool,
    ) -> bool:
        if not state_enabled or not self.response_id or self.turn_state is None:
            return False
        next_body, next_input = _normalize_request(next_api_kwargs)
        return dict(self.request_body) == next_body and next_input[: len(self.input_items)] == self.input_items and len(next_input) > len(self.input_items)

    def incremental_input(self, next_api_kwargs: Mapping[str, Any]) -> list[Any]:
        _, next_input = _normalize_request(next_api_kwargs)
        if next_input[: len(self.input_items)] != self.input_items:
            return [_copy_value(item) for item in next_input]
        return [_copy_value(item) for item in next_input[len(self.input_items) :]]


class ResponsesWebsocketSession:
    def __init__(self, *, state_enabled: bool) -> None:
        self.state_enabled = bool(state_enabled)
        self._snapshot: ResponsesRequestSnapshot | None = None

    @property
    def snapshot(self) -> ResponsesRequestSnapshot | None:
        return self._snapshot

    def commit_snapshot(
        self,
        api_kwargs: Mapping[str, Any],
        response_id: str | None,
        turn_state: Any,
    ) -> None:
        self._snapshot = ResponsesRequestSnapshot.from_api_kwargs(
            api_kwargs,
            response_id,
            turn_state,
        )

    def commit_terminal_state(self, api_kwargs: Mapping[str, Any], terminal_event: Any) -> None:
        self.commit_snapshot(api_kwargs, _terminal_response_id(terminal_event), terminal_event)

    def build_request(self, api_kwargs: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        body, input_items = _normalize_request(api_kwargs)
        snapshot = self._snapshot
        if snapshot and snapshot.can_increment(api_kwargs, state_enabled=self.state_enabled):
            body["input"] = snapshot.incremental_input(api_kwargs)
            body["previous_response_id"] = snapshot.response_id
            return body, "incremental"
        body["input"] = [_copy_value(item) for item in input_items]
        return body, "full"
