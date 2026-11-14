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
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from agent import provider_config, registry_filter
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


class FilteredPlanner:
    """Ask `inner` about ONE intent at a time, offering it only `registry_filter.select_tools(intent, tools, max_tools)` (D31).

    A planner asked about all intents at once must be shown every tool any of them could use; measured on this registry
    (31 tools, 10 intents) a cap of 8 per intent still unions to 25 tools, which saves almost nothing. One small request per
    intent is what keeps a prompt short. The results are concatenated in the order the intents were given and then ordered
    so that no call precedes the call whose result it `consumes` (stable: ties keep the given sequence). Deterministic
    whenever `inner` is. `last_stats` describes the last plan; the contract itself is `inner`'s to meet, unchanged."""

    def __init__(self, inner: IntentPlanner, max_tools: int = registry_filter.DEFAULT_MAX_TOOLS, duties: Any = None) -> None:
        self.inner, self.max_tools, self.duties = inner, max_tools, duties
        self.name = f"{inner.name}+filtered"
        self.last_stats: dict[str, Any] = {}

    def plan(self, intents: Sequence[dict], tools: Sequence[dict]) -> list[PlannedCall]:
        calls: list[PlannedCall] = []
        offered: set[str] = set()
        per_request: list[int] = []
        seconds: list[float] = []
        for intent in intents:
            subset = registry_filter.select_tools(intent["intent"], tools, self.max_tools, self.duties)
            offered |= {t["name"] for t in subset}
            per_request.append(len(subset))
            began = time.monotonic()
            calls += self.inner.plan([intent], subset)
            seconds.append(round(time.monotonic() - began, 3))
        self.last_stats = {"registry_size_before": len(tools), "registry_size_after": len(offered),
                           "requests": len(per_request), "tools_per_request": per_request, "request_seconds": seconds}
        return _by_prerequisite(intents, calls)


def _by_prerequisite(intents: Sequence[dict], calls: list[PlannedCall]) -> list[PlannedCall]:
    """Stable ordering: repeatedly take the first call whose `consumes` are all already placed (or not planned at all)."""
    consumes = {i["id"]: i.get("consumes") or [] for i in intents}
    planned = {c.intent_id for c in calls}
    placed: set[str] = set()
    pending, out = list(calls), []
    while pending:
        nxt = next((c for c in pending if all(d in placed or d not in planned for d in consumes.get(c.intent_id, []))), None)
        if nxt is None:        # a cycle: leave the rest as given; the contract will say so
            return out + pending
        pending.remove(nxt)
        placed.add(nxt.intent_id)
        out.append(nxt)
    return out


MAX_TOOLS_ENV = "PLANNER_MAX_TOOLS"
STATS_FILE_ENV = "PLANNER_STATS_FILE"


def max_tools_from_env(env: Any = None) -> int | None:
    """PLANNER_MAX_TOOLS: unset or empty = no filtering; a positive integer = the per-request cap. Anything else is an error."""
    raw = (os.environ if env is None else env).get(MAX_TOOLS_ENV, "").strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{MAX_TOOLS_ENV} must be a positive integer, got {raw!r}")
    return int(raw)


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

    def __init__(self, client: Any = None, model: str | None = None, max_tools: int | None = None) -> None:
        self._client = client
        self._model = model or provider_config.resolve_model()  # PROVIDER_MODEL; Anthropic's model by default (D29)
        # D31: None = one request that carries the whole registry (what the wiring tests pin); N = one request per intent
        # offering at most N tools. PLANNER_MAX_TOOLS sets it from the environment, so a live run needs no code change.
        self._max_tools = max_tools if max_tools is not None else max_tools_from_env()
        self.last_stats: dict[str, Any] = {}

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
        began = time.monotonic()
        if self._max_tools is None:
            calls = self._plan_all(intents, tools)
            self.last_stats = {"registry_size_before": len(tools), "registry_size_after": len(tools), "requests": 1,
                               "tools_per_request": [len(tools)], "request_seconds": [round(time.monotonic() - began, 3)]}
        else:
            filtered = FilteredPlanner(_Whole(self), self._max_tools)
            calls = filtered.plan(intents, tools)
            self.last_stats = dict(filtered.last_stats)
        self._write_stats(began)
        return calls

    def _write_stats(self, began: float) -> None:
        """PLANNER_STATS_FILE: append one JSON line per plan (sizes, requests, seconds), so a run inside a pytest subprocess can
        report what it measured. Opt-in and best effort: a measurement never fails a plan."""
        path = os.environ.get(STATS_FILE_ENV)
        if not path:
            return
        try:
            line = json.dumps({**self.last_stats, "model": self._model, "max_tools": self._max_tools,
                               "total_seconds": round(time.monotonic() - began, 3)})
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def _plan_all(self, intents: Sequence[dict], tools: Sequence[dict]) -> list[PlannedCall]:
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


class _Whole:
    """`ClaudePlanner` seen as an IntentPlanner that sends every request it is given as one (no filtering of its own)."""

    def __init__(self, planner: ClaudePlanner) -> None:
        self.name, self.plan = planner.name, planner._plan_all
