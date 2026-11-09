"""Offline tests: scripted stub LLM + the real travel MCP server over stdio."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.orchestrator import Orchestrator  # noqa: E402
from agent.tool_registry import ToolRegistry  # noqa: E402

SERVERS = Path(__file__).parent.parent / "servers"
# These tests drive travel tools only. ToolRegistry(SERVERS) would spawn a real stdio subprocess (~1 s, mostly
# `import mcp`) for EVERY servers/*_server.py, so each test paid for all of them and its cost grew with every new
# system (D17). Scope the registry to the server under test; full discovery is covered once, below.
TRAVEL = "travel_server.py"
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


def use(i: str, name: str, **args):
    return NS(type="tool_use", id=i, name=name, input=args)


def say(text: str):
    return NS(type="text", text=text)


class StubClient:
    """Plays back scripted assistant turns; each item is a list of content blocks."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.messages = self

    async def create(self, **_kw):
        blocks = self.turns.pop(0)
        return NS(content=blocks)


class RegistryDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_registry_discovers_and_connects_every_server_over_stdio(self):
        """The one place that spawns ALL servers as real subprocesses (it costs ~1 s per server, so only once)."""
        expected = {p.name[: -len("_server.py")] for p in SERVERS.glob("*_server.py")}
        async with ToolRegistry(SERVERS) as reg:
            self.assertEqual(reg.connect_errors, {})
            self.assertEqual({t["name"].partition("__")[0] for t in reg.anthropic_tools()}, expected)


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, turns, **kw):
        async with ToolRegistry(SERVERS, pattern=TRAVEL) as reg:
            self.assertIn("travel__search_flights", [t["name"] for t in reg.anthropic_tools()])
            with tempfile.TemporaryDirectory() as d:
                orch = Orchestrator(reg, client=StubClient(turns), handoff_dir=Path(d), **kw)
                result = await orch.run("book a flight")
                return result, list(Path(d).glob("*.json"))

    async def test_happy_path(self):
        turns = [
            [use("1", "travel__search_flights", origin="DEL", destination="BOM", date=TOMORROW)],
            [use("2", "travel__book_flight", flight_id=f"AI-865_DELBOM_{TOMORROW}",
                 passenger_name="A B", passenger_email="a@b.co")],
            [say("Booked.")],
        ]
        result, files = await self._run(turns)
        self.assertTrue(result.success)
        self.assertEqual(result.state.failed, [])
        self.assertEqual(files, [])

    async def test_sold_out_is_recovered_by_choosing_another_flight(self):
        # SOLD_OUT is retryable=false but recovery=agent: the agent gets ONE self-correction
        turns = [
            [use("1", "travel__book_flight", flight_id=f"UK-995_DELBOM_{TOMORROW}",
                 passenger_name="A B", passenger_email="a@b.co")],
            [use("2", "travel__book_flight", flight_id=f"AI-865_DELBOM_{TOMORROW}",
                 passenger_name="A B", passenger_email="a@b.co")],
            [say("Booked the other flight.")],
        ]
        result, files = await self._run(turns)
        self.assertTrue(result.success)
        self.assertEqual([o.error_code for o in result.state.failed], ["SOLD_OUT"])
        self.assertEqual(files, [])

    async def test_second_failure_of_the_same_tool_hands_off(self):
        sold_out = lambda i: [use(i, "travel__book_flight", flight_id=f"UK-995_DELBOM_{TOMORROW}",
                                  passenger_name="A B", passenger_email="a@b.co")]
        result, files = await self._run([sold_out("1"), sold_out("2"), sold_out("3")])
        self.assertFalse(result.success)
        self.assertEqual(len(result.handoff.attempted_operations), 2)
        self.assertEqual(result.handoff.error_snapshot["error_code"], "SOLD_OUT")
        self.assertEqual(result.handoff.error_snapshot["recovery"], "agent")
        self.assertIn("could not self-correct", result.handoff.reason)
        self.assertEqual(len(files), 1)

    async def test_handoff_on_consecutive_retryable_failures(self):
        os.environ["TRAVEL_FAIL_MODE"] = "search_down"  # SERVICE_UNAVAILABLE, retryable=true
        try:
            search = lambda i: [use(i, "travel__search_flights", origin="DEL", destination="BOM",
                                    date=TOMORROW)]
            result, files = await self._run([search("1"), search("2"), search("3")])
        finally:
            del os.environ["TRAVEL_FAIL_MODE"]
        self.assertFalse(result.success)
        h = result.handoff
        self.assertEqual(len(h.attempted_operations), 2)  # stops at the 2nd, never reaches a 3rd
        self.assertEqual(h.error_codes, ["SERVICE_UNAVAILABLE"])
        self.assertIn("2 consecutive", h.reason)
        self.assertEqual(len(files), 1)  # snapshot persisted

    async def test_handoff_preserves_completed_side_effects(self):
        turns = [
            [use("1", "travel__book_flight", flight_id=f"AI-865_DELBOM_{TOMORROW}",
                 passenger_name="A B", passenger_email="a@b.co")],
            [use("2", "travel__send_confirmation", email="not-an-email", booking_ref="X", details="d")],
            [say("I could not send the email.")],
        ]
        result, _ = await self._run(turns)
        self.assertFalse(result.success)
        done = result.handoff.current_state["completed_operations"]
        self.assertEqual(done[0]["tool"], "travel__book_flight")
        self.assertEqual(done[0]["result"]["status"], "CONFIRMED")

    async def test_llm_failure_is_escalated_not_swallowed(self):
        class Boom:
            messages = None

            async def create(self, **_):
                raise ConnectionError("no network")

        Boom.messages = Boom()
        async with ToolRegistry(SERVERS, pattern=TRAVEL) as reg:
            result = await Orchestrator(reg, client=Boom()).run("x")
        self.assertFalse(result.success)
        self.assertIn("ConnectionError", result.handoff.reason)

    async def test_outage_mode_reports_service_unavailable(self):
        os.environ["TRAVEL_FAIL_MODE"] = "all_down"
        try:
            turns = [[use(str(i), "travel__search_flights", origin="DEL", destination="BOM",
                          date=TOMORROW)] for i in range(3)]
            result, _ = await self._run(turns)
        finally:
            del os.environ["TRAVEL_FAIL_MODE"]
        self.assertEqual(result.handoff.error_codes, ["SERVICE_UNAVAILABLE"])


if __name__ == "__main__":
    unittest.main()
