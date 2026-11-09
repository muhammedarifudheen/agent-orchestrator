"""Automatic slow-run capture (D17). A DIAGNOSTIC, never a gate: nothing here can fail or slow a test run.

After every run of at least MIN_TESTS tests (partial runs are ignored) the suite's wall time is compared with the
median of the last HISTORY_KEEP comparable runs (comparable = a similar number of tests). If it exceeds SLOW_FACTOR x
that median, a `--durations=50`-style report is written to `<perf_dir>/last_slow_run.txt`, so the NEXT mystery slowdown
arrives with per-test data instead of a guess. The median of the last 5 runs is kept in `<perf_dir>/median.txt`.

Files (repo root `.perf/`, git-ignored):
    history.json        the last HISTORY_LIMIT runs: {ts, total_s, n_tests}
    median.txt          median wall time (s) of the last 5 comparable runs, including the one just finished
    last_slow_run.txt   the most recent slow run's report (kept until the next slow run overwrites it)
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HISTORY_KEEP = 5       # runs that define "normal"
HISTORY_LIMIT = 20     # runs remembered in history.json
MIN_HISTORY = 2        # a median needs 2 runs (their mean); a false positive costs only a report file, a miss costs the evidence
MIN_TESTS = 50         # smaller runs (one file, one -k) are not recorded: they would drag the median around
# Also not recorded: a run smaller than COMPARABLE[0] x the LARGEST run ever recorded. 50 tests is not "the suite": a
# 71-test run of one directory passed the 50 rule and flipped median.txt to 0.6 s, which is how this rule was found.
SLOW_FACTOR = 2.0
TOP = 50
COMPARABLE = (0.8, 1.25)  # a run is comparable when its test count is within this factor of the current one


def _comparable(history: list[dict[str, Any]], n_tests: int) -> list[dict[str, Any]]:
    lo, hi = COMPARABLE[0] * n_tests, COMPARABLE[1] * n_tests
    return [h for h in history if lo <= h["n_tests"] <= hi]


def _report(total_s: float, median_s: float, n_tests: int, durations: dict[tuple[str, str], float], when: str) -> str:
    lines = [f"SLOW RUN captured {when}",
             f"total {total_s:.1f}s over {n_tests} tests = {total_s / median_s:.1f}x the median {median_s:.1f}s "
             f"of the last {HISTORY_KEEP} comparable runs (threshold {SLOW_FACTOR:g}x)",
             "", f"slowest {TOP} durations (seconds, phase, test):"]
    ranked = sorted(durations.items(), key=lambda kv: -kv[1])[:TOP]
    lines += [f"{secs:9.2f}s {phase:<8} {nodeid}" for (nodeid, phase), secs in ranked]
    return "\n".join(lines) + "\n"


def record_run(perf_dir: Path, total_s: float, n_tests: int, durations: dict[tuple[str, str], float],
               now: datetime | None = None) -> dict[str, Any]:
    """Record one finished run. NEVER raises: any problem comes back as {"error": ...}."""
    try:
        if n_tests < MIN_TESTS:
            return {"recorded": False, "reason": f"partial run ({n_tests} < {MIN_TESTS} tests)"}
        when = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        perf_dir = Path(perf_dir)
        perf_dir.mkdir(parents=True, exist_ok=True)
        hist_path = perf_dir / "history.json"
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            history = []
        biggest = max((h["n_tests"] for h in history), default=0)
        if n_tests < COMPARABLE[0] * biggest:
            return {"recorded": False,
                    "reason": f"partial run ({n_tests} tests; the suite's usual size is {biggest})"}

        recent = _comparable(history, n_tests)[-HISTORY_KEEP:]
        median_before = statistics.median(h["total_s"] for h in recent) if len(recent) >= MIN_HISTORY else None
        slow = median_before is not None and total_s > SLOW_FACTOR * median_before
        report = None
        if slow:
            report = perf_dir / "last_slow_run.txt"
            report.write_text(_report(total_s, median_before, n_tests, durations, when), encoding="utf-8")

        history.append({"ts": when, "total_s": round(total_s, 2), "n_tests": n_tests})
        history = history[-HISTORY_LIMIT:]
        hist_path.write_text(json.dumps(history, indent=1) + "\n", encoding="utf-8")
        last5 = _comparable(history, n_tests)[-HISTORY_KEEP:]
        median_now = statistics.median(h["total_s"] for h in last5)
        (perf_dir / "median.txt").write_text(f"{median_now:.1f}\n", encoding="utf-8")
        return {"recorded": True, "slow": slow, "median_before": median_before, "median": median_now,
                "report": report}
    except Exception as exc:  # a diagnostic must never break the run it observes
        return {"recorded": False, "error": f"{type(exc).__name__}: {exc}"}
