"""The recovery-aware policy, end to end: real Orchestrator + real ToolRegistry + real MCP servers
(in-memory), with a scripted model. Each test names the behaviour it proves."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import retail_server  # noqa: F401  (conftest resets its state)
from agent.orchestrator import Orchestrator
from inmem import SpyRegistry

SERVERS = Path(__file__).parent.parent / "servers"
TOMORROW = (date.today() + timedelta(days=1)).isoformat()
FILENAME = re.compile(r"^\d{8}_\d{6}_tester\.json$")


def use(i, name, **args):
    return NS(type="tool_use", id=i, name=name, input=args)


def say(text):
    return NS(type="text", text=text)


class Scripted:
    """Plays turns from a function(turn_number, last_tool_result_text) -> content blocks."""

    def __init__(self, fn):
        self.fn, self.turns, self.seen = fn, 0, []
        self.messages = self

    async def create(self, *, messages, **_):
        self.turns += 1
        last = messages[-1]["content"]
        text = last[0]["content"] if isinstance(last, list) else ""
        self.seen.append(text)
        return NS(content=self.fn(self.turns, text))


async def run(tmp_path, pattern, llm, goal="goal", **kw):
    async with SpyRegistry(SERVERS, pattern=pattern) as reg:
        result = await Orchestrator(reg, allow_writes=True, client=llm, handoff_dir=tmp_path, user_id="tester", **kw).run(goal)
        return result, reg.calls


async def test_agent_self_correct(tmp_path):
    """travel INVALID_DATE (recovery=agent) once, then a corrected call succeeds: run completes, no handoff."""

    def script(turn, last):
        if turn == 1:
            return [say("Searching for tomorrow."),
                    use("a", "travel__search_flights", origin="DEL", destination="BOM", date="tomorrow")]
        if turn == 2:  # the model must have been re-prompted with the error and parameter hints
            assert "[INVALID_DATE] retryable=false recovery=agent" in last
            assert "Self-correction (ONE attempt" in last
            assert "Valid parameters for travel__search_flights" in last
            assert "YYYY-MM-DD" in last  # taken from the tool's own Args documentation
            return [use("b", "travel__search_flights", origin="DEL", destination="BOM", date=TOMORROW)]
        return [say("Found flights.")]

    llm = Scripted(script)
    result, calls = await run(tmp_path, "travel_server.py", llm)

    assert result.success and result.handoff is None
    assert calls == ["travel__search_flights", "travel__search_flights"]
    assert [o.error_code for o in result.state.failed] == ["INVALID_DATE"]
    assert result.state.agent_failures == {}  # the success cleared the tool's budget
    assert result.state.consecutive_failures == 0
    assert list(tmp_path.glob("*.json")) == []  # no handoff file


async def test_agent_self_correct_only_once(tmp_path):
    """The same tool failing a second time (still recovery=agent) hands off; no second re-prompt."""

    def script(turn, last):
        return [use(f"t{turn}", "travel__search_flights", origin="DEL", destination="BOM", date="tomorrow")]

    llm = Scripted(script)
    result, calls = await run(tmp_path, "travel_server.py", llm)

    assert not result.success and len(calls) == 2 and llm.turns == 2
    assert "could not self-correct travel__search_flights" in result.handoff.reason
    assert sum("Self-correction" in text for text in llm.seen) == 1  # exactly one re-prompt was ever issued
    assert len(list(tmp_path.glob("*.json"))) == 1


async def test_human_handoff_fast(tmp_path, card_declined):
    """retail CARD_DECLINED (recovery=human): handoff on the FIRST failure, JSON written, nothing runs after."""
    order = {}

    def script(turn, last):
        if turn == 1:
            return [use("a", "retail__check_inventory", sku="SKU-1001", qty=2)]
        if turn == 2:
            return [use("b", "retail__create_order", sku="SKU-1001", qty=2, customer_id="C-1001")]
        if turn == 3:
            order.update(json.loads(last))
            return [use("c", "retail__charge_payment", order_id=order["order_id"], amount=order["total_amount"])]
        raise AssertionError("the orchestrator called the model again after a recovery=human failure")

    llm = Scripted(script)
    result, calls = await run(tmp_path, "retail_server.py", llm)

    assert calls == ["retail__check_inventory", "retail__create_order", "retail__charge_payment"]
    assert llm.turns == 3 and not result.success
    assert result.state.consecutive_failures == 1  # one failure was enough
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1 and FILENAME.match(files[0].name)
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert "CARD_DECLINED" in files[0].read_text(encoding="utf-8")
    assert data["error_snapshot"]["recovery"] == "human" and data["error_snapshot"]["retryable"] is False
    assert [o["tool"] for o in data["current_state"]["completed_operations"]] == [
        "retail__check_inventory", "retail__create_order"]  # the unpaid order a human must handle


async def test_consecutive_two(tmp_path, monkeypatch):
    """Two retryable=true failures in a row (different tools) hand off, though each tool failed only once."""
    monkeypatch.setenv("TRAVEL_FAIL_MODE", "all_down")  # every travel tool: SERVICE_UNAVAILABLE, retryable=true

    def script(turn, last):
        if turn == 1:
            return [use("a", "travel__search_flights", origin="DEL", destination="BOM", date=TOMORROW)]
        if turn == 2:
            return [use("b", "travel__book_flight", flight_id=f"AI-865_DELBOM_{TOMORROW}",
                        passenger_name="A B", passenger_email="a@b.co")]
        raise AssertionError("called the model after the 2nd consecutive retryable failure")

    llm = Scripted(script)
    result, calls = await run(tmp_path, "travel_server.py", llm)

    assert calls == ["travel__search_flights", "travel__book_flight"]
    assert result.state.consecutive_failures == 2 and result.state.consecutive_retryable == 2
    assert "2 consecutive retryable failures" in result.handoff.reason
    data = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert data["error_snapshot"]["error_code"] == "SERVICE_UNAVAILABLE"
    assert (data["error_snapshot"]["retryable"], data["error_snapshot"]["recovery"]) == (True, "agent")
    assert data["error_snapshot"]["consecutive_retryable"] == 2
