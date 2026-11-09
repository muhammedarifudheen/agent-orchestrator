"""The cross-industry scenario, executed with each IntentPlanner. KeywordPlanner always; ClaudePlanner only when
opted in (skipped WITH A REASON otherwise). Same assertions for both. Not a gate module: the opt-in half needs network.

The deeper per-industry assertions live in test_cross_industry.py (keyword, offline, 2 s budget); here the point is that
the SAME scenario and the SAME checks hold whoever chooses the tools.
"""
from __future__ import annotations

import pytest

import banking_server
import retail_server
import travel_server
from planners import ClaudePlanner, KeywordPlanner
from test_cross_industry import INTENTS, SCENARIO, fresh_worlds, handoff, industry_of, run_scenario  # noqa: F401
from test_planner_contract import claude_skip_reason

PLANNERS = [
    pytest.param(KeywordPlanner, id="keyword"),
    pytest.param(ClaudePlanner, id="claude",
                 marks=pytest.mark.skipif(claude_skip_reason() is not None, reason=claude_skip_reason() or "opted in")),
]


@pytest.mark.parametrize("make_planner", PLANNERS)
async def test_success_path_with_this_planner(make_planner, tmp_path, monkeypatch):
    result, llm, calls, _ = await run_scenario(tmp_path, monkeypatch=monkeypatch, planner=make_planner())
    assert result.success, result.final_message
    assert sorted(i for i, _ in llm.issued) == sorted(i["id"] for i in INTENTS)
    pos = {i: n for n, (i, _) in enumerate(llm.issued)}
    for before, after in SCENARIO["order_constraints"]:
        assert pos[before] < pos[after], (before, after)
    assert list(dict.fromkeys(industry_of(t) for t in calls)) == SCENARIO["expected_industries_in_order"]
    assert any(b["status"] == "CONFIRMED" for b in travel_server._BOOKINGS.values())
    assert len(banking_server._TRANSFERS) == 1
    assert next(iter(retail_server._ORDERS.values()))["status"] == "SHIPPED"
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.parametrize("make_planner", PLANNERS)
async def test_banking_outage_hands_off_with_this_planner(make_planner, tmp_path, monkeypatch):
    inj = SCENARIO["failure_injection"]
    result, llm, calls, _ = await run_scenario(tmp_path, env=inj["env"], monkeypatch=monkeypatch, planner=make_planner())
    assert result.success is False
    data = handoff(tmp_path)
    assert data["failure_industry"] == "banking" and "travel" in data["industries_touched"]
    assert data["tools_called_by_industry"].keys() == set(data["industries_touched"])
    assert retail_server._ORDERS == {} or "retail" in data["industries_touched"]
