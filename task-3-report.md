# Task 3 Report: OpenAI Image Endpoint Overrides

## Scope

Rebuilt the endpoint and credential override behavior from fork commit
`ef1e14a01818fe1c401197f3684e3ec07d4d561d` on top of Task 2's current
upstream-based worktree. The implementation preserves the existing model,
image edit/reference, URL caching, and managed artifact behavior.

## Implementation

- Added profile-aware `image_gen.openai.base_url` normalization (trailing
  slashes removed) and forwarded it to the OpenAI client.
- Reused `hermes_cli.providers.is_official_openai_host` for exact OpenAI host
  family validation, including regional hosts and spoof rejection.
- Official/default endpoints may resolve `OPENAI_API_KEY` through
  `agent.secret_scope.get_secret`; custom endpoints require a non-empty
  `key_env` or `api_key_env` name and resolve only that secret.
- Missing custom bindings or bound secrets return `auth_required` before the
  OpenAI SDK is imported or a client is constructed. Plain API-key config
  values are not read or forwarded, and secrets are not logged.
- Added provider and artifact regressions covering official fallback, custom
  key binding, missing binding, missing secret, spoofed host, and image edits.

## TDD Evidence

The required security regression was run before production edits and failed as
expected because the old provider reached the client with the global key:

```text
1 failed: expected auth_required, got io_error
```

## Tests

Final focused matrix:

```text
32 passed in 1.42s
```

Command:

```text
.venv/bin/python -m pytest -q tests/plugins/image_gen/test_openai_provider.py tests/tools/test_image_generation_artifacts.py
```

Additional checks:

- `ruff check` on all changed Python files: passed.
- `py_compile` on all changed Python files: passed.
- `git diff --check`: passed.

## Reviewer Fix Round

Added a RED regression for official/default endpoints with an explicitly
configured but missing or empty `key_env`; the pre-fix run produced `1 failed,
1 passed`. The resolver now retries the profile-aware global
`OPENAI_API_KEY` fallback only for official OpenAI hosts. Custom endpoints
still reject missing bindings or secrets without consulting the global key.

## Residual Concerns

The focused suite emits no new warnings or failures. Broader repository tests
were not run because this task is isolated to the OpenAI image provider and
artifact paths.
