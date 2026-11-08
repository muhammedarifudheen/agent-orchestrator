"""End-to-end handoff: real orchestrator + real registry + real retail MCP server (in-memory),
scripted LLM, one tool monkeypatched to fail. Asserts the JSON handoff file on disk."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import retail_server
from agent.orchestrator import Orchestrator
from inmem import SpyRegistry

SERVERS = Path(__file__).parent.parent / "servers"
GOAL = "Buy 2 units of SKU-1001 for customer C-1001 and ship them to 12 MG Road, Pune 411001."
FILENAME = re.compile(r"^\d{8}_\d{6}_tester\.json$")


class ScriptedLLM:
    """check_inventory -> create_order -> charge_payment [-> create_shipment]. Counts LLM turns."""

    def __init__(self, parallel_tail: bool = False, retry_payment_once: bool = False) -> None:
        self.messages = self
        self.turns = 0
        self.parallel_tail = parallel_tail
        self.retry_payment_once = retry_payment_once
        self.order: dict | None = None

    @staticmethod
    def use(i, name, **args):
        return NS(type="tool_use", id=i, name=name, input=args)

    async def create(self, *, messages, **_):
        self.turns += 1
        last = messages[-1]["content"]
        text = last[0]["content"] if isinstance(last, list) else ""
        pay = lambda: self.use(f"p{self.turns}", "retail__charge_payment",
                               order_id=self.order["order_id"], amount=self.order["total_amount"])
        ship = lambda: self.use(f"s{self.turns}", "retail__create_shipment",
                                order_id=self.order["order_id"], address="12 MG Road, Pune 411001")
        if self.turns == 1:
            return NS(content=[NS(type="text", text="Checking stock first."),
                               self.use("a", "retail__check_inventory", sku="SKU-1001", qty=2)])
        if self.turns == 2:
            return NS(content=[NS(type="text", text="In stock, creating the order."),
                               self.use("b", "retail__create_order", sku="SKU-1001", qty=2,
                                        customer_id="C-1001")])
        if self.turns == 3:
            self.order = json.loads(text)
            blocks = [NS(type="text", text="Order created, charging payment."), pay()]
            return NS(content=blocks + ([ship()] if self.parallel_tail else []))
        if self.retry_payment_once and self.turns == 4:  # payment timed out: retry once
            return NS(content=[NS(type="text", text="Gateway timed out, retrying payment."), pay()])
        if self.retry_payment_once and self.turns == 5:
            return NS(content=[ship()])
        if self.retry_payment_once and self.turns == 6:
            return NS(content=[NS(type="text", text="Paid on retry and shipped.")])
        # Only reachable if the orchestrator wrongly keeps going after a failure.
        return NS(content=[NS(type="text", text="Continuing anyway."), ship()])


async def run(tmp_path: Path, llm: ScriptedLLM, **kw):
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        orch = Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", **kw)
        result = await orch.run(GOAL)
        return result, reg.calls


def only_handoff_file(tmp_path: Path) -> Path:
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1, files
    return files[0]


async def test_non_retryable_failure_writes_handoff_json_and_stops(tmp_path, card_declined):
    llm = ScriptedLLM()
    result, calls = await run(tmp_path, llm)

    # run stopped: no tool call and no LLM turn after the failing charge_payment
    assert calls == ["retail__check_inventory", "retail__create_order", "retail__charge_payment"]
    assert llm.turns == 3
    assert result.success is False
    assert result.state.consecutive_failures == 1  # counter incremented by the failure

    path = only_handoff_file(tmp_path)
    assert FILENAME.match(path.name), path.name
    raw = path.read_text(encoding="utf-8")
    assert "CARD_DECLINED" in raw
    data = json.loads(raw)

    assert data["goal"] == GOAL and data["user"] == "tester"
    assert "Human required" in data["reason"] and "recovery=human" in data["reason"]
    assert data["error_snapshot"] == {
        "step": 3, "tool": "retail__charge_payment",
        "arguments": data["error_snapshot"]["arguments"], "error_code": "CARD_DECLINED",
        "error_message": "Issuer declined the card.", "retryable": False, "recovery": "human",
        "http_status": None, "consecutive_failures": 1, "consecutive_retryable": 0}
    assert data["error_snapshot"]["arguments"]["order_id"].startswith("ORD-")

    # decision log: each step has thought, tool, args, result; completed steps precede the failure
    steps = data["steps"]
    assert [s["tool"] for s in steps] == ["retail__check_inventory", "retail__create_order",
                                          "retail__charge_payment"]
    assert all({"thought", "tool", "arguments", "result"} <= s.keys() for s in steps)
    assert [s["ok"] for s in steps] == [True, True, False]
    assert steps[0]["thought"] == "Checking stock first."
    assert steps[1]["result"]["status"] == "PENDING_PAYMENT"
    assert steps[2]["result"]["error_code"] == "CARD_DECLINED"

    # current state: the order that already exists must be visible to the human
    state = data["current_state"]
    assert state["consecutive_failures"] == 1
    assert [o["tool"] for o in state["completed_operations"]] == ["retail__check_inventory",
                                                                  "retail__create_order"]
    assert state["skipped_operations"] == []
    assert data["decision_log"], "human-readable log must be included"


async def test_calls_later_in_the_same_turn_are_skipped_not_executed(tmp_path, card_declined):
    llm = ScriptedLLM(parallel_tail=True)  # turn 3 asks for charge_payment AND create_shipment
    result, calls = await run(tmp_path, llm)

    assert "retail__create_shipment" not in calls
    assert llm.turns == 3
    data = json.loads(only_handoff_file(tmp_path).read_text(encoding="utf-8"))
    assert [s["tool"] for s in data["current_state"]["skipped_operations"]] == ["retail__create_shipment"]
    assert retail_server._SHIPMENTS == {}  # nothing shipped for an unpaid order


async def test_retryable_failure_recovers_and_resets_counter_without_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("RETAIL_FAIL_MODE", "payment_down")
    llm = ScriptedLLM(retry_payment_once=True)
    real_call = SpyRegistry.call

    async def heal_after_first_payment(self, name, args):
        out = await real_call(self, name, args)
        if name == "retail__charge_payment" and not out.ok:
            monkeypatch.delenv("RETAIL_FAIL_MODE")  # outage ends
        return out

    monkeypatch.setattr(SpyRegistry, "call", heal_after_first_payment)
    result, calls = await run(tmp_path, llm)

    assert calls.count("retail__charge_payment") == 2
    assert calls[-1] == "retail__create_shipment"
    assert result.success is True and result.handoff is None
    assert result.state.consecutive_failures == 0  # reset by the successful retry
    assert list(tmp_path.glob("*.json")) == []


async def test_two_consecutive_retryable_failures_trigger_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("RETAIL_FAIL_MODE", "payment_down")  # never heals
    llm = ScriptedLLM(retry_payment_once=True)
    result, calls = await run(tmp_path, llm)

    assert calls.count("retail__charge_payment") == 2 and "retail__create_shipment" not in calls
    assert result.state.consecutive_failures == 2
    data = json.loads(only_handoff_file(tmp_path).read_text(encoding="utf-8"))
    assert "2 consecutive" in data["reason"]
    assert data["error_snapshot"]["error_code"] == "PAYMENT_GATEWAY_TIMEOUT"
    assert data["error_snapshot"]["retryable"] is True
