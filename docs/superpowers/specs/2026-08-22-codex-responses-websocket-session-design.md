# Codex Responses WebSocket Session Design

## Status

Design approved in conversation; implementation has not started.

## Goal

Upgrade Hermes' generic `codex_responses` WebSocket path from one socket per
request to an agent/session-scoped Responses WebSocket session. The design
borrows the connection lifecycle and state reuse model from Codex while
preserving Hermes' synchronous conversation loop and its no-blind-replay
boundary after `response.create` is sent.

## Scope and Boundaries

- Applies only to named custom providers using `api_mode: codex_responses`.
- The upstream provider is expected to support the Codex Responses WebSocket
  v2 contract: repeated `response.create` frames on one connection,
  `previous_response_id`, incremental `input`, turn state, and the Responses
  WebSocket beta header.
- The capability is explicit per provider through `responses_ws_state: true`.
  The default is `false`.
- Existing `responses_transport`, `responses_ws_url`, and
  `responses_transport_provider` settings remain the routing controls.
- Prewarm is explicitly out of scope. A connection is opened only for the
  first real request.
- No core model tools, system-prompt mutation, or process-global connection
  state is added.

## Architecture

Add an agent/session-owned `ResponsesWebsocketSession` responsible for the
connection and request state. `run_codex_stream()` delegates transport work to
this object and continues to consume normalized events through the existing
`_consume_codex_event_stream()` path.

The session exposes these conceptual operations:

- `ensure_connected()`
- `send_response_create(request)`
- `receive_events()`
- `build_incremental_request(request)`
- `update_response_state(terminal_event)`
- `reset(reason)`
- `close()`

The session owns one WebSocket and one in-flight request. A dedicated pump
thread owns the socket and communicates with the synchronous caller through
command and event queues. The pump handles connection setup, frame send/recv,
Ping/Pong, idle timeout, close, and transport errors. Every request carries a
generation token so events from an old connection cannot enter a new request.

Session lifecycle:

1. Agent construction creates the session object without opening a socket.
2. The first real Responses turn calls `ensure_connected()`.
3. The first request sends a complete `response.create`.
4. A terminal event commits response state and keeps the connection alive.
5. A later turn uses an incremental request only when the state contract below
   is satisfied; otherwise it sends a complete request.
6. Provider/model/base URL/WS URL/capability changes reset and close the old
   session.
7. Agent close, cancellation, provider fallback, and terminal failure stop the
   pump and close the socket.

## Request State and Incremental Input

The session stores an immutable snapshot of the last completed request:

- model
- instructions
- tools and tool choice
- reasoning settings
- service tier, include, text, and prompt cache key
- complete input
- response ID
- turn state

Incremental mode is allowed only when all of the following hold:

- request properties other than input are identical;
- `responses_ws_state` is enabled;
- the new input is a strict append to the previous input;
- the previous request reached a normal terminal event;
- both response ID and turn state are present.

The incremental frame contains `previous_response_id` and only the appended
input items. Any model, tool, instruction, compression, or runtime change
falls back to a complete request without the old response ID.

State is committed only after a terminal event. Partial streams never update
the reusable snapshot.

## Error and Recovery Semantics

### Before send

Connection and serialization failures before invoking WebSocket `send()` are
classified as not-started. The session may rebuild the socket and retry. In
`auto` transport mode, the caller may fall back to SSE.

### After send

Ordinary close, timeout, network, or partial-stream failures after
`response.create` crosses the send boundary invalidate the session and do not
replay the request. Provider fallback is not activated for that request.

### Explicit stale-state recovery

`previous_response_not_found` is a protocol-defined non-execution error. On
the first occurrence, clear response ID and turn state and retry once with a
complete input. A second failure terminates the turn; no loop of state recovery
is allowed.

### Session fallback

Repeated connection failures can disable WebSocket for the current runtime
identity and use SSE according to the existing `responses_transport` policy.
The fallback decision is session-scoped and is reset when the provider/model/
endpoint identity changes.

## Configuration and Integration

Provider configuration carries:

```yaml
responses_transport: auto
responses_ws_url: wss://provider.example/v1/responses
responses_ws_state: true
```

`OpenAI-Beta: responses_websockets=2026-02-06` is added for the v2 protocol;
existing `extra_headers` can add or override provider-specific headers. Header
values and credentials are never logged.

The new capability follows the existing runtime propagation path:

```text
custom_providers
  -> resolve_runtime_provider()
  -> AIAgent initialization
  -> CLI/Gateway/ACP/TUI runtime snapshot
  -> ResponsesWebsocketSession
```

Runtime changes reset the session. The feature remains outside
`_HERMES_CORE_TOOLS` and does not alter the system prompt, preserving prompt
cache stability.

## Testing and Observability

Protocol unit tests cover request serialization, header merging, complete vs
incremental request selection, state mismatch, one-shot stale-state recovery,
and terminal-only state commits.

Real in-process WebSocket integration tests cover repeated requests on one
connection, incremental input, connection rebuild, Ping/Pong, idle timeout,
cancellation, no-prewarm behavior, and no replay after send.

Conversation-loop regression tests cover pre-send SSE fallback, post-send
fallback suppression, provider/model reset, and prevention of duplicate tool
calls during state recovery.

Safe logs expose only metadata such as transport, connection reuse,
`request_kind`, response-ID presence, state reset reason, and fallback choice.
Prompts, input items, token values, and header credentials remain redacted.

## Non-goals

- Prewarm or speculative prompt execution.
- Process-global WebSocket pooling.
- Converting Hermes' synchronous conversation loop to async.
- Blind retries after a request has been sent.
- Sending Codex-private state fields to providers without the explicit state
  capability.
