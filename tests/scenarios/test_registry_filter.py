"""Registry filtering (CLAUDE.md D31): a model is offered the few tools an intent could use, not the whole registry.

What is held to account here: the filter is deterministic; a matching duty's tools are never filtered out; an intent it cannot
read gets the full registry (no silent exclusion); the planner CONTRACT (the check_* functions of test_planner_contract.py,
imported, not copied) still passes on filtered requests; and the Orchestrator records how many tools it had and how many it
offered. The contract's own tests are not changed: it still runs against the whole registry for every planner."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from agent import registry_filter
from agent.duties import DutyLibrary
from agent.orchestrator import Orchestrator
from fake_anthropic import FakeAnthropicTransport, tool_use_response
from ingest.anthropic_http import AnthropicHTTP
from inmem import InMemoryRegistry, SpyRegistry
from planners import ClaudePlanner, FilteredPlanner, KeywordPlanner, PlannedCall, max_tools_from_env
from test_planner_contract import (INTENTS, SCENARIO, check_at_least_two_industries,
                                   check_each_intent_once_with_a_tool_of_its_declared_industry, check_no_call_before_its_prerequisite,
                                   check_only_registry_tools, industry)

ROOT = Path(__file__).resolve().parents[2]
CAP = registry_filter.DEFAULT_MAX_TOOLS


@pytest.fixture(scope="module")
def tools() -> list[dict]:
    async def load():
        async with InMemoryRegistry(ROOT / "servers") as reg:
            return [dict(t) for t in reg.anthropic_tools()]
    return asyncio.run(load())


def names(subset) -> list[str]:
    return [t["name"] for t in subset]


# -- (1) deterministic --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("intent", INTENTS, ids=[i["id"] for i in INTENTS])
def test_the_filter_is_deterministic_across_20_runs_and_does_not_depend_on_the_registrys_listing_sequence(tools, intent):
    first = names(registry_filter.select_tools(intent["intent"], tools, CAP))
    for _ in range(20):
        assert names(registry_filter.select_tools(intent["intent"], tools, CAP)) == first
    assert names(registry_filter.select_tools(intent["intent"], list(reversed(tools)), CAP)) == list(reversed(first)), \
        "the same SET of tools however the registry lists them"


def test_the_filter_actually_filters_each_intent_gets_at_most_the_cap(tools):
    for intent in INTENTS:
        subset = registry_filter.select_tools(intent["intent"], tools, CAP)
        assert 1 <= len(subset) <= CAP < len(tools), f"{intent['id']}: {len(subset)} of {len(tools)}"


def test_under_a_tight_cap_an_industry_the_intent_names_comes_before_a_higher_scoring_tool_of_another():
    synthetic = [{"name": "other__track", "description": "Track the record.", "input_schema": {}},
                 {"name": "bank__lookup", "description": "Look up a record.", "input_schema": {}}]
    intent = "track the bank record"
    assert names(registry_filter.select_tools("track the record", synthetic, 1)) == ["other__track"], "without the hint the stronger match wins"
    assert names(registry_filter.select_tools(intent, synthetic, 1)) == ["bank__lookup"], "the named industry is preferred (iii)"


def test_a_cap_below_one_is_refused(tools):
    with pytest.raises(ValueError, match="at least 1"):
        registry_filter.select_tools("find flights", tools, 0)


# -- (2) a matching duty's tools are always included ----------------------------------------------------------------------

def test_a_matching_duty_keeps_every_one_of_its_tools_even_past_the_cap(tools):
    duties = DutyLibrary()
    goal = "refund order ORD-ABCD1234"
    [match] = [m for m in duties.candidates(goal) if m.duty.name == "process_refund"]
    wanted = {s.tool for s in match.duty.steps}
    assert len(wanted) >= 3, "the duty needs several tools, so a cap of 1 would drop some"
    subset = registry_filter.select_tools(goal, tools, 1, duties)
    assert wanted <= set(names(subset)), f"duty tools missing: {sorted(wanted - set(names(subset)))}"
    assert len(subset) <= len(wanted) + 1, "the cap still limits everything that is not a duty tool"


def test_without_a_matching_duty_nothing_is_forced_in(tools):
    duties = DutyLibrary()
    with_duties = names(registry_filter.select_tools("look up available flights between two airports", tools, CAP, duties))
    assert with_duties == names(registry_filter.select_tools("look up available flights between two airports", tools, CAP))


# -- (3) the planner contract still passes on filtered requests -----------------------------------------------------------

def test_the_planner_contract_passes_with_a_filtered_registry_on_the_cross_industry_fixture(tools):
    planner = FilteredPlanner(KeywordPlanner(), CAP)
    plan = planner.plan(INTENTS, tools)
    check_each_intent_once_with_a_tool_of_its_declared_industry(plan)
    check_at_least_two_industries(plan)
    check_only_registry_tools(plan, tools)
    check_no_call_before_its_prerequisite(plan)
    stats = planner.last_stats
    assert stats["registry_size_before"] == len(tools) and stats["requests"] == len(INTENTS)
    assert all(n <= CAP for n in stats["tools_per_request"]) and stats["registry_size_after"] < len(tools)


def test_every_intents_correct_tool_survives_the_filter(tools):
    """If the filter excluded the tool the contract needs, that is a FILTER bug: assert it per intent, against the unfiltered choice."""
    unfiltered = {c.intent_id: c.tool for c in KeywordPlanner().plan(INTENTS, tools)}
    for intent in INTENTS:
        offered = names(registry_filter.select_tools(intent["intent"], tools, CAP))
        assert unfiltered[intent["id"]] in offered, f"{intent['id']}: {unfiltered[intent['id']]} was filtered out of {offered}"
        assert industry(unfiltered[intent["id"]]) == next(i["industry"] for i in SCENARIO["intents"] if i["id"] == intent["id"])


def test_the_filtered_registry_view_offers_the_subset_and_still_calls_and_classifies_through_the_real_one(tools):
    class Base:
        connect_errors = {"x": "boom"}

        def anthropic_tools(self):
            return tools

        def is_read_only(self, name):
            return name.endswith("search_flights")

    view = registry_filter.filter("look up available flights between two airports", Base(), CAP)
    assert view.size_before == len(tools) and view.size_after == len(view.anthropic_tools()) <= CAP
    assert view.is_read_only("travel__search_flights") is True and view.connect_errors == {"x": "boom"}


# -- (4) unmatchable: the full registry, never a silent exclusion --------------------------------------------------------------

@pytest.mark.parametrize("intent", ["zzz qqq xyzzy", "", "   ", "the a an of"])
def test_an_unmatchable_intent_gets_the_full_registry(tools, intent):
    assert names(registry_filter.select_tools(intent, tools, 3)) == names(tools)


def test_an_unmatchable_intent_through_filter_reports_equal_sizes(tools):
    class Base:
        def anthropic_tools(self):
            return tools

    view = registry_filter.filter("zzz qqq xyzzy", Base(), 3)
    assert (view.size_before, view.size_after) == (len(tools), len(tools))


# -- the planner wiring: one small request per intent --------------------------------------------------------------------------

SMALL_TOOLS = [{"name": "s1__find", "description": "Find things in the catalogue.", "input_schema": {}},
               {"name": "s2__act", "description": "Act on things.", "input_schema": {}},
               {"name": "s3__polish", "description": "Polish widgets until they shine.", "input_schema": {}},
               {"name": "s4__weigh", "description": "Weigh parcels.", "input_schema": {}}]
SMALL_INTENTS = [{"id": "act", "intent": "act on things", "uses": ["secret"], "consumes": ["find"]},
                 {"id": "find", "intent": "find things", "uses": [], "consumes": []}]  # given OUT of dependency sequence
KEY = "sk-ant-test-not-a-real-key"


def fake_planner(*calls, max_tools=2):
    transport = FakeAnthropicTransport(*[(200, tool_use_response([c])) for c in calls])
    return ClaudePlanner(client=AnthropicHTTP(KEY, transport=transport), model="m", max_tools=max_tools), transport


def test_filtered_claude_planner_sends_one_request_per_intent_with_only_that_intents_tools():
    p, fake = fake_planner({"intent_id": "act", "tool": "s2__act"}, {"intent_id": "find", "tool": "s1__find"})
    plan = p.plan(SMALL_INTENTS, SMALL_TOOLS)
    assert len(fake.requests) == 2
    sent = [json.loads(b["messages"][0]["content"]) for b in fake.bodies]
    assert [[i["id"] for i in s["intents"]] for s in sent] == [["act"], ["find"]]
    assert all(0 < len(s["tools"]) <= 2 for s in sent)
    assert "s3__polish" not in json.dumps(sent) and "secret" not in json.dumps(sent)
    assert p.last_stats["registry_size_before"] == 4 and p.last_stats["requests"] == 2
    assert p.last_stats["registry_size_after"] == len({t["name"] for s in sent for t in s["tools"]})
    assert plan == [PlannedCall("find", "s1__find"), PlannedCall("act", "s2__act")], "a call never precedes the call it consumes"


def test_without_a_cap_claude_planner_still_sends_the_whole_registry_in_one_request(monkeypatch):
    monkeypatch.delenv("PLANNER_MAX_TOOLS", raising=False)
    p, fake = fake_planner({"intent_id": "act", "tool": "s2__act"}, max_tools=None)
    p.plan(SMALL_INTENTS[:1], SMALL_TOOLS)
    assert len(fake.requests) == 1
    assert len(json.loads(fake.bodies[0]["messages"][0]["content"])["tools"]) == 4
    assert (p.last_stats["registry_size_before"], p.last_stats["registry_size_after"]) == (4, 4)


def test_each_filtered_request_keeps_the_planners_own_retry():
    transport = FakeAnthropicTransport((200, {"content": [{"type": "text", "text": "hmm"}], "stop_reason": "end_turn",
                                              "role": "assistant", "type": "message"}),
                                       (200, tool_use_response([{"intent_id": "find", "tool": "s1__find"}])))
    p = ClaudePlanner(client=AnthropicHTTP(KEY, transport=transport), model="m", max_tools=2)
    assert p.plan(SMALL_INTENTS[1:], SMALL_TOOLS) == [PlannedCall("find", "s1__find")]
    assert len(transport.requests) == 2, "one miss, one retry, same as the unfiltered planner"


def test_planner_max_tools_comes_from_the_environment_and_a_bad_value_is_an_error():
    assert max_tools_from_env({}) is None and max_tools_from_env({"PLANNER_MAX_TOOLS": " "}) is None
    assert max_tools_from_env({"PLANNER_MAX_TOOLS": "8"}) == 8
    for bad in ("0", "-1", "abc", "2.5"):
        with pytest.raises(ValueError, match="PLANNER_MAX_TOOLS"):
            max_tools_from_env({"PLANNER_MAX_TOOLS": bad})


def test_the_environment_switches_filtering_on_without_a_code_change(monkeypatch):
    monkeypatch.setenv("PLANNER_MAX_TOOLS", "2")
    transport = FakeAnthropicTransport((200, tool_use_response([{"intent_id": "find", "tool": "s1__find"}])))
    p = ClaudePlanner(client=AnthropicHTTP(KEY, transport=transport), model="m")
    p.plan(SMALL_INTENTS[1:], SMALL_TOOLS)
    assert len(json.loads(transport.bodies[0]["messages"][0]["content"])["tools"]) <= 2


def test_the_stats_file_gets_one_json_line_per_plan(monkeypatch, tmp_path):
    stats = tmp_path / "stats.jsonl"
    monkeypatch.setenv("PLANNER_STATS_FILE", str(stats))
    p, _ = fake_planner({"intent_id": "find", "tool": "s1__find"})
    p.plan(SMALL_INTENTS[1:], SMALL_TOOLS)
    [line] = stats.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["registry_size_before"] == 4 and record["max_tools"] == 2 and record["requests"] == 1
    assert record["model"] == "m" and record["total_seconds"] >= 0 and "key" not in line.lower()


# -- the Orchestrator records before and after -----------------------------------------------------------------------------

class Capture:
    """A model that only records which tools it was offered, then stops."""

    def __init__(self):
        self.seen, self.messages = [], self

    async def create(self, *, tools, **_):
        self.seen.append(names(tools))
        return NS(content=[NS(type="text", text="done")])


async def test_the_orchestrator_offers_the_filtered_subset_and_records_the_sizes(tmp_path):
    async with SpyRegistry(ROOT / "servers") as reg:
        total = len(reg.anthropic_tools())
        llm = Capture()
        orch = Orchestrator(reg, client=llm, user_id="t", run_log_dir=tmp_path, duties=None, registry_filter_max_tools=3)
        result = await orch.run("look up available flights between two airports")
    [offered] = llm.seen
    assert result.success and 0 < len(offered) <= 3 < total
    assert len(orch._tool_defs) == total, "every tool stays known to the duty and policy paths; only the OFFER is filtered"
    assert (result.state.registry_size_before, result.state.registry_size_after) == (total, len(offered))
    record = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert (record["registry_size_before"], record["registry_size_after"]) == (total, len(offered))
    assert any(f"REGISTRY SIZE: before={total} after={len(offered)}" in line for line in result.state.log)


async def test_without_a_cap_the_orchestrator_offers_everything_and_records_equal_sizes(tmp_path):
    async with SpyRegistry(ROOT / "servers") as reg:
        total = len(reg.anthropic_tools())
        llm = Capture()
        result = await Orchestrator(reg, client=llm, user_id="t", run_log_dir=tmp_path, duties=None).run("look up flights")
    assert len(llm.seen[0]) == total
    assert (result.state.registry_size_before, result.state.registry_size_after) == (total, total)
    record = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert record["registry_size_before"] == record["registry_size_after"] == total


async def test_a_duty_run_under_a_tiny_cap_still_finds_every_duty_tool(tmp_path):
    from duty_helpers import book_flight_goal, reset_worlds
    reset_worlds()
    try:
        async with SpyRegistry(ROOT / "servers") as reg:
            orch = Orchestrator(reg, allow_writes=True, client=Capture(), user_id="t", run_log_dir=tmp_path,
                                registry_filter_max_tools=1)
            result = await orch.run(book_flight_goal())
    finally:
        reset_worlds()
    assert result.duty_used == "book_flight", "the duty's tools stay known to the orchestrator whatever the cap"
    assert result.state.registry_size_after >= 3
