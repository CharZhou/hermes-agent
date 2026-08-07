# MCP Context Propagation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Forward Hermes' existing run, session, and tool-call context to opted-in MCP servers through the standard MCP `tools/call.params._meta` field.

**Architecture:** Reuse `gateway.session_context` for request/session values and add one independent `HERMES_RUN_ID` ContextVar. Keep per-call values explicit through the existing `handle_function_call -> registry.dispatch -> handler(**kwargs)` path. The MCP handler snapshots both sources before scheduling work on the dedicated MCP event loop and adds a namespaced `meta` only when the target server's config enables `forward_hermes_context`.

**Tech Stack:** Python 3.13, `contextvars`, existing Hermes tool registry, MCP Python SDK `ClientSession.call_tool(meta=...)`, pytest/pytest-asyncio, aiohttp test utilities.

## Global Constraints

- Do not add `X-Hermes-Session-Id` support or change any existing API request contract.
- Do not put context in model-visible tool schemas or ordinary MCP `arguments`.
- `run_id`, `task_id`, `session_id`, and `session_key` remain independent identifiers; missing values are omitted rather than inferred.
- `forward_hermes_context` is per-server and defaults to `false`; unconfigured and disabled servers retain the current `call_tool(name, arguments=args)` call shape.
- The approval namespace remains isolated from the user/session namespace: `/v1/runs` continues to use `run_id` for approval lookup, while MCP `session_key` comes from the existing API session-key resolution.
- Non-MCP handlers must tolerate the additional dispatch keyword arguments through the registry's existing `**kwargs` contract.
- Use `scripts/run_tests.sh` for focused verification and preserve behavior-based tests rather than fixed enumeration snapshots.

---

## File Map

- Modify `gateway/session_context.py`: add the run-id ContextVar to the existing session binding/clearing API.
- Modify `gateway/platforms/api_server.py`: bind `run_id` for `/v1/runs` and keep approval-session and API-session keys separate.
- Modify `model_tools.py`: forward call-level context to both registry dispatch branches.
- Modify `tools/mcp_tool.py`: build namespaced metadata, read the per-server opt-in, and pass `meta` to `ClientSession.call_tool()`.
- Modify `tests/gateway/test_session_env.py`: verify run-id binding and cleanup.
- Modify `tests/gateway/test_api_server_runs.py`: verify a live run exposes independent run/session/approval context.
- Modify `tests/test_dispatch_session_id.py`: verify all call-level fields reach `registry.dispatch` on normal and `execute_code` paths.
- Modify `tests/tools/test_mcp_tool.py`: verify metadata construction, opt-in behavior, argument separation, and concurrent snapshot isolation.
- Modify `website/docs/reference/mcp-config-reference.md`: document the new opt-in key and metadata behavior.
- Modify `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/reference/mcp-config-reference.md`: add the corresponding Chinese configuration reference entry.

### Task 1: Add independent runtime run context

**Files:**
- Modify: `gateway/session_context.py:70-145, 250-305`
- Test: `tests/gateway/test_session_env.py`

**Interfaces:**
- Produces `HERMES_RUN_ID` through `get_session_env("HERMES_RUN_ID")`.
- Extends `set_session_vars(..., run_id="")` without changing existing callers.
- `clear_session_vars()` explicitly clears run ID; `reset_session_vars()` returns it to the `_UNSET` state through `_VAR_MAP`.

- [ ] **Step 1: Write failing tests for run-id binding and cleanup**

Add tests beside the existing `set_session_vars`/`clear_session_vars` coverage:

```python
def test_run_id_is_bound_and_explicitly_cleared():
    from gateway.session_context import clear_session_vars, get_session_env, set_session_vars

    tokens = set_session_vars(run_id="run-A")
    try:
        assert get_session_env("HERMES_RUN_ID") == "run-A"
    finally:
        clear_session_vars(tokens)

    assert get_session_env("HERMES_RUN_ID") == ""


def test_run_id_is_reset_with_other_session_context():
    from gateway.session_context import get_session_env, reset_session_vars, set_session_vars

    set_session_vars(run_id="run-A")
    reset_session_vars()
    assert get_session_env("HERMES_RUN_ID") == ""
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
scripts/run_tests.sh tests/gateway/test_session_env.py -k run_id -q
```

Expected: FAIL because `set_session_vars` has no `run_id` parameter and
`HERMES_RUN_ID` is not in the session variable map.

- [ ] **Step 3: Implement the minimal ContextVar extension**

In `gateway/session_context.py`:

1. Declare `_RUN_ID = ContextVar("HERMES_RUN_ID", default=_UNSET)` next to `_SESSION_ID`.
2. Add `"HERMES_RUN_ID": _RUN_ID` to `_VAR_MAP` so `get_session_env()` and `reset_session_vars()` cover it.
3. Add `run_id: str = ""` to `set_session_vars()` and set `_RUN_ID` in the returned token list.
4. Add `_RUN_ID` to the explicit-clear tuple in `clear_session_vars()`.
5. Leave the process-global environment bridge unchanged; `HERMES_RUN_ID` is runtime ContextVar state, not a user-facing environment setting.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run:

```bash
scripts/run_tests.sh tests/gateway/test_session_env.py -k run_id -q
```

Expected: PASS, with the existing session environment tests still green.

- [ ] **Step 5: Commit the runtime context unit**

```bash
git add gateway/session_context.py tests/gateway/test_session_env.py
git commit -m "feat: add isolated Hermes run context"
```

### Task 2: Bind `/v1/runs` without conflating approval and session keys

**Files:**
- Modify: `gateway/platforms/api_server.py:6008-6038, 6393-6400, 6480-6495, 6589-6649`
- Test: `tests/gateway/test_api_server_runs.py`

**Interfaces:**
- `_bind_api_server_session(..., run_id="")` passes the generated run ID into `set_session_vars` while retaining `platform="api_server"` and `async_delivery=False`.
- `/v1/runs` calls `_bind_api_server_session` with `run_id=run_id` and `session_key=gateway_session_key or session_id or ""`.
- `set_current_session_key(approval_session_key)` remains separately bound to `run_id` for approvals.

- [ ] **Step 1: Write the failing integration test**

Add a test to `TestStartRun` that uses `auth_adapter`, sends an existing
`X-Hermes-Session-Key`, and captures context inside the mocked agent's
`run_conversation`:

```python
def _capture_run(user_message=None, conversation_history=None, task_id=None):
    from gateway.session_context import get_session_env
    from tools.approval import get_current_session_key

    captured.update({
        "run_id": get_session_env("HERMES_RUN_ID"),
        "session_id": get_session_env("HERMES_SESSION_ID"),
        "session_key": get_session_env("HERMES_SESSION_KEY"),
        "platform": get_session_env("HERMES_SESSION_PLATFORM"),
        "approval_key": get_current_session_key(),
        "task_id": task_id,
    })
    return {"final_response": "done"}
```

Post `{"input": "hello", "session_id": "conversation-A"}` with
`X-Hermes-Session-Key: scope-A`, wait for completion, then assert:

```python
assert captured["run_id"] == response_data["run_id"]
assert captured["session_id"] == "conversation-A"
assert captured["session_key"] == "scope-A"
assert captured["platform"] == "api_server"
assert captured["approval_key"] == response_data["run_id"]
assert captured["run_id"] != captured["task_id"]
```

The last assertion should compare against the actual task ID passed by the
route rather than assuming a particular generated task format.

- [ ] **Step 2: Run the focused test and verify the current conflation**

Run:

```bash
scripts/run_tests.sh tests/gateway/test_api_server_runs.py -k "run_context or session_key" -q
```

Expected: FAIL because `/v1/runs` does not bind `HERMES_RUN_ID` and currently
binds its approval key into `HERMES_SESSION_KEY`.

- [ ] **Step 3: Bind the generated run ID and the existing API session key**

Implement the smallest route change:

1. Add `run_id: str = ""` to `_bind_api_server_session()` and pass it to `set_session_vars`.
2. In the `/v1/runs` worker, call `_bind_api_server_session(run_id=run_id, session_key=gateway_session_key or session_id or "", session_id=session_id or "", chat_id=session_id or "")`.
3. Keep `approval_session_key = run_id`, `set_current_session_key(approval_session_key)`, approval notifications, approval POST lookup, and cleanup unchanged.
4. Do not alter body `session_id` parsing, run ID generation, or non-Run API routes except for the new defaulted helper parameter.

- [ ] **Step 4: Run the focused API tests**

Run:

```bash
scripts/run_tests.sh tests/gateway/test_api_server_runs.py tests/gateway/test_session_context_inheritance.py -q
```

Expected: PASS, including approval isolation and context cleanup coverage.

- [ ] **Step 5: Commit the API lifecycle unit**

```bash
git add gateway/platforms/api_server.py tests/gateway/test_api_server_runs.py
git commit -m "fix: separate run and API session context"
```

### Task 3: Preserve call-level context through tool dispatch

**Files:**
- Modify: `model_tools.py:1123-1139, 1421-1439`
- Test: `tests/test_dispatch_session_id.py`

**Interfaces:**
- `handle_function_call()` keeps its existing optional parameters and forwards `tool_call_id`, `turn_id`, and `api_request_id` to `registry.dispatch` along with `task_id` and `session_id`.
- Both the normal dispatch branch and the `execute_code` branch pass the same context keywords; existing handlers continue to receive them through `**kwargs`.

- [ ] **Step 1: Add failing forwarding tests**

Extend the existing mocked-registry tests with one table-driven assertion:

```python
def test_call_context_fields_reach_registry_dispatch():
    captured = {}
    with patch("model_tools.registry", _make_registry(captured)):
        from model_tools import handle_function_call
        handle_function_call(
            "web_search",
            {"query": "test"},
            task_id="task-A",
            tool_call_id="call-A",
            session_id="session-A",
            turn_id="turn-A",
            api_request_id="request-A",
            skip_pre_tool_call_hook=True,
        )

    assert captured == {
        "task_id": "task-A",
        "tool_call_id": "call-A",
        "session_id": "session-A",
        "turn_id": "turn-A",
        "api_request_id": "request-A",
        "user_task": None,
    }
```

Add the same field assertions to the existing `execute_code` path test so
that branch cannot silently regress.

- [ ] **Step 2: Run the forwarding tests and verify they fail**

Run:

```bash
scripts/run_tests.sh tests/test_dispatch_session_id.py -q
```

Expected: FAIL because only `task_id`, `session_id`, and (normal path)
`user_task` currently reach the registry.

- [ ] **Step 3: Forward the fields in both dispatch closures**

Add `tool_call_id=tool_call_id`, `turn_id=turn_id`, and
`api_request_id=api_request_id` to each `registry.dispatch()` call in
`model_tools.handle_function_call()`. Do not alter middleware hook payloads,
argument rewriting, or tool schemas.

- [ ] **Step 4: Run the focused and adjacent model-tool tests**

Run:

```bash
scripts/run_tests.sh tests/test_dispatch_session_id.py tests/test_model_tools.py tests/agent/test_tool_dispatch_helpers.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the dispatch unit**

```bash
git add model_tools.py tests/test_dispatch_session_id.py
git commit -m "feat: forward tool call context through registry"
```

### Task 4: Inject opt-in MCP metadata and prove isolation

**Files:**
- Modify: `tools/mcp_tool.py:4878-4958`
- Test: `tests/tools/test_mcp_tool.py`

**Interfaces:**
- Add pure helper `_build_hermes_context_meta(**fields) -> dict[str, dict[str, str]]` that returns `{"io.nous.hermes/context": {"version": "1", ...}}` and filters empty/non-string values without inference.
- `_make_tool_handler()` snapshots `run_id`, `session_key`, and `platform` from `get_session_env()` plus `task_id`, `session_id`, `tool_call_id`, `turn_id`, and `api_request_id` from `kwargs` before creating the coroutine.
- `forward_hermes_context` is parsed with existing `_parse_boolish(..., default=False)` from `server._config`.
- Enabled calls invoke `session.call_tool(tool_name, arguments=args, meta=context_meta)`; disabled calls retain exactly `session.call_tool(tool_name, arguments=args)`.

- [ ] **Step 1: Write failing pure-builder and handler tests**

Add tests in `TestToolHandler`:

```python
def test_context_builder_omits_empty_values_without_guessing():
    from tools.mcp_tool import _build_hermes_context_meta

    assert _build_hermes_context_meta(
        run_id="run-A", session_id="session-A", task_id="", platform=None
    ) == {
        "io.nous.hermes/context": {
            "version": "1",
            "run_id": "run-A",
            "session_id": "session-A",
        }
    }


def test_opted_in_handler_passes_meta_and_not_arguments(monkeypatch):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.mcp_tool import _make_tool_handler, _servers

    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_make_call_result("ok"))
    server = _make_mock_server("test_srv", session=session)
    server._config = {"forward_hermes_context": True}
    _servers["test_srv"] = server
    tokens = set_session_vars(
        run_id="run-A", session_key="scope-A", platform="api_server"
    )
    try:
        handler = _make_tool_handler("test_srv", "greet", 120)
        with self._patch_mcp_loop():
            handler({"name": "world"}, task_id="task-A", session_id="session-A",
                    tool_call_id="call-A", turn_id="turn-A", api_request_id="req-A")
        call_kwargs = session.call_tool.call_args.kwargs
        assert call_kwargs["arguments"] == {"name": "world"}
        assert call_kwargs["meta"]["io.nous.hermes/context"] == {
            "version": "1", "run_id": "run-A", "task_id": "task-A",
            "session_id": "session-A", "session_key": "scope-A",
            "tool_call_id": "call-A", "turn_id": "turn-A",
            "api_request_id": "req-A", "platform": "api_server",
        }
    finally:
        clear_session_vars(tokens)
        _servers.pop("test_srv", None)
```

Add a disabled-server assertion that `assert_called_once_with("greet", arguments={...})`
has no `meta` key. Add a concurrent test that runs two copied ContextVars
through `_build_hermes_context_meta` and asserts each snapshot retains its own
`run_id/session_id/session_key/tool_call_id`; this specifically tests that the
snapshot is created before the MCP event-loop hop.

- [ ] **Step 2: Run the MCP tests and verify the new tests fail**

Run:

```bash
scripts/run_tests.sh tests/tools/test_mcp_tool.py -k "context or successful_call" -q
```

Expected: the existing successful-call test remains green for the disabled
default, while the new builder/opt-in tests fail because no metadata helper or
`meta` call argument exists.

- [ ] **Step 3: Implement the pure metadata builder**

Near the MCP handler helpers, define a fixed ordered field list and build a
fresh dictionary on every call:

```python
_HERMES_CONTEXT_META_KEY = "io.nous.hermes/context"
_HERMES_CONTEXT_FIELDS = (
    "run_id", "task_id", "session_id", "session_key",
    "tool_call_id", "turn_id", "api_request_id", "platform",
)

def _build_hermes_context_meta(**fields):
    context = {"version": "1"}
    for field in _HERMES_CONTEXT_FIELDS:
        value = fields.get(field)
        if value is not None and value != "":
            context[field] = str(value)
    return {_HERMES_CONTEXT_META_KEY: context}
```

- [ ] **Step 4: Snapshot context and conditionally pass SDK metadata**

Inside the synchronous `_handler`, after obtaining `server` and before
defining/scheduling `_call`, compute:

```python
forward_context = _parse_boolish(
    server._config.get("forward_hermes_context", False), default=False
)
context_meta = None
if forward_context:
    from gateway.session_context import get_session_env
    context_meta = _build_hermes_context_meta(
        run_id=get_session_env("HERMES_RUN_ID"),
        task_id=kwargs.get("task_id"),
        session_id=kwargs.get("session_id") or get_session_env("HERMES_SESSION_ID"),
        session_key=get_session_env("HERMES_SESSION_KEY"),
        tool_call_id=kwargs.get("tool_call_id"),
        turn_id=kwargs.get("turn_id"),
        api_request_id=kwargs.get("api_request_id"),
        platform=get_session_env("HERMES_SESSION_PLATFORM"),
    )
```

Call the SDK with a conditional keyword dictionary so the disabled path has
no `meta` argument at all:

```python
call_kwargs = {"arguments": args}
if context_meta is not None:
    call_kwargs["meta"] = context_meta
result = await server.session.call_tool(tool_name, **call_kwargs)
```

Keep `_pending_call_context` assignment and cleanup around the RPC unchanged;
the metadata snapshot must not be rebuilt inside `_call`.

- [ ] **Step 5: Run MCP unit and concurrency tests**

Run:

```bash
scripts/run_tests.sh tests/tools/test_mcp_tool.py tests/test_dispatch_session_id.py -q
```

Expected: PASS, including existing reconnect, timeout, error, and image
content tests.

- [ ] **Step 6: Commit the MCP transport unit**

```bash
git add tools/mcp_tool.py tests/tools/test_mcp_tool.py
git commit -m "feat: forward opted-in context to MCP calls"
```

### Task 5: Document the opt-in configuration and run regression verification

**Files:**
- Modify: `website/docs/reference/mcp-config-reference.md`
- Modify: `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/reference/mcp-config-reference.md`

**Interfaces:**
- Document `forward_hermes_context` as a bool-like per-server setting with default `false`.
- Document the `_meta["io.nous.hermes/context"]` namespace, the forwarded fields, and the fact that values never enter tool arguments.

- [ ] **Step 1: Add the English and Chinese configuration entries**

Add `forward_hermes_context: false` to each root config example and a row to
each server-key table. State that enabling it sends only non-empty runtime
values (`run_id`, `task_id`, `session_id`, `session_key`, `tool_call_id`,
`turn_id`, `api_request_id`, `platform`) to trusted MCP servers.

- [ ] **Step 2: Run documentation checks and the complete focused regression set**

Run:

```bash
scripts/run_tests.sh \
  tests/gateway/test_session_env.py \
  tests/gateway/test_session_context_inheritance.py \
  tests/gateway/test_api_server_runs.py \
  tests/test_dispatch_session_id.py \
  tests/test_model_tools.py \
  tests/tools/test_mcp_tool.py -q
git diff --check
```

Expected: all selected tests pass and `git diff --check` is clean. If the
repository provides a docs build/lint command for website changes, run that
command as well and record its result; do not claim a full website build was
run unless it actually completed.

- [ ] **Step 3: Inspect the final diff for contract regressions**

Confirm manually that:

1. No new `X-Hermes-Session-Id` parsing or request-body changes were added.
2. Disabled MCP servers have the exact pre-change SDK call shape.
3. Approval checks still use `run_id` and never use the forwarded
   `session_key` as an authorization key.
4. No full `session_key` is logged.
5. No context field appears in an MCP tool schema or model arguments.

- [ ] **Step 4: Commit documentation and verification updates**

```bash
git add website/docs/reference/mcp-config-reference.md \
  website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/reference/mcp-config-reference.md
git commit -m "docs: document MCP context forwarding"
```

## Handoff

After this plan is approved, execute it with `superpowers:subagent-driven-development`
or `superpowers:executing-plans`, one task at a time, preserving the test and
commit checkpoints above.
