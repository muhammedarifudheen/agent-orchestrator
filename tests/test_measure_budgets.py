"""scripts/measure_budgets.py: re-derive, verify and record the heavy-test budgets (D17) - and never edit anything."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "measure_budgets.py"
spec = importlib.util.spec_from_file_location("measure_budgets", SCRIPT)
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)


def rec(test, total, budget=10.0):
    return {"test": test, "total_s": total, "budget_s": budget, "steps": [], "steps_s": 0.0}


def runs_of(test, totals, budget=10.0):
    return [rec(test, t, budget) for t in totals]


def write_log(tmp_path, records):
    log = tmp_path / "log.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return log


# -- RE-DERIVE ------------------------------------------------------------------------------------

def test_the_formula_is_max_of_two_p95_and_median_plus_two_rounded_up_to_a_tenth():
    assert mb.recommend(5.99, 6.71) == 13.5            # 2 x p95 = 13.42 -> 13.5 (the real D17 row)
    assert mb.recommend(0.5, 0.6) == 2.5               # median + 2 wins for a fast test
    assert mb.recommend(1.0, 1.0) == 3.0
    assert mb.pct([1.0, 2.0, 3.0, 4.0, 5.0], 0.95) == pytest.approx(4.8)


def test_derive_applies_the_formula_per_function_and_takes_the_worst_parametrized_case():
    records = (runs_of("test_big", [5.0] * 14 + [6.0]) + runs_of("test_param[a]", [4.0] * 15)
               + runs_of("test_param[b]", [7.0] * 15) + runs_of("test_light", [0.3] * 15, budget=2.0))
    rows = {r["test"]: r for r in mb.derive(records)}
    assert set(rows) == {"test_big", "test_param"}, "default-budget (2 s) tests are not part of the formula"
    big = rows["test_big"]
    assert big["median"] == 5.0 and big["p95"] == pytest.approx(5.0 + 0.3) and big["recommended"] == 10.6
    assert rows["test_param"]["worst"] == "b" and rows["test_param"]["recommended"] == 14.0
    assert rows["test_param"]["current"] == 10.0 and rows["test_param"]["n"] == 15


def test_a_recommendation_more_than_20_percent_from_the_current_budget_is_flagged_and_exactly_20_is_not():
    exactly = mb.derive(runs_of("t", [6.0] * 15, budget=10.0))[0]      # recommended 12.0 vs 10.0: +20.0%
    assert exactly["recommended"] == 12.0 and exactly["flag"] is False
    over = mb.derive(runs_of("t", [6.05] * 15, budget=10.0))[0]        # recommended 12.1: +21%
    assert over["recommended"] == 12.1 and over["flag"] is True
    under = mb.derive(runs_of("t", [2.0] * 15, budget=30.0))[0]         # 6.0 vs 30: -80%
    assert under["flag"] is True and under["delta"] < 0


def test_the_table_has_the_same_shape_as_the_d17_table_and_marks_only_flagged_rows():
    rows = mb.derive(runs_of("t_ok", [6.0] * 15, budget=12.0) + runs_of("t_off", [2.0] * 15, budget=30.0))
    text = mb.format_table(rows, 15)
    header = text.splitlines()[0]
    for col in ("test", "runs", "median", "p95", "max", "max/p95", "current", "recommended", "delta"):
        assert col in header
    ok_line = next(ln for ln in text.splitlines() if ln.startswith("t_ok"))
    off_line = next(ln for ln in text.splitlines() if ln.startswith("t_off"))
    assert "UPDATE D17" not in ok_line and "UPDATE D17" in off_line
    assert "max(2 x p95, median + 2 s)" in text and "nothing was edited" in text


# -- VERIFY ---------------------------------------------------------------------------------------

BASE = {"t_slow": {"median": 5.0, "stdev": 0.5, "n": 15}, "t_fine": {"median": 5.0, "stdev": 0.5, "n": 15}}


def test_verify_names_every_test_above_75_percent_with_its_sigma_over_the_recorded_median():
    records = [rec("t_slow", 8.0, 10.0), rec("t_fine", 5.0, 10.0), rec("t_unknown", 9.0, 10.0)]
    text, near = mb.verify(records, BASE)
    assert [x["test"] for x in near] == ["t_unknown", "t_slow"]
    assert "NEAR BUDGET" in text
    assert "t_slow\n    time 8.00 s of a 10 s budget (80%); +6.0 standard deviations above the recorded median 5.00 s (D17)" in text
    assert "t_unknown\n    time 9.00 s of a 10 s budget (90%); no recorded median for this test" in text
    assert "t_fine\n" not in text.split("NEAR BUDGET")[1], "a test at 50% of its budget is not reported as near"


def test_exactly_75_percent_is_not_near_and_a_clean_run_reports_the_smallest_headroom():
    text, near = mb.verify([rec("t_fine", 7.5, 10.0), rec("t_slow", 5.0, 10.0)], BASE)
    assert near == [] and "No timed test is above 75%" in text and "1.3x slowdown" in text


def test_the_baseline_file_holds_the_d17_medians_for_every_explicitly_budgeted_test():
    base = mb.load_baseline(mb.DEFAULT_BASELINE)
    assert len(base) >= 9 and all(v["median"] > 0 and v["stdev"] > 0 and v["n"] == 15 for v in base.values())
    green = base["test_approved_proposal_is_applied_end_to_end_with_the_real_verification"]
    assert green["median"] == pytest.approx(5.99, abs=0.01)
    assert mb.load_baseline(Path("/nonexistent/baseline.json")) == {}, "a missing baseline degrades to 'no baseline'"


# -- RECORD ---------------------------------------------------------------------------------------

def test_five_or_more_runs_append_one_json_line_and_fewer_do_not(tmp_path):
    rows = mb.derive(runs_of("t", [5.0] * 15))
    assert mb.record_history(tmp_path, rows, 4, [80.0]) is None and not (tmp_path / "budget_history.jsonl").exists()
    path = mb.record_history(tmp_path, rows, 5, [78.0, 80.0, 82.0])
    mb.record_history(tmp_path, rows, 15, [80.0])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, "appended, never rewritten"
    first = json.loads(lines[0])
    assert {"date", "host", "cpu", "runs", "table_hash", "recommended", "suite_median_s", "os", "python"} <= set(first)
    assert first["runs"] == 5 and first["suite_median_s"] == 80.0 and first["recommended"] == {"t": 10.0}
    assert len(first["table_hash"]) == 12 and first["host"]


def test_the_table_hash_is_stable_for_the_same_numbers_and_changes_when_they_change():
    a = mb.derive(runs_of("t", [5.0] * 15))
    assert mb.table_hash(a) == mb.table_hash(mb.derive(runs_of("t", [5.0] * 15)))
    assert mb.table_hash(a) != mb.table_hash(mb.derive(runs_of("t", [5.5] * 15)))


# -- it never edits anything ----------------------------------------------------------------------

def _tracked_digest() -> str:
    h = hashlib.sha256()
    files = sorted((ROOT / "tests").rglob("*.py")) + [ROOT / "CLAUDE.md", mb.DEFAULT_BASELINE, ROOT / "pytest.ini"]
    for p in files:
        h.update(p.name.encode() + p.read_bytes())
    return h.hexdigest()


def test_running_every_mode_changes_no_test_file_no_claude_md_and_not_the_baseline(tmp_path, capsys):
    log = write_log(tmp_path, runs_of("t_flag", [2.0] * 15, budget=30.0) + runs_of("t_slow", [8.0] * 15))
    before = _tracked_digest()
    assert mb.main(["--from-log", str(log), "--runs", "5", "--emit-baseline", "--perf-dir", str(tmp_path / "perf"),
                    "--baseline", str(tmp_path / "none.json")]) == 0
    assert mb.main(["--from-log", str(log), "--verify", "--perf-dir", str(tmp_path / "perf")]) == 0
    out = capsys.readouterr().out
    assert "UPDATE D17" in out and "NEAR BUDGET" in out and '"tests"' in out and "recorded in" in out
    assert _tracked_digest() == before
    assert [p.name for p in (tmp_path / "perf").iterdir()] == ["budget_history.jsonl"], "its only write is the history"


def test_the_script_source_has_exactly_one_write_site_and_it_is_the_append_only_history():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    writes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in ("write_text", "write_bytes", "unlink", "rmtree", "remove", "rename", "replace"):
                writes.append(name)
            if name == "open" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                writes.append(f"open({node.args[1].value})")
    assert writes == ["open(a)"], writes


def test_help_names_the_three_modes_and_the_no_edit_promise(capsys):
    with pytest.raises(SystemExit):
        mb.main(["--help"])
    text = capsys.readouterr().out
    for needle in ("--runs", "--verify", "RE-DERIVE", "VERIFY", "NEVER EDITS A TEST FILE", "budget_history.jsonl"):
        assert needle in text


# -- measurement hygiene: a machine that sleeps mid-measurement ------------------------------------

def test_a_run_far_slower_than_the_others_is_excluded_whole_and_named():
    runs = [{"wall": w, "records": []} for w in (80, 82, 47090, 79, 81)]      # the real incident: a 13-hour run
    kept, notes = mb.exclude_interrupted_runs(runs)
    assert [r["wall"] for r in kept] == [80, 82, 79, 81]
    assert len(notes) == 1 and "run 3 EXCLUDED" in notes[0] and "47090 s" in notes[0] and "slept or hung" in notes[0]


def test_two_runs_are_too_few_to_judge_and_a_merely_slow_run_is_kept():
    two = [{"wall": 80, "records": []}, {"wall": 9000, "records": []}]
    assert mb.exclude_interrupted_runs(two) == (two, [])
    slow = [{"wall": w, "records": []} for w in (80, 82, 239, 81)]             # 2.9x the median is slow, not suspended
    assert len(mb.exclude_interrupted_runs(slow)[0]) == 4
    assert len(mb.exclude_interrupted_runs([{"wall": w, "records": []} for w in (80, 82, 246, 81)])[0]) == 3


def test_a_single_record_far_beyond_its_own_budget_is_dropped_as_implausible():
    good, dropped = mb.drop_implausible([rec("t", 5.0, 10.0), rec("t", 100.0, 10.0), rec("t", 100.1, 10.0)])
    assert dropped == 1 and [r["total_s"] for r in good] == [5.0, 100.0], "exactly 10x is still kept"


def test_main_survives_a_sleeping_run_and_the_table_and_history_stay_sane(tmp_path, monkeypatch, capsys):
    """Five fake suite runs; run 3 'slept' (47,090 s, with one test at 3,000 s). No pytest is started."""
    walls = [80.0, 82.0, 47090.0, 79.0, 81.0]
    calls = []

    def fake_run_suite(log, extra, env_extra=None):
        i = len(calls)
        calls.append(log)
        t = 3000.0 if i == 2 else 5.0 + 0.1 * i
        Path(log).write_text(json.dumps(rec("test_heavy", t, 13.5)) + "\n" + json.dumps(rec("test_param[x]", 4.0, 12.0)) + "\n",
                             encoding="utf-8")
        return (0 if i != 2 else 1), walls[i], f"fake run {i + 1}", (["ERROR tests/t.py::test_x - budget"] if i == 2 else [])

    monkeypatch.setattr(mb, "run_suite", fake_run_suite)
    code = mb.main(["--runs", "5", "--perf-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert len(calls) == 5 and len({str(c) for c in calls}) == 5, "one log file per run"
    assert "NOTE: run 3 EXCLUDED" in out
    heavy = next(ln for ln in out.splitlines() if ln.startswith("test_heavy"))
    assert " 4 " in heavy and "3000" not in heavy and "5506" not in out, "the sleeping run does not reach the table"
    line = json.loads((tmp_path / "budget_history.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["runs"] == 5 and line["runs_used"] == 4 and any("run 3 EXCLUDED" in n for n in line["notes"])
    assert line["recommended"]["test_heavy"] < 30, "a sane budget, not thousands of seconds"
    assert code == 1, "the suite did fail in the excluded run, and the exit code still says so"
    assert "ERROR tests/t.py::test_x - budget" in out, "a failing run names the failing test instead of only counting it"


# -- --verify --allow-loaded: a separate, looser gate for a loaded host ----------------------------

def test_allow_loaded_separates_tolerated_over_budget_tests_from_ones_over_even_3x():
    records = [rec("t_ok", 5.0, 10.0), rec("t_over", 20.0, 10.0), rec("t_way_over", 31.0, 10.0)]
    strict, _ = mb.verify(records, BASE)
    assert "LOADED-HOST MODE" not in strict, "the default report is the strict one"
    text, near = mb.verify(records, BASE, allow_loaded=True)
    assert "LOADED-HOST MODE: every gate budget was multiplied by 3" in text
    assert "on a normal host the tests below are bugs, on a loaded one they are expected" in text
    tolerated = text.split("OVER THE QUIET-STATE BUDGET")[1].split("OVER EVEN THE LOADED ALLOWANCE")[0]
    assert "t_over: 20.00 s against a 10 s budget (2.0x)" in tolerated and "t_way_over" not in tolerated
    beyond = text.split("OVER EVEN THE LOADED ALLOWANCE")[1]
    assert "t_way_over: 31.00 s against a 10 s budget (3.1x)" in beyond and "t_over:" not in beyond
    assert "t_ok" not in text.split("LOADED-HOST MODE")[1], "a test inside its budget is not mentioned in the loaded section"
    assert mb.verify([rec("t_ok", 5.0, 10.0)], BASE, allow_loaded=True)[0].endswith("No timed test exceeded its quiet-state budget in this run.")


def test_exactly_3x_is_tolerated_and_anything_above_is_not():
    text, _ = mb.verify([rec("t_edge", 30.0, 10.0), rec("t_beyond", 30.1, 10.0)], BASE, allow_loaded=True)
    assert "t_edge: 30.00 s" in text.split("OVER THE QUIET-STATE BUDGET")[1].split("OVER EVEN")[0]
    assert "t_beyond: 30.10 s" in text.split("OVER EVEN THE LOADED ALLOWANCE")[1]


def test_the_flag_reaches_the_suite_as_a_budget_multiplier_only_with_verify_allow_loaded(tmp_path, monkeypatch, capsys):
    seen = []

    def fake_run_suite(log, extra, env_extra=None):
        seen.append(env_extra)
        Path(log).write_text(json.dumps(rec("t", 5.0, 10.0)) + chr(10), encoding="utf-8")
        return 0, 80.0, "fake", []

    monkeypatch.setattr(mb, "run_suite", fake_run_suite)
    assert mb.main(["--verify", "--allow-loaded", "--perf-dir", str(tmp_path)]) == 0
    assert mb.main(["--verify", "--perf-dir", str(tmp_path)]) == 0
    assert seen == [{"BUDGET_MULTIPLIER": "3"}, None], "strict by default: no multiplier is passed"
    out = capsys.readouterr().out
    assert "VERIFY [LOADED-HOST (budgets x3)]" in out and "VERIFY [STRICT]" in out


@pytest.mark.parametrize("argv", [["--allow-loaded"], ["--runs", "5", "--allow-loaded"]])
def test_allow_loaded_is_refused_outside_verify(argv, capsys, monkeypatch):
    def never(*a, **k):  # if the guard regressed, fail fast instead of launching a real 15-run suite measurement
        raise AssertionError("the suite must not start: --allow-loaded was not refused")

    monkeypatch.setattr(mb, "run_suite", never)
    with pytest.raises(SystemExit) as exc:
        mb.main(argv)
    assert exc.value.code == 2 and "--allow-loaded only applies to --verify" in capsys.readouterr().err


def test_a_strict_run_scrubs_a_multiplier_inherited_from_the_callers_shell(monkeypatch, tmp_path):
    """run_suite builds the child env: without --allow-loaded BUDGET_MULTIPLIER must NOT leak through."""
    monkeypatch.setenv("BUDGET_MULTIPLIER", "3")
    captured = {}

    class Done:
        stdout, returncode = "1 passed in 0.1s", 0

    def fake_subprocess_run(cmd, **kw):
        captured["env"] = kw["env"]
        return Done()

    monkeypatch.setattr(mb.subprocess, "run", fake_subprocess_run)
    mb.run_suite(tmp_path / "log.jsonl", [])
    assert "BUDGET_MULTIPLIER" not in captured["env"]
    mb.run_suite(tmp_path / "log.jsonl", [], {"BUDGET_MULTIPLIER": "3"})
    assert captured["env"]["BUDGET_MULTIPLIER"] == "3"


def test_conftest_multiplier_only_ever_loosens_budgets(monkeypatch):
    import conftest
    monkeypatch.delenv("BUDGET_MULTIPLIER", raising=False)
    assert conftest.budget_multiplier() == 1.0
    for value, expected in (("3", 3.0), ("1.5", 1.5), ("0.5", 1.0), ("0", 1.0), ("-4", 1.0), ("abc", 1.0), ("", 1.0)):
        monkeypatch.setenv("BUDGET_MULTIPLIER", value)
        assert conftest.budget_multiplier() == expected, value
    monkeypatch.setenv("BUDGET_MULTIPLIER", "3")
    # a 5 s test against a 2 s default budget fails strict and passes under the loaded allowance (2 s x 3 = 6 s)
    assert conftest.budget_violation("t", 5.0, 2.0) is not None
    assert conftest.budget_violation("t", 5.0, 2.0 * conftest.budget_multiplier()) is None
    assert conftest.budget_violation("t", 6.1, 2.0 * conftest.budget_multiplier()) is not None


# -- budget overruns are warnings by default now: --verify keeps the strict meaning, --runs only measures ---------

def test_verify_asks_the_suite_for_the_strict_gate_and_runs_does_not(tmp_path, monkeypatch):
    seen = []

    def fake_run_suite(log, extra, env_extra=None):
        seen.append(list(extra))
        Path(log).write_text(json.dumps(rec("t", 5.0, 10.0)) + chr(10), encoding="utf-8")
        return 0, 80.0, "fake", []

    monkeypatch.setattr(mb, "run_suite", fake_run_suite)
    mb.main(["--verify", "--perf-dir", str(tmp_path)])
    mb.main(["--runs", "1", "--perf-dir", str(tmp_path)])
    assert seen == [["--strict-budgets"], []]


def test_a_run_pytest_reported_as_invalid_is_excluded_even_with_few_runs():
    runs = [{"wall": 80.0, "records": [], "invalid": ""}, {"wall": 82.0, "records": [],
            "invalid": "INVALID RUN (clock event): 1 environmental interruption(s), run_id=X, 568 passed in 479s"}]
    kept, notes = mb.exclude_interrupted_runs(runs)
    assert [r["wall"] for r in kept] == [80.0]
    assert len(notes) == 1 and "run 2 EXCLUDED: pytest reported INVALID RUN (clock event)" in notes[0]
