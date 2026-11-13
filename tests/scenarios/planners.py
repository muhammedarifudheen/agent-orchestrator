"""IntentPlanner: who decides WHICH tool satisfies each intent, separated from who executes the plan.

    plan(intents, tools) -> list[PlannedCall]      # the order of the list is the proposed order of execution

Deviation from the brief's `plan(intents)`: a planner cannot choose a tool without being shown the registry, so the
offered tool definitions are an argument. Arguments of the calls are NOT part of the plan: they depend on earlier
results (a flight id exists only after the search), so the executor (`intent_llm.PlannedLLM`) resolves them at run time.

Two implementations:
  * KeywordPlanner  - deterministic keyword matcher (see intent_llm.choose_tool). Default. No key. Not a model.
  * ClaudePlanner   - asks a real Claude model (plain-HTTP client, no SDK). Opt-in: ANTHROPIC_API_KEY and LIVE_TESTS=1.

Both must pass the SAME contract (tests/scenarios/test_planner_contract.py); there is no looser version for either.
A planner sees only plain-words intents (id, text, consumes) and the offered tool definitions: no industry labels,
no facts, no tool names from the scenario.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from agent import provider_config
from intent_llm import choose_tool


class PlannerError(RuntimeError):
    """A planner could not produce a plan. Explicit and loud: there is no silent fallback to another planner."""


@dataclass(frozen=True)
class PlannedCall:
    intent_id: str
    tool: str  # qualified name as offered by the registry, e.g. "<server>__<tool>"


class IntentPlanner(Protocol):
    name: str

    def plan(self, intents: Sequence[dict], tools: Sequence[dict]) -> list[PlannedCall]: ...


class KeywordPlanner:
    """One call per intent, in the order the intents are given, tool chosen by keyword overlap."""

    name = "keyword"

    def plan(self, intents: Sequence[dict], tools: Sequence[dict]) -> list[PlannedCall]:
        return [PlannedCall(i["id"], choose_tool(i["intent"], list(tools))["name"]) for i in intents]


SUBMIT_PLAN = {
    "name": "submit_plan",
    "description": "Submit the plan: exactly one tool call per intent, in the order they should be executed.",
    "input_schema": {
        "type": "object",
        "properties": {"calls": {"type": "array", "items": {
            "type": "object",
            "properties": {"intent_id": {"type": "string"}, "tool": {"type": "string"}},
            "required": ["intent_id", "tool"]}}},
        "required": ["calls"],
    },
}

SYSTEM = ("You plan tool use for an agent. For EVERY intent choose exactly ONE tool from the provided list that "
          "satisfies it, using the tool descriptions. Order the calls so that a call comes after every intent whose "
          "result it consumes (`consumes`). Use only tool names from the list, spelled exactly. Reply ONLY by "
          "calling the submit_plan tool: no prose, no questions.")


STRICT_SUFFIX = ("\nIMPORTANT: your previous reply did not call submit_plan. Reply ONLY by calling the submit_plan "
                 "tool. No prose, no questions; if you are unsure, still submit your best plan.")


class ClaudePlanner:
    """Real-model planner over the Messages API (anthropic_http.AnthropicHTTP, no SDK). `client` is injectable; tests
    pass one backed by FakeAnthropicTransport, which verifies the wiring only. The CONTRACT for this planner runs
    against the real API alone and is SKIPPED, with a reason, when no key is present.

    A reply without the tool call is retried EXACTLY ONCE with a stricter prompt (auto-first, like D16: a forced
    tool_choice is rejected by the current default model); a second miss raises PLANNER_NO_TOOL_CALL. API errors are
    never retried here. No fallback to another planner, ever."""

    name = "claude"

    def __init__(self, client: Any = None, model: str | None = None) -> None:
        self._client = client
        self._model = model or provider_config.resolve_model()  # PROVIDER_MODEL; Anthropic's model by default (D29)

    def _ask(self, client: Any, payload: dict[str, Any], strict: bool) -> Any:
        try:
            resp = client.messages.create(
                model=self._model, max_tokens=4000, system=SYSTEM + (STRICT_SUFFIX if strict else ""),
                tools=[SUBMIT_PLAN], tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": json.dumps(payload)}])
        except Exception as exc:
            raise PlannerError(f"PLANNER_API_FAILED: {type(exc).__name__}: {exc}") from exc
        block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"
                      and getattr(b, "name", None) == "submit_plan"), None)
        return block, getattr(resp, "stop_reason", None)

    def plan(self, intents: Sequence[dict], tools: Sequence[dict]) -> list[PlannedCall]:
        client = self._client
        if client is None:
            from ingest.anthropic_http import AnthropicHTTP  # lazy: building the keyword planner needs no key
            client = AnthropicHTTP()
        payload = {"intents": [{"id": i["id"], "intent": i["intent"], "consumes": i.get("consumes") or []}
                               for i in intents],
                   "tools": [{"name": t["name"], "description": t.get("description", "")} for t in tools]}
        block, stop = self._ask(client, payload, strict=False)
        if block is None:  # the one visible retry
            block, stop = self._ask(client, payload, strict=True)
        if block is None:
            raise PlannerError("PLANNER_NO_TOOL_CALL: the model did not call submit_plan, even after one retry with "
                               f"a stricter prompt (stop_reason={stop!r})")
        calls = (block.input or {}).get("calls")
        if not isinstance(calls, list) or not all(isinstance(c, dict) and isinstance(c.get("intent_id"), str)
                                                  and isinstance(c.get("tool"), str) for c in calls):
            raise PlannerError(f"PLANNER_BAD_PLAN: calls must be a list of {{intent_id, tool}}, got {calls!r}")
        return [PlannedCall(c["intent_id"], c["tool"]) for c in calls]
