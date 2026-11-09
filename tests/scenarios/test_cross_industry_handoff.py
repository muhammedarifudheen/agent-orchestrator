"""Gate C: a handoff from a REAL mid-scenario failure goes through learning/collect.py unchanged.

The failure is injected into a real tool call (BANKING_FAIL_MODE inside the real banking tool); nothing about the
handoff is mocked or hand-written. collect.py must ingest it with no parse error and tag it with the FAILING system,
not with the industry that had already completed.
"""
from __future__ import annotations

import json

from learning.collect import collect
from test_cross_industry import SCENARIO, fresh_worlds, handoff, run_scenario  # noqa: F401  (fresh_worlds: autouse fixture)


async def test_banking_failure_after_travel_succeeded_is_ingested_and_tagged_banking(tmp_path, monkeypatch):
    handoffs = tmp_path / "handoffs"
    result, *_ = await run_scenario(handoffs, env=SCENARIO["failure_injection"]["env"], monkeypatch=monkeypatch)
    data = handoff(handoffs)
    assert not result.success
    assert data["industries_touched"] == ["travel", "banking"] and data["failure_industry"] == "banking"

    out = tmp_path / "failures.jsonl"
    records = collect(handoffs, out)  # must not raise: no parse error on the cross-industry handoff

    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]  # valid JSONL
    assert lines == records and len(lines) == 1
    rec = lines[0]
    assert rec["system"] == "banking", "tagged with the FAILING industry, not travel (which had completed)"
    assert rec["operation"] == "verify_kyc" and rec["error_code"] == "CORE_BANKING_UNAVAILABLE"
    assert rec["retryable"] is True and rec["recovery"] == "agent"
    assert rec["recovery_attempted"] is True, "the agent retried verify_kyc once before the handoff"
    assert rec["sources"] == [next(handoffs.glob("*.json")).name]
    facts = SCENARIO["facts"]
    assert str(facts["customer_id"]) not in out.read_text(encoding="utf-8"), "argument values never reach failures.jsonl"


async def test_each_failing_industry_gets_its_own_tag_when_collected_together(tmp_path, monkeypatch):
    handoffs = tmp_path / "handoffs"
    await run_scenario(handoffs, env={"BANKING_FAIL_MODE": "core_down"}, monkeypatch=monkeypatch)
    monkeypatch.delenv("BANKING_FAIL_MODE")
    await run_scenario(handoffs, env={"TRAVEL_FAIL_MODE": "all_down"}, monkeypatch=monkeypatch)
    records = collect(handoffs, tmp_path / "failures.jsonl")
    assert sorted((r["system"], r["error_code"]) for r in records) == [
        ("banking", "CORE_BANKING_UNAVAILABLE"), ("travel", "SERVICE_UNAVAILABLE")]
