# Task 1 Report: Custom Provider Request Overrides

## Scope

Rebuilt the gateway-only request-override propagation from fork commit
`224c5f1f75a018593e206ad096e382b7d95dff4b` on top of the current upstream
structure containing `ba9964ff0`. No Responses WebSocket fields were added.

## Root Cause

The current runtime-provider resolver already returned `request_overrides`, but
the gateway adapter functions projected runtime dictionaries without that key.
The model switch result also had no field for the resolved overrides. As a
result, both gateway `/model` storage paths dropped provider request fields;
credentialed session overrides returned before runtime overrides were attached;
persisted rehydration restored credentials but not request overrides; turn
routing replaced provider overrides with an empty or fast-mode-only mapping; and
the shared API-server runtime projection dropped overrides when an API request
reused a `/model` session.

## Implementation

- Added optional `request_overrides` to `ModelSwitchResult` and propagated the
  resolved mapping through explicit-provider, current-provider, and named
  custom-provider switch paths.
- Preserved overrides in the gateway primary, provider-specific, and fallback
  runtime resolver projections.
- Preserved overrides in the API-server runtime resolver and runtime override
  allowlist so shared API sessions retain custom-provider request fields.
- Stored resolved overrides in both typed `/model` and inline-picker session
  override paths. Existing persistence sanitization continues to write only
  `model`, `provider`, and `base_url`; credentials and request overrides are not
  serialized.
- Rehydrated provider request overrides from live credential/runtime resolution
  after restart.
- Added a gateway-local deep merge helper using the existing config merge
  semantics. Session-specific nested values win over current runtime values;
  fast-mode values merge on top without deleting provider fields.
- Updated turn routing and cached-agent per-turn assignment so the next real
  agent turn receives the resolved request overrides in its wire kwargs.

## TDD Evidence

RED was run before production edits:

```text
9 failed, 6 warnings in 2.28s
```

The failures were the expected missing-field, dropped-runtime, replacement-
instead-of-deep-merge, picker-storage, rehydration, and next-agent-turn
assertions. A separately discovered fallback projection was then covered by a
new RED test before its one-line propagation fix.

## Tests

Final focused command:

```text
233 passed, 7 warnings in 8.02s
```

The command covered gateway model-command and picker paths, session override
persistence/routing, fast mode, fallback resolution, custom-provider runtime
and model switching, custom-provider extra-body matching, chat-completions
transport tests, and transport parity tests.
API-server runtime resolution and override application are also covered.

Additional checks:

- `ruff check` on all changed Python files: passed.
- `py_compile` on changed production and new test files: passed.
- `git diff --check`: passed.

## Self Review

- Request overrides are copied at resolver/storage boundaries, so callers do
  not share the runtime provider's mutable mapping.
- Nested merge precedence is explicit and tested at both session and fast-mode
  boundaries.
- Persisted session data remains limited to the existing non-secret whitelist;
  the persistence test includes request overrides and verifies they are absent
  from the serialized form while live rehydration restores them.
- The end-to-end regression invokes the real gateway `/model` handler followed
  by the real `_run_agent` path and asserts on captured `AIAgent` constructor
  kwargs, rather than only testing a helper.
- No changes were made to config normalization, agent-init provider parsing,
  Responses WebSocket behavior, or unrelated transport behavior.

## Residual Concerns

The focused suite emits existing third-party deprecation warnings from
`pkg_resources`/`lark_oapi`; they are unrelated to this change. No functional
concerns remain within Task 1 scope.
