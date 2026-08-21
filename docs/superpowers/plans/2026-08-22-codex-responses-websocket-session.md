# Codex Responses WebSocket Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-request generic Codex Responses WebSocket path with an agent-scoped reusable session that supports Codex state reuse without prewarm or unsafe post-send replay.

**Architecture:** Add `ResponsesWebsocketSession` as the single owner of the WebSocket, pump lifecycle, request snapshot, response ID, and turn state. Keep `run_codex_stream()` synchronous and continue feeding normalized events into `_consume_codex_event_stream()`. Gate `previous_response_id`, incremental input, and turn state behind explicit provider capability `responses_ws_state`.

**Tech Stack:** Python 3.11+, `websockets.sync.client`, existing Hermes Responses event normalizer, pytest, in-process WebSocket test server.

**Spec:** `docs/superpowers/specs/2026-08-22-codex-responses-websocket-session-design.md`

## Global Constraints

- Scope is limited to named custom providers using `api_mode: codex_responses`.
- `responses_ws_state` defaults to `false`; state fields are never sent without the capability.
- No prewarm or speculative model request is implemented.
- No blind replay after the `websocket.send()` boundary.
- Session state is agent/session scoped, never process global.
- System prompts and tool schemas remain byte-stable during a conversation.
- Existing unrelated dirty worktree files remain untouched.
- Header values, credentials, prompts, and input items are never logged.

## File Map

- Create: `agent/codex_responses_ws_session.py` — session state, pump, request diffing, and lifecycle.
- Modify: `agent/codex_responses_ws_transport.py` — shared wire helpers, protocol error types, beta header handling, and compatibility wrapper.
- Modify: `agent/codex_runtime.py` — route eligible WS requests through the agent session and handle state recovery/fallback.
- Modify: `agent/agent_init.py` — initialize `responses_ws_state` and the session identity fields.
- Modify: `agent/agent_runtime_helpers.py` — preserve/reset the new runtime fields on runtime rebuild and snapshot/restore.
- Modify: `hermes_cli/runtime_provider.py` and `hermes_cli/config.py` — normalize and propagate `responses_ws_state` from named provider config.
- Modify: `hermes_cli/cli_agent_setup_mixin.py`, `hermes_cli/model_switch.py`, `gateway/run.py`, `gateway/slash_commands.py`, `gateway/session.py`, `gateway/platforms/api_server.py`, `acp_adapter/session.py`, and `tui_gateway/server.py` only where existing Responses transport fields are copied, so the capability reaches every agent constructor.
- Modify: `tests/agent/test_codex_responses_ws_transport.py` — preserve current wire/error coverage and add header/capability cases.
- Create: `tests/agent/test_codex_responses_ws_session.py` — state and session unit tests.
- Modify: `tests/agent/test_codex_ws_conversation_no_replay.py` — session reset and post-send no-replay regressions.
- Modify: `website/docs/integrations/providers.md` and `cli-config.yaml.example` — document the new provider setting and no-prewarm behavior.

### Task 1: Add Provider Capability Propagation

**Files:**
- Modify: `hermes_cli/config.py`
- Modify: `hermes_cli/runtime_provider.py`
- Modify: `agent/agent_init.py`
- Modify: `agent/agent_runtime_helpers.py`
- Test: `tests/agent/test_codex_responses_ws_transport.py`

**Interfaces:**
- Produces `responses_ws_state: bool` in every resolved runtime dict.
- `AIAgent` exposes `responses_ws_state` and defaults it to `False`.
- Runtime snapshot/restore preserves the boolean and resets any existing WS session when it changes.

- [ ] **Step 1: Write failing propagation tests**

```python
def test_named_provider_responses_ws_state_defaults_false():
    runtime = resolve_runtime_provider_from_entry({
        "name": "relay",
        "base_url": "https://relay.example/v1",
        "api_key": "test-key",
        "api_mode": "codex_responses",
    })
    assert runtime["responses_ws_state"] is False


def test_named_provider_responses_ws_state_round_trips_true():
    runtime = resolve_runtime_provider_from_entry({
        "name": "relay",
        "base_url": "https://relay.example/v1",
        "api_key": "test-key",
        "api_mode": "codex_responses",
        "responses_ws_state": True,
    })
    assert runtime["responses_ws_state"] is True
```

- [ ] **Step 2: Run the focused tests and verify the new assertions fail**

Run: `scripts/run_tests.sh tests/agent/test_codex_responses_ws_transport.py -q`

Expected: the new tests fail because the runtime dict and agent do not yet expose `responses_ws_state`.

- [ ] **Step 3: Implement normalization and propagation**

Normalize only booleans/truthy config values at the named-provider boundary. Add the field to the same runtime-copy lists that already carry `responses_transport`, `responses_ws_url`, and `responses_transport_provider`. Initialize it in `agent_init.py` and reset the future session slot when the value changes.

- [ ] **Step 4: Re-run the focused tests**

Run: `scripts/run_tests.sh tests/agent/test_codex_responses_ws_transport.py -q`

Expected: all existing transport tests and the new propagation tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent/agent_init.py agent/agent_runtime_helpers.py hermes_cli/config.py hermes_cli/runtime_provider.py tests/agent/test_codex_responses_ws_transport.py
git commit -m "feat: propagate Responses WebSocket state capability"
```

### Task 2: Implement Request Snapshot and Incremental Diffing

**Files:**
- Create: `agent/codex_responses_ws_session.py`
- Create: `tests/agent/test_codex_responses_ws_session.py`

**Interfaces:**
- `ResponsesRequestSnapshot.from_api_kwargs(api_kwargs: Mapping[str, Any], response_id: str | None, turn_state: Any) -> ResponsesRequestSnapshot`.
- `ResponsesRequestSnapshot.can_increment(next_api_kwargs: Mapping[str, Any], *, state_enabled: bool) -> bool`.
- `ResponsesRequestSnapshot.incremental_input(next_api_kwargs: Mapping[str, Any]) -> list[Any]`.
- `ResponsesWebsocketSession.build_request(api_kwargs: Mapping[str, Any]) -> tuple[dict[str, Any], str]`, returning wire body and `request_kind` (`full` or `incremental`).
- `ResponsesWebsocketSession.commit_terminal_state(api_kwargs: Mapping[str, Any], terminal_event: Any) -> None`.

- [ ] **Step 1: Write failing state tests**

```python
def test_appended_input_builds_incremental_request():
    session = make_session(state_enabled=True)
    session.commit_snapshot(api_kwargs(model="m", input=[{"id": "a"}]), "resp-1", {"turn": 1})
    body, kind = session.build_request(api_kwargs(model="m", input=[{"id": "a"}, {"id": "b"}]))
    assert kind == "incremental"
    assert body["previous_response_id"] == "resp-1"
    assert body["input"] == [{"id": "b"}]


def test_request_property_change_forces_full_input():
    session = make_session(state_enabled=True)
    session.commit_snapshot(api_kwargs(model="m", input=[{"id": "a"}]), "resp-1", {"turn": 1})
    body, kind = session.build_request(api_kwargs(model="m2", input=[{"id": "a"}, {"id": "b"}]))
    assert kind == "full"
    assert "previous_response_id" not in body
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `scripts/run_tests.sh tests/agent/test_codex_responses_ws_session.py -q`

Expected: collection or assertion failure because the session and snapshot types do not exist.

- [ ] **Step 3: Implement immutable snapshot comparison**

Compare model, instructions, tools, tool choice, parallel tool calls, reasoning, service tier, include, prompt cache key, text, and all non-input request fields. Treat input as an ordered item list and require the previous list to be an exact prefix. Never mutate the caller's `api_kwargs` or stored snapshot.

- [ ] **Step 4: Run state tests and add mismatch cases**

Run: `scripts/run_tests.sh tests/agent/test_codex_responses_ws_session.py -q`

Expected: PASS for append-only input and full-request fallback for model, tools, instructions, reordered input, missing state, and disabled capability.

- [ ] **Step 5: Commit**

```bash
git add agent/codex_responses_ws_session.py tests/agent/test_codex_responses_ws_session.py
git commit -m "feat: add Responses WebSocket request state"
```

### Task 3: Implement the Reusable WebSocket Session and Pump

**Files:**
- Modify: `agent/codex_responses_ws_session.py`
- Modify: `agent/codex_responses_ws_transport.py`
- Test: `tests/agent/test_codex_responses_ws_session.py`

**Interfaces:**
- `ResponsesWebsocketSession.stream_request(api_kwargs: Mapping[str, Any], *, collect_events: Callable[[Any], Any], interrupted: Callable[[], bool] | None, register_abort: Callable[[Callable[[str], None]], None] | None) -> Any`.
- `ResponsesWebsocketSession.reset(reason: str) -> None`.
- `ResponsesWebsocketSession.close() -> None`.
- Existing `GenericWsNotStartedError`, `GenericWsStartedError`, and `GenericWsRejectedError` remain import-compatible.

- [ ] **Step 1: Write failing pump tests**

```python
def test_two_requests_share_one_connection(fake_connect):
    session = make_session(connect=fake_connect, state_enabled=True)
    session.stream_request(api_kwargs(model="m", input=[{"id": "a"}]), collect_events=collect)
    session.stream_request(api_kwargs(model="m", input=[{"id": "a"}, {"id": "b"}]), collect_events=collect)
    assert fake_connect.call_count == 1
    assert [json.loads(p)["type"] for p in fake_connect.socket.sent] == ["response.create", "response.create"]


def test_close_stops_pump_and_clears_socket():
    session = make_session()
    session.close()
    assert session.is_closed()
```

- [ ] **Step 2: Run the pump tests and verify they fail**

Run: `scripts/run_tests.sh tests/agent/test_codex_responses_ws_session.py -q`

Expected: failure because the session currently has no connection owner or pump.

- [ ] **Step 3: Implement the session worker**

Use a command queue for `send`, `cancel`, and `close`, and an event queue for normalized frames and terminal errors. The worker alone calls `connect`, `send`, `recv`, and `close`. Configure `ping_interval`, `ping_timeout`, `close_timeout`, and the existing connect/idle limits. Attach a generation to each command and discard events from retired generations.

- [ ] **Step 4: Add protocol and lifecycle handling**

Emit the v2 beta header, preserve existing auth/extra header merging, normalize terminal events, route `error` frames to `GenericWsRejectedError`, and mark the send boundary before invoking `send`. Reset the session on ordinary post-send errors; permit exactly one full-input recovery for `previous_response_not_found`.

- [ ] **Step 5: Run the session tests**

Run: `scripts/run_tests.sh tests/agent/test_codex_responses_ws_session.py tests/agent/test_codex_responses_ws_transport.py -q`

Expected: PASS, including one connection for two requests, generation fencing, cancellation, idle timeout, header merging, and no prewarm.

- [ ] **Step 6: Commit**

```bash
git add agent/codex_responses_ws_session.py agent/codex_responses_ws_transport.py tests/agent/test_codex_responses_ws_session.py tests/agent/test_codex_responses_ws_transport.py
git commit -m "feat: add reusable Codex Responses WebSocket session"
```

### Task 4: Integrate the Session with Codex Runtime and Fallback

**Files:**
- Modify: `agent/codex_runtime.py`
- Modify: `agent/agent_init.py`
- Modify: `agent/agent_runtime_helpers.py`
- Modify: `tests/agent/test_codex_ws_conversation_no_replay.py`

**Interfaces:**
- `AIAgent._codex_responses_ws_session` stores the session or `None`.
- `run_codex_stream()` calls `session.stream_request(...)` for eligible WebSocket requests and retains the existing SSE path.

- [ ] **Step 1: Write failing runtime integration tests**

```python
def test_post_send_session_error_does_not_activate_provider_fallback(ws_agent):
    ws_agent._codex_responses_ws_session = failing_started_session()
    ws_agent._try_activate_fallback = MagicMock(return_value=False)
    with pytest.raises(GenericWsStartedError):
        run_codex_stream(ws_agent, request_kwargs())
    ws_agent._try_activate_fallback.assert_not_called()


def test_provider_change_closes_old_session(ws_agent):
    old = ws_agent._codex_responses_ws_session = make_session()
    reset_runtime_provider(ws_agent, provider="custom:other")
    old.close.assert_called_once()
```

- [ ] **Step 2: Run the integration tests and verify they fail**

Run: `scripts/run_tests.sh tests/agent/test_codex_ws_conversation_no_replay.py -q`

Expected: failure because runtime still calls the one-shot transport and does not own a session.

- [ ] **Step 3: Route eligible requests through the session**

Construct the session lazily from agent runtime fields, pass the existing collector callbacks unchanged, and preserve writer-token interruption checks. For `auto`, only `GenericWsNotStartedError` activates the existing sticky SSE fallback. `GenericWsStartedError` and `GenericWsRejectedError` remain terminal for the turn.

- [ ] **Step 4: Wire reset points**

Close and clear the session when runtime identity or capability changes, fallback activates, the agent is interrupted, and final agent cleanup runs. Do not close the session after every successful terminal response.

- [ ] **Step 5: Run runtime regressions**

Run: `scripts/run_tests.sh tests/agent/test_codex_ws_conversation_no_replay.py tests/agent/test_codex_responses_ws_session.py -q`

Expected: PASS for connection reuse, SSE pre-send fallback, post-send no replay, provider reset, and one-shot stale-state recovery.

- [ ] **Step 6: Commit**

```bash
git add agent/agent_init.py agent/agent_runtime_helpers.py agent/codex_runtime.py tests/agent/test_codex_ws_conversation_no_replay.py
git commit -m "feat: integrate reusable Responses WebSocket session"
```

### Task 5: Document Provider Configuration and Cross-Surface Runtime Copies

**Files:**
- Modify: `hermes_cli/cli_agent_setup_mixin.py`
- Modify: `hermes_cli/model_switch.py`
- Modify: `gateway/run.py`
- Modify: `gateway/slash_commands.py`
- Modify: `gateway/session.py`
- Modify: `gateway/platforms/api_server.py`
- Modify: `acp_adapter/session.py`
- Modify: `tui_gateway/server.py`
- Modify: `website/docs/integrations/providers.md`
- Modify: `cli-config.yaml.example`
- Test: `tests/cli/test_cli_ws_route_cache.py`

**Interfaces:**
- Every runtime constructor that already copies Responses transport fields also copies `responses_ws_state`.
- Documentation shows state reuse enabled explicitly and states that no prewarm occurs.

- [ ] **Step 1: Write failing cross-surface propagation tests**

```python
def test_runtime_snapshot_preserves_responses_ws_state():
    runtime = make_runtime(responses_ws_state=True)
    restored = restore_runtime(runtime)
    assert restored["responses_ws_state"] is True
```

- [ ] **Step 2: Run the focused CLI test and verify the new assertion fails**

Run: `scripts/run_tests.sh tests/cli/test_cli_ws_route_cache.py -q`

Expected: failure because the new capability is not present in route/runtime snapshots.

- [ ] **Step 3: Add the field to existing copy and persistence lists**

Update only the code paths that already handle `responses_transport`, `responses_ws_url`, or `responses_transport_provider`. Do not create a parallel runtime configuration mechanism.

- [ ] **Step 4: Update docs and config example**

Document `responses_ws_state: true`, the required upstream contract, the v2 beta header behavior, connection reuse, incremental requests, no prewarm, and the no-replay boundary.

- [ ] **Step 5: Run propagation tests**

Run: `scripts/run_tests.sh tests/cli/test_cli_ws_route_cache.py tests/agent/test_codex_responses_ws_transport.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add acp_adapter/session.py cli-config.yaml.example gateway/platforms/api_server.py gateway/run.py gateway/session.py gateway/slash_commands.py hermes_cli/cli_agent_setup_mixin.py hermes_cli/model_switch.py tui_gateway/server.py website/docs/integrations/providers.md tests/cli/test_cli_ws_route_cache.py
git commit -m "docs: expose Responses WebSocket state capability"
```

### Task 6: Full Verification and Review Checkpoint

**Files:**
- Test: `tests/agent/test_codex_responses_ws_session.py`
- Test: `tests/agent/test_codex_responses_ws_transport.py`
- Test: `tests/agent/test_codex_ws_conversation_no_replay.py`
- Test: `tests/cli/test_cli_ws_route_cache.py`

- [ ] **Step 1: Run the focused suite**

Run: `scripts/run_tests.sh tests/agent/test_codex_responses_ws_session.py tests/agent/test_codex_responses_ws_transport.py tests/agent/test_codex_ws_conversation_no_replay.py tests/cli/test_cli_ws_route_cache.py -q`

Expected: all focused tests pass.

- [ ] **Step 2: Run static checks**

Run: `python3 -m compileall -q agent/codex_responses_ws_session.py agent/codex_responses_ws_transport.py agent/codex_runtime.py`

Expected: exit code 0 and no syntax errors.

- [ ] **Step 3: Inspect the final diff**

Run: `git diff --check HEAD~6..HEAD && git status --short --branch`

Expected: no whitespace errors; only implementation files and the approved documentation/config changes are modified in the worktree.

- [ ] **Step 4: Commit the final verification metadata if needed**

Do not create a metadata-only commit. Report the exact test limitation if pytest remains unavailable.
