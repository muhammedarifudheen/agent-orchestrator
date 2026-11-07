"""Failure counter + handoff semantics, with a scripted registry (no MCP processes, no LLM)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from agent.orchestrator import Orchestrator  # noqa: E402
from agent.tool_registry import ToolOutcome  # noqa: E402
from servers.errors import ToolFailure, guarded  # noqa: E402

OK = ToolOutcome(True, "t__op", data={"booking_ref": "BK1"})
BAD = ToolOutcome(False, "t__op", error_code="SERVICE_UNAVAILABLE",
                  error_message="down", retryable=True)


class FakeRegistry:
    """Returns scripted outcomes in order, one per call."""

    connect_errors: dict = {}

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def anthropic_tools(self):
        return [{"name": "t__op", "description": "d", "input_schema": {"type": "object"}}]

    async def call(self, name, args):
        return self.outcomes.pop(0)


class FakeLLM:
    """One tool call per turn for `n_calls` turns, then a final text turn."""

    def __init__(self, n_calls):
        self.n, self.i = n_calls, 0
        self.messages = self

    async def create(self, **_):
        self.i += 1
        if self.i <= self.n:
            return NS(content=[NS(type="tool_use", id=f"id{self.i}", name="t__op", input={})])
        return NS(content=[NS(type="text", text="I'm done.")])


async def run(outcomes, **kw):
    orch = Orchestrator(FakeRegistry(outcomes), client=FakeLLM(len(outcomes)), **kw)
    return await orch.run("goal")


class FailureCounterTests(unittest.IsolatedAsyncioTestCase):
    async def test_failures_increment_counter_and_trigger_handoff(self):
        result = await run([OK, BAD, BAD, BAD])
        self.assertEqual(result.state.consecutive_failures, 3)
        self.assertFalse(result.success)
        h = result.handoff
        self.assertIn("3 consecutive", h.reason)
        self.assertEqual(h.error_codes, ["SERVICE_UNAVAILABLE"])
        self.assertEqual(len(h.attempted_operations), 4)
        self.assertIs(h.attempted_operations[-1]["retryable"], True)
        self.assertEqual(len(h.current_state["completed_operations"]), 1)  # the earlier success
        self.assertEqual(h.current_state["consecutive_failures"], 3)

    async def test_success_resets_counter_so_no_handoff(self):
        # fail, fail, OK would have been the 3rd failure without the reset
        result = await run([BAD, BAD, OK, BAD, BAD, OK])
        self.assertTrue(result.success)
        self.assertIsNone(result.handoff)
        self.assertEqual(result.state.consecutive_failures, 0)

    async def test_counter_counts_run_since_last_success(self):
        # fail, fail, OK, fail, fail -> counter is 2 (not 4); model then stops with last call failed
        result = await run([BAD, BAD, OK, BAD, BAD], max_consecutive_failures=5)
        self.assertEqual(result.state.consecutive_failures, 2)
        self.assertFalse(result.success)
        self.assertIn("unrecovered", result.handoff.reason)

    async def test_threshold_is_configurable(self):
        result = await run([BAD, BAD], max_consecutive_failures=2)
        self.assertFalse(result.success)
        self.assertIn("2 consecutive", result.handoff.reason)


class ErrorContractTests(unittest.TestCase):
    def test_tool_failure_text_format(self):
        e = ToolFailure("SOLD_OUT", "no seats", retryable=False)
        self.assertEqual(str(e), "[SOLD_OUT] retryable=false no seats")
        self.assertTrue(ToolFailure("X", "m", True).retryable)

    def test_guarded_converts_raw_exceptions(self):
        @guarded
        def boom(x: int) -> int:
            return 1 // x

        with self.assertRaises(ToolFailure) as cm:
            boom(0)
        self.assertEqual(cm.exception.code, "INTERNAL_ERROR")
        self.assertFalse(cm.exception.retryable)

    def test_guarded_timeout_is_retryable_and_toolfailure_passes_through(self):
        @guarded
        def slow() -> None:
            raise TimeoutError("upstream")

        @guarded
        def known() -> None:
            raise ToolFailure("MINE", "m", retryable=False)

        with self.assertRaises(ToolFailure) as cm:
            slow()
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("TIMEOUT", True))
        with self.assertRaises(ToolFailure) as cm:
            known()
        self.assertEqual(cm.exception.code, "MINE")


if __name__ == "__main__":
    unittest.main()
