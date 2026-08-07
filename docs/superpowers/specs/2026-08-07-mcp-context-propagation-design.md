# MCP Context Propagation

## Goal

When Hermes invokes a tool on a trusted MCP server, include the source
session and runtime context in the standard MCP `tools/call.params._meta`
field. This lets the MCP server distinguish concurrent Hermes sessions,
runs, turns, and tool calls without exposing identity fields to the model or
changing the MCP tool schema.

Context forwarding is opt-in for each MCP server. Existing servers retain
their current behavior unless explicitly configured.

## Scope

This change only propagates context that Hermes already knows. It does not
add HTTP headers or change the input contract of `/v1/runs`; each API, CLI,
TUI, and gateway entry point continues to obtain session information through
its existing logic.

The following fields are forwarded when they are available:

- `run_id`
- `task_id`
- `session_id`
- `session_key`
- `tool_call_id`
- `turn_id`
- `api_request_id`
- `platform`

Identifiers remain independent. A missing `run_id`, `task_id`, or
`session_id` is omitted and is never inferred from another identifier.

This design does not add browser-executed tools, remote tool-result
submission, agent-loop suspension, BFF mapping behavior, or BFF idempotency
logic.

## Configuration

Each MCP server may explicitly enable forwarding:

```yaml
mcp_servers:
  internal_bff:
    command: ...
    forward_hermes_context: true
```

`forward_hermes_context` defaults to `false`. When it is false or absent,
Hermes does not send the Hermes context namespace to that server.

The initial configuration is a boolean rather than a field allowlist. An
enabled server receives every context field that is reliably available at
the call site.

## MCP Contract

Enabled servers receive a versioned, namespaced metadata object:

```json
{
  "io.nous.hermes/context": {
    "version": "1",
    "run_id": "run_abc123",
    "task_id": "task_internal_456",
    "session_id": "conversation_8a3f",
    "session_key": "opaque_scope_7f4c9a2d",
    "tool_call_id": "call_xyz789",
    "turn_id": "turn_01",
    "api_request_id": "req_02",
    "platform": "api_server"
  }
}
```

Hermes passes this object through the MCP SDK's
`ClientSession.call_tool(..., meta=...)` argument. The metadata is separate
from tool `arguments`, is not added to the tool schema, and cannot be supplied
or overridden by model output.

The namespace always contains `version: "1"`. Other fields are included only
when Hermes has a real, non-empty value for them.

## Architecture

Context is divided by lifetime:

- Request- and session-level values such as `session_id`, `session_key`,
  `platform`, and `api_request_id` reuse Hermes' existing runtime context.
  The generated `run_id` receives its own runtime slot so it is not conflated
  with a session or task identifier.
- Call-level values such as `task_id`, `tool_call_id`, and `turn_id` travel
  explicitly through the agent tool-dispatch path.

The data flow is:

```text
API / CLI / TUI / Gateway
  -> runtime ContextVars
  -> tool executor
  -> model_tools.handle_function_call
  -> registry dispatch
  -> MCP handler
  -> ClientSession.call_tool(meta=...)
```

`/v1/runs` binds its generated `run_id` independently for the lifetime of the
run. It does not reuse `session_id` or `task_id` as a substitute. Entry points
that do not already have a run ID do not synthesize one.

The run approval key remains a separate authorization namespace keyed by
`run_id`. It is not forwarded as `session_key`. The MCP context uses the
existing API session-key resolution (`gateway_session_key`, falling back to
the durable session identifier when that is the entry point's established
behavior), while approval routing continues to use its dedicated approval
ContextVar.

The registry continues to forward context through its existing keyword
argument mechanism. Non-MCP handlers may ignore the additional context and
must not regress when it is present.

Immediately before handing work to the MCP server's event loop, the MCP
handler combines runtime and call-level context into a detached per-call
snapshot. The handler does not read `ContextVar` state again after the thread
or event-loop transition. Explicit call-level values take precedence when
both sources provide the same field.

MCP reconnects and `notifications/tools/list_changed` refresh tool discovery
but do not change the server's forwarding configuration.

## Lifecycle And Failure Handling

Request and run entry points use Hermes' existing `set_session_vars()` /
`clear_session_vars()` lifecycle. Success, failure, cancellation, and worker
exit paths explicitly clear the values in `finally` blocks so a later request
cannot observe stale context or fall back to a stale process environment.

For an enabled server, missing optional values do not fail the tool call. The
metadata namespace is still sent with `version: "1"`, and the MCP server may
decide whether its business operation requires additional fields.

Hermes does not silently retry without metadata if the installed MCP SDK
cannot accept `meta`. That condition is an implementation or dependency error
and should fail visibly during testing or execution.

## Security

- Only Hermes runtime code constructs the metadata.
- Context is never copied into model-visible schemas or tool arguments.
- Forwarding is disabled by default and enabled only for explicitly trusted
  MCP servers.
- `session_key` is treated as an opaque scope. Hermes does not interpret it or
  add personal user information.
- Logs must not print a complete `session_key`; diagnostics use a digest or a
  short redacted representation.
- MCP transport authentication remains required. Context metadata identifies
  the source scope but does not replace service authentication.

## Testing

Tests assert behavioral relationships rather than a fixed field count:

1. The context builder includes version and all available fields, and omits
   empty values without inferring replacements.
2. An enabled MCP server receives the namespaced `meta`; a disabled or
   unconfigured server receives no Hermes metadata.
3. Identity context never appears in tool arguments or model-visible schemas.
4. Concurrent sessions sharing one MCP connection retain distinct
   `session_id` and `session_key` values.
5. Parallel tool calls in one run retain distinct `tool_call_id` values.
6. A `/v1/runs` invocation propagates its generated `run_id`; entry points
   without a run ID continue normally.
7. Success, exception, and cancellation paths restore runtime context and do
   not leak values into a subsequent call.
8. Reconnection and tool-list refresh preserve the configured forwarding
   behavior.
9. Existing MCP, registry, tool executor, API server, CLI, TUI, and gateway
   tests continue to pass.

## Acceptance Criteria

- Trusted MCP servers can identify the source Hermes session, run, turn, and
  tool call using standard MCP request metadata.
- Context comes exclusively from Hermes runtime state.
- Concurrent requests and tool calls do not exchange context.
- Servers must explicitly opt in before receiving session context.
- Existing API contracts, MCP schemas, and non-enabled MCP integrations remain
  compatible.
