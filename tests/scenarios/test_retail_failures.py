"""Retail failure variants of the cross-industry scenario, both REAL faults inside the real charge tool:

* RETAIL_FAIL_MODE=payment_down  -> retryable, recovery=agent: the agent gets one retry, then the handoff.
* a declined card (CARD_DECLINED, recovery=human, http_status=402) -> immediate handoff, ZERO retries.
The pair proves the recovery="human" branch is real: the same step fails in the same scenario and the number of
attempts differs only because of the recovery field.
"""
from __future__ import annotations

import pytest

import banking_server
import retail_server
import travel_server
from errors import ToolFailure
from inject import failing_tool
from learning.collect import collect
from test_cross_industry import fresh_worlds, handoff, industry_of, run_scenario  # noqa: F401  (autouse fixture)

PAY = "retail__charge_payment"
BEFORE_PAYMENT = ["retail__check_inventory", "retail__create_order"]


async def test_payment_gateway_down_retries_once_then_hands_off_as_retail(tmp_path, monkeypatch):
    result, llm, calls, _ = await run_scenario(tmp_path, env={"RETAIL_FAIL_MODE": "payment_down"}, monkeypatch=monkeypatch)
    assert result.success is False
    data = handoff(tmp_path)

    assert data["failure_industry"] == "retail"
    assert data["industries_touched"] == ["travel", "banking", "retail"]
    retail_calls = data["tools_called_by_industry"]["retail"]
    assert retail_calls[:2] == BEFORE_PAYMENT, "the retail steps that completed before payment"
    assert retail_calls.count(PAY) == 2, "recovery=agent: the failing call was retried once"
    done = [o["tool"] for o in data["current_state"]["completed_operations"]]
    assert BEFORE_PAYMENT[0] in done and BEFORE_PAYMENT[1] in done and PAY not in done
    snap = data["error_snapshot"]
    assert (snap["tool"], snap["error_code"], snap["retryable"], snap["recovery"]) == (
        PAY, "PAYMENT_GATEWAY_TIMEOUT", True, "agent")
    assert "consecutive retryable failures" in data["reason"]
    # recovery=agent went through the retry path, so the recovery=human flags must be ABSENT (not false, not null)
    assert "human_required" not in data and "agent_recovery_skipped" not in data
    assert "retail__create_shipment" not in calls
    assert len(retail_server._ORDERS) == 1 and next(iter(retail_server._ORDERS.values()))["status"] != "SHIPPED"


async def test_declined_card_hands_off_immediately_with_zero_retries(tmp_path, monkeypatch):
    declined = ToolFailure("CARD_DECLINED", "Issuer declined the card.", retryable=False, recovery="human", http_status=402)
    async with failing_tool(retail_server, "charge_payment", declined):
        result, llm, calls, _ = await run_scenario(tmp_path, monkeypatch=monkeypatch)
    assert result.success is False
    data = handoff(tmp_path)

    snap = data["error_snapshot"]
    assert (snap["tool"], snap["error_code"], snap["retryable"], snap["recovery"], snap["http_status"]) == (
        PAY, "CARD_DECLINED", False, "human", 402)
    assert "Human required" in data["reason"] and "recovery=human" in data["reason"]
    # observable side effects that ONLY the recovery=human branch produces (D18): not just a string in the reason
    assert data["human_required"] is True
    assert data["agent_recovery_skipped"] is True

    # zero retry attempts, shown four independent ways
    assert calls.count(PAY) == 1, "the server saw the charge exactly once"
    assert data["tools_called_by_industry"]["retail"].count(PAY) == 1
    assert [s["tool"] for s in data["steps"]].count(PAY) == 1
    assert data["current_state"]["agent_failures"] == {}, "no agent self-correction was granted"
    assert result.state.consecutive_failures == 1
    assert calls[-1] == PAY, "nothing at all ran after the decline (no shipment, no second LLM-driven attempt)"

    assert data["failure_industry"] == "retail" and data["industries_touched"] == ["travel", "banking", "retail"]
    assert data["tools_called_by_industry"]["retail"][:2] == BEFORE_PAYMENT
    # money already moved at the bank and the order exists: the human must see that in the handoff
    done = [o["tool"] for o in data["current_state"]["completed_operations"]]
    assert "banking__transfer_funds" in done and "retail__create_order" in done
    assert len(banking_server._TRANSFERS) == 1 and travel_server._BOOKINGS


async def test_the_declined_card_handoff_reaches_the_learning_loop_with_its_status(tmp_path, monkeypatch):
    declined = ToolFailure("CARD_DECLINED", "Issuer declined the card.", retryable=False, recovery="human", http_status=402)
    async with failing_tool(retail_server, "charge_payment", declined):
        await run_scenario(tmp_path / "h", monkeypatch=monkeypatch)
    [rec] = collect(tmp_path / "h", tmp_path / "f.jsonl")
    assert (rec["system"], rec["operation"], rec["error_code"], rec["status"], rec["outcome"]) == (
        "retail", "charge_payment", "CARD_DECLINED", 402, "handed_off_human")
    assert rec["recovery_attempted"] is False and "deprecated_parse" not in rec


async def test_injecting_a_failure_leaves_the_tool_exactly_as_it_was(tmp_path):
    """The helper must not leak: description and safety annotations are restored (test_contract depends on them)."""
    from mcp import Client

    async def snapshot():
        async with Client(retail_server.mcp) as c:
            return {t.name: (t.description, t.annotations) for t in (await c.list_tools()).tools}

    before = await snapshot()
    async with failing_tool(retail_server, "charge_payment", ToolFailure("X", "m", False, "human")):
        during = await snapshot()
        assert during["charge_payment"] == before["charge_payment"], "same description and annotations while injected"
    assert await snapshot() == before
