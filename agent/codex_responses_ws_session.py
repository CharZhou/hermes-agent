from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from itertools import count
from types import MappingProxyType
from typing import Any

from agent.codex_responses_ws_transport import (
    GenericWsNotStartedError,
    GenericWsRejectedError,
    GenericWsStartedError,
    _TERMINAL_EVENT_TYPES,
    _build_headers,
    _event_namespace,
    _normalize_terminal_event,
    _recv_frame,
    _server_error_message,
    _server_error_status,
    build_ws_wire_body,
    resolve_responses_ws_url,
)


def _copy_value(value: Any) -> Any:
    try:
        return deepcopy(value)
    except Exception as exc:
        raise TypeError(
            f"Unsupported non-copyable request value of type {type(value).__name__}"
        ) from exc


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
        candidates.extend(
            [getattr(terminal_event, "response_id", None), getattr(terminal_event, "id", None)]
        )
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


def _close_socket(websocket: Any) -> None:
    close = getattr(websocket, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _ensure_responses_beta_header(headers: Mapping[str, str]) -> dict[str, str]:
    result = dict(headers)
    existing_name: str | None = None
    for key, value in result.items():
        if key.lower() == "openai-beta":
            existing_name = key
            break
    token = "responses_websockets=2026-02-06"
    if existing_name is None:
        result["OpenAI-Beta"] = token
        return result
    result[existing_name] = token
    return result


def _is_previous_response_not_found(error: BaseException) -> bool:
    body = getattr(error, "body", None)
    if isinstance(body, Mapping):
        err = body.get("error")
        if isinstance(err, Mapping):
            code = str(err.get("code") or "").strip().lower()
            message = str(err.get("message") or "").strip().lower()
            return code == "previous_response_not_found" or message == "previous_response_not_found"
    return False


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
        return (
            dict(self.request_body) == next_body
            and next_input[: len(self.input_items)] == self.input_items
            and len(next_input) > len(self.input_items)
        )

    def incremental_input(self, next_api_kwargs: Mapping[str, Any]) -> list[Any]:
        _, next_input = _normalize_request(next_api_kwargs)
        if next_input[: len(self.input_items)] != self.input_items:
            return [_copy_value(item) for item in next_input]
        return [_copy_value(item) for item in next_input[len(self.input_items) :]]


@dataclass(slots=True)
class _WorkerCommand:
    kind: str
    generation: int
    request_id: int | None = None
    api_kwargs: Mapping[str, Any] | None = None
    request_body: Mapping[str, Any] | None = None


@dataclass(slots=True)
class _WorkerEvent:
    kind: str
    generation: int
    request_id: int
    payload: Any


class ResponsesWebsocketSession:
    def __init__(
        self,
        *,
        state_enabled: bool,
        connect: Callable[..., Any],
        client: Any,
        api_key: Any,
        headers: Mapping[str, Any] | None,
        provider: Any,
        base_url: Any,
        transport: Any,
        timeout: float,
        idle_timeout: float | None,
        recv_poll_timeout: float,
        ping_interval: float | None,
        ping_timeout: float | None,
        close_timeout: float | None,
        responses_ws_url: Any = None,
    ) -> None:
        self.state_enabled = bool(state_enabled)
        self.connect = connect
        self.client = client
        self.api_key = api_key
        self.headers = headers
        self.provider = provider
        self.base_url = base_url
        self.transport = transport
        self.timeout = float(timeout)
        self.idle_timeout = idle_timeout
        self.recv_poll_timeout = float(recv_poll_timeout)
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.close_timeout = close_timeout
        self.responses_ws_url = responses_ws_url

        self._snapshot: ResponsesRequestSnapshot | None = None
        self._command_queue: queue.Queue[_WorkerCommand] = queue.Queue()
        self._event_queue: queue.Queue[_WorkerEvent] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._request_condition = threading.Condition()
        self._request_in_flight = False
        self._active_request_identity: tuple[int, int] | None = None
        self._active_request_thread_ident: int | None = None
        self._closed = False
        self._generation = 0
        self._request_ids = count(1)

    @property
    def snapshot(self) -> ResponsesRequestSnapshot | None:
        return self._snapshot

    def is_closed(self) -> bool:
        return self._closed

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

    def reset(self, reason: str) -> None:
        del reason
        active_request: tuple[int, int] | None = None
        active_request_thread_ident: int | None = None
        with self._request_condition:
            active_request = self._active_request_identity
            active_request_thread_ident = self._active_request_thread_ident
        with self._worker_lock:
            if self._closed:
                return
            self._clear_response_state()
            self._generation += 1
            self._command_queue.put(_WorkerCommand(kind="reset", generation=self._generation))
        if (
            active_request is not None
            and active_request_thread_ident != threading.get_ident()
        ):
            self._event_queue.put(
                _WorkerEvent(
                    kind="error",
                    generation=active_request[0],
                    request_id=active_request[1],
                    payload=GenericWsStartedError(
                        "Responses WebSocket stream failed after request start: "
                        "session reset during active request"
                    ),
                )
            )

    def close(self) -> None:
        active_request: tuple[int, int] | None = None
        worker: threading.Thread | None = None
        with self._request_condition:
            active_request = self._active_request_identity
        with self._worker_lock:
            if self._closed:
                return
            self._closed = True
            self._clear_response_state()
            self._generation += 1
            self._command_queue.put(_WorkerCommand(kind="close", generation=self._generation))
            worker = self._worker
        if active_request is not None:
            self._event_queue.put(
                _WorkerEvent(
                    kind="error",
                    generation=active_request[0],
                    request_id=active_request[1],
                    payload=GenericWsStartedError(
                        "Responses WebSocket stream failed after request start: "
                        "session closed during active request"
                    ),
                )
            )
        with self._request_condition:
            self._request_condition.notify_all()
        if worker is not None:
            worker.join(timeout=max(self.timeout, self.close_timeout or 0.0, 0.1))

    def _clear_response_state(self) -> None:
        self._snapshot = None
        while True:
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                return

    def stream_request(
        self,
        api_kwargs: Mapping[str, Any],
        *,
        collect_events: Callable[[Any], Any],
        interrupted: Callable[[], bool] | None,
        register_abort: Callable[[Callable[[str], None]], None] | None,
    ) -> Any:
        if self._closed:
            raise GenericWsNotStartedError(
                "Responses WebSocket session is closed",
                retryable=False,
            )

        with self._request_condition:
            while self._request_in_flight and not self._closed:
                self._request_condition.wait()
            if self._closed:
                raise GenericWsNotStartedError(
                    "Responses WebSocket session is closed",
                    retryable=False,
                )
            self._request_in_flight = True

        try:
            return self._stream_request_exclusive(
                api_kwargs,
                collect_events=collect_events,
                interrupted=interrupted,
                register_abort=register_abort,
            )
        finally:
            with self._request_condition:
                self._request_in_flight = False
                self._request_condition.notify_all()

    def _stream_request_exclusive(
        self,
        api_kwargs: Mapping[str, Any],
        *,
        collect_events: Callable[[Any], Any],
        interrupted: Callable[[], bool] | None,
        register_abort: Callable[[Callable[[str], None]], None] | None,
    ) -> Any:
        allow_full_retry = True
        force_full = False
        active_request: tuple[int, int] | None = None

        try:
            while True:
                generation = self._generation
                request_body, request_kind = self._build_stream_request(
                    api_kwargs,
                    force_full=force_full,
                )
                request_id = next(self._request_ids)
                active_request = (generation, request_id)
                with self._request_condition:
                    self._active_request_identity = active_request
                    self._active_request_thread_ident = threading.get_ident()
                cancel_sent = False
                locally_interrupted = False
                terminal_event: dict[str, Any] | None = None

                self._ensure_worker()
                self._command_queue.put(
                    _WorkerCommand(
                        kind="send",
                        generation=generation,
                        request_id=request_id,
                        api_kwargs=_copy_request_kwargs(api_kwargs),
                        request_body=request_body,
                    )
                )

                def _request_cancel(_reason: str) -> None:
                    nonlocal cancel_sent
                    if cancel_sent or self._closed:
                        return
                    cancel_sent = True
                    self._command_queue.put(
                        _WorkerCommand(
                            kind="cancel",
                            generation=generation,
                            request_id=request_id,
                        )
                    )

                if register_abort is not None:
                    register_abort(_request_cancel)

                def _events():
                    nonlocal cancel_sent, locally_interrupted, terminal_event
                    while True:
                        if interrupted is not None and interrupted() and not cancel_sent:
                            locally_interrupted = True
                            _request_cancel("interrupted")
                        try:
                            item = self._event_queue.get(timeout=self.recv_poll_timeout)
                        except queue.Empty:
                            if self._closed:
                                raise GenericWsStartedError(
                                    "Responses WebSocket stream failed after request start: "
                                    "session closed during active request"
                                )
                            continue
                        if item.generation != generation or item.request_id != request_id:
                            continue
                        if item.kind == "error":
                            raise item.payload

                        event = item.payload
                        terminal = event.get("type") in _TERMINAL_EVENT_TYPES
                        if terminal:
                            terminal_event = event
                        yield _event_namespace(event)
                        if terminal:
                            if locally_interrupted:
                                raise InterruptedError(
                                    "Agent interrupted during Responses WebSocket stream"
                                )
                            return

                try:
                    result = collect_events(_events())
                except GenericWsRejectedError as exc:
                    if allow_full_retry and _is_previous_response_not_found(exc):
                        allow_full_retry = False
                        force_full = True
                        self.reset("previous_response_not_found")
                        continue
                    self.reset("post-send rejection")
                    raise
                except GenericWsStartedError:
                    self.reset("post-send failure")
                    raise
                except InterruptedError:
                    raise
                else:
                    if terminal_event is not None and not locally_interrupted:
                        self.commit_terminal_state(api_kwargs, terminal_event)
                    return result
        finally:
            with self._request_condition:
                if self._active_request_identity == active_request:
                    self._active_request_identity = None
                    self._active_request_thread_ident = None

    def _build_stream_request(
        self,
        api_kwargs: Mapping[str, Any],
        *,
        force_full: bool,
    ) -> tuple[dict[str, Any], str]:
        if force_full:
            body = build_ws_wire_body(api_kwargs)
            body["input"] = [_copy_value(item) for item in _input_items(api_kwargs.get("input"))]
            body.pop("previous_response_id", None)
            return body, "full"
        body, kind = self.build_request(api_kwargs)
        wire_body = build_ws_wire_body(body)
        wire_body["input"] = [_copy_value(item) for item in _input_items(body.get("input"))]
        if "previous_response_id" in body:
            wire_body["previous_response_id"] = body["previous_response_id"]
        return wire_body, kind

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            if self._closed:
                raise GenericWsNotStartedError(
                    "Responses WebSocket session is closed",
                    retryable=False,
                )
            self._worker = threading.Thread(
                target=self._worker_main,
                name="responses-ws-session",
                daemon=True,
            )
            self._worker.start()

    def _worker_main(self) -> None:
        websocket: Any = None
        generation = 0
        active_request_id: int | None = None
        active_generation = 0
        last_event_at = 0.0
        idle_limit = float(self.idle_timeout or 0.0)
        if idle_limit <= 0:
            idle_limit = max(self.timeout, 180.0)

        while True:
            command: _WorkerCommand | None = None
            try:
                if active_request_id is None:
                    command = self._command_queue.get()
                else:
                    command = self._command_queue.get(timeout=self.recv_poll_timeout)
            except queue.Empty:
                command = None

            if command is not None:
                if command.kind == "close":
                    _close_socket(websocket)
                    return
                if command.kind == "reset":
                    generation = max(generation, command.generation)
                    active_request_id = None
                    _close_socket(websocket)
                    websocket = None
                    continue
                if command.kind == "cancel":
                    if (
                        websocket is not None
                        and active_request_id == command.request_id
                        and active_generation == command.generation
                    ):
                        try:
                            websocket.send(json.dumps({"type": "response.cancel"}))
                        except Exception as exc:
                            self._event_queue.put(
                                _WorkerEvent(
                                    kind="error",
                                    generation=active_generation,
                                    request_id=active_request_id,
                                    payload=GenericWsStartedError(
                                        f"Responses WebSocket stream failed after request start: {exc}"
                                    ),
                                )
                            )
                            _close_socket(websocket)
                            websocket = None
                            active_request_id = None
                    continue
                if command.kind == "send":
                    request_id = command.request_id or 0
                    started = False
                    try:
                        if websocket is None or generation != command.generation:
                            _close_socket(websocket)
                            websocket = None
                            generation = command.generation
                            websocket = self._open_websocket(command.api_kwargs or {})
                        payload = json.dumps({"type": "response.create", **dict(command.request_body or {})})
                        active_request_id = request_id
                        active_generation = command.generation
                        started = True
                        websocket.send(payload)
                        last_event_at = time.monotonic()
                    except GenericWsNotStartedError as exc:
                        self._event_queue.put(
                            _WorkerEvent(
                                kind="error",
                                generation=command.generation,
                                request_id=request_id,
                                payload=exc,
                            )
                        )
                        active_request_id = None
                        _close_socket(websocket)
                        websocket = None
                    except Exception as exc:
                        self._event_queue.put(
                            _WorkerEvent(
                                kind="error",
                                generation=command.generation,
                                request_id=request_id,
                                payload=(
                                    GenericWsStartedError(
                                        f"Responses WebSocket stream failed after request start: {exc}",
                                        status_code=getattr(exc, "status_code", None)
                                        if isinstance(getattr(exc, "status_code", None), int)
                                        else None,
                                    )
                                    if started
                                    else GenericWsNotStartedError(
                                        f"Responses WebSocket connection failed: {exc}",
                                        status_code=getattr(exc, "status_code", None)
                                        if isinstance(getattr(exc, "status_code", None), int)
                                        else None,
                                    )
                                ),
                            )
                        )
                        active_request_id = None
                        _close_socket(websocket)
                        websocket = None
                    continue

            if websocket is None or active_request_id is None:
                continue

            try:
                frame = _recv_frame(websocket, poll_timeout=self.recv_poll_timeout)
            except TimeoutError:
                if time.monotonic() - last_event_at >= idle_limit:
                    self._event_queue.put(
                        _WorkerEvent(
                            kind="error",
                            generation=active_generation,
                            request_id=active_request_id,
                            payload=GenericWsStartedError(
                                f"Responses WebSocket stream failed after request start: "
                                f"Responses WebSocket stream idle for {idle_limit:g}s"
                            ),
                        )
                    )
                    _close_socket(websocket)
                    websocket = None
                    active_request_id = None
                continue
            except Exception as exc:
                if type(exc).__name__ in {"TimeoutError", "TimeoutException"}:
                    if time.monotonic() - last_event_at >= idle_limit:
                        self._event_queue.put(
                            _WorkerEvent(
                                kind="error",
                                generation=active_generation,
                                request_id=active_request_id,
                                payload=GenericWsStartedError(
                                    f"Responses WebSocket stream failed after request start: "
                                    f"Responses WebSocket stream idle for {idle_limit:g}s"
                                ),
                            )
                        )
                        _close_socket(websocket)
                        websocket = None
                        active_request_id = None
                    continue
                self._event_queue.put(
                    _WorkerEvent(
                        kind="error",
                        generation=active_generation,
                        request_id=active_request_id,
                        payload=GenericWsStartedError(
                            f"Responses WebSocket stream failed after request start: {exc}",
                            status_code=getattr(exc, "status_code", None)
                            if isinstance(getattr(exc, "status_code", None), int)
                            else None,
                        ),
                    )
                )
                _close_socket(websocket)
                websocket = None
                active_request_id = None
                continue

            last_event_at = time.monotonic()
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8")
            event = json.loads(frame)
            if not isinstance(event, dict):
                continue
            event = _normalize_terminal_event(event)
            if event.get("type") == "error":
                self._event_queue.put(
                    _WorkerEvent(
                        kind="error",
                        generation=active_generation,
                        request_id=active_request_id,
                        payload=GenericWsRejectedError(
                            _server_error_message(event),
                            status_code=_server_error_status(event),
                            body=event,
                        ),
                    )
                )
                active_request_id = None
                continue

            self._event_queue.put(
                _WorkerEvent(
                    kind="event",
                    generation=active_generation,
                    request_id=active_request_id,
                    payload=event,
                )
            )
            if event.get("type") in _TERMINAL_EVENT_TYPES:
                active_request_id = None

    def _open_websocket(self, api_kwargs: Mapping[str, Any]) -> Any:
        try:
            url = resolve_responses_ws_url(self.base_url, self.responses_ws_url)
        except Exception as exc:
            raise GenericWsNotStartedError(
                f"Responses WebSocket connection failed: {exc}"
            ) from exc
        headers = _build_headers(
            api_kwargs=api_kwargs,
            client=self.client,
            api_key=self.api_key,
            headers=self.headers,
        )
        headers = _ensure_responses_beta_header(headers)
        try:
            return self.connect(
                url,
                headers=headers,
                timeout=self.timeout,
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
                close_timeout=self.close_timeout,
            )
        except Exception as exc:
            raise GenericWsNotStartedError(
                f"Responses WebSocket connection failed: {exc}",
                status_code=getattr(exc, "status_code", None)
                if isinstance(getattr(exc, "status_code", None), int)
                else None,
            ) from exc
