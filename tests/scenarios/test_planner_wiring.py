"""ClaudePlanner wiring, tested WITHOUT a key through FakeAnthropicTransport.

The planner talks to the Messages API through anthropic_http.AnthropicHTTP, and the only fake is the HTTP transport
underneath it: the request body, headers, response shapes and error shapes are the real wire format. This verifies the
WIRING (request construction, parsing, error handling, the one retry). It says NOTHING about whether a real model plans
well: the plans here are whatever the test queues. The real-model contract (test_planner_contract.py) stays SKIPPED
without ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import json

import pytest

from ingest.anthropic_http import AnthropicAPIError, AnthropicHTTP
from fake_anthropic import FakeAnthropicTransport, error_body, text_response, tool_use_response
from planners import SUBMIT_PLAN, SYSTEM, STRICT_SUFFIX, ClaudePlanner, PlannedCall, PlannerError

INTENTS = [{"id": "a", "intent": "look things up", "uses": ["secret_fact"], "consumes": []},
           {"id": "b", "intent": "act on it", "uses": [], "consumes": ["a"]}]
TOOLS = [{"name": "s1__find", "description": "Find things.",
          "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
         {"name": "s2__act", "description": "Act.", "input_schema": {"type": "object", "properties": {}}}]
GOOD = [{"intent_id": "a", "tool": "s1__find"}, {"intent_id": "b", "tool": "s2__act"}]
KEY = "sk-ant-test-not-a-real-key"


def planner(*responses):
    transport = FakeAnthropicTransport(*responses)
    return ClaudePlanner(client=AnthropicHTTP(KEY, transport=transport), model="m-under-test"), transport


# (i) the request carries the registry and the schema ------------------------------------------------

def test_request_body_includes_the_tool_registry_with_its_schema():
    p, fake = planner((200, tool_use_response(GOOD)))
    p.plan(INTENTS, TOOLS)
    [req] = fake.requests
    body = fake.bodies[0]
    assert (req.method, req.url.path) == ("POST", "/v1/messages")
    assert req.headers["x-api-key"] == KEY and req.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "m-under-test" and body["max_tokens"] > 0
    assert body["system"] == SYSTEM
    assert body["tool_choice"] == {"type": "auto"}, "a forced tool_choice is rejected by the current default model (D16)"
    assert body["tools"] == [SUBMIT_PLAN], "the planner offers exactly the submit_plan tool, with its exact schema"
    assert body["tools"][0]["input_schema"]["properties"]["calls"]["items"]["required"] == ["intent_id", "tool"]
    [msg] = body["messages"]
    assert msg["role"] == "user"
    sent = json.loads(msg["content"])
    # the REGISTRY goes to the model: every offered tool, name and description
    assert sent["tools"] == [{"name": "s1__find", "description": "Find things."},
                             {"name": "s2__act", "description": "Act."}]
    assert sent["intents"] == [{"id": "a", "intent": "look things up", "consumes": []},
                               {"id": "b", "intent": "act on it", "consumes": ["a"]}]
    assert "secret_fact" not in req.content.decode(), "facts and `uses` never leave the process"
    assert KEY not in req.content.decode(), "the key travels in a header only, never in the body"


# (ii) a valid tool_use response parses -------------------------------------------------------------

def test_a_valid_tool_use_response_parses_to_planned_calls():
    p, fake = planner((200, tool_use_response(GOOD)))
    assert p.plan(INTENTS, TOOLS) == [PlannedCall("a", "s1__find"), PlannedCall("b", "s2__act")]
    assert len(fake.requests) == 1, "a good first reply needs no retry"


# (iii) no tool_use: the documented error -----------------------------------------------------------

def test_no_tool_use_twice_raises_the_documented_error_after_exactly_two_requests():
    p, fake = planner((200, text_response("Sure! First I would look things up.")),
                      (200, text_response("Of course. Here is my plan in prose.")))
    with pytest.raises(PlannerError, match=r"PLANNER_NO_TOOL_CALL.*stop_reason='end_turn'"):
        p.plan(INTENTS, TOOLS)
    assert len(fake.requests) == 2, "one try plus exactly one retry, then the error (never a third request)"


# (iv) the retry-with-auto path: fires once ---------------------------------------------------------

def test_one_miss_then_a_valid_reply_succeeds_with_exactly_one_retry_using_a_stricter_prompt():
    p, fake = planner((200, text_response("Let me think about this...")), (200, tool_use_response(GOOD)))
    assert p.plan(INTENTS, TOOLS) == [PlannedCall("a", "s1__find"), PlannedCall("b", "s2__act")]
    first, second = fake.bodies
    assert first["system"] == SYSTEM and second["system"] == SYSTEM + STRICT_SUFFIX
    assert "did not call submit_plan" in second["system"]
    assert first["tool_choice"] == second["tool_choice"] == {"type": "auto"}, "the retry is still auto, not forced"
    assert first["messages"] == second["messages"] and first["tools"] == second["tools"]


# supporting wiring: errors are explicit, never retried, never swallowed ------------------------------

def test_an_api_error_is_wrapped_not_retried_and_leaks_no_key():
    p, fake = planner((529, error_body("overloaded_error", "Overloaded")))
    with pytest.raises(PlannerError, match="PLANNER_API_FAILED: AnthropicAPIError: HTTP 529 overloaded_error") as exc:
        p.plan(INTENTS, TOOLS)
    assert len(fake.requests) == 1 and KEY not in str(exc.value)


@pytest.mark.parametrize("calls", ["nope", [{"intent_id": "a"}], [{"intent_id": 1, "tool": "x"}], None])
def test_a_malformed_plan_is_refused(calls):
    p, _ = planner((200, tool_use_response(calls)))
    with pytest.raises(PlannerError, match="PLANNER_BAD_PLAN"):
        p.plan(INTENTS, TOOLS)


def test_a_tool_use_block_for_the_wrong_tool_counts_as_a_miss():
    p, fake = planner((200, tool_use_response(GOOD, tool="something_else")), (200, tool_use_response(GOOD)))
    assert len(p.plan(INTENTS, TOOLS)) == 2 and len(fake.requests) == 2


def test_a_non_message_body_is_an_error_not_a_plan():
    p, _ = planner((200, {"hello": "world"}))
    with pytest.raises(PlannerError, match="PLANNER_API_FAILED.*invalid_response"):
        p.plan(INTENTS, TOOLS)


def test_building_the_client_without_a_key_fails_loudly_and_never_calls_out(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AnthropicAPIError, match="no_api_key"):
        AnthropicHTTP()
    assert ClaudePlanner().name == "claude"  # constructing the planner is free; only .plan() reaches for a client
