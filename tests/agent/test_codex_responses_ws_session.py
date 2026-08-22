from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import pytest

from agent.codex_responses_ws_transport import (
    GenericWsNotStartedError,
    GenericWsStartedError,
)
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


class _IdleClosingSocket(_ScriptedSocket):
    """Simulate a peer's normal close after a completed response."""

    def __init__(self, scripts: list[_QueuedRequest]) -> None:
        super().__init__(scripts)
        self._terminal_delivered = False

    def send(self, payload: str) -> None:
        if self._terminal_delivered:
            raise OSError("received 1000 (OK) websocket idle timeout")
        super().send(payload)

    def recv(self, timeout: float | None = None) -> str:
        if self._terminal_delivered:
            raise OSError("received 1000 (OK) websocket idle timeout")
        frame = super().recv(timeout)
        if '"response.done"' in frame:
            self._terminal_delivered = True
        return frame


class _SendClosingSocket(_ScriptedSocket):
    """Simulate an idle close discovered while attempting the next send."""

    def __init__(self, scripts: list[_QueuedRequest]) -> None:
        super().__init__(scripts)
        self._response_create_count = 0

    def send(self, payload: str) -> None:
        message = json.loads(payload)
        if message.get("type") == "response.create":
            self._response_create_count += 1
            if self._response_create_count == 2:
                raise OSError("received 1000 (OK) websocket idle timeout")
        super().send(payload)


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
    session.commit_terminal_state(
        api_kwargs(),
        {
            "type": "response.completed",
            "response": {"id": "resp-1", "status": "completed"},
        },
    )

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


def test_idle_peer_close_reconnects_before_next_incremental_request() -> None:
    first_socket = _IdleClosingSocket(
        [
            _QueuedRequest(
                frames=[
                    json.dumps({"type": "response.created", "response": {"id": "resp-1"}}),
                    json.dumps(
                        {
                            "type": "response.done",
                            "response": {"id": "resp-1", "status": "completed"},
                        }
                    ),
                ],
                cancel_frames=[],
            )
        ]
    )
    second_socket = _ScriptedSocket(
        [
            _QueuedRequest(
                frames=[
                    json.dumps({"type": "response.created", "response": {"id": "resp-2"}}),
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
    sockets = [first_socket, second_socket]
    connect_calls = 0

    def connect(*_args: Any, **_kwargs: Any) -> _ScriptedSocket:
        nonlocal connect_calls
        socket = sockets[connect_calls]
        connect_calls += 1
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

    assert session.stream_request(
        api_kwargs={"model": "gpt-5", "input": [{"id": "a"}]},
        collect_events=lambda events: [event.type for event in events],
        interrupted=lambda: False,
        register_abort=None,
    ) == ["response.created", "response.completed"]

    assert session.stream_request(
        api_kwargs={"model": "gpt-5", "input": [{"id": "a"}, {"id": "b"}]},
        collect_events=lambda events: [event.type for event in events],
        interrupted=lambda: False,
        register_abort=None,
    ) == ["response.created", "response.completed"]

    assert connect_calls == 2
    assert first_socket.closed is True
    assert len(first_socket.sent) == 1
    assert second_socket.sent[0]["previous_response_id"] == "resp-1"
    assert second_socket.sent[0]["input"] == [{"id": "b"}]


def test_idle_close_during_send_reconnects_and_retries_unstarted_request() -> None:
    first_socket = _SendClosingSocket(
        [
            _QueuedRequest(
                frames=[
                    json.dumps({"type": "response.created", "response": {"id": "resp-1"}}),
                    json.dumps(
                        {
                            "type": "response.done",
                            "response": {"id": "resp-1", "status": "completed"},
                        }
                    ),
                ],
                cancel_frames=[],
            )
        ]
    )
    second_socket = _ScriptedSocket(
        [
            _QueuedRequest(
                frames=[
                    json.dumps({"type": "response.created", "response": {"id": "resp-2"}}),
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
    sockets = [first_socket, second_socket]
    connect_calls = 0

    def connect(*_args: Any, **_kwargs: Any) -> _ScriptedSocket:
        nonlocal connect_calls
        socket = sockets[connect_calls]
        connect_calls += 1
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

    session.stream_request(
        api_kwargs={"model": "gpt-5", "input": [{"id": "a"}]},
        collect_events=lambda events: [event.type for event in events],
        interrupted=lambda: False,
        register_abort=None,
    )

    assert session.stream_request(
        api_kwargs={"model": "gpt-5", "input": [{"id": "a"}, {"id": "b"}]},
        collect_events=lambda events: [event.type for event in events],
        interrupted=lambda: False,
        register_abort=None,
    ) == ["response.created", "response.completed"]

    assert connect_calls == 2
    assert len(first_socket.sent) == 1
    assert second_socket.sent[0]["previous_response_id"] == "resp-1"


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
            "default_headers": {"X-Default": "one", "OpenAI-Beta": "responses=v2"},
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
    assert headers["OpenAI-Beta"] == "responses_websockets=2026-02-06"


def test_reset_and_close_clear_snapshot_state() -> None:
    session = make_session(state_enabled=True)
    session.commit_snapshot(api_kwargs(), "resp-1", {"turn": 1})

    session.reset("discard stale response state")

    assert session.snapshot is None

    session.commit_snapshot(api_kwargs(), "resp-2", {"turn": 2})
    session.close()

    assert session.snapshot is None


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
    session.commit_terminal_state(
        {"model": "gpt-5", "input": [{"id": "a"}]},
        {
            "type": "response.completed",
            "response": {"id": "stale-resp", "status": "completed"},
        },
    )

    observed_snapshot_before_second_connect: list[Any] = []

    original_connect = connect

    def connect_with_snapshot_probe(*args: Any, **kwargs: Any) -> _ScriptedSocket:
        if connect_calls["n"] == 1:
            observed_snapshot_before_second_connect.append(session.snapshot)
        return original_connect(*args, **kwargs)

    session.connect = connect_with_snapshot_probe

    result = session.stream_request(
        api_kwargs={"model": "gpt-5", "input": [{"id": "a"}, {"id": "b"}]},
        collect_events=lambda events: [event.type for event in events],
        interrupted=lambda: False,
        register_abort=None,
    )

    assert result == ["response.created", "response.output_text.delta", "response.completed"]
    assert connect_calls["n"] == 2
    assert first.closed is True
    assert observed_snapshot_before_second_connect == [None]
    assert first.sent[0]["previous_response_id"] == "stale-resp"
    assert first.sent[0]["input"] == [{"id": "b"}]
    assert second.sent[0]["input"] == [{"id": "a"}, {"id": "b"}]
    assert "previous_response_id" not in second.sent[0]
    assert session.snapshot is not None
    assert session.snapshot.response_id == "resp-2"


def test_request_serialization_failure_is_not_started_error() -> None:
    socket = _ScriptedSocket([])

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

    with pytest.raises(GenericWsNotStartedError):
        session.stream_request(
            api_kwargs={"model": "gpt-5", "input": [{"bad": object()}]},
            collect_events=lambda events: list(events),
            interrupted=lambda: False,
            register_abort=None,
        )

    assert socket.sent == []


class _QueueSocket:
    def __init__(self) -> None:
        self.frames: queue.Queue[str] = queue.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self, timeout: float | None = None) -> str:
        if self.closed:
            raise OSError("socket closed")
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("poll idle") from exc

    def close(self) -> None:
        self.closed = True


def test_concurrent_stream_requests_are_serialized() -> None:
    socket = _QueueSocket()

    def connect(*_args: Any, **_kwargs: Any) -> _QueueSocket:
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
        idle_timeout=1.0,
        recv_poll_timeout=0.01,
        ping_interval=30.0,
        ping_timeout=60.0,
        close_timeout=5.0,
    )
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def run_request(request_id: str) -> None:
        try:
            result = session.stream_request(
                api_kwargs={"model": "gpt-5", "input": [{"id": request_id}]},
                collect_events=lambda events: [event.type for event in events],
                interrupted=lambda: False,
                register_abort=None,
            )
            results.append(result)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run_request, args=("a",))
    second = threading.Thread(target=run_request, args=("b",))
    first.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(socket.sent) < 1:
        time.sleep(0.005)
    second.start()
    time.sleep(0.05)

    assert len(socket.sent) == 1

    socket.frames.put(json.dumps({"type": "response.done", "response": {"id": "resp-1", "status": "completed"}}))
    first.join(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(socket.sent) < 2:
        time.sleep(0.005)
    socket.frames.put(json.dumps({"type": "response.done", "response": {"id": "resp-2", "status": "completed"}}))
    second.join(timeout=1.0)

    assert errors == []
    assert len(socket.sent) == 2
    assert results == [["response.completed"], ["response.completed"]]


def test_close_unblocks_active_stream_request() -> None:
    socket = _QueueSocket()

    def connect(*_args: Any, **_kwargs: Any) -> _QueueSocket:
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
        idle_timeout=60.0,
        recv_poll_timeout=0.01,
        ping_interval=30.0,
        ping_timeout=60.0,
        close_timeout=5.0,
    )
    errors: list[BaseException] = []

    def run_request() -> None:
        try:
            session.stream_request(
                api_kwargs={"model": "gpt-5", "input": [{"id": "a"}]},
                collect_events=lambda events: [event.type for event in events],
                interrupted=lambda: False,
                register_abort=None,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_request)
    thread.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(socket.sent) < 1:
        time.sleep(0.005)

    assert len(socket.sent) == 1

    session.close()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], GenericWsStartedError)


def test_reset_unblocks_active_stream_request() -> None:
    socket = _QueueSocket()

    def connect(*_args: Any, **_kwargs: Any) -> _QueueSocket:
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
        idle_timeout=60.0,
        recv_poll_timeout=0.01,
        ping_interval=30.0,
        ping_timeout=60.0,
        close_timeout=5.0,
    )
    errors: list[BaseException] = []

    def run_request() -> None:
        try:
            session.stream_request(
                api_kwargs={"model": "gpt-5", "input": [{"id": "a"}]},
                collect_events=lambda events: [event.type for event in events],
                interrupted=lambda: False,
                register_abort=None,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_request)
    thread.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(socket.sent) < 1:
        time.sleep(0.005)

    assert len(socket.sent) == 1

    session.reset("external test reset")
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], GenericWsStartedError)


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


def test_interrupt_then_completed_terminal_still_raises_interrupted() -> None:
    socket = _ScriptedSocket(
        [
            _QueuedRequest(
                frames=[
                    json.dumps({"type": "response.created", "response": {"id": "resp-1"}}),
                    json.dumps(
                        {
                            "type": "response.done",
                            "response": {"id": "resp-1", "status": "completed"},
                        }
                    ),
                ],
                cancel_frames=[],
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
        return checks["n"] > 1

    with pytest.raises(InterruptedError):
        session.stream_request(
            api_kwargs={"model": "gpt-5", "input": [{"id": "a"}]},
            collect_events=lambda events: list(events),
            interrupted=interrupted,
            register_abort=None,
        )

    assert any(item.get("type") == "response.cancel" for item in socket.sent)
