from __future__ import annotations

from typing import Any

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
    return ResponsesWebsocketSession(state_enabled=state_enabled)


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
