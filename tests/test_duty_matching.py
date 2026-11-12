"""Duty matching and binding (D22): exact first, then a scored fuzzy match, against DUTY_CONFIDENCE_THRESHOLD.

The matcher is pure text work (no tools, no model), so these tests are about what it accepts and, as much, what it must
refuse: a request that is not clearly a duty goes to the planner."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from agent import duties as duties_module
from agent.duties import (AMBIGUITY_MARGIN, DUTY_CONFIDENCE_THRESHOLD, DutyContractError, DutyError, DutyLibrary, holds,
                          resolve, substitute_setup)
from duty_helpers import DUTIES, make_library

ROOT = Path(__file__).resolve().parent.parent
FLIGHT = "book a flight from DEL to BOM on 2027-03-15 for Asha Verma at asha@example.com"


@pytest.fixture(scope="module")
def library() -> DutyLibrary:
    lib = DutyLibrary()
    assert lib.errors == {}, lib.errors
    return lib


def best(library, goal):
    match, ambiguous = library.best_match(goal)
    return match, ambiguous


# -- the decision on record --------------------------------------------------------------------------------------------

def test_the_threshold_in_the_code_is_the_threshold_recorded_in_claude_md():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    recorded = re.search(r"DUTY_CONFIDENCE_THRESHOLD = ([0-9.]+)", text)
    assert recorded, "CLAUDE.md D22 must state `DUTY_CONFIDENCE_THRESHOLD = <number>`"
    assert float(recorded.group(1)) == DUTY_CONFIDENCE_THRESHOLD, "edit CLAUDE.md D22 and agent/duties.py in the same commit"


def test_no_duty_name_is_hard_coded_in_the_agent_package():
    names = {p.stem for p in DUTIES.glob("*.yaml")}
    assert {"book_flight", "process_refund", "kyc_onboarding"} <= names
    for path in (ROOT / "agent").glob("*.py"):
        strings = [n.value for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        hits = [s for s in strings if any(name in s for name in names)]
        assert hits == [], f"{path.name} names a duty: {hits}"


# -- exact ---------------------------------------------------------------------------------------------------------

def test_an_exact_request_matches_with_confidence_one_and_binds_transformed_parameters(library):
    match, ambiguous = best(library, FLIGHT.replace("DEL", "del").replace("BOM", "bom"))
    assert (match.duty.name, match.kind, match.confidence, ambiguous) == ("book_flight", "exact", 1.0, False)
    assert match.captures == {"origin": "DEL", "destination": "BOM", "date": "2027-03-15",
                              "passenger_name": "Asha Verma", "passenger_email": "asha@example.com"}


@pytest.mark.parametrize("goal,duty,params", [
    ("refund order ord-abcd1234", "process_refund", {"order_id": "ORD-ABCD1234"}),
    ("onboard customer Priya Nair with document doc-valid-123 in inr", "kyc_onboarding",
     {"name": "Priya Nair", "document_id": "DOC-VALID-123", "currency": "INR"}),
])
def test_each_duty_matches_its_own_request_shape(library, goal, duty, params):
    match, _ = best(library, goal)
    assert match.duty.name == duty and match.confidence == 1.0 and match.captures == params


def test_whitespace_case_and_a_final_full_stop_do_not_matter(library):
    goal = "  BOOK a   flight from DEL to BOM on 2027-03-15 for Asha Verma at Asha@Example.com.  "
    match, _ = best(library, goal)
    assert match.confidence == 1.0 and match.captures["passenger_email"] == "asha@example.com"


# -- parameters decide whether a request can run at all --------------------------------------------------------------

@pytest.mark.parametrize("goal", [
    "book a flight from Delhi to BOM on 2027-03-15 for Asha Verma at asha@example.com",   # not an IATA code
    "book a flight from DEL to BOM on 15-03-2027 for Asha Verma at asha@example.com",      # not a YYYY-MM-DD date
    "book a flight from DEL to BOM on 2027-03-15 for Asha Verma at not-an-email",          # not an email
    "refund order 12345",                                                                   # not an order id
])
def test_a_request_whose_parameters_do_not_fit_cannot_run_a_duty(library, goal):
    match, _ = best(library, goal)
    assert match is None or match.confidence < DUTY_CONFIDENCE_THRESHOLD


# -- fuzzy: what is accepted, and exactly where it stops --------------------------------------------------------------

def test_a_polite_extra_word_still_clears_the_threshold(library):
    match, ambiguous = best(library, "Please " + FLIGHT)
    assert match.kind == "fuzzy" and DUTY_CONFIDENCE_THRESHOLD <= match.confidence < 1.0 and not ambiguous
    assert match.captures["origin"] == "DEL"


def test_a_misspelled_pattern_word_counts_at_its_similarity(library):
    match, _ = best(library, FLIGHT.replace("book", "bok"))
    assert match.kind == "fuzzy" and DUTY_CONFIDENCE_THRESHOLD <= match.confidence < 1.0


def test_a_dropped_pattern_word_falls_below_the_threshold(library):
    match, _ = best(library, FLIGHT.replace("book a flight", "book flight"))
    assert match.confidence == pytest.approx(7 / 8, abs=1e-3) and match.confidence < DUTY_CONFIDENCE_THRESHOLD


def test_a_chatty_request_falls_below_the_threshold(library):
    match, _ = best(library, "Could you please " + FLIGHT + " thanks")
    assert match.confidence < DUTY_CONFIDENCE_THRESHOLD


def test_a_trailing_word_after_the_last_parameter_is_extra_not_part_of_it(library):
    match, _ = best(library, "refund order ORD-ABCD1234 please")
    assert match.captures == {"order_id": "ORD-ABCD1234"} and match.confidence < DUTY_CONFIDENCE_THRESHOLD


@pytest.mark.parametrize("goal", ["what is the weather in Paris", "refund my last purchase", "", "book", "hello there"])
def test_unrelated_requests_match_nothing_clearly(library, goal):
    match, _ = best(library, goal)
    assert match is None or match.confidence < DUTY_CONFIDENCE_THRESHOLD


def test_the_ranking_is_deterministic_and_best_first(library):
    first = [(m.duty.name, m.confidence) for m in library.candidates("Please " + FLIGHT)]
    assert first == [(m.duty.name, m.confidence) for m in library.candidates("Please " + FLIGHT)]
    assert [c for _, c in first] == sorted((c for _, c in first), reverse=True)


# -- ambiguity: two different duties that fit about equally well go to the planner ---------------------------------

def test_two_duties_that_fit_equally_are_ambiguous(tmp_path):
    def clone(name, data):
        pass

    lib = make_library(tmp_path, clone, only=("process_refund",))
    twin = yaml.safe_load((tmp_path / "duties" / "process_refund.yaml").read_text(encoding="utf-8"))
    twin["name"] = "process_refund_twin"
    (tmp_path / "duties" / "process_refund_twin.yaml").write_text(yaml.safe_dump(twin, sort_keys=False), encoding="utf-8")
    lib = DutyLibrary(tmp_path / "duties")
    match, ambiguous = lib.best_match("refund order ORD-ABCD1234")
    assert ambiguous and match.confidence == 1.0 and AMBIGUITY_MARGIN > 0


# -- the library: discovered, and a broken file never runs ------------------------------------------------------------

def test_the_library_is_discovered_from_the_directory_and_a_broken_file_is_recorded_not_matched(tmp_path):
    lib = make_library(tmp_path)
    assert sorted(lib.duties) == sorted(p.stem for p in DUTIES.glob("*.yaml")) and lib.errors == {}
    (tmp_path / "duties" / "garbage.yaml").write_text("name: garbage\nsteps: not-a-list\n", encoding="utf-8")
    (tmp_path / "duties" / "not_yaml.yaml").write_text("a: [unclosed\n", encoding="utf-8")
    lib = DutyLibrary(tmp_path / "duties")
    assert set(lib.errors) == {"garbage.yaml", "not_yaml.yaml"}
    assert "garbage" not in lib.duties and len(lib) == 3


def test_a_missing_directory_or_schema_gives_an_empty_library(tmp_path):
    assert len(DutyLibrary(tmp_path / "nope")) == 0
    (tmp_path / "empty").mkdir()
    assert len(DutyLibrary(tmp_path / "empty")) == 0


@pytest.mark.parametrize("mutate,message", [
    (lambda d: d.__setitem__("name", "other_name"), "file stem"),
    (lambda d: d["parameters"].pop("origin"), "do not match the placeholders"),
    (lambda d: d["steps"][0]["args"].__setitem__("origin", "$intent.nope"), "undeclared parameter"),
    (lambda d: d["steps"][0]["args"].__setitem__("origin", "$book.booking_ref"), "EARLIER step"),
    (lambda d: d["write_boundary"].__setitem__("before", "ghost"), "not a step id"),
    (lambda d: d["success"].__setitem__("check", "$ghost"), "must reference a step"),
])
def test_cross_field_rules_the_schema_cannot_express_are_enforced_at_load(tmp_path, mutate, message):
    lib = make_library(tmp_path, lambda name, data: mutate(data) if name == "book_flight" else None, only=("book_flight",))
    assert "book_flight" not in lib.duties
    assert message in lib.errors["book_flight.yaml"], lib.errors


# -- references ---------------------------------------------------------------------------------------------------------

def test_references_resolve_keys_and_list_indexes_and_leave_literals_alone():
    results = {"search": [{"flight_id": "F1", "seats": 3}], "book": {"ref": "R1"}}
    intent = {"origin": "DEL"}
    assert resolve("$intent.origin", intent, results) == "DEL"
    assert resolve("$search.0.flight_id", intent, results) == "F1"
    assert resolve("$book", intent, results) == {"ref": "R1"}
    assert resolve({"a": "$book.ref", "b": ["x", "$search.0.seats"], "c": 5}, intent, results) == {"a": "R1", "b": ["x", 3], "c": 5}


@pytest.mark.parametrize("expr", ["$search.1.flight_id", "$search.0.nope", "$ghost.x", "$book.ref.deeper", "$search.zero"])
def test_a_reference_that_does_not_resolve_is_a_contract_error_naming_it(expr):
    with pytest.raises(DutyContractError) as err:
        resolve(expr, {}, {"search": [{"flight_id": "F1"}], "book": {"ref": "R1"}})
    assert err.value.kind == "unresolved_reference" and expr in err.value.detail


def test_holds_compares_to_equals_or_requires_presence():
    results = {"s": {"ok": True, "n": 0, "text": "SENT"}}
    assert holds({"check": "$s.ok", "equals": True}, {}, results)
    assert not holds({"check": "$s.text", "equals": "NOPE"}, {}, results)
    assert holds({"check": "$s.text"}, {}, results) and not holds({"check": "$s.n"}, {}, results)


def test_example_setup_references_are_substituted_from_setup_results():
    text = substitute_setup("refund order $setup.order.order_id now", {"order": {"order_id": "ORD-1"}})
    assert text == "refund order ORD-1 now"


def test_the_module_exposes_the_documented_names():
    assert DutyError and duties_module.TYPO_FLOOR == 0.85
