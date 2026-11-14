"""Planner contract: EVERY IntentPlanner must satisfy the same assertions on the cross-industry intents.

KeywordPlanner always runs. ClaudePlanner runs only when opted in (ANTHROPIC_API_KEY set and LIVE_TESTS=1) and is
otherwise SKIPPED with the reason, never passed: a skipped test says "not checked", a pass would claim it was.
The assertions are plain functions (check_*) used by both planners; `test_the_checks_can_fail` feeds them bad plans
to prove they are not vacuous. This module is deliberately not a gate module (the opt-in Claude half needs network).

The scenario is therefore validated by this contract, not by tuning the keyword matcher: the matcher is one
candidate planner that has to pass it, and a model-written plan is held to exactly the same bar.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml

from agent import provider_config
from inmem import InMemoryRegistry
from planners import ClaudePlanner, IntentPlanner, KeywordPlanner, PlannedCall

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = yaml.safe_load((ROOT / "scenarios" / "cross_industry.yaml").read_text(encoding="utf-8"))
# what a planner is allowed to see: plain words only (no industry label, no facts)
INTENTS = [{k: i.get(k) for k in ("id", "intent", "uses", "consumes")} for i in SCENARIO["intents"]]
DECLARED = {i["id"]: i["industry"] for i in SCENARIO["intents"]}


def industry(tool: str) -> str:
    return tool.partition("__")[0]


# -- the contract (shared, identical for every planner) -------------------------------------------

def check_each_intent_once_with_a_tool_of_its_declared_industry(plan: list[PlannedCall]) -> None:
    ids = [c.intent_id for c in plan]
    assert sorted(ids) == sorted(DECLARED), f"every intent exactly once; got {ids}"
    for c in plan:
        assert industry(c.tool) == DECLARED[c.intent_id], f"{c.intent_id}: {c.tool} is not a {DECLARED[c.intent_id]} tool"


def check_at_least_two_industries(plan: list[PlannedCall]) -> None:
    industries = {industry(c.tool) for c in plan}
    assert len(industries) >= 2, f"a cross-industry plan must touch >= 2 industries, got {sorted(industries)}"


def check_only_registry_tools(plan: list[PlannedCall], tools: list[dict]) -> None:
    offered = {t["name"] for t in tools}
    outside = [c.tool for c in plan if c.tool not in offered]
    assert not outside, f"tools proposed that the registry does not offer: {outside}"


def check_no_call_before_its_prerequisite(plan: list[PlannedCall]) -> None:
    pos = {c.intent_id: n for n, c in enumerate(plan)}
    needs = [(dep, i["id"]) for i in INTENTS for dep in (i["consumes"] or [])] + [tuple(p) for p in SCENARIO["order_constraints"]]
    for before, after in needs:
        assert before in pos and after in pos, f"plan lacks {before} or {after}"
        assert pos[before] < pos[after], f"{after} is planned before its prerequisite {before}"


# -- planners under test --------------------------------------------------------------------------

def claude_skip_reason() -> str | None:
    if not provider_config.credentials_present():  # ANTHROPIC_API_KEY, or a free provider configured through PROVIDER_* (D29)
        return "ANTHROPIC_API_KEY absent"
    if os.environ.get("LIVE_TESTS") != "1":
        return "LIVE_TESTS=1 not set"
    return None


PLANNERS = [
    pytest.param("keyword", id="keyword"),
    pytest.param("claude", id="claude",
                 marks=pytest.mark.skipif(claude_skip_reason() is not None, reason=claude_skip_reason() or "opted in")),
]
_BUILD = {"keyword": KeywordPlanner, "claude": ClaudePlanner}


@pytest.fixture(scope="module")
def tools() -> list[dict]:
    async def load():
        async with InMemoryRegistry(ROOT / "servers") as reg:  # every registered server, generated ones included
            return [dict(t) for t in reg.anthropic_tools()]
    return asyncio.run(load())


_PLANS: dict[str, list[PlannedCall]] = {}


@pytest.fixture
def plan(request, tools) -> list[PlannedCall]:
    name = request.param
    if name not in _PLANS:  # one real API call per run, not one per assertion
        planner: IntentPlanner = _BUILD[name]()
        _PLANS[name] = planner.plan(INTENTS, tools)
    return _PLANS[name]


@pytest.mark.parametrize("plan", PLANNERS, indirect=True)
class TestPlannerContract:
    def test_each_intent_exactly_once_with_a_tool_of_its_declared_industry(self, plan):
        check_each_intent_once_with_a_tool_of_its_declared_industry(plan)

    def test_uses_at_least_two_industries(self, plan):
        check_at_least_two_industries(plan)

    def test_never_proposes_a_tool_outside_the_registry(self, plan, tools):
        check_only_registry_tools(plan, tools)

    def test_never_proposes_a_call_before_its_prerequisite(self, plan):
        check_no_call_before_its_prerequisite(plan)


# -- the checks themselves must be able to fail ---------------------------------------------------

def _good(tools) -> list[PlannedCall]:
    return KeywordPlanner().plan(INTENTS, tools)


def test_the_checks_pass_on_the_keyword_plan_and_fail_on_bad_plans(tools):
    good = _good(tools)
    check_each_intent_once_with_a_tool_of_its_declared_industry(good)
    check_at_least_two_industries(good)
    check_only_registry_tools(good, tools)
    check_no_call_before_its_prerequisite(good)

    one_industry = [PlannedCall(c.intent_id, good[0].tool) for c in good]          # everything sent to one industry
    with pytest.raises(AssertionError, match=">= 2 industries"):
        check_at_least_two_industries(one_industry)

    ghost = [PlannedCall(good[0].intent_id, "ghost__do_thing"), *good[1:]]          # a tool the registry never offered
    with pytest.raises(AssertionError, match="does not offer"):
        check_only_registry_tools(ghost, tools)

    book_before_search = [good[1], good[0], *good[2:]]                              # a call before its prerequisite
    with pytest.raises(AssertionError, match="before its prerequisite"):
        check_no_call_before_its_prerequisite(book_before_search)

    missing = good[:-1]                                                             # an intent silently dropped
    with pytest.raises(AssertionError, match="exactly once"):
        check_each_intent_once_with_a_tool_of_its_declared_industry(missing)

    wrong_industry = [PlannedCall(good[0].intent_id, good[-1].tool), *good[1:]]     # right shape, wrong system
    with pytest.raises(AssertionError, match="is not a"):
        check_each_intent_once_with_a_tool_of_its_declared_industry(wrong_industry)


def test_claude_skip_reason_names_what_is_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert claude_skip_reason() == "ANTHROPIC_API_KEY absent"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    assert claude_skip_reason() == "LIVE_TESTS=1 not set"
    monkeypatch.setenv("LIVE_TESTS", "1")
    assert claude_skip_reason() is None
