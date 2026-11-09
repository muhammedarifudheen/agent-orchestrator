"""Slow-run capture (D17): synthetic runs prove the report is written on a slow run and NOT on a normal one, that the
median is kept, that partial runs are ignored, and that the capture can never fail or break a run."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import conftest
import perf_capture as pc
from perf_capture import record_run

N = 500  # a full-suite-sized run


def durs(n=60):
    """n fake per-test durations, descending ids, so the ranking is checkable."""
    return {(f"tests/test_x.py::test_{i:03d}", "call"): float(i) / 10 for i in range(n)}


def seed(perf_dir, totals, n=N):
    for t in totals:
        record_run(perf_dir, t, n, durs())


def test_a_slow_run_writes_the_durations_report(tmp_path):
    seed(tmp_path, [100, 110, 95, 105, 100])
    out = record_run(tmp_path, 250.0, N, durs(60))
    assert out["slow"] is True and out["median_before"] == 100
    text = (tmp_path / "last_slow_run.txt").read_text(encoding="utf-8")
    assert "SLOW RUN captured" in text and "total 250.0s over 500 tests = 2.5x the median 100.0s" in text
    rows = [ln for ln in text.splitlines() if "tests/test_x.py::" in ln]
    assert len(rows) == pc.TOP == 50, "a --durations=50 style list"
    assert rows[0].lstrip().startswith("5.90s") and "test_059" in rows[0], "slowest first"
    secs = [float(r.split("s ")[0]) for r in rows]
    assert secs == sorted(secs, reverse=True)


def test_a_normal_run_writes_no_report_and_leaves_an_earlier_one_alone(tmp_path):
    seed(tmp_path, [100, 110, 95, 105, 100])
    out = record_run(tmp_path, 190.0, N, durs())     # 1.9x: slower, but under the 2x threshold
    assert out["slow"] is False and out["report"] is None
    assert not (tmp_path / "last_slow_run.txt").exists()
    record_run(tmp_path, 300.0, N, durs())           # a slow one
    kept = (tmp_path / "last_slow_run.txt").read_text(encoding="utf-8")
    record_run(tmp_path, 105.0, N, durs())           # then a normal one: the evidence stays
    assert (tmp_path / "last_slow_run.txt").read_text(encoding="utf-8") == kept


def test_the_threshold_is_exactly_two_times_the_median(tmp_path):
    seed(tmp_path, [100] * 5)
    assert record_run(tmp_path, 200.0, N, durs())["slow"] is False   # not MORE than 2x
    seed(tmp_path / "b", [100] * 5)
    assert record_run(tmp_path / "b", 200.1, N, durs())["slow"] is True


def test_median_file_holds_the_median_of_the_last_five_runs_including_the_one_just_finished(tmp_path):
    for total, expected in [(100, 100), (110, 105), (90, 100), (120, 105), (105, 105), (300, 110)]:
        record_run(tmp_path, total, N, durs())
        assert float((tmp_path / "median.txt").read_text(encoding="utf-8")) == expected, total
    # last five are 110, 90, 120, 105, 300 -> median 110 (the 100 fell out of the window)


def test_too_little_history_gives_no_slow_verdict(tmp_path):
    seed(tmp_path, [100, 100])                       # only 2 prior runs: a median of 2 is noise
    out = record_run(tmp_path, 900.0, N, durs())
    assert out["slow"] is False and not (tmp_path / "last_slow_run.txt").exists()


def test_partial_runs_are_not_recorded(tmp_path):
    seed(tmp_path, [100, 100, 100])
    before = (tmp_path / "history.json").read_text(encoding="utf-8")
    out = record_run(tmp_path, 3.0, 12, durs(12))    # e.g. `pytest tests/test_x.py`
    assert out["recorded"] is False and "partial" in out["reason"]
    assert (tmp_path / "history.json").read_text(encoding="utf-8") == before
    assert float((tmp_path / "median.txt").read_text(encoding="utf-8")) == 100


def test_runs_of_a_very_different_size_are_not_compared(tmp_path):
    seed(tmp_path, [20, 20, 20, 20], n=100)          # a small suite's history
    assert record_run(tmp_path, 500.0, N, durs())["slow"] is False  # no comparable history for a 500-test run


def test_history_is_capped(tmp_path):
    seed(tmp_path, [100] * (pc.HISTORY_LIMIT + 7))
    assert len(json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))) == pc.HISTORY_LIMIT


def test_capture_never_raises_even_when_the_perf_dir_cannot_be_written(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    out = record_run(blocker / ".perf", 999.0, N, durs())            # mkdir under a FILE
    assert out["recorded"] is False and "error" in out


# -- the pytest hooks in conftest.py --------------------------------------------------------------

def run_hooks(monkeypatch, tmp_path, total, record=None):
    """Drive the real hooks with a fake clock: sessionstart at 0, sessionfinish at `total` seconds."""
    ticks = iter([0.0, total])
    # private hook state: the REAL run's durations (conftest._perf) must not be reset or polluted by this test
    monkeypatch.setattr(conftest, "_perf", {"t0": None, "durations": {}})
    monkeypatch.setattr(conftest, "_perf_clock", lambda: next(ticks))
    monkeypatch.setattr(conftest, "PERF_DIR", tmp_path)
    if record is not None:
        monkeypatch.setattr(pc, "record_run", record)
    conftest.pytest_sessionstart(None)
    for i in range(N):
        conftest.pytest_runtest_logreport(NS(nodeid=f"tests/t.py::t{i}", when="call", duration=i / 100))
    conftest.pytest_sessionfinish(None, 0)


def test_the_hooks_feed_the_real_total_and_per_test_durations_to_the_capture(monkeypatch, tmp_path):
    seen = {}
    run_hooks(monkeypatch, tmp_path, 321.0, record=lambda d, t, n, du: seen.update(dir=d, total=t, n=n, du=du))
    assert seen["total"] == 321.0 and seen["n"] == N and seen["dir"] == tmp_path
    assert seen["du"][("tests/t.py::t499", "call")] == 4.99


def test_hooks_write_a_report_on_a_slow_synthetic_run_and_not_on_a_normal_one(monkeypatch, tmp_path):
    seed(tmp_path, [100, 100, 100, 100, 100])
    run_hooks(monkeypatch, tmp_path, 120.0)          # normal
    assert not (tmp_path / "last_slow_run.txt").exists()
    run_hooks(monkeypatch, tmp_path, 260.0)          # slow
    text = (tmp_path / "last_slow_run.txt").read_text(encoding="utf-8")
    assert "total 260.0s" in text and "tests/t.py::t499" in text


def test_a_failing_capture_can_never_fail_the_run(monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    run_hooks(monkeypatch, tmp_path, 1.0, record=boom)  # must return normally
    assert "perf capture skipped: RuntimeError: disk on fire" in capsys.readouterr().out
