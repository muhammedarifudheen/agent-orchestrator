"""Phase B gate: failures.jsonl -> reviewable .patch proposals. Never writes specs/, never proposes below the threshold."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from generators.from_spec import validate_schema
from learning.patchfmt import apply_diff, parse_proposal
from learning.propose import MIN_RECURRENCE, propose

ROOT = Path(__file__).resolve().parent.parent.parent
SPEC_SRC = ROOT / "specs" / "weather.yaml"
OP = "get_current_weather"
FORECAST = "get_daily_forecast"


def rec(code, *, operation=OP, system="weather", status=None, arg_types=None, recurrence=1, ts="2026-11-07T10:00:00+00:00",
        retryable=False, recovery="agent", n=0):
    return {"system": system, "operation": operation, "error_code": code, "recovery_attempted": False,
            "outcome": "handed_off_exhausted", "retryable": retryable, "recovery": recovery, "status": status,
            "message": "m", "arg_types": arg_types if arg_types is not None else {"latitude": "number"},
            "ts": ts, "recurrence": recurrence, "sources": [f"h{n}.json"]}


def setup(tmp_path, records, spec_text=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    if spec_text is None:
        shutil.copyfile(SPEC_SRC, specs / "weather.yaml")
    else:
        (specs / "weather.yaml").write_bytes(spec_text.encode("utf-8"))
    failures = tmp_path / "failures.jsonl"
    failures.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return failures, specs, tmp_path / "proposals"


def spec_text(specs):
    return (specs / "weather.yaml").read_bytes().decode("utf-8").replace("\r\n", "\n")


def tree_hash(d):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


def patched(specs, path):
    header, diff = parse_proposal(path.read_text(encoding="utf-8"))
    old = spec_text(specs)
    return header, diff, old, apply_diff(old, diff)


def changed_lines(diff):
    return [ln for ln in diff.split("\n") if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]


def op_of(spec, name=OP):
    return next(o for o in spec["operations"] if o["name"] == name)


def two(code, **kw):
    """Two same-key records in different 24h windows (recurrence 1 each): total 2."""
    base = kw.pop("n", 0) * 10
    return [rec(code, n=base + 1, **kw), rec(code, n=base + 2, ts="2026-11-09T10:00:00+00:00", **kw)]


def only(paths, type_):
    assert len(paths) == 1
    assert paths[0].name.endswith(f"__{type_}.patch"), paths[0].name
    return paths[0]


# ---- threshold -------------------------------------------------------------------------------------------------

def test_threshold_is_two_and_a_total_of_one_yields_nothing(tmp_path):
    assert MIN_RECURRENCE == 2
    f, s, o = setup(tmp_path, [rec("NOT_FOUND", status=404)])
    assert propose(f, s, o) == []
    assert not o.exists() or list(o.rglob("*.patch")) == []


def test_two_records_of_one_key_in_separate_windows_add_up_to_a_proposal(tmp_path):
    f, s, o = setup(tmp_path, two("NOT_FOUND", status=404))
    p = only(propose(f, s, o), "add_error_code")
    header, _, _, _ = patched(s, p)
    assert header["recurrence"] == "2"


def test_single_record_with_recurrence_two_is_enough_and_totals_sum(tmp_path):
    f, s, o = setup(tmp_path, [rec("NOT_FOUND", status=404, recurrence=2)])
    assert len(propose(f, s, o)) == 1
    f, s, o = setup(tmp_path / "b", [rec("NOT_FOUND", status=404, recurrence=2, n=1),
                                     rec("NOT_FOUND", status=404, recurrence=3, n=2, ts="2026-11-09T10:00:00+00:00")])
    header, _, _, _ = patched(s, only(propose(f, s, o), "add_error_code"))
    assert header["recurrence"] == "5"


def test_different_keys_do_not_add_up(tmp_path):
    f, s, o = setup(tmp_path, [rec("NOT_FOUND", status=404, n=1), rec("OTHER", status=404, n=2),
                               rec("NOT_FOUND", status=404, operation=FORECAST, n=3)])
    assert propose(f, s, o) == []


# ---- type 3: add_error_code ------------------------------------------------------------------------------------

def test_add_error_code_appends_exactly_one_rule_and_nothing_else(tmp_path):
    f, s, o = setup(tmp_path, two("NOT_FOUND", status=404, retryable=False, recovery="agent"))
    p = only(propose(f, s, o), "add_error_code")
    assert p.name == f"weather__{OP}__NOT_FOUND__add_error_code.patch"
    assert p.parent == o
    header, diff, old, new = patched(s, p)
    assert header["proposal"] == "add_error_code"
    assert (header["system"], header["operation"], header["error_code"]) == ("weather", OP, "NOT_FOUND")
    assert header["spec"] == "specs/weather.yaml" and header["rationale"].strip()
    before, after = yaml.safe_load(old), yaml.safe_load(new)
    rules = op_of(after)["errors"]
    assert len(rules) == len(op_of(before)["errors"]) + 1
    rule = rules[-1]
    assert rule["when"] == {"status": 404} and rule["code"] == "NOT_FOUND"
    assert rule["retryable"] is False and rule["recovery"] == "agent"
    assert isinstance(rule.get("meaning"), str) and rule["meaning"]
    expect = copy.deepcopy(before)
    op_of(expect)["errors"].append(rule)
    assert after == expect
    assert len(changed_lines(diff)) <= 8
    validate_schema(after)


def test_add_error_code_copies_retryable_and_recovery_from_the_record(tmp_path):
    f, s, o = setup(tmp_path, two("BUSY", status=409, retryable=True, recovery="human"))
    _, _, _, new = patched(s, only(propose(f, s, o), "add_error_code"))
    rule = op_of(yaml.safe_load(new))["errors"][-1]
    assert rule["when"] == {"status": 409} and rule["code"] == "BUSY"
    assert rule["retryable"] is True and rule["recovery"] == "human"


def test_status_none_never_yields_add_error_code(tmp_path):
    f, s, o = setup(tmp_path, two("NOT_FOUND", status=None))
    only(propose(f, s, o), "mark_needs_review")


def test_declared_operation_code_yields_mark_needs_review_not_add_error_code(tmp_path):
    f, s, o = setup(tmp_path, two("INVALID_COORDINATES", status=400))
    only(propose(f, s, o), "mark_needs_review")


def test_code_declared_in_top_level_error_map_yields_mark_needs_review(tmp_path):
    f, s, o = setup(tmp_path, two("RATE_LIMITED", status=429))
    only(propose(f, s, o), "mark_needs_review")


def test_a_later_record_with_a_status_is_enough_for_add_error_code(tmp_path):
    f, s, o = setup(tmp_path, [rec("NOT_FOUND", status=None, n=1),
                               rec("NOT_FOUND", status=404, n=2, ts="2026-11-09T10:00:00+00:00")])
    only(propose(f, s, o), "add_error_code")


# ---- type 2: correct_param_type --------------------------------------------------------------------------------

def test_correct_param_type_changes_only_that_params_type(tmp_path):
    f, s, o = setup(tmp_path, two("INVALID_ARGUMENT", arg_types={"latitude": "string", "longitude": "number"}))
    p = only(propose(f, s, o), "correct_param_type")
    assert p.name == f"weather__{OP}__INVALID_ARGUMENT__correct_param_type.patch"
    header, diff, old, new = patched(s, p)
    assert header["proposal"] == "correct_param_type" and header["rationale"].strip()
    before, after = yaml.safe_load(old), yaml.safe_load(new)
    expect = copy.deepcopy(before)
    next(x for x in op_of(expect)["params"] if x["name"] == "latitude")["type"] = "string"
    assert after == expect
    assert len(changed_lines(diff)) <= 8
    validate_schema(after)


def test_correct_param_type_integer_spec_param_seen_as_number(tmp_path):
    f, s, o = setup(tmp_path, two("INVALID_REQUEST", operation=FORECAST,
                                  arg_types={"latitude": "number", "longitude": "number", "days": "number"}))
    _, _, old, new = patched(s, only(propose(f, s, o), "correct_param_type"))
    days = next(x for x in op_of(yaml.safe_load(new), FORECAST)["params"] if x["name"] == "days")
    assert days["type"] == "number"
    before = yaml.safe_load(old)
    expect = copy.deepcopy(before)
    next(x for x in op_of(expect, FORECAST)["params"] if x["name"] == "days")["type"] = "number"
    assert yaml.safe_load(new) == expect


def test_correct_param_type_wins_over_add_error_code_when_both_match(tmp_path):
    f, s, o = setup(tmp_path, two("INVALID_ARGUMENT", status=400, arg_types={"latitude": "string"}))
    only(propose(f, s, o), "correct_param_type")


def test_spec_number_seen_as_integer_is_not_a_type_error(tmp_path):
    f, s, o = setup(tmp_path, two("INVALID_ARGUMENT", status=None, arg_types={"latitude": "integer", "longitude": "integer"}))
    only(propose(f, s, o), "mark_needs_review")


def test_matching_types_or_unsupported_observed_type_are_not_type_errors(tmp_path):
    f, s, o = setup(tmp_path, two("INVALID_ARGUMENT", status=None, arg_types={"latitude": "number", "longitude": "array"}))
    only(propose(f, s, o), "mark_needs_review")


def test_param_type_correction_needs_one_of_the_invalid_argument_codes(tmp_path):
    f, s, o = setup(tmp_path, two("NOT_FOUND", status=None, arg_types={"latitude": "string"}))
    only(propose(f, s, o), "mark_needs_review")
    f, s, o = setup(tmp_path / "b", two("INVALID_COORDINATES", status=400, arg_types={"latitude": "string"}))
    only(propose(f, s, o), "mark_needs_review")


def test_unknown_param_name_is_not_a_type_error(tmp_path):
    f, s, o = setup(tmp_path, two("INVALID_ARGUMENT", status=None, arg_types={"nonsense": "string"}))
    only(propose(f, s, o), "mark_needs_review")


# ---- type 1: mark_needs_review ---------------------------------------------------------------------------------

def test_mark_needs_review_sets_flag_and_reasons_only(tmp_path):
    f, s, o = setup(tmp_path, two("INVALID_COORDINATES", status=400))
    p = only(propose(f, s, o), "mark_needs_review")
    assert p.name == f"weather__{OP}__INVALID_COORDINATES__mark_needs_review.patch"
    header, diff, old, new = patched(s, p)
    assert header["proposal"] == "mark_needs_review" and header["rationale"].strip()
    before, after = yaml.safe_load(old), yaml.safe_load(new)
    op = op_of(after)
    assert op["needs_review"] is True
    assert isinstance(op["review_reasons"], list) and op["review_reasons"]
    assert all(isinstance(r, str) and r for r in op["review_reasons"])
    expect = copy.deepcopy(before)
    op_of(expect)["needs_review"] = True
    op_of(expect)["review_reasons"] = op["review_reasons"]
    assert after == expect
    assert len(changed_lines(diff)) <= 8
    validate_schema(after)


# ---- header ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("records,type_", [
    (two("NOT_FOUND", status=404), "add_error_code"),
    (two("INVALID_ARGUMENT", arg_types={"latitude": "string"}), "correct_param_type"),
    (two("NOT_FOUND", status=None), "mark_needs_review"),
])
def test_header_carries_spec_hash_and_total_recurrence(tmp_path, records, type_):
    f, s, o = setup(tmp_path, records)
    raw = (s / "weather.yaml").read_bytes()
    header, _ = parse_proposal(only(propose(f, s, o), type_).read_text(encoding="utf-8"))
    assert header["proposal"] == type_
    assert header["base_sha256"] == hashlib.sha256(raw).hexdigest()
    assert header["recurrence"] == "2"
    assert header["spec"] == "specs/weather.yaml"
    assert header["system"] == "weather" and header["operation"] == OP


# ---- skipped cases ---------------------------------------------------------------------------------------------

def test_unknown_system_gets_no_proposal(tmp_path):
    f, s, o = setup(tmp_path, two("NOT_FOUND", system="ghost", status=404))
    assert propose(f, s, o) == []
    assert not o.exists() or list(o.rglob("*.patch")) == []


def test_unknown_operation_gets_no_proposal(tmp_path):
    f, s, o = setup(tmp_path, two("NOT_FOUND", operation="no_such_op", status=404))
    assert propose(f, s, o) == []
    assert not o.exists() or list(o.rglob("*.patch")) == []


def test_operation_already_needs_review_gets_no_proposal(tmp_path):
    text = SPEC_SRC.read_bytes().decode("utf-8").replace("\r\n", "\n")
    marker = f"  - name: {OP}\n"
    assert marker in text
    flagged = text.replace(marker, marker + "    needs_review: true\n", 1)
    for records in (two("NOT_FOUND", status=404), two("NOT_FOUND", status=None),
                    two("INVALID_ARGUMENT", arg_types={"latitude": "string"})):
        f, s, o = setup(tmp_path, records, spec_text=flagged)
        assert propose(f, s, o) == []


def test_other_operation_of_the_same_spec_is_still_proposed_when_one_needs_review(tmp_path):
    text = SPEC_SRC.read_bytes().decode("utf-8").replace("\r\n", "\n")
    marker = f"  - name: {OP}\n"
    flagged = text.replace(marker, marker + "    needs_review: true\n", 1)
    f, s, o = setup(tmp_path, two("NOT_FOUND", operation=FORECAST, status=404), spec_text=flagged)
    assert only(propose(f, s, o), "add_error_code").name.startswith(f"weather__{FORECAST}__")


def test_missing_failures_file_or_empty_file_yields_nothing(tmp_path):
    f, s, o = setup(tmp_path, [])
    assert propose(f, s, o) == []
    assert propose(tmp_path / "absent.jsonl", s, o) == []


# ---- specs/ is never written, comments survive -----------------------------------------------------------------

def test_specs_dir_is_byte_identical_before_and_after_even_after_two_runs(tmp_path):
    records = (two("NOT_FOUND", status=404) + two("INVALID_ARGUMENT", arg_types={"longitude": "string"}, n=5)
               + two("BOOM", status=None, operation=FORECAST, n=7))
    f, s, o = setup(tmp_path, records)
    before = tree_hash(s)
    first = propose(f, s, o)
    assert len(first) == 3
    assert tree_hash(s) == before
    propose(f, s, o)
    assert tree_hash(s) == before
    assert sorted(p.name for p in s.iterdir()) == ["weather.yaml"]


def test_spec_comments_survive_in_every_proposal(tmp_path):
    records = two("NOT_FOUND", status=404) + two("INVALID_ARGUMENT", arg_types={"latitude": "string"}, n=3) \
        + two("WEIRD", status=None, operation=FORECAST, n=6)
    f, s, o = setup(tmp_path, records)
    paths = propose(f, s, o)
    assert len(paths) == 3
    old = spec_text(s)
    comments = [ln for ln in old.split("\n") if ln.lstrip().startswith("#")]
    assert comments
    for p in paths:
        _, diff, _, new = patched(s, p)
        assert all(c in new.split("\n") for c in comments), p.name
        assert 0 < len(changed_lines(diff)) <= 8, p.name


# ---- idempotence -----------------------------------------------------------------------------------------------

def test_second_run_writes_nothing_and_returns_nothing(tmp_path):
    f, s, o = setup(tmp_path, two("NOT_FOUND", status=404))
    first = propose(f, s, o)
    assert len(first) == 1
    content = first[0].read_bytes()
    assert propose(f, s, o) == []
    assert sorted(p.name for p in o.rglob("*") if p.is_file()) == [first[0].name]
    assert first[0].read_bytes() == content


@pytest.mark.parametrize("folder", ["approved", "rejected", "applied"])
def test_no_new_proposal_when_the_first_was_moved_to_a_decision_folder(tmp_path, folder):
    f, s, o = setup(tmp_path, two("NOT_FOUND", status=404))
    p = propose(f, s, o)[0]
    (o / folder).mkdir()
    p.rename(o / folder / p.name)
    assert propose(f, s, o) == []
    assert [x.name for x in o.rglob("*.patch")] == [p.name]


def test_a_new_key_is_still_proposed_after_an_earlier_one_was_decided(tmp_path):
    f, s, o = setup(tmp_path, two("NOT_FOUND", status=404))
    p = propose(f, s, o)[0]
    (o / "rejected").mkdir()
    p.rename(o / "rejected" / p.name)
    f.write_text(f.read_text(encoding="utf-8") + "".join(json.dumps(r) + "\n" for r in two("BOOM", status=None, n=9)),
                 encoding="utf-8")
    again = propose(f, s, o)
    assert [x.name for x in again] == [f"weather__{OP}__BOOM__mark_needs_review.patch"]
