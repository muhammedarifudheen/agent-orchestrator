"""Registry filtering: offer a model the few tools an intent could use, not the whole registry (CLAUDE.md D31).

A request that carries every tool description costs prompt time on every call; on a CPU-only host that alone made planning
exceed the client timeout. `filter(intent, registry)` returns a view of the registry holding a small, DETERMINISTIC subset:

  (i)   every tool a matching duty references is always included (even past `max_tools`: a duty must be able to run);
  (ii)  the remaining tools are scored by keyword overlap with the intent text (the same scoring the keyword planner uses:
        `score`, below), and only a tool that overlaps at all is a candidate;
  (iii) the candidates are capped at `max_tools`, those from an industry the intent names (the part of a qualified tool name
        before `__`) first, then by score, then by name.

If nothing matches (no duty, no overlapping tool) the FULL registry comes back: an intent this module cannot read is never
silently starved of tools. Same intent and same registry give the same subset, however the registry happens to list its tools.
This module names no industry and no tool; it knows only the shapes of the data it is given."""
from __future__ import annotations

import math
import re
from typing import Any, Sequence

from agent.tool_registry import SEP

DEFAULT_MAX_TOOLS = 8

_STOP = {"a", "an", "the", "of", "to", "for", "and", "or", "on", "in", "is", "are", "it", "its", "that", "this",
         "with", "by", "be", "as", "at", "from", "will", "their", "them", "one", "any", "get", "up", "per"}


# -- the matcher's scoring (moved here unchanged from tests/scenarios/intent_llm.py, which imports it back) ----------------

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


def doc_freq(tools: Sequence[dict]) -> dict[str, int]:
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


# -- the filter -------------------------------------------------------------------------------------------------------

def industry_of(tool_name: str) -> str:
    return tool_name.partition(SEP)[0]


def industries_named(intent_text: str, tools: Sequence[dict]) -> set[str]:
    """Industries the intent talks about: a word of the intent that is a prefix of the industry's name or the other way round
    (four letters at least, so `ship` would name `shipping`). A hint for the cap, never a requirement."""
    words = {w for w in tokens(intent_text) if len(w) >= 4}
    named = set()
    for ind in {industry_of(t["name"]) for t in tools}:
        key = next(iter(tokens(ind)), ind)
        if len(key) >= 4 and any(w.startswith(key) or key.startswith(w) for w in words):
            named.add(ind)
    return named


def duty_tools(intent_text: str, tools: Sequence[dict], duties: Any = None) -> set[str]:
    """Tools of every duty whose pattern the intent fits at all (a fuzzy fit counts: this decides what to SHOW, not what to run)."""
    if duties is None or not len(duties):
        return set()
    offered = {t["name"] for t in tools}
    return {s.tool for m in duties.candidates(intent_text) for s in m.duty.steps if s.tool in offered}


def select_tools(intent: str, tools: Sequence[dict], max_tools: int = DEFAULT_MAX_TOOLS, duties: Any = None) -> list[dict]:
    """The subset (in the registry's own sequence) or all of `tools` when the intent matches nothing. See the module docstring."""
    if max_tools < 1:
        raise ValueError(f"max_tools must be at least 1, got {max_tools}")
    tools = list(tools)
    keep = duty_tools(intent, tools, duties)
    df = doc_freq(tools)
    named = industries_named(intent, tools)
    scored = []
    for t in tools:
        s, name_hits = score(intent, t, df, len(tools))
        if s > 0 and t["name"] not in keep:
            scored.append((industry_of(t["name"]) not in named, -s, -name_hits, t["name"]))
    if not keep and not scored:
        return tools                      # unmatchable: never a silent exclusion
    keep |= {name for *_, name in sorted(scored)[:max(max_tools - len(keep), 0)]}
    return [t for t in tools if t["name"] in keep]


class FilteredRegistry:
    """A registry restricted to a subset of its tools for OFFERING. Everything else (calls, safety metadata, connection
    errors) is the underlying registry's, so a run still executes and classifies against the real thing."""

    def __init__(self, base: Any, names: set[str], size_before: int) -> None:
        self._base, self._names = base, set(names)
        self.size_before, self.size_after = size_before, len(self._names)

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [t for t in self._base.anthropic_tools() if t["name"] in self._names]

    def __getattr__(self, name: str) -> Any:   # call, tool_meta, is_read_only, connect_errors, ...
        return getattr(self._base, name)


def filter(intent: str, registry: Any, max_tools: int = DEFAULT_MAX_TOOLS, duties: Any = None) -> FilteredRegistry:  # noqa: A001
    """The registry view to offer for `intent`. `registry` is anything with `anthropic_tools()` (a `ToolRegistry`)."""
    tools = registry.anthropic_tools()
    chosen = select_tools(intent, tools, max_tools, duties)
    return FilteredRegistry(registry, {t["name"] for t in chosen}, len(tools))
