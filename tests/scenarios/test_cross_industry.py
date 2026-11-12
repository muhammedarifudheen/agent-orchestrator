"""Cross-industry scenario: real Orchestrator + real registry over ALL servers + IntentLLM stub (see intent_llm.py).

Proves the orchestrator/registry are industry-neutral; it does not prove a real model chooses well."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

import banking_server
import retail_server
import travel_server
from agent.orchestrator import Orchestrator
from inmem import SpyRegistry
from intent_llm import IntentLLM, PlannedLLM

ROOT = Path(__file__).parent.parent.parent
SERVERS = ROOT / "servers"
SCENARIO = yaml.safe_load((ROOT / "scenarios" / "cross_industry.yaml").read_text(encoding="utf-8"))
INTENTS = SCENARIO["intents"]
INDUSTRY = {i["id"]: i["industry"] for i in INTENTS}


def industry_of(tool: str) -> str:
    return tool.partition("__")[0]


@pytest.fixture(autouse=True)
def fresh_worlds(monkeypatch):
    for var in ("TRAVEL_FAIL_MODE", "BANKING_FAIL_MODE", "RETAIL_FAIL_MODE"):
        monkeypatch.delenv(var, raising=False)
    travel_server._BOOKINGS.clear()
    banking_server._seed()
    retail_server._reset_state()
    yield
    travel_server._BOOKINGS.clear()
    banking_server._seed()
    retail_server._reset_state()


async def run_scenario(tmp_path: Path, intents=None, env: dict | None = None, monkeypatch=None, planner=None):
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    llm = (PlannedLLM(intents or INTENTS, SCENARIO["facts"], planner) if planner is not None
           else IntentLLM(intents or INTENTS, SCENARIO["facts"]))
    async with SpyRegistry(SERVERS) as reg:  # default pattern: every server, generated ones included
        orch = Orchestrator(reg, allow_writes=True, client=llm, handoff_dir=tmp_path, user_id="tester")
        result = await orch.run(SCENARIO["goal"])
        return result, llm, list(reg.calls), sorted(reg._tools)


def handoff(tmp_path: Path) -> dict:
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


async def test_success_path_touches_every_industry_in_the_expected_order(tmp_path, monkeypatch):
    result, llm, calls, _ = await run_scenario(tmp_path)
    assert result.success, result.final_text if hasattr(result, "final_text") else result
    ids = [i["id"] for i in INTENTS]
    assert [i for i, _ in llm.issued] == ids                 # one call per intent, scenario sequence
    assert calls == [t for _, t in llm.issued]               # every issued call reached a server
    first = {}
    for pos, (iid, _) in enumerate(llm.issued):
        first.setdefault(iid, pos)
    for before, after in SCENARIO["order_constraints"]:
        assert first[before] < first[after], (before, after)
    touched = list(dict.fromkeys(industry_of(t) for t in calls))
    assert touched == SCENARIO["expected_industries_in_order"]

    # success checks in the mock worlds, one block per industry the scenario names
    assert {c["industry"] for c in SCENARIO["success_checks"]} == {"travel", "banking", "retail"}
    f = SCENARIO["facts"]
    assert [b for b in travel_server._BOOKINGS.values()
            if b["status"] == "CONFIRMED" and b["passenger_email"] == f["passenger_email"]]
    kyc = next(r for i, r in llm.results.items() if i == "confirm_customer_identity")
    assert kyc["kyc_status"] == "VERIFIED"
    transfers = list(banking_server._TRANSFERS.values())
    assert len(transfers) == 1
    assert llm.results["transfer_money_for_gear"]["status"] == "COMPLETED"
    assert (transfers[0]["from"], transfers[0]["to"], float(transfers[0]["amount"])) == (
        f["from_acct"], f["to_acct"], float(f["amount"]))
    assert len(retail_server._ORDERS) == 1
    order = next(iter(retail_server._ORDERS.values()))
    assert order["status"] == "SHIPPED" and order["qty"] == f["qty"]
    assert len(retail_server._PAYMENTS) == 1 and len(retail_server._SHIPMENTS) == 1
    assert list(tmp_path.glob("*.json")) == []               # no handoff


async def test_each_intent_was_satisfied_by_a_tool_of_the_declared_industry(tmp_path):
    result, llm, _, registered = await run_scenario(tmp_path)
    assert result.success
    for iid, tool in llm.issued:
        assert industry_of(tool) == INDUSTRY[iid], (iid, tool)

    # guard: the scenario text never names a tool or a qualified prefix (intents must not be disguised calls)
    texts = [SCENARIO["goal"]] + [i["intent"] for i in INTENTS]
    for text in texts:
        low = text.lower()
        for name in registered:
            bare = name.partition("__")[2]
            assert name not in low and bare not in low and bare.replace("_", " ") not in low, (name, text)
        assert not re.search(r"\b\w+__\w+", low), text
    stub_fields = set().union(*(i.keys() for i in llm.intents))
    assert stub_fields == {"id", "intent", "uses", "consumes"}   # the stub never got industry labels


async def test_injected_banking_outage_hands_off_with_cross_industry_context(tmp_path, monkeypatch):
    inj = SCENARIO["failure_injection"]
    result, llm, calls, _ = await run_scenario(tmp_path, env=inj["env"], monkeypatch=monkeypatch)
    assert result.success is False
    data = handoff(tmp_path)
    assert data["error_snapshot"]["tool"].startswith(inj["industry"] + "__")

    ids = [i["id"] for i in INTENTS]
    before = {INDUSTRY[i] for i in ids[:ids.index(inj["at_intent"])]}
    assert "travel" in before
    done = [o["tool"] for o in data["current_state"]["completed_operations"]]
    assert done and {industry_of(t) for t in done} == before
    assert len(done) == ids.index(inj["at_intent"])          # every earlier intent succeeded exactly once

    assert data["industries_touched"] == inj["expect"]["industries_touched"]
    assert data["failure_industry"] == inj["expect"]["failure_industry"] == "banking"
    executed = {}
    for _, tool in llm.issued:
        executed.setdefault(industry_of(tool), []).append(tool)
    assert calls == [t for _, t in llm.issued]               # nothing skipped here: all issued calls executed
    assert data["tools_called_by_industry"] == executed
    assert set(data["tools_called_by_industry"]) == {"travel", "banking"}
    assert all(industry_of(t) != "retail" for t in calls)
    assert retail_server._ORDERS == {} and banking_server._TRANSFERS == {}


async def test_failure_in_a_different_industry_also_hands_off_with_the_same_shape(tmp_path, monkeypatch):
    sub_a, sub_b = tmp_path / "a", tmp_path / "b"
    await run_scenario(sub_a, env=SCENARIO["failure_injection"]["env"], monkeypatch=monkeypatch)
    monkeypatch.delenv("BANKING_FAIL_MODE")
    result, llm, calls, _ = await run_scenario(sub_b, env={"TRAVEL_FAIL_MODE": "all_down"}, monkeypatch=monkeypatch)
    assert result.success is False
    a, b = handoff(sub_a), handoff(sub_b)
    assert set(a) == set(b)
    assert b["failure_industry"] == "travel"
    assert b["industries_touched"] == ["travel"]
    assert set(b["tools_called_by_industry"]) == {"travel"}
    assert b["error_snapshot"]["tool"].startswith("travel__")
    assert a["failure_industry"] == "banking" and a["failure_industry"] != b["failure_industry"]
