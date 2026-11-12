"""duties/*.yaml: the duty library is checked against the real tool catalog, so a duty cannot name a tool that does not exist,
write before its write boundary, or claim success on a field no tool returns.

The check functions take a duty dict plus a tool catalog ({qualified name: {"description", "read_only"}}), so the self-tests
exercise them on synthetic duties without the real files or servers. Every function returns a list of problem strings
(empty = fine) that name the duty."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from agent.duties import substitute_setup

ROOT = Path(__file__).resolve().parent.parent
DUTIES = ROOT / "duties"
SCHEMA = DUTIES / "schema.json"
REQUIRED_DUTIES = ["book_flight", "process_refund", "kyc_onboarding"]
PLACEHOLDER = re.compile(r"\{(\w+)\}")
TRANSFORMS = {"upper": str.upper, "lower": str.lower, "strip": str.strip, "title": str.title}


def duty_files() -> list[Path]:
    return sorted(DUTIES.glob("*.yaml"))


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# -- references -----------------------------------------------------------------------------------------------------

def refs_in(value):
    """Every `$...` string anywhere inside a (possibly nested) value."""
    if isinstance(value, str):
        if value.startswith("$"):
            yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from refs_in(v)
    elif isinstance(value, list):
        for v in value:
            yield from refs_in(v)


def split_ref(ref: str) -> tuple[str, list[str]]:
    head, *path = ref[1:].split(".")
    return head, path


def resolve(ref: str, scope: dict):
    """Walk `$name.seg.seg` through scope[name]: a segment is a dict key or an integer list index."""
    head, path = split_ref(ref)
    value = scope[head]
    for seg in path:
        value = value[int(seg)] if isinstance(value, list) else value[seg]
    return value


# -- structural checks (no tools needed) ------------------------------------------------------------------------------

def check_structure(duty: dict) -> list[str]:
    name = duty.get("name", "?")
    bad: list[str] = []
    params = duty.get("parameters", {})
    steps = duty.get("steps", [])

    used = {p for pattern in duty.get("intent_patterns", []) for p in PLACEHOLDER.findall(pattern)}
    for p in sorted(used - set(params)):
        bad.append(f"{name}: placeholder {{{p}}} is used in an intent pattern but not declared in parameters")
    for p in sorted(set(params) - used):
        bad.append(f"{name}: parameter {p} is declared but no intent pattern uses it")

    for p, spec in params.items():
        try:
            re.compile(spec.get("pattern", ""))
        except re.error as exc:
            bad.append(f"{name}: parameters.{p}.pattern does not compile: {exc}")
        if spec.get("transform") not in (None, *TRANSFORMS):
            bad.append(f"{name}: parameters.{p}.transform {spec['transform']!r} is not one of {sorted(TRANSFORMS)}")

    ids = [s.get("id") for s in steps]
    for i in sorted({i for i in ids if ids.count(i) > 1}):
        bad.append(f"{name}: step id {i!r} is not unique")

    def check_ref(ref: str, earlier: set[str], where: str) -> None:
        head, _ = split_ref(ref)
        if head == "intent":
            if split_ref(ref)[1][:1] and split_ref(ref)[1][0] not in params:
                bad.append(f"{name}: {where} references {ref} but {split_ref(ref)[1][0]} is not a declared parameter")
            elif not split_ref(ref)[1]:
                bad.append(f"{name}: {where} references bare $intent")
        elif head == "setup":
            bad.append(f"{name}: {where} references {ref}; $setup is only valid inside example")
        elif head not in earlier:
            kind = "a later step" if head in ids else "no step"
            bad.append(f"{name}: {where} references {ref}, which is {kind} (only earlier steps may be referenced)")

    for n, step in enumerate(steps):
        earlier = set(ids[:n])
        for ref in refs_in(step.get("args", {})):
            check_ref(ref, earlier, f"step {step.get('id')} args")
        guard = step.get("guard")
        if guard:
            for ref in refs_in(guard.get("check")):
                check_ref(ref, earlier, f"step {step.get('id')} guard")

    success = duty.get("success", {})
    for ref in refs_in(success.get("check")):
        if split_ref(ref)[0] not in ids:
            bad.append(f"{name}: success.check references {ref}, which is not a step id")

    if not str(duty.get("review", {}).get("reviewer", "")).strip():
        bad.append(f"{name}: review.reviewer is empty")

    example = duty.get("example")
    if example:
        setup_ids = [s.get("id") for s in example.get("setup", [])]
        for n, s in enumerate(example.get("setup", [])):
            for ref in refs_in(s.get("args", {})):
                head, path = split_ref(ref)
                if head != "setup" or not path or path[0] not in setup_ids[:n]:
                    bad.append(f"{name}: example.setup {s.get('id')} references {ref}; only earlier $setup.<id> is allowed")
        for ref in re.findall(r"\$setup\.(\w+)", example.get("intent", "")):
            if ref not in setup_ids:
                bad.append(f"{name}: example.intent references $setup.{ref}, which is not a setup id")
    return bad


# -- checks against the tool catalog ----------------------------------------------------------------------------------

def all_tools(duty: dict) -> list[tuple[str, str]]:
    found = [(f"step {s['id']}", s["tool"]) for s in duty.get("steps", [])]
    found += [(f"example.setup {s['id']}", s["tool"]) for s in duty.get("example", {}).get("setup", [])]
    return found


def check_tools_exist(duty: dict, catalog: dict) -> list[str]:
    return [f"{duty.get('name', '?')}: {where} uses tool {tool}, which no server offers"
            for where, tool in all_tools(duty) if tool not in catalog]


def check_write_boundary(duty: dict, catalog: dict) -> list[str]:
    """The boundary step is a real step whose tool is a write (read_only is False or unknown), and every step before it
    is provably read-only (read_only is True). A duty that writes earlier than its declared boundary is a safety hole."""
    name = duty.get("name", "?")
    steps = duty.get("steps", [])
    ids = [s["id"] for s in steps]
    before = duty.get("write_boundary", {}).get("before")
    if before not in ids:
        return [f"{name}: write_boundary.before {before!r} is not a step id"]
    bad = []
    index = ids.index(before)
    for step in steps[:index]:
        if catalog.get(step["tool"], {}).get("read_only") is not True:
            bad.append(f"{name}: step {step['id']} ({step['tool']}) comes before write_boundary {before!r} "
                       "but is not read-only")
    if catalog.get(steps[index]["tool"], {}).get("read_only") is True:
        bad.append(f"{name}: write_boundary {before!r} names a read-only tool ({steps[index]['tool']}); "
                   "the boundary must be the first write")
    return bad


def returns_text(description: str) -> str:
    """The `Returns:` section of a tool docstring (up to the next section), or the whole description if it has none."""
    m = re.search(r"Returns:\s*(.*?)(?:\n\s*\n|\n\s*(?:Errors|Raises|Args):|\Z)", description, re.S)
    return " ".join(m.group(1).split()) if m else " ".join(description.split())


def is_bare_value(description: str) -> bool:
    """Rule: a tool returns a bare value (so `$step` alone may be checked) when its Returns text starts with True/False,
    or says nothing about a dict, list, object, array or fields ("with"/"containing"). Anything else returns a structure
    and the success check must name a field of it."""
    text = returns_text(description)
    if re.match(r"(True|False)\b", text):
        return True
    return bool(text) and not re.search(r"\b(dict|list|object|array|with|containing)\b", text, re.I)


def check_success(duty: dict, catalog: dict) -> list[str]:
    """success.check points at a real step; its first path segment must be a word in that tool's Returns text, and a
    whole-result check (`$step`) is only allowed for a bare-value tool."""
    name = duty.get("name", "?")
    ref = duty.get("success", {}).get("check", "")
    if not isinstance(ref, str) or not ref.startswith("$"):
        return [f"{name}: success.check {ref!r} is not a reference"]
    head, path = split_ref(ref)
    by_id = {s["id"]: s for s in duty.get("steps", [])}
    if head not in by_id:
        return [f"{name}: success.check references {ref}, which is not a step id"]
    tool = by_id[head]["tool"]
    if tool not in catalog:
        return []  # reported by check_tools_exist
    description = catalog[tool]["description"]
    if not path:
        if is_bare_value(description):
            return []
        return [f"{name}: success.check {ref} is a whole-result check but {tool} returns a structure; "
                "name a field it returns"]
    seg = path[0]
    if seg.isdigit():
        return [] if re.search(r"\blist\b", returns_text(description), re.I) else [
            f"{name}: success.check {ref} indexes into {tool}'s result but it does not return a list"]
    if not re.search(rf"\b{re.escape(seg)}\b", returns_text(description)):
        return [f"{name}: success.check {ref}: {tool} never mentions a field {seg!r} in its Returns text"]
    return []


# -- example intent ---------------------------------------------------------------------------------------------------

def pattern_regex(pattern: str) -> re.Pattern:
    out, last = "", 0
    for m in PLACEHOLDER.finditer(pattern):
        out += re.escape(pattern[last:m.start()]) + f"(?P<{m.group(1)}>.+?)"
        last = m.end()
    return re.compile(out + re.escape(pattern[last:]), re.I)


def check_example_intent(duty: dict, intent: str) -> list[str]:
    """The (already $setup-substituted) example intent matches an intent pattern, and every capture satisfies its
    parameter pattern after the transform."""
    name = duty.get("name", "?")
    params = duty.get("parameters", {})
    for pattern in duty.get("intent_patterns", []):
        m = pattern_regex(pattern).fullmatch(intent)
        if not m:
            continue
        bad = []
        for p, raw in m.groupdict().items():
            value = TRANSFORMS.get(params.get(p, {}).get("transform"), lambda s: s)(raw)
            if not re.search(params.get(p, {}).get("pattern", ""), value):
                bad.append(f"{name}: example capture {p}={value!r} does not satisfy its parameter pattern")
        return bad
    return [f"{name}: example intent {intent!r} matches none of the intent patterns"]


# -- the real tool catalog ----------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def registry_catalog():
    """{qualified tool name: {description, read_only}} over every servers/*_server.py, built once in-process."""
    sys.path.insert(0, str(ROOT / "servers"))
    from inmem import InMemoryRegistry

    async def build():
        async with InMemoryRegistry(ROOT / "servers") as reg:
            assert not reg.connect_errors, reg.connect_errors
            return {t["name"]: {"description": t["description"], "read_only": reg.is_read_only(t["name"])}
                    for t in reg.anthropic_tools()}

    return asyncio.run(build())


# -- the real library -------------------------------------------------------------------------------------------------

def test_the_three_named_duties_exist():
    missing = [n for n in REQUIRED_DUTIES if not (DUTIES / f"{n}.yaml").exists()]
    assert not missing, f"missing duty files: {missing}"


def test_the_library_is_discovered_not_hardcoded():
    assert {p.stem for p in duty_files()} >= set(REQUIRED_DUTIES)


@pytest.mark.parametrize("path", duty_files(), ids=lambda p: p.stem)
def test_duty_validates_against_the_schema(path):
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(load(path)), key=lambda e: list(e.path))
    assert not errors, f"{path.name}: " + "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors)


@pytest.mark.parametrize("path", duty_files(), ids=lambda p: p.stem)
def test_duty_name_is_the_file_stem(path):
    assert load(path)["name"] == path.stem


@pytest.mark.parametrize("path", duty_files(), ids=lambda p: p.stem)
def test_duty_is_structurally_sound(path):
    assert not check_structure(load(path))


@pytest.mark.parametrize("path", duty_files(), ids=lambda p: p.stem)
def test_every_tool_a_duty_references_exists(path, registry_catalog):
    assert not check_tools_exist(load(path), registry_catalog)


@pytest.mark.parametrize("path", duty_files(), ids=lambda p: p.stem)
def test_nothing_writes_before_the_write_boundary(path, registry_catalog):
    assert not check_write_boundary(load(path), registry_catalog)


@pytest.mark.parametrize("path", duty_files(), ids=lambda p: p.stem)
def test_success_criterion_references_a_real_artifact(path, registry_catalog):
    assert not check_success(load(path), registry_catalog)


@pytest.mark.parametrize("path", [p for p in duty_files() if "example" in load(p)], ids=lambda p: p.stem)
async def test_example_intent_matches_a_pattern_after_setup(path):
    """Runs the example's setup tools for real (in-process, fresh state per test) to substitute `$setup.*`."""
    sys.path.insert(0, str(ROOT / "servers"))
    from inmem import InMemoryRegistry

    duty = load(path)
    scope: dict = {}
    async with InMemoryRegistry(ROOT / "servers") as reg:
        for step in duty["example"].get("setup", []):
            args = {k: resolve(v, {"setup": scope}) if isinstance(v, str) and v.startswith("$") else v
                    for k, v in step.get("args", {}).items()}
            outcome = await reg.call(step["tool"], args)
            assert outcome.ok, f"{duty['name']}: setup {step['id']} failed: {outcome.error_code} {outcome.error_message}"
            scope[step["id"]] = outcome.data
    intent = substitute_setup(duty["example"]["intent"], scope)
    assert not check_example_intent(duty, intent)


# -- self-tests: the checks must be able to fail ----------------------------------------------------------------------------

CATALOG = {
    "t__search": {"description": "Search.\n\n    Returns:\n        List of items with item_id and price.\n", "read_only": True},
    "t__check": {"description": "Check.\n\n    Returns:\n        Dict with eligible and reason.\n", "read_only": True},
    "t__book": {"description": "Book.\n\n    Returns:\n        Dict with booking_ref and status.\n", "read_only": False},
    "t__notify": {"description": "Notify.\n\n    Returns:\n        True if the email was accepted.\n", "read_only": False},
}


def good_duty() -> dict:
    return {
        "name": "demo", "description": "d", "review": {"status": "reviewed", "reviewer": "A. Person"},
        "intent_patterns": ["book {item} for {who}"],
        "parameters": {"item": {"type": "string", "pattern": "^[A-Z]{3}$", "transform": "upper"},
                       "who": {"type": "string", "pattern": ".+"}},
        "steps": [
            {"id": "search", "tool": "t__search", "args": {"q": "$intent.item"}},
            {"id": "book", "tool": "t__book", "args": {"item": "$search.0.item_id", "who": "$intent.who"},
             "guard": {"check": "$search.0.price", "equals": 1}},
            {"id": "notify", "tool": "t__notify", "args": {"ref": "$book.booking_ref"}},
        ],
        "write_boundary": {"before": "book"},
        "success": {"name": "sent", "check": "$notify", "equals": True},
        "example": {"intent": "book abc for Sam", "expect": "success"},
    }


def run_all(duty: dict) -> list[str]:
    return (check_structure(duty) + check_tools_exist(duty, CATALOG) + check_write_boundary(duty, CATALOG)
            + check_success(duty, CATALOG) + check_example_intent(duty, duty["example"]["intent"]))


def roundtrip(tmp_path: Path, duty: dict) -> dict:
    """Through a real yaml file, as the library would be."""
    path = tmp_path / f"{duty['name']}.yaml"
    path.write_text(yaml.safe_dump(duty, sort_keys=False), encoding="utf-8")
    return load(path)


def test_a_sound_synthetic_duty_passes_every_check(tmp_path):
    assert run_all(roundtrip(tmp_path, good_duty())) == []


def test_a_boundary_after_a_write_step_fails(tmp_path):
    duty = good_duty()
    duty["write_boundary"]["before"] = "notify"  # `book` writes before the declared boundary
    bad = check_write_boundary(roundtrip(tmp_path, duty), CATALOG)
    assert any("step book" in b and "not read-only" in b for b in bad), bad


def test_a_boundary_on_a_read_step_fails(tmp_path):
    duty = good_duty()
    duty["write_boundary"]["before"] = "search"
    assert check_write_boundary(roundtrip(tmp_path, duty), CATALOG)


def test_a_boundary_naming_no_step_fails(tmp_path):
    duty = good_duty()
    duty["write_boundary"]["before"] = "nope"
    assert check_write_boundary(roundtrip(tmp_path, duty), CATALOG)


def test_a_tool_of_unknown_safety_counts_as_a_write(tmp_path):
    catalog = {**CATALOG, "t__search": {**CATALOG["t__search"], "read_only": None}}
    assert check_write_boundary(roundtrip(tmp_path, good_duty()), catalog)


def test_a_nonexistent_tool_fails(tmp_path):
    duty = good_duty()
    duty["steps"][0]["tool"] = "t__ghost"
    duty["example"]["setup"] = [{"id": "s", "tool": "t__phantom", "args": {}}]
    bad = check_tools_exist(roundtrip(tmp_path, duty), CATALOG)
    assert len(bad) == 2 and "t__ghost" in bad[0] and "t__phantom" in bad[1]


def test_a_success_check_on_a_field_the_tool_never_returns_fails(tmp_path):
    duty = good_duty()
    duty["success"]["check"] = "$book.refund_id"
    bad = check_success(roundtrip(tmp_path, duty), CATALOG)
    assert any("refund_id" in b for b in bad), bad


def test_a_success_check_on_a_returned_field_passes(tmp_path):
    duty = good_duty()
    duty["success"] = {"name": "ref", "check": "$book.booking_ref"}
    assert check_success(roundtrip(tmp_path, duty), CATALOG) == []


def test_a_whole_result_check_on_a_structure_fails(tmp_path):
    duty = good_duty()
    duty["success"]["check"] = "$book"
    assert check_success(roundtrip(tmp_path, duty), CATALOG)


def test_a_success_check_naming_no_step_fails(tmp_path):
    duty = good_duty()
    duty["success"]["check"] = "$ghost"
    assert check_success(roundtrip(tmp_path, duty), CATALOG)


def test_an_undeclared_placeholder_fails(tmp_path):
    duty = good_duty()
    duty["intent_patterns"] = ["book {item} for {who} on {date}"]
    bad = check_structure(roundtrip(tmp_path, duty))
    assert any("{date}" in b for b in bad), bad


def test_a_declared_but_unused_parameter_fails(tmp_path):
    duty = good_duty()
    duty["parameters"]["extra"] = {"type": "string", "pattern": ".+"}
    assert any("extra" in b for b in check_structure(roundtrip(tmp_path, duty)))


def test_a_forward_reference_fails(tmp_path):
    duty = good_duty()
    duty["steps"][0]["args"] = {"q": "$book.booking_ref"}  # `book` comes later
    bad = check_structure(roundtrip(tmp_path, duty))
    assert any("later step" in b for b in bad), bad


def test_a_guard_on_a_later_step_fails(tmp_path):
    duty = good_duty()
    duty["steps"][1]["guard"] = {"check": "$notify", "equals": True}
    assert any("guard" in b for b in check_structure(roundtrip(tmp_path, duty)))


def test_an_intent_reference_to_an_undeclared_parameter_fails(tmp_path):
    duty = good_duty()
    duty["steps"][0]["args"] = {"q": "$intent.colour"}
    assert any("colour" in b for b in check_structure(roundtrip(tmp_path, duty)))


def test_setup_references_outside_example_fail(tmp_path):
    duty = good_duty()
    duty["steps"][0]["args"] = {"q": "$setup.order.id"}
    assert any("$setup" in b for b in check_structure(roundtrip(tmp_path, duty)))


def test_duplicate_step_ids_and_a_bad_regex_and_an_empty_reviewer_fail(tmp_path):
    duty = good_duty()
    duty["steps"][1]["id"] = "search"
    duty["parameters"]["item"]["pattern"] = "^[A-Z"
    duty["review"]["reviewer"] = "  "
    bad = " | ".join(check_structure(roundtrip(tmp_path, duty)))
    assert "not unique" in bad and "does not compile" in bad and "reviewer is empty" in bad


def test_an_example_intent_that_matches_no_pattern_fails():
    assert check_example_intent(good_duty(), "cancel everything")


def test_an_example_capture_that_breaks_its_parameter_pattern_fails():
    assert check_example_intent(good_duty(), "book abcd for Sam")  # 4 letters, pattern wants 3
