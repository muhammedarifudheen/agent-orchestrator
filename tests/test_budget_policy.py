"""Budget overruns are warnings by default and failures under --strict-budgets; environmental interruptions are never failures
(CLAUDE.md D17). Unit tests for the pure parts, then three runs of real probe tests in a subprocess to show the behavior end to
end: the probes (tests/test_probe_*.py) skip themselves unless started here (RUN_PROBE=1)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import budget_policy
import clock_guard
import conftest

ROOT = Path(__file__).resolve().parent.parent
BASE = {"test_slow_one": {"median": 2.0, "stdev": 0.1, "n": 15}}


# -- the overrun policy (pure) ---------------------------------------------------------------------

def test_within_budget_is_not_an_overrun_and_exactly_the_budget_is_within():
    assert budget_policy.evaluate("t.py::test_slow_one", 5.0, 6.0, BASE) is None
    assert budget_policy.evaluate("t.py::test_slow_one", 6.0, 6.0, BASE) is None


def test_an_overrun_carries_both_multipliers_the_budget_one_and_the_quiet_median_one():
    o = budget_policy.evaluate("tests/t.py::test_slow_one", 9.0, 6.0, BASE)
    assert (o.ratio_to_budget, o.quiet_median_s, o.ratio_to_quiet_median) == (1.5, 2.0, 4.5)
    text = budget_policy.warning_text(o)
    assert text.startswith("BUDGET OVERRUN (warning, not a failure): tests/t.py::test_slow_one took 9.0 s")
    assert "against a 6 s quiet-state budget = 1.5x the budget, 4.5x its quiet-state median of 2.00 s" in text


def test_a_test_without_a_recorded_median_still_reports_the_budget_multiplier():
    o = budget_policy.evaluate("tests/t.py::test_unknown", 3.0, 2.0, BASE)
    assert o.quiet_median_s is None and o.ratio_to_quiet_median is None and o.ratio_to_budget == 1.5
    assert "= 1.5x the budget" in budget_policy.warning_text(o) and "quiet-state median" not in budget_policy.warning_text(o)


def test_the_real_baseline_knows_the_heavy_tests():
    assert "test_clean_read_spec_is_generated_verified_and_promoted" in budget_policy.quiet_baseline()


def test_log_appends_one_json_line_per_overrun_with_host_test_ratio_and_time(tmp_path):
    o = budget_policy.evaluate("tests/t.py::test_slow_one", 9.0, 6.0, BASE)
    budget_policy.log(tmp_path, o, "warn", "RUN1", 1.0)
    path = budget_policy.log(tmp_path, o, "strict", "RUN1", 3.0)
    first, second = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert {"ts", "host", "mode", "run_id", "test", "elapsed_s", "budget_s", "ratio_to_budget", "quiet_median_s",
            "ratio_to_quiet_median", "budget_multiplier"} <= set(first)
    assert (first["mode"], second["mode"], first["ratio_to_quiet_median"], second["budget_multiplier"]) == ("warn", "strict", 4.5, 3.0)
    assert first["host"] and first["ts"].endswith("+00:00")


# -- clock events: classification and the heartbeat ------------------------------------------------

def test_classify_flags_negative_elapsed_a_negative_remaining_time_a_clock_disagreement_and_a_pause():
    assert clock_guard.classify(5.0) is None and clock_guard.classify(5.0, 0.4, 0.2) is None
    assert "negative elapsed time (-267.8 s)" in clock_guard.classify(-267.8)
    assert "negative remaining time" in clock_guard.classify(10.0, 0.0, 0.0, "TimeoutExpired: timed out after -267.844 seconds")
    assert "timed out after 60 seconds" not in (clock_guard.classify(10.0, 0.0, 0.0, "timed out after 60 seconds") or "")
    assert "clock disagreement" in clock_guard.classify(10.0, 0.0, 30.0)
    assert "process paused for 268.0 s" in clock_guard.classify(300.0, 268.0, 0.0)
    assert clock_guard.classify(10.0, clock_guard.GAP_S, 0.0) is None, "exactly the threshold is not a pause"


def test_the_heartbeat_records_a_pause_and_not_ordinary_ticks():
    """Simulate a machine that slept with an INJECTED clock (never patch `time`: the real detector is watching this run):
    the clock jumps 300 s between two heartbeat ticks."""
    real = time.monotonic
    calls = {"n": 0}

    def jumping():
        calls["n"] += 1
        return real() + (300.0 if calls["n"] > 3 else 0.0)

    hb = clock_guard.Heartbeat(tick_s=0.01, clock=jumping)
    hb.start()
    deadline = real() + 3.0
    while not hb.gaps and real() < deadline:
        time.sleep(0.01)
    hb.stop()
    assert hb.gaps and hb.gaps[0][1] > 290, hb.gaps
    assert hb.longest_gap_since(0.0) > 290 and hb.longest_gap_since(real() + 10_000) == 0.0


def test_the_heartbeat_stays_quiet_on_a_normal_process():
    hb = clock_guard.Heartbeat(tick_s=0.01)
    hb.start()
    time.sleep(0.15)
    hb.stop()
    assert hb.gaps == []


# -- the hooks: an interrupted run is invalid, never green, never recorded ----------------------------

def test_an_interrupted_session_is_not_recorded_and_exits_non_zero(monkeypatch, tmp_path):
    seen = []
    interrupted = [{"test": "tests/x.py::t", "reason": "paused", "outcome_was": "failed"}]
    session = NS(config=NS(pluginmanager=NS(get_plugin=lambda n: None), _env_interruptions=interrupted), exitstatus=0)
    monkeypatch.setattr(conftest, "_perf", {"t0": 0.0, "durations": {("tests/x.py::t", "call"): 1.0}})
    monkeypatch.setattr(conftest, "PERF_DIR", tmp_path)
    monkeypatch.setattr(conftest.pytest, "ExitCode", pytest.ExitCode)
    import perf_capture
    monkeypatch.setattr(perf_capture, "record_run", lambda *a, **k: seen.append(a))
    conftest.pytest_sessionfinish(session, 0)
    assert seen == [], "an invalid run is not evidence about speed"
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_the_summary_line_is_prefixed_invalid_run_only_when_a_test_was_interrupted():
    parts_in = [("568 passed", {"green": True}), ("23 skipped", {"yellow": True})]
    terminal = NS(build_summary_stats_line=lambda: (list(parts_in), "green"))
    config = NS(_env_interruptions=[], _run_id="RID-1")
    conftest._mark_invalid_runs_in_the_summary_line(config, terminal)
    assert terminal.build_summary_stats_line() == (parts_in, "green"), "a normal run's line is untouched"
    config._env_interruptions.append({"test": "t", "reason": "r", "outcome_was": "failed"})
    parts, color = terminal.build_summary_stats_line()
    assert parts[0][0] == "INVALID RUN (clock event): 1 environmental interruption(s), run_id=RID-1" and color == "red"
    assert parts[1:] == parts_in


# -- end to end, in a subprocess ---------------------------------------------------------------------

def run_probe(tmp_path, probe, *args):
    env = {**os.environ, "RUN_PROBE": "1", "AGENT_PERF_DIR": str(tmp_path)}
    for var in ("STRICT_BUDGETS", "BUDGET_MULTIPLIER", "STEP_TIMING_LOG"):
        env.pop(var, None)
    done = subprocess.run([sys.executable, "-m", "pytest", f"tests/{probe}", "-q", "-p", "no:cacheprovider", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    return done, done.stdout + done.stderr


@pytest.mark.budget(5)
def test_default_run_warns_names_the_test_and_the_multiplier_and_exits_zero(tmp_path):
    done, out = run_probe(tmp_path, "test_probe_budget_overrun.py")
    assert done.returncode == 0, out[-800:]
    assert "budget overrun warnings (not failures" in out
    line = next(ln for ln in out.splitlines() if ln.startswith("BUDGET OVERRUN (warning, not a failure):"))
    assert "tests/test_probe_budget_overrun.py::test_probe_exceeds_its_budget took 0." in line
    assert re.search(r"against a 0\.1 s quiet-state budget = [2-9]\.\dx the budget", line), line
    assert "Re-run on a quiet host; if the warning persists it is a real regression" in out
    logged = [json.loads(ln) for ln in (tmp_path / "overruns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(logged) == 1 and logged[0]["mode"] == "warn" and logged[0]["ratio_to_budget"] >= 2.0
    assert logged[0]["test"].endswith("::test_probe_exceeds_its_budget") and logged[0]["host"]


@pytest.mark.budget(5)
def test_strict_budgets_fails_the_same_overrun_by_name_and_still_logs_it(tmp_path):
    done, out = run_probe(tmp_path, "test_probe_budget_overrun.py", "--strict-budgets")
    assert done.returncode == 1, out[-800:]
    assert "GATE TEST TOO SLOW: tests/test_probe_budget_overrun.py::test_probe_exceeds_its_budget took 0." in out
    assert "budget 0.1s" in out and "BUDGET OVERRUN (warning" not in out
    [entry] = [json.loads(ln) for ln in (tmp_path / "overruns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert entry["mode"] == "strict"


@pytest.mark.budget(5)
def test_a_failure_during_a_clock_event_is_not_a_test_failure_and_the_run_is_marked_invalid(tmp_path):
    """The elapsed-time computation is monkeypatched (inside the probe) to return -267.8; the probe then raises the exact
    `TimeoutExpired ... -267.8` it was seen with. The runner must refuse to fail it."""
    done, out = run_probe(tmp_path, "test_probe_clock_event.py")
    summary = next(ln for ln in out.splitlines() if "INVALID RUN" in ln and "passed" in ln or "INVALID RUN" in ln and "skipped" in ln)
    assert re.match(r"^=* ?INVALID RUN \(clock event\): 1 environmental interruption\(s\), run_id=\d{8}T\d{6}Z-\d+", summary), summary
    assert "1 failed" not in out and "FAILED" not in out, "the probe is NOT reported as a failure"
    assert "1 skipped" in summary, "it is reported as an environmental interruption (a skip)"
    assert "ENVIRONMENTAL INTERRUPTIONS (run_id=" in out and "negative elapsed time (-267.8 s)" in out
    assert "INVALID for budget purposes" in out
    assert done.returncode == 1, "an invalid run is never green"
    assert not (tmp_path / "history.json").exists(), "and never recorded as evidence about speed"


def test_a_negative_elapsed_time_alone_turns_a_plain_failure_into_an_environmental_interruption():
    """No clock is patched here (the real detector is watching this very run): a window that STARTED in the future has a
    negative elapsed time by construction. The failure text says nothing about a clock."""
    item = NS(_clock_start=(time.time() + 1000, time.monotonic() + 1000), nodeid="tests/x.py::t", path=Path("tests/x.py"),
              location=("tests/x.py", 3, "t"), config=NS(_env_interruptions=[], _run_id="RID", _heartbeat=clock_guard.Heartbeat()))
    report = NS(when="call", failed=True, outcome="failed", longreprtext="AssertionError: nope", longrepr=None)
    conftest._inspect_call_report(item, report)
    assert report.outcome == "skipped", "refused to fail the test on an environmental event"
    [entry] = item.config._env_interruptions
    assert entry["outcome_was"] == "failed" and "negative elapsed time" in entry["reason"]
    assert report.longrepr[2].startswith("Skipped: environmental interruption: negative elapsed time") and "RID" in report.longrepr[2]


def test_a_passing_test_in_an_interrupted_window_stays_passed_but_the_run_is_still_invalid():
    item = NS(_clock_start=(time.time() + 1000, time.monotonic() + 1000), nodeid="tests/x.py::t", path=Path("tests/x.py"),
              location=("tests/x.py", 3, "t"), config=NS(_env_interruptions=[], _run_id="RID", _heartbeat=clock_guard.Heartbeat()))
    report = NS(when="call", failed=False, outcome="passed", longreprtext="", longrepr=None)
    conftest._inspect_call_report(item, report)
    assert report.outcome == "passed" and item.config._env_interruptions[0]["outcome_was"] == "passed"


def test_a_normal_window_is_not_an_interruption():
    item = NS(_clock_start=clock_guard.sample(), nodeid="tests/x.py::t", path=Path("tests/x.py"), location=("tests/x.py", 3, "t"),
              config=NS(_env_interruptions=[], _run_id="RID", _heartbeat=clock_guard.Heartbeat()))
    report = NS(when="call", failed=True, outcome="failed", longreprtext="AssertionError: a real failure", longrepr=None)
    conftest._inspect_call_report(item, report)
    assert report.outcome == "failed" and item.config._env_interruptions == [], "a real failure is still a failure"
