from __future__ import annotations

import json
import queue
from dataclasses import dataclass
from typing import Any

import pytest

from agent.codex_responses_ws_session import (
    ResponsesRequestSnapshot,
    ResponsesWebsocketSession,
)


def api_kwargs(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": "m",
        "input": [{"id": "a"}],
    }
    base.update(kwargs)
    return base


def make_session(*, state_enabled: bool) -> ResponsesWebsocketSession:
    return ResponsesWebsocketSession(
        state_enabled=state_enabled,
        connect=_fake_connect_factory(),
        client=object(),
        api_key="test-key",
        headers={"X-Explicit": "ok"},
        provider="custom:relay",
        base_url="https://relay.example.com/v1",
        responses_ws_url=None,
        transport="websocket",
        timeout=0.05,
        idle_timeout=0.2,
        recv_poll_timeout=0.01,
        ping_interval=30.0,
        ping_timeout=60.0,
        close_timeout=5.0,
    )


@dataclass
class _QueuedRequest:
    frames: list[Any]
    cancel_frames: list[Any]


class _ScriptedSocket:
    def __init__(self, scripts: list[_QueuedRequest]) -> None:
        self._scripts = scripts
        self._request_index = -1
        self._frame_index = 0
        self._cancel_requested = False
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def __enter__(self) -> "_ScriptedSocket":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def send(self, payload: str) -> None:
        message = json.loads(payload)
        self.sent.append(message)
        if message.get("type") == "response.create":
            self._request_index += 1
            self._frame_index = 0
            self._cancel_requested = False
        elif message.get("type") == "response.cancel":
            self._cancel_requested = True

    def recv(self, timeout: float | None = None) -> str:
        if self.closed:
            raise OSError("socket closed")
        if self._request_index < 0 or self._request_index >= len(self._scripts):
            raise TimeoutError("no scripted request")
        request = self._scripts[self._request_index]
        if self._frame_index < len(request.frames):
            frame = request.frames[self._frame_index]
            self._frame_index += 1
            return frame
        if self._cancel_requested and request.cancel_frames:
            frame = request.cancel_frames.pop(0)
            return frame
        raise TimeoutError("poll idle")

    def close(self) -> None:
        self.closed = True


def _fake_connect_factory():
    sockets: list[_ScriptedSocket] = []
    calls: list[dict[str, Any]] = []

    def connect(*_args: Any, **_kwargs: Any) -> _ScriptedSocket:
        calls.append(dict(_kwargs))
        if not sockets:
            socket = _ScriptedSocket(
                [
                    _QueuedRequest(
                        frames=[
                            json.dumps({"type": "response.created", "response": {"id": "resp-1"}}),
                            json.dumps({"type": "response.output_text.delta", "delta": "one"}),
                            json.dumps(
                                {
                                    "type": "response.done",
                                    "response": {"id": "resp-1", "status": "completed"},
                                }
                            ),
                        ],
                        cancel_frames=[],
                    ),
                    _QueuedRequest(
                        frames=[
                            json.dumps({"type": "response.created", "response": {"id": "resp-2"}}),
                            json.dumps({"type": "response.output_text.delta", "delta": "two"}),
                            json.dumps(
                                {
                                    "type": "response.done",
                                    "response": {"id": "resp-2", "status": "completed"},
                                }
                            ),
                        ],
                        cancel_frames=[],
                    ),
                ]
            )
            sockets.append(socket)
        return sockets[0]

    connect.sockets = sockets  # type: ignore[attr-defined]
    connect.calls = calls  # type: ignore[attr-defined]
    connect.call_count = lambda: len(calls)  # type: ignore[attr-defined]
    return connect


def test_appended_input_builds_incremental_request() -> None:
    session = make_session(state_enabled=True)
    session.commit_terminal_state(api_kwargs(), {"response": {"id": "resp-1"}})

    body, kind = session.build_request(api_kwargs(input=[{"id": "a"}, {"id": "b"}]))

    assert kind == "incremental"
    assert body["previous_response_id"] == "resp-1"
    assert body["input"] == [{"id": "b"}]


def test_request_property_change_forces_full_input() -> None:
    session = make_session(state_enabled=True)
    session.commit_terminal_state(api_kwargs(), {"response": {"id": "resp-1"}})

    body, kind = session.build_request(api_kwargs(model="m2", input=[{"id": "a"}, {"id": "b"}]))

    assert kind == "full"
    assert "previous_response_id" not in body


def test_reordered_input_forces_full_input() -> None:
    session = make_session(state_enabled=True)
    session.commit_terminal_state(api_kwargs(input=[{"id": "a"}, {"id": "b"}]), {"response": {"id": "resp-1"}})

    body, kind = session.build_request(api_kwargs(input=[{"id": "b"}, {"id": "a"}]))

    assert kind == "full"
    assert "previous_response_id" not in body
    assert body["input"] == [{"id": "b"}, {"id": "a"}]


def test_missing_state_forces_full_input() -> None:
    session = make_session(state_enabled=True)

    body, kind = session.build_request(api_kwargs(input=[{"id": "a"}, {"id": "b"}]))

    assert kind == "full"
    assert "previous_response_id" not in body


def test_disabled_state_forces_full_input() -> None:
    session = make_session(state_enabled=False)
    session.commit_terminal_state(api_kwargs(), {"response": {"id": "resp-1"}})

    body, kind = session.build_request(api_kwargs(input=[{"id": "a"}, {"id": "b"}]))

    assert kind == "full"
    assert "previous_response_id" not in body


def test_missing_turn_state_disables_incremental_request() -> None:
    snapshot = ResponsesRequestSnapshot.from_api_kwargs(api_kwargs(), "resp-1", None)

    assert not snapshot.can_increment(api_kwargs(input=[{"id": "a"}, {"id": "b"}]), state_enabled=True)


def test_commit_snapshot_alias_records_state() -> None:
    session = make_session(state_enabled=True)
    session.commit_snapshot(api_kwargs(), "resp-1", {"turn": 1})

    assert session.snapshot is not None
    assert session.snapshot.response_id == "resp-1"
    assert session.snapshot.turn_state == {"turn": 1}


def test_snapshot_does_not_mutate_source_kwargs() -> None:
    request = api_kwargs(
        tools=[{"type": "function", "name": "tool", "parameters": {}}],
        reasoning={"effort": "medium"},
        include=["reasoning.encrypted_content"],
        tool_choice="auto",
        parallel_tool_calls=True,
        service_tier="default",
        prompt_cache_key="key",
        text={"format": "text"},
    )

    snapshot = ResponsesRequestSnapshot.from_api_kwargs(request, "resp-1", {"turn": 1})
    request["input"].append({"id": "b"})

    assert snapshot.incremental_input(api_kwargs(input=[{"id": "a"}, {"id": "b"}])) == [{"id": "b"}]
    assert request["input"] == [{"id": "a"}, {"id": "b"}]


def test_snapshot_rejects_non_copyable_mutable_values() -> None:
    class NonCopyableMutable:
        def __init__(self) -> None:
            self.payload = []

        def __deepcopy__(self, memo: dict[int, Any]) -> Any:
            raise TypeError("no deepcopy")

    request = api_kwargs()
    request["metadata"] = NonCopyableMutable()

    try:
        ResponsesRequestSnapshot.from_api_kwargs(request, "resp-1", {"turn": 1})
    except TypeError as exc:
        assert "Unsupported non-copyable request value" in str(exc)
    else:
        raise AssertionError("expected TypeError")


def test_two_requests_share_one_connection_and_reuse_snapshot() -> None:
    connect = _fake_connect_factory()
    session = ResponsesWebsocketSession(
        state_enabled=True,
        connect=connect,
        client=object(),
        api_key="test-key",
        headers={"X-Explicit": "ok"},
        provider="custom:relay",
        base_url="https://relay.example.com/v1",
        transport="websocket",
        timeout=0.05,
        idle_timeout=0.2,
        recv_poll_timeout=0.01,
        ping_interval=30.0,
        ping_timeout=60.0,
        close_timeout=5.0,
    )

    first = session.stream_request(
        api_kwargs={"model": "gpt-5", "input": [{"id": "a"}]},
        collect_events=lambda events: [event.type for event in events],
        interrupted=lambda: False,
        register_abort=None,
    )
    second = session.stream_request(
        api_kwargs={"model": "gpt-5", "input": [{"id": "a"}, {"id": "b"}]},
        collect_events=lambda events: [event.type for event in events],
        interrupted=lambda: False,
        register_abort=None,
    )

    socket = connect.sockets[0]
    assert first == ["response.created", "response.output_text.delta", "response.completed"]
    assert second == ["response.created", "response.output_text.delta", "response.completed"]
    assert len(socket.sent) == 2
    assert socket.sent[0]["type"] == "response.create"
    assert socket.sent[1]["type"] == "response.create"
    assert socket.sent[1]["previous_response_id"] == "resp-1"
    assert socket.sent[1]["input"] == [{"id": "b"}]
    assert not session.is_closed()


def test_session_does_not_prewarm_connection() -> None:
    connect = _fake_connect_factory()
    session = ResponsesWebsocketSession(
        state_enabled=True,
        connect=connect,
        client=object(),
        api_key="test-key",
        headers={},
        provider="custom:relay",
        base_url="https://relay.example.com/v1",
        transport="websocket",
        timeout=0.05,
        idle_timeout=0.2,
        recv_poll_timeout=0.01,
        ping_interval=30.0,
        ping_timeout=60.0,
        close_timeout=5.0,
    )

    assert connect.call_count() == 0
    assert session.is_closed() is False


def test_stream_request_merges_headers_and_beta_header() -> None:
    connect = _fake_connect_factory()
    client = type(
        "_Client",
        (),
        {
            "default_headers": {"X-Default": "one"},
            "_custom_headers": {"X-Custom": "two", "Authorization": "Bearer override"},
        },
    )()
    session = ResponsesWebsocketSession(
        state_enabled=True,
        connect=connect,
        client=client,
        api_key="test-key",
        headers={"X-Explicit": "three"},
        provider="custom:relay",
        base_url="https://relay.example.com/v1",
        transport="websocket",
        timeout=0.05,
        idle_timeout=0.2,
        recv_poll_timeout=0.01,
        ping_interval=30.0,
        ping_timeout=60.0,
        close_timeout=5.0,
    )

    session.stream_request(
        api_kwargs={
            "model": "gpt-5",
            "input": [{"id": "a"}],
            "extra_headers": {"X-Request": "four"},
        },
        collect_events=lambda events: [event.type for event in events],
        interrupted=lambda: False,
        register_abort=None,
    )

    headers = connect.calls[0]["headers"]
    assert headers["X-Default"] == "one"
    assert headers["X-Custom"] == "two"
    assert headers["X-Explicit"] == "three"
    assert headers["X-Request"] == "four"
    assert headers["Authorization"] == "Bearer override"
    assert headers["OpenAI-Beta"] == "responses=v2"


def test_previous_response_not_found_retries_with_full_input() -> None:
    first = _ScriptedSocket(
        [
            _QueuedRequest(
                frames=[
                    json.dumps(
                        {
                            "type": "error",
                            "status_code": 404,
                            "error": {
                                "code": "previous_response_not_found",
                                "message": "previous_response_not_found",
                            },
                        }
                    )
                ],
                cancel_frames=[],
            )
        ]
    )
    second = _ScriptedSocket(
        [
            _QueuedRequest(
                frames=[
                    json.dumps({"type": "response.created", "response": {"id": "resp-2"}}),
                    json.dumps({"type": "response.output_text.delta", "delta": "two"}),
                    json.dumps(
                        {
                            "type": "response.done",
                            "response": {"id": "resp-2", "status": "completed"},
                        }
                    ),
                ],
                cancel_frames=[],
            )
        ]
    )
    connect_calls = {"n": 0}

    def connect(*_args: Any, **_kwargs: Any) -> _ScriptedSocket:
        connect_calls["n"] += 1
        return first if connect_calls["n"] == 1 else second

    session = ResponsesWebsocketSession(
        state_enabled=True,
        connect=connect,
        client=object(),
        api_key="test-key",
        headers={},
        provider="custom:relay",
        base_url="https://relay.example.com/v1",
        transport="websocket",
        timeout=0.05,
        idle_timeout=0.2,
        recv_poll_timeout=0.01,
        ping_interval=30.0,
        ping_timeout=60.0,
        close_timeout=5.0,
    )

    result = session.stream_request(
        api_kwargs={"model": "gpt-5", "input": [{"id": "a"}, {"id": "b"}]},
        collect_events=lambda events: [event.type for event in events],
        interrupted=lambda: False,
        register_abort=None,
    )

    assert result == ["response.created", "response.output_text.delta", "response.completed"]
    assert connect_calls["n"] == 2
    assert first.closed is True
    assert second.sent[0]["input"] == [{"id": "a"}, {"id": "b"}]
    assert "previous_response_id" not in second.sent[0]


def test_close_stops_pump_and_clears_socket() -> None:
    connect = _fake_connect_factory()
    session = ResponsesWebsocketSession(
        state_enabled=True,
        connect=connect,
        client=object(),
        api_key="test-key",
        headers={},
        provider="custom:relay",
        base_url="https://relay.example.com/v1",
        transport="websocket",
        timeout=0.05,
        idle_timeout=0.2,
        recv_poll_timeout=0.01,
        ping_interval=30.0,
        ping_timeout=60.0,
        close_timeout=5.0,
    )

    session.stream_request(
        api_kwargs={"model": "gpt-5", "input": [{"id": "a"}]},
        collect_events=lambda events: list(events),
        interrupted=lambda: False,
        register_abort=None,
    )
    session.close()

    socket = connect.sockets[0]
    assert socket.closed is True
    assert session.is_closed() is True


def test_interrupt_sends_response_cancel() -> None:
    socket = _ScriptedSocket(
        [
            _QueuedRequest(
                frames=[
                    json.dumps({"type": "response.created", "response": {"id": "resp-1"}}),
                    json.dumps({"type": "response.output_text.delta", "delta": "one"}),
                ],
                cancel_frames=[
                    json.dumps(
                        {
                            "type": "response.done",
                            "response": {"id": "resp-1", "status": "canceled"},
                        }
                    )
                ],
            )
        ]
    )

    def connect(*_args: Any, **_kwargs: Any) -> _ScriptedSocket:
        return socket

    session = ResponsesWebsocketSession(
        state_enabled=True,
        connect=connect,
        client=object(),
        api_key="test-key",
        headers={},
        provider="custom:relay",
        base_url="https://relay.example.com/v1",
        transport="websocket",
        timeout=0.05,
        idle_timeout=0.2,
        recv_poll_timeout=0.01,
        ping_interval=30.0,
        ping_timeout=60.0,
        close_timeout=5.0,
    )

    checks = {"n": 0}

    def interrupted() -> bool:
        checks["n"] += 1
        return checks["n"] > 2

    with pytest.raises(InterruptedError):
        session.stream_request(
            api_kwargs={"model": "gpt-5", "input": [{"id": "a"}]},
            collect_events=lambda events: list(events),
            interrupted=interrupted,
            register_abort=None,
        )

    assert any(item.get("type") == "response.cancel" for item in socket.sent)
