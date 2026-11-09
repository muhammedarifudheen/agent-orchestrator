"""ClaudePlanner wiring, tested WITHOUT a key through an injected fake client (same approach as D16's StubAnthropic).

This is NOT the planner contract: it shows the request is well-formed and the reply is parsed or refused loudly. It
says nothing about whether a real model plans well; only test_planner_contract.py against the real API can.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from planners import ClaudePlanner, PlannedCall, PlannerError

INTENTS = [{"id": "a", "intent": "look things up", "uses": ["secret_fact"], "consumes": []},
           {"id": "b", "intent": "act on it", "uses": [], "consumes": ["a"]}]
TOOLS = [{"name": "s1__find", "description": "Find things."}, {"name": "s2__act", "description": "Act."}]


class FakeClient:
    def __init__(self, content, stop_reason="tool_use", boom=None):
        self.messages, self.calls, self._content, self._stop, self._boom = self, [], content, stop_reason, boom

    def create(self, **kw):
        self.calls.append(kw)
        if self._boom:
            raise self._boom
        return NS(content=self._content, stop_reason=self._stop)


def submit(calls):
    return NS(type="tool_use", name="submit_plan", id="x", input={"calls": calls})


def test_plan_is_parsed_and_the_request_is_well_formed():
    client = FakeClient([submit([{"intent_id": "a", "tool": "s1__find"}, {"intent_id": "b", "tool": "s2__act"}])])
    plan = ClaudePlanner(client=client, model="m").plan(INTENTS, TOOLS)
    assert plan == [PlannedCall("a", "s1__find"), PlannedCall("b", "s2__act")]
    [req] = client.calls
    assert req["tool_choice"] == {"type": "auto"} and req["model"] == "m"
    assert [t["name"] for t in req["tools"]] == ["submit_plan"]
    sent = json.loads(req["messages"][0]["content"])
    assert [t["name"] for t in sent["tools"]] == ["s1__find", "s2__act"]
    assert [i["id"] for i in sent["intents"]] == ["a", "b"] and sent["intents"][1]["consumes"] == ["a"]
    assert "secret_fact" not in req["messages"][0]["content"], "facts and `uses` never go to the planner"


def test_a_reply_without_the_tool_call_is_an_explicit_failure_not_a_fallback():
    client = FakeClient([NS(type="text", text="Sure! First I would...")], stop_reason="end_turn")
    with pytest.raises(PlannerError, match="PLANNER_NO_TOOL_CALL"):
        ClaudePlanner(client=client).plan(INTENTS, TOOLS)
    assert len(client.calls) == 1, "no silent second attempt"


@pytest.mark.parametrize("calls", ["nope", [{"intent_id": "a"}], [{"intent_id": 1, "tool": "x"}], None])
def test_a_malformed_plan_is_refused(calls):
    with pytest.raises(PlannerError, match="PLANNER_BAD_PLAN"):
        ClaudePlanner(client=FakeClient([submit(calls)])).plan(INTENTS, TOOLS)


def test_an_api_failure_is_wrapped_with_its_type():
    with pytest.raises(PlannerError, match="PLANNER_API_FAILED: TimeoutError"):
        ClaudePlanner(client=FakeClient([], boom=TimeoutError("slow"))).plan(INTENTS, TOOLS)


def test_building_the_default_planner_needs_no_key_or_sdk_until_plan_is_called(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ClaudePlanner().name == "claude"  # construction is free; only .plan() would reach for the SDK
