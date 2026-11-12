"""Recovery-aware failure routing, with a scripted registry (no MCP processes, no LLM).

Policy under test (agent/orchestrator.py):
  1. recovery=human (or an error that states no recovery): hand off at once.
  2. consecutive retryable=true failures >= max_consecutive_failures (2): hand off.
  3. recovery=agent: ONE self-correction per tool; the 2nd failure of the same tool hands off.
  Any success resets the consecutive counters and that tool's self-correction budget.
"""
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

DESC = "Do a thing.\n\nArgs:\n    date: Travel date as YYYY-MM-DD.\n    qty: How many (integer).\n\nReturns:\n    x\n"


def ok(tool="t__a"):
    return ToolOutcome(True, tool, data={"done": True})


def transient(tool="t__a"):  # retryable=true, recovery=agent
    return ToolOutcome(False, tool, error_code="SERVICE_UNAVAILABLE", error_message="down",
                       retryable=True, recovery="agent")


def bad_args(tool="t__a"):  # retryable=false, recovery=agent
    return ToolOutcome(False, tool, error_code="INVALID_DATE", error_message="not YYYY-MM-DD",
                       retryable=False, recovery="agent")


def declined(tool="t__a"):  # retryable=false, recovery=human
    return ToolOutcome(False, tool, error_code="CARD_DECLINED", error_message="declined",
                       retryable=False, recovery="human")


def no_contract(tool="t__a"):  # error text did not follow the contract
    return ToolOutcome(False, tool, error_code="TOOL_ERROR", error_message="?", retryable=None, recovery=None)


class FakeRegistry:
    """Returns scripted outcomes in order, one per call."""

    connect_errors: dict = {}

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.tools = sorted({o.tool for o in outcomes})

    def anthropic_tools(self):
        return [{"name": t, "description": DESC,
                 "input_schema": {"type": "object", "required": ["date"],
                                  "properties": {"date": {"type": "string"}, "qty": {"type": "integer"}}}}
                for t in self.tools]

    def is_read_only(self, name):  # these scripted tools stand for reads: this file tests failure routing, not writes
        return True

    def tool_meta(self, name):
        return {"writes": False}

    async def call(self, name, args):
        assert self.outcomes, "orchestrator kept calling tools after it should have stopped"
        return self.outcomes.pop(0)


class FakeLLM:
    """Calls the tool of each scripted outcome in turn, then ends the run. Records what it was shown."""

    def __init__(self, outcomes):
        self.tools = [o.tool for o in outcomes]
        self.turns = 0
        self.seen: list[str] = []  # tool_result text shown to the model before each turn
        self.messages = self

    async def create(self, *, messages, **_):
        last = messages[-1]["content"]
        if isinstance(last, list):
            self.seen.append(last[0]["content"])
        self.turns += 1
        if self.turns <= len(self.tools):
            return NS(content=[NS(type="tool_use", id=f"id{self.turns}", name=self.tools[self.turns - 1], input={})])
        return NS(content=[NS(type="text", text="I'm done.")])


async def run(outcomes, **kw):
    llm = FakeLLM(outcomes)
    result = await Orchestrator(FakeRegistry(outcomes), allow_writes=True, client=llm, **kw).run("goal")
    return result, llm


class HumanFlagsTests(unittest.IsolatedAsyncioTestCase):
    """human_required / agent_recovery_skipped are written ONLY when the recovery=human branch fired (D18)."""

    async def test_recovery_human_sets_both_flags(self):
        result, _ = await run([declined()])
        self.assertIs(result.handoff.human_required, True)
        self.assertIs(result.handoff.agent_recovery_skipped, True)
        self.assertIs(result.state.human_required, True)

    async def test_the_fail_safe_for_an_unknown_recovery_stops_at_once_but_sets_neither_flag(self):
        result, _ = await run([no_contract()])
        self.assertIn("failing safe", result.handoff.reason)
        self.assertIsNone(result.handoff.human_required)
        self.assertIsNone(result.handoff.agent_recovery_skipped)

    async def test_agent_recovery_paths_set_neither_flag(self):
        for outcomes in ([transient("t__a"), transient("t__b")], [bad_args("t__a"), bad_args("t__a")]):
            result, _ = await run(outcomes)
            self.assertIsNotNone(result.handoff)
            self.assertIsNone(result.handoff.human_required)
            self.assertIsNone(result.handoff.agent_recovery_skipped)

    async def test_on_disk_json_omits_the_flags_unless_true(self):
        import json
        import tempfile
        for outcomes, present in (([declined()], True), ([transient("t__a"), transient("t__b")], False)):
            with tempfile.TemporaryDirectory() as d:
                await Orchestrator(FakeRegistry(outcomes), allow_writes=True, client=FakeLLM(outcomes), handoff_dir=Path(d)).run("goal")
                data = json.loads(next(Path(d).glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(("human_required" in data, "agent_recovery_skipped" in data), (present, present))
            if present:
                self.assertEqual((data["human_required"], data["agent_recovery_skipped"]), (True, True))


class RecoveryPolicyTests(unittest.IsolatedAsyncioTestCase):
    # -- rule 2: consecutive retryable failures ---------------------------------------------
    async def test_two_consecutive_retryable_failures_trigger_handoff(self):
        # different tools, so rule 3 (per-tool budget) cannot be what fires
        result, llm = await run([ok(), transient("t__a"), transient("t__b"), ok()])
        self.assertFalse(result.success)
        self.assertEqual(result.state.consecutive_failures, 2)
        self.assertEqual(result.state.consecutive_retryable, 2)
        h = result.handoff
        self.assertIn("2 consecutive retryable", h.reason)
        self.assertEqual(h.error_codes, ["SERVICE_UNAVAILABLE"])
        self.assertEqual(len(h.attempted_operations), 3)  # stopped right after the 2nd failure
        self.assertEqual(llm.turns, 3)
        self.assertIs(h.attempted_operations[-1]["retryable"], True)
        self.assertEqual(len(h.current_state["completed_operations"]), 1)  # the earlier success survives

    async def test_threshold_is_configurable(self):
        result, _ = await run([transient("t__a"), transient("t__b"), transient("t__c")],
                              max_consecutive_failures=3)
        self.assertIn("3 consecutive retryable", result.handoff.reason)
        self.assertEqual(len(result.handoff.attempted_operations), 3)

    async def test_single_retryable_failure_does_not_hand_off(self):
        result, _ = await run([transient(), ok()])
        self.assertTrue(result.success)
        self.assertEqual(result.state.consecutive_failures, 0)

    async def test_success_resets_counters_and_self_correction_budget(self):
        # same tool: fail, OK, fail, OK. Without the reset the 2nd failure would hand off.
        result, _ = await run([transient(), ok(), transient(), ok()])
        self.assertTrue(result.success)
        self.assertIsNone(result.handoff)
        self.assertEqual((result.state.consecutive_failures, result.state.consecutive_retryable), (0, 0))
        self.assertEqual(result.state.agent_failures, {})

    async def test_non_retryable_failure_breaks_the_retryable_streak(self):
        # transient(a), bad_args(b), transient(c): never 2 retryable in a row; each tool failed once
        result, _ = await run([transient("t__a"), bad_args("t__b"), transient("t__c"), ok()])
        self.assertTrue(result.success)

    # -- rule 1: recovery=human -------------------------------------------------------------
    async def test_human_recovery_hands_off_immediately(self):
        result, llm = await run([ok(), declined(), ok()])
        self.assertFalse(result.success)
        self.assertEqual(result.state.consecutive_failures, 1)  # one failure is enough
        self.assertEqual(llm.turns, 2)
        h = result.handoff
        self.assertIn("Human required", h.reason)
        self.assertEqual(h.error_snapshot["error_code"], "CARD_DECLINED")
        self.assertEqual((h.error_snapshot["retryable"], h.error_snapshot["recovery"]), (False, "human"))
        self.assertEqual(len(h.attempted_operations), 2)

    async def test_error_without_a_recovery_path_fails_safe_to_human(self):
        result, _ = await run([no_contract(), ok()])
        self.assertFalse(result.success)
        self.assertIn("failing safe", result.handoff.reason)

    # -- rule 3: recovery=agent, one self-correction per tool -------------------------------
    async def test_agent_failure_gets_one_reprompt_with_parameter_hints(self):
        result, llm = await run([bad_args(), ok()])
        self.assertTrue(result.success)
        self.assertIsNone(result.handoff)
        shown = llm.seen[0]  # what the model saw after the failed call
        self.assertIn("[INVALID_DATE] retryable=false recovery=agent", shown)
        self.assertIn("Self-correction (ONE attempt", shown)
        self.assertIn("Valid parameters for t__a", shown)
        self.assertIn("date (string, required)", shown)
        self.assertIn("date: Travel date as YYYY-MM-DD.", shown)  # from the tool's own Args block
        self.assertEqual(result.state.agent_failures, {})  # cleared by the later success

    async def test_transient_failure_hint_says_retry_once(self):
        _, llm = await run([transient(), ok()])
        self.assertIn("you may repeat the same call once", llm.seen[0])

    async def test_second_agent_failure_of_the_same_tool_hands_off(self):
        result, llm = await run([bad_args(), bad_args(), ok()])
        self.assertFalse(result.success)
        self.assertIn("could not self-correct t__a", result.handoff.reason)
        self.assertEqual(result.state.consecutive_failures, 2)
        self.assertEqual(llm.turns, 2)
        self.assertNotIn("Self-correction", "".join(llm.seen[1:]))  # no 2nd re-prompt was issued

    async def test_each_tool_gets_its_own_single_self_correction(self):
        # a fails once, b fails once (both fixable, non-retryable): 2 failures in a row, no handoff
        result, _ = await run([bad_args("t__a"), bad_args("t__b"), ok("t__a"), ok("t__b")])
        self.assertTrue(result.success)

    async def test_stopping_after_an_unrecovered_failure_is_a_handoff(self):
        # model gives up right after a failure that it was allowed to fix: still escalated
        result, _ = await run([ok(), transient()], max_consecutive_failures=5)
        self.assertFalse(result.success)
        self.assertIn("unrecovered", result.handoff.reason)
        self.assertEqual(result.state.consecutive_failures, 1)


class ErrorContractTests(unittest.TestCase):
    def test_tool_failure_text_format(self):
        e = ToolFailure("SOLD_OUT", "no seats", retryable=False, recovery="agent")
        self.assertEqual(str(e), "[SOLD_OUT] retryable=false recovery=agent no seats")
        self.assertTrue(ToolFailure("X", "m", True, "human").retryable)

    def test_recovery_is_validated(self):
        with self.assertRaises(ValueError):
            ToolFailure("X", "m", retryable=False, recovery="robot")

    def test_guarded_converts_raw_exceptions_to_human_internal_error(self):
        @guarded
        def boom(x: int) -> int:
            return 1 // x

        with self.assertRaises(ToolFailure) as cm:
            boom(0)
        self.assertEqual((cm.exception.code, cm.exception.retryable, cm.exception.recovery),
                         ("INTERNAL_ERROR", False, "human"))

    def test_guarded_timeout_policy(self):
        @guarded
        def slow() -> None:
            raise TimeoutError("upstream")

        @guarded(idempotent=False)
        def charge() -> None:
            raise TimeoutError("upstream")

        @guarded
        def known() -> None:
            raise ToolFailure("MINE", "m", retryable=False, recovery="human")

        with self.assertRaises(ToolFailure) as cm:
            slow()
        self.assertEqual((cm.exception.code, cm.exception.retryable, cm.exception.recovery), ("TIMEOUT", True, "agent"))
        with self.assertRaises(ToolFailure) as cm:
            charge()
        self.assertEqual((cm.exception.code, cm.exception.retryable, cm.exception.recovery),
                         ("OUTCOME_UNKNOWN", False, "human"))
        with self.assertRaises(ToolFailure) as cm:
            known()
        self.assertEqual(cm.exception.code, "MINE")


if __name__ == "__main__":
    unittest.main()
