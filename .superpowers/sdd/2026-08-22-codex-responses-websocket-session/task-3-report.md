Status: fixed
Commit: pending
Tests:
- `.venv/bin/pytest tests/agent/test_codex_responses_ws_session.py tests/agent/test_codex_responses_ws_transport.py -q`
- `python3 -m py_compile agent/codex_responses_ws_session.py agent/codex_responses_ws_transport.py agent/codex_runtime.py tests/agent/test_codex_responses_ws_session.py tests/agent/test_codex_responses_ws_transport.py`
Concerns:
- `tests/agent/test_codex_responses_ws_transport.py` now stubs `httpx` and `openai` for the `run_codex_stream` unit cases because this worktree venv does not provide those imports.
