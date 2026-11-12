"""Duty library: named, human-reviewed sequences of tool calls that the orchestrator prefers over fresh planning (D22).

Industry-agnostic like the rest of agent/: a duty is DATA in duties/*.yaml, discovered at run time. Nothing here knows a
duty name, a tool name or a domain. A duty has intent patterns (what a user's request looks like), typed parameters
captured from the request, steps (qualified tool names with arguments bound to the request or to earlier results), a
write boundary (the first step that changes the world) and a success criterion.

Matching is exact first (the whole request fits a pattern: confidence 1.0), then a scored fuzzy match (below). Whether a
match is trusted enough to run is the caller's decision against DUTY_CONFIDENCE_THRESHOLD.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DUTIES_DIR = Path(__file__).resolve().parent.parent / "duties"
SCHEMA_NAME = "schema.json"

# D22: a duty runs only if the request matches one of its patterns with at least this confidence. 1.0 is a whole-request
# match. A missing or misspelled word of an 8-word pattern costs 1/8 (0.875) and every word of the request outside the
# pattern costs its share, so 0.9 passes one polite extra word on a normal request and refuses a dropped pattern word.
# Changing the number is a decision: edit CLAUDE.md D22 and this line in the same commit
# (tests/test_duty_matching.py fails if they disagree). Never lower it to make a request match.
DUTY_CONFIDENCE_THRESHOLD = 0.9
AMBIGUITY_MARGIN = 0.03  # two different duties this close at the top: the request is ambiguous, so no duty
TYPO_FLOOR = 0.85  # a pattern word counts, at its similarity, when a request word is at least this similar

_PLACEHOLDER = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_TRAILING = ",;:!?"


@dataclass
class BoundaryEvent:
    """What a write gate is shown at a duty's write boundary, BEFORE the first write-classified step runs."""

    duty: str
    step: str  # the step about to run: the first one that changes the world
    tool: str
    arguments: dict[str, Any]
    write_tools: list[str]  # every write-classified tool the rest of the duty will call
    completed: list[str]  # the (read-only) tools that already ran


def allow_reviewed_duty(event: BoundaryEvent) -> bool:
    """The default write gate (D22): a duty in duties/ was reviewed by a named person as THIS sequence, so crossing its own
    boundary is allowed. The point of the boundary is not this default; it is that the gate is consulted first, every time,
    with the full list of writes ahead, and that nothing before it can write. A caller that wants a per-run approval
    (an interactive confirm, a ticket, a second person) passes its own `write_gate`; a False return stops the run."""
    return True


class DutyError(ValueError):
    """A duty file is not usable (schema or cross-field rule). The library records it and never matches it."""


class DutyContractError(Exception):
    """A duty's contract did not hold while it was running (a reference that does not resolve, a guard that is false,
    a success criterion that is not met). `kind` is a stable short code, `detail` says what."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


@dataclass
class Param:
    name: str
    pattern: str | None = None
    transform: str | None = None

    def accept(self, raw: str) -> str | None:
        """The captured text after the transform, or None if it does not satisfy the parameter's pattern."""
        value = raw.strip()
        if self.transform == "upper":
            value = value.upper()
        elif self.transform == "lower":
            value = value.lower()
        if not value or (self.pattern and not re.fullmatch(self.pattern, value)):
            return None
        return value


@dataclass
class Step:
    id: str
    tool: str
    args: dict[str, Any]
    guard: dict[str, Any] | None = None


@dataclass
class Duty:
    name: str
    description: str
    reviewer: str
    intent_patterns: list[str]
    parameters: dict[str, Param]
    steps: list[Step]
    boundary_step: str
    success: dict[str, Any]
    example: dict[str, Any]
    path: Path | None = None

    @property
    def boundary_index(self) -> int:
        return next(i for i, s in enumerate(self.steps) if s.id == self.boundary_step)


@dataclass
class DutyMatch:
    duty: Duty
    confidence: float
    kind: str  # "exact" | "fuzzy"
    pattern: str
    captures: dict[str, str]


# -- references ----------------------------------------------------------------------------------------------------

def _walk(value: Any, segments: list[str], where: str) -> Any:
    for seg in segments:
        if isinstance(value, dict):
            if seg not in value:
                raise DutyContractError("unresolved_reference", f"{where}: no key {seg!r} in {sorted(value)[:8]}")
            value = value[seg]
        elif isinstance(value, list):
            if not seg.isdigit() or int(seg) >= len(value):
                raise DutyContractError("unresolved_reference", f"{where}: no item {seg!r} in a list of {len(value)}")
            value = value[int(seg)]
        else:
            raise DutyContractError("unresolved_reference", f"{where}: cannot read {seg!r} from a {type(value).__name__}")
    return value


def resolve(expr: Any, intent: dict[str, str], results: dict[str, Any]) -> Any:
    """Bind one argument: a `$...` string is a reference (`$intent.p`, `$step`, `$step.a.0.b`), anything else a literal."""
    if isinstance(expr, str) and expr.startswith("$"):
        head, *segments = expr[1:].split(".")
        if head == "intent":
            return _walk(intent, segments, expr)
        if head not in results:
            raise DutyContractError("unresolved_reference", f"{expr}: step {head!r} has not produced a result")
        return _walk(results[head], segments, expr)
    if isinstance(expr, dict):
        return {k: resolve(v, intent, results) for k, v in expr.items()}
    if isinstance(expr, list):
        return [resolve(v, intent, results) for v in expr]
    return expr


def holds(check: dict[str, Any], intent: dict[str, str], results: dict[str, Any]) -> bool:
    """A guard or success check: `check` resolves, and equals `equals` if given (else: is present and not None/False/empty)."""
    value = resolve(check["check"], intent, results)
    if "equals" in check:
        return value == check["equals"]
    return value not in (None, False, "", [], {})


def substitute_setup(text: str, setup_results: dict[str, Any], today: date | None = None) -> str:
    """Expand an example intent: `$setup.<id>.<path>` from the results of the example's setup calls, and `$today+N` as the
    date N days from today (so a shipped example never expires)."""
    def one(m: re.Match[str]) -> str:
        _, *segments = m.group(0)[1:].split(".")
        return str(_walk(setup_results.get(segments[0]) if segments else None, segments[1:], m.group(0)))
    text = re.sub(r"\$setup(?:\.[A-Za-z0-9_]+)+", one, text)
    return re.sub(r"\$today\+(\d+)", lambda m: ((today or date.today()) + timedelta(days=int(m.group(1)))).isoformat(), text)


# -- loading -------------------------------------------------------------------------------------------------------

def _refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith("$") else []
    if isinstance(value, dict):
        return [r for v in value.values() for r in _refs(v)]
    if isinstance(value, list):
        return [r for v in value for r in _refs(v)]
    return []


def _pattern_tokens(pattern: str) -> list[tuple[str, str]]:
    return [("slot", m.group(1)) if (m := _PLACEHOLDER.match(tok)) else ("lit", tok) for tok in pattern.split()]


def parse_duty(data: dict[str, Any], path: Path | None = None) -> Duty:
    """Build a Duty from schema-valid data, enforcing the rules a JSON Schema cannot express."""
    name = data["name"]
    if path is not None and path.stem != name:
        raise DutyError(f"name {name!r} must equal the file stem {path.stem!r}")
    params = {k: Param(k, v.get("pattern"), v.get("transform")) for k, v in data["parameters"].items()}
    used: set[str] = set()
    for pattern in data["intent_patterns"]:
        tokens = _pattern_tokens(pattern)
        if re.search(r"[{}]", " ".join(t for kind, t in tokens if kind == "lit")):
            raise DutyError(f"pattern {pattern!r}: a placeholder must be a whole word, like {{origin}}")
        slots = [t for kind, t in tokens if kind == "slot"]
        if len(set(slots)) != len(slots):
            raise DutyError(f"pattern {pattern!r}: a placeholder is used twice")
        used.update(slots)
    if used != set(params):
        raise DutyError(f"parameters {sorted(params)} do not match the placeholders used {sorted(used)}")
    steps = [Step(s["id"], s["tool"], s.get("args", {}), s.get("guard")) for s in data["steps"]]
    ids = [s.id for s in steps]
    if len(set(ids)) != len(ids) or "intent" in ids or any(not _NAME.match(i) for i in ids):
        raise DutyError(f"step ids must be unique names (and not 'intent'): {ids}")
    for i, step in enumerate(steps):
        earlier = set(ids[:i])
        for ref in _refs(step.args) + _refs(step.guard or {}):
            head = ref[1:].split(".")[0]
            if head == "intent":
                if ref[1:].split(".")[1:2] and ref[1:].split(".")[1] not in params:
                    raise DutyError(f"step {step.id}: {ref} names an undeclared parameter")
            elif head not in earlier:
                raise DutyError(f"step {step.id}: {ref} must point at an EARLIER step")
    boundary = data["write_boundary"]["before"]
    if boundary not in ids:
        raise DutyError(f"write_boundary.before {boundary!r} is not a step id")
    success_head = data["success"]["check"][1:].split(".")[0]
    if success_head not in ids:
        raise DutyError(f"success.check {data['success']['check']!r} must reference a step")
    return Duty(name, data["description"], data["review"]["reviewer"], list(data["intent_patterns"]), params, steps,
                boundary, dict(data["success"]), dict(data["example"]), path)


def load_schema(duties_dir: Path) -> dict[str, Any]:
    import json
    return json.loads((duties_dir / SCHEMA_NAME).read_text(encoding="utf-8"))


def validate_duty_data(data: Any, duties_dir: Path = DEFAULT_DUTIES_DIR) -> None:
    """Raise DutyError unless `data` satisfies duties/schema.json."""
    from jsonschema import validators
    schema = load_schema(duties_dir)
    errors = sorted(validators.validator_for(schema)(schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        raise DutyError("; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:5]))


def load_duty_file(path: Path, duties_dir: Path | None = None) -> Duty:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DutyError(f"not valid YAML: {exc}") from exc
    validate_duty_data(data, duties_dir or path.parent)
    return parse_duty(data, path)


class DutyLibrary:
    """Every duty found in a directory. A file that does not load is recorded in `errors` and never matched: a broken
    duty must not stop the orchestrator, but it must not run either."""

    def __init__(self, duties_dir: Path | None = None) -> None:
        self.dir = Path(duties_dir) if duties_dir is not None else DEFAULT_DUTIES_DIR
        self.duties: dict[str, Duty] = {}
        self.errors: dict[str, str] = {}
        if not self.dir.is_dir() or not (self.dir / SCHEMA_NAME).is_file():
            return
        for path in sorted(self.dir.glob("*.yaml")):
            try:
                duty = load_duty_file(path, self.dir)
            except (DutyError, OSError) as exc:
                self.errors[path.name] = str(exc)
                continue
            self.duties[duty.name] = duty

    def __len__(self) -> int:
        return len(self.duties)

    def candidates(self, goal: str) -> list[DutyMatch]:
        """Every duty pattern the request fits at all, best first (ties by duty name, so the ranking is deterministic)."""
        found = [m for duty in self.duties.values() for pattern in duty.intent_patterns
                 if (m := match_pattern(duty, pattern, goal)) is not None]
        return sorted(found, key=lambda m: (-m.confidence, m.duty.name, m.pattern))

    def best_match(self, goal: str) -> tuple[DutyMatch | None, bool]:
        """(the best candidate or None, ambiguous). Ambiguous = another duty is within AMBIGUITY_MARGIN of the best."""
        ranked = self.candidates(goal)
        if not ranked:
            return None, False
        best = ranked[0]
        rival = next((m for m in ranked[1:] if m.duty.name != best.duty.name), None)
        return best, rival is not None and best.confidence - rival.confidence <= AMBIGUITY_MARGIN


# -- matching ------------------------------------------------------------------------------------------------------

def normalize(goal: str) -> str:
    text = " ".join(goal.split())
    return text[:-1] if text.endswith((".", "!", "?")) else text  # sentence punctuation; no value we capture ends in one


def _exact_regex(pattern: str) -> re.Pattern[str]:
    parts = []
    for kind, tok in _pattern_tokens(pattern):
        parts.append(f"(?P<{tok}>\\S.*?)" if kind == "slot" else re.escape(tok))
    return re.compile(r"\s+".join(parts), re.IGNORECASE)


def _bind(duty: Duty, raw: dict[str, str]) -> dict[str, str] | None:
    out = {}
    for name, text in raw.items():
        value = duty.parameters[name].accept(text)
        if value is None:
            return None
        out[name] = value
    return out


def _clean(word: str) -> str:
    return word.rstrip(_TRAILING).lower()


def match_pattern(duty: Duty, pattern: str, goal: str) -> DutyMatch | None:
    """Score one pattern against a request. None = it cannot bind (a parameter is missing or does not fit its pattern)."""
    text = normalize(goal)
    whole = _exact_regex(pattern).fullmatch(text)
    if whole:
        captures = _bind(duty, whole.groupdict())
        if captures is not None:
            return DutyMatch(duty, 1.0, "exact", pattern, captures)
        # the words fit but a capture swallowed extra words (a trailing "please"): score it as a fuzzy match instead

    tokens, words = _pattern_tokens(pattern), text.split()
    literal_total = sum(1 for kind, _ in tokens if kind == "lit")
    if not literal_total or not words:
        return None
    # Align the pattern's literal words to the request in sequence; a literal that is not found is skipped.
    anchors: dict[int, tuple[int, float]] = {}  # pattern token index -> (word index, similarity)
    cursor = 0
    for ti, (kind, tok) in enumerate(tokens):
        if kind != "lit":
            continue
        for wi in range(cursor, len(words)):
            sim = 1.0 if _clean(words[wi]) == tok.lower() else difflib.SequenceMatcher(None, _clean(words[wi]), tok.lower()).ratio()
            if sim == 1.0 or sim >= TYPO_FLOOR:
                anchors[ti] = (wi, sim)
                cursor = wi + 1
                break
    if not anchors:
        return None
    raw: dict[str, str] = {}
    used_words = set()
    ordered = sorted(anchors.items())
    for ti, (wi, _) in ordered:
        used_words.add(wi)
    for ti, (kind, tok) in enumerate(tokens):
        if kind != "slot":
            continue
        prev = max((anchors[j][0] for j in anchors if j < ti), default=-1)
        nxt = min((anchors[j][0] for j in anchors if j > ti), default=None)
        region = words[prev + 1:nxt] if nxt is not None else words[prev + 1:prev + 2]  # a final slot takes ONE word
        if not region:
            return None
        raw[tok] = " ".join(region)
        used_words.update(range(prev + 1, prev + 1 + len(region)))
    captures = _bind(duty, raw)
    if captures is None:
        return None
    extra = len(words) - len(used_words)
    confidence = (sum(sim for _, sim in anchors.values()) / literal_total) * (1 - extra / len(words))
    return DutyMatch(duty, round(confidence, 4), "fuzzy", pattern, captures)
