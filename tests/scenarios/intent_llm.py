"""IntentLLM: a deterministic stand-in for the model in cross-industry scenario tests.

HONESTY NOTE. This is a keyword-matching stub, NOT a language model. It is handed only a scenario's
plain-words intents and scalar facts (never tool names), and at every turn it:

1. takes the next intent;
2. CHOOSES a tool from the list the orchestrator offers in ``tools=`` by scoring the intent text against each
   tool's name and description (token overlap, see ``score``), tie-break documented on ``choose_tool``;
3. BUILDS the arguments by matching the chosen tool's input-schema property names to the intent's ``uses``
   facts and to fields of earlier tool RESULTS named by the intent's ``consumes``;
4. on a tool error does what a simple agent would: ``recovery=agent`` -> retry the same call once, anything
   else -> stop issuing calls and give a final text.

What a passing test proves: the Orchestrator and the registry are industry-neutral (the same loop, failure
routing and handoff code serve any set of servers, with the model choosing from whatever is offered). It does
NOT prove that a real model would choose well. This module contains no tool names and no industry names; it
knows only the shapes of the data it is given.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta
from types import SimpleNamespace as NS
from typing import Any

_STOP = {"a", "an", "the", "of", "to", "for", "and", "or", "on", "in", "is", "are", "it", "its", "that", "this",
         "with", "by", "be", "as", "at", "from", "will", "their", "them", "one", "any", "get", "up", "per"}


def tokens(text: str) -> set[str]:
    """Lower-case word set with a crude plural strip, stopwords removed."""
    out = set()
    for w in re.split(r"[^a-z0-9]+", text.lower()):
        if not w or w in _STOP:
            continue
        if len(w) > 3 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def resolve_facts(facts: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Resolve ``{days_from_today: N}`` facts to YYYY-MM-DD strings; other facts pass through."""
    today = today or date.today()
    return {k: ((today + timedelta(days=v["days_from_today"])).isoformat()
                if isinstance(v, dict) and "days_from_today" in v else v) for k, v in facts.items()}


def _doc_freq(tools: list[dict]) -> dict[str, int]:
    """How many offered tools mention each token in name or description (rare tokens are more telling)."""
    df: dict[str, int] = {}
    for t in tools:
        for w in tokens(t["name"].split("__", 1)[-1]) | tokens(t.get("description", "")):
            df[w] = df.get(w, 0) + 1
    return df


def score(intent_text: str, tool: dict, df: dict[str, int], n_tools: int) -> tuple[float, int]:
    """(weighted overlap, name overlap). Each shared token counts log(1 + n_tools/df) (an idf weight), times 3
    when it appears in the tool NAME and 1 when only in the description."""
    want = tokens(intent_text)
    name = tokens(tool["name"].split("__", 1)[-1])
    desc = tokens(tool.get("description", ""))
    head = next((t for t in re.split(r"[^a-z0-9]+", intent_text.lower()) if t and t not in _STOP), "")
    head = next(iter(tokens(head)), "")   # the intent's first content word is its verb: it counts 1.5x
    w = lambda tok: math.log(1 + n_tools / max(df.get(tok, 1), 1)) * (1.5 if tok == head else 1)
    return (sum(3 * w(t) for t in want & name) + sum(1.0 * w(t) for t in want & (desc - name)), len(want & name))


def choose_tool(intent_text: str, tools: list[dict]) -> dict:
    """Highest weighted overlap wins. Ties: more name-token overlap, then the SHORTER description (the more
    specific tool), then alphabetical name, so the choice is fully deterministic."""
    df = _doc_freq(tools)
    return max(tools, key=lambda t: (*score(intent_text, t, df, len(tools)), -len(t.get("description", "")),
                                     tuple(-ord(c) for c in t["name"])))


def _walk(obj: Any):
    """Yield every (key, value) pair in nested dict/list data."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _usable(item: Any) -> bool:
    """A list element is usable unless a field about availability is zero or false."""
    if not isinstance(item, dict):
        return True
    return all(bool(v) for k, v in item.items()
               if "avail" in k.lower() and isinstance(v, (int, float, bool)))


def _from_results(prop: str, results: list[Any]) -> Any:
    """Find a value for schema property ``prop`` in earlier results (latest first): exact key, else a key that
    ends with ``_prop``. In a list, the first usable element supplies the value."""
    for exact in (True, False):
        for res in reversed(results):
            if isinstance(res, list):
                res = next((r for r in res if _usable(r)), None)
            for k, v in _walk(res):
                if (k == prop) if exact else k.endswith("_" + prop):
                    return v
    for res in reversed(results):   # last resort: a plural list field (``xs``) supplies its first item for ``x``
        for k, v in _walk(res):
            if k == prop + "s" and isinstance(v, list) and v:
                return v[0]
    return None


def _from_facts(prop: str, uses: list[str], facts: dict[str, Any]) -> Any:
    """Match a property to the intent's used facts: exact name, else a fact named ``<x>_prop`` or ``prop_<x>``
    (so ``email`` takes ``passenger_email``)."""
    for key in uses:
        if key == prop:
            return facts[key]
    for key in uses:
        if key.endswith("_" + prop) or prop.endswith("_" + key):
            return facts[key]
    return None


class PlannedLLM:
    """Quacks like the Anthropic client (``llm.messages.create(...)``); see module docstring. The tool CHOICE comes
    from a planner (planners.IntentPlanner), asked once, on the first turn, with the tools the orchestrator offers;
    this class only executes the plan: it builds each call's arguments, retries once on recovery=agent, and stops
    on anything else."""

    def __init__(self, intents: list[dict], facts: dict[str, Any], planner: Any) -> None:
        self.planner = planner
        self.plan: list[Any] | None = None
        # only plain-words fields are kept: no industry label, no tool name ever reaches the stub
        self.intents = [{k: i.get(k) for k in ("id", "intent", "uses", "consumes")} for i in intents]
        self.facts = resolve_facts(facts)
        self.messages = self
        self.issued: list[tuple[str, str]] = []   # (intent id, qualified tool) for every call it issued, in sequence
        self.results: dict[str, Any] = {}          # intent id -> parsed successful result
        self._pos = 0
        self._pending: tuple[dict, dict, dict] | None = None   # (intent, tool, args) awaiting a result
        self._retried: set[str] = set()

    # -- the client interface -------------------------------------------------------------------
    async def create(self, *, messages, tools, **_):
        if self._pending is not None:
            intent, tool, args = self._pending
            text = _last_result_text(messages)
            m = re.match(r"\[(\w+)\] retryable=\S+ recovery=(\w+)", text)
            if m is None:
                self.results[intent["id"]] = _parse(text)
                self._pending, self._pos = None, self._pos + 1
            elif m.group(2) == "agent" and intent["id"] not in self._retried:
                self._retried.add(intent["id"])
                return self._call(intent, tool, args, "That failed; retrying the same call once.")
            else:
                return NS(content=[NS(type="text", text=f"Stopping: {intent['id']} failed ({m.group(1)}).")])
        if self.plan is None:
            self.plan = self.planner.plan(self.intents, tools)
        if self._pos >= len(self.plan):
            return NS(content=[NS(type="text", text="All requested steps are done.")])
        planned = self.plan[self._pos]
        intent = next(i for i in self.intents if i["id"] == planned.intent_id)
        tool = next((t for t in tools if t["name"] == planned.tool), {"name": planned.tool, "input_schema": {}})
        prior = [self.results[c] for c in intent["consumes"] if c in self.results]
        args = self._build_args(intent, tool, prior)
        return self._call(intent, tool, args, f"Next: {intent['intent']}.")

    # -- helpers --------------------------------------------------------------------------------
    def _call(self, intent, tool, args, thought):
        self._pending = (intent, tool, args)
        self.issued.append((intent["id"], tool["name"]))
        block = NS(type="tool_use", id=f"t{len(self.issued)}", name=tool["name"], input=dict(args))
        return NS(content=[NS(type="text", text=thought), block])

    def _build_args(self, intent: dict, tool: dict, prior: list[Any]) -> dict[str, Any]:
        schema = tool.get("input_schema") or {}
        required = set(schema.get("required") or [])
        args: dict[str, Any] = {}
        for prop, spec in (schema.get("properties") or {}).items():
            value = _from_facts(prop, intent["uses"] or [], self.facts)
            if value is None:
                value = _from_results(prop, prior)
            if value is None and prop in required and spec.get("type") == "string":
                value = intent["intent"]   # free-text field (e.g. a summary): say what we were asked to do
            if value is not None:
                args[prop] = value
        return args


class IntentLLM(PlannedLLM):
    """The default stand-in: PlannedLLM with the keyword planner (kept so existing tests read the same)."""

    def __init__(self, intents: list[dict], facts: dict[str, Any]) -> None:
        from planners import KeywordPlanner  # lazy: planners imports choose_tool from this module
        super().__init__(intents, facts, KeywordPlanner())


def _last_result_text(messages: list[dict]) -> str:
    last = messages[-1]["content"]
    return last[0]["content"] if isinstance(last, list) else ""


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text
