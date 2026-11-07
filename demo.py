"""Demo: user asks in natural language; the orchestrator plans, calls MCP tools, prints the log.

    python demo.py                                    # happy path
    set TRAVEL_FAIL_MODE=all_down & python demo.py    # shows the human-handoff snapshot
    python demo.py --offline                          # no API key: scripted stand-in for Claude
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from agent.orchestrator import Orchestrator
from agent.tool_registry import ToolRegistry

ROOT = Path(__file__).parent
GOAL = "Book a flight from Delhi to Mumbai tomorrow for me. My name is Aarav Sharma, email aarav@example.com."


class ScriptedLLM:
    """Offline stand-in for Claude (NOT a test of model reasoning): search -> book first seat -> confirm.

    Exercises the real MCP servers, registry, loop and failure routing without an API key.
    """

    def __init__(self) -> None:
        self.messages = self
        self.step = 0
        self.flight: dict | None = None
        self.booking: dict | None = None

    @staticmethod
    def _tool(i: str, name: str, **args):
        return SimpleNamespace(type="tool_use", id=i, name=name, input=args)

    @staticmethod
    def _last_result(messages: list) -> str:
        content = messages[-1]["content"]
        return content[0]["content"] if messages[-1]["role"] == "user" and isinstance(content, list) else ""

    @staticmethod
    def _is_error(text: str) -> bool:
        return re.match(r"\[[A-Z][A-Z0-9_]*\] retryable=", text) is not None  # "[CODE] retryable=..."

    async def create(self, *, messages: list, **_kw):
        self.step += 1
        last = self._last_result(messages)
        if self.step == 1:
            day = (date.today() + timedelta(days=1)).isoformat()
            return SimpleNamespace(content=[
                SimpleNamespace(type="text", text="I'll search DEL->BOM for tomorrow."),
                self._tool("a", "travel__search_flights", origin="DEL", destination="BOM", date=day)])
        if self.step == 2:
            if self._is_error(last):  # tool error: nothing to book with, keep trying
                return SimpleNamespace(content=[self._tool("b", "travel__search_flights",
                                                           origin="DEL", destination="BOM",
                                                           date=date.today().isoformat())])
            flights = [f for f in json.loads(last) if f["seats_available"] > 0]
            self.flight = min(flights, key=lambda f: f["price_inr"])
            return SimpleNamespace(content=[
                SimpleNamespace(type="text", text=f"Cheapest bookable: {self.flight['flight_number']}."),
                self._tool("c", "travel__book_flight", flight_id=self.flight["flight_id"],
                           passenger_name="Aarav Sharma", passenger_email="aarav@example.com")])
        if self.step == 3 and not self._is_error(last):
            self.booking = json.loads(last)
            return SimpleNamespace(content=[self._tool(
                "d", "travel__send_confirmation", email="aarav@example.com",
                booking_ref=self.booking["booking_ref"], details=f"Flight {self.flight['flight_number']}")])
        if self._is_error(last):  # keep retrying the same failing step until the loop escalates
            return SimpleNamespace(content=[self._tool("r", "travel__search_flights", origin="DEL",
                                                       destination="BOM", date=date.today().isoformat())])
        ref = self.booking["booking_ref"] if self.booking else "?"
        return SimpleNamespace(content=[SimpleNamespace(
            type="text", text=f"Booked {self.flight['flight_number']} (ref {ref}); confirmation sent.")])


async def main() -> int:
    offline = "--offline" in sys.argv
    if not offline and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY first (or use --offline).", file=sys.stderr)
        return 2
    if offline:
        print("[OFFLINE MODE] Claude is replaced by a scripted stand-in; MCP servers and loop are real.")
    print(f"User: {GOAL}\n\n--- Decision log ---")
    async with ToolRegistry(ROOT / "servers") as registry:
        for name, err in registry.connect_errors.items():
            print(f"WARNING: server '{name}' failed to start: {err}")
        orch = Orchestrator(registry, client=ScriptedLLM() if offline else None,
                            handoff_dir=ROOT / "handoffs", on_log=print)
        result = await orch.run(GOAL)
    print("\n--- Outcome ---")
    if result.success:
        print(f"SUCCESS after {result.state.steps} LLM steps, {len(result.state.operations)} tool calls.")
        print(result.final_message)
        return 0
    print(result.handoff.to_text())
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
