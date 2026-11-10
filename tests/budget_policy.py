"""What a time-budget overrun means (CLAUDE.md D17).

Budgets are sized for a QUIET host and their job is to catch a REGRESSION, not to absorb host load. So an overrun is data,
and what it DOES depends on an explicit choice, never on a guess about the host:

  default `pytest`            a WARNING naming the test and the observed multipliers; the run still passes (exit 0)
  `pytest --strict-budgets`   the same overrun FAILS that test by name (CI uses this; it is the gate that merges)

Either way the overrun is appended to `.perf/overruns.jsonl` (host, test, ratio to the quiet median, timestamp), so a pattern
across contributors' laptops is visible, and a real regression (an overrun that persists on a quiet host) is not lost.
"""
from __future__ import annotations

import json
import platform
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASELINE = Path(__file__).resolve().parent.parent / "scripts" / "budget_baseline.json"
_cache: dict[str, dict[str, Any]] | None = None


@dataclass
class Overrun:
    test: str
    elapsed_s: float
    budget_s: float
    ratio_to_budget: float
    quiet_median_s: float | None
    ratio_to_quiet_median: float | None


def quiet_baseline() -> dict[str, dict[str, Any]]:
    """Recorded quiet-state medians (scripts/budget_baseline.json, the D17 table). Empty when unreadable."""
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(BASELINE.read_text(encoding="utf-8"))["tests"]
        except (OSError, ValueError, KeyError):
            _cache = {}
    return _cache


def evaluate(test: str, elapsed: float, budget: float, baseline: dict[str, dict[str, Any]] | None = None) -> Overrun | None:
    """None when within budget; otherwise the overrun with both multipliers (None for a test with no recorded median)."""
    if elapsed <= budget:
        return None
    base = (baseline if baseline is not None else quiet_baseline()).get(test.rsplit("::", 1)[-1])
    median = base["median"] if base else None
    return Overrun(test=test, elapsed_s=round(elapsed, 3), budget_s=budget, ratio_to_budget=round(elapsed / budget, 2),
                   quiet_median_s=median, ratio_to_quiet_median=round(elapsed / median, 2) if median else None)


def warning_text(o: Overrun) -> str:
    median = (f", {o.ratio_to_quiet_median:.1f}x its quiet-state median of {o.quiet_median_s:.2f} s"
              if o.ratio_to_quiet_median else "")
    return (f"BUDGET OVERRUN (warning, not a failure): {o.test} took {o.elapsed_s:.1f} s against a {o.budget_s:g} s "
            f"quiet-state budget = {o.ratio_to_budget:.1f}x the budget{median}")


ADVICE = ("Re-run on a quiet host; if the warning persists it is a real regression. CI runs with --strict-budgets and "
          "fails on it (CLAUDE.md D17). Every overrun is logged in .perf/overruns.jsonl.")


def log(perf_dir: Path, o: Overrun, mode: str, run_id: str | None = None, multiplier: float = 1.0) -> Path:
    """Append one JSON line. `mode` is "warn" (default run) or "strict" (--strict-budgets)."""
    perf_dir = Path(perf_dir)
    perf_dir.mkdir(parents=True, exist_ok=True)
    line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "host": platform.node(), "mode": mode,
            "run_id": run_id, "budget_multiplier": multiplier, **asdict(o)}
    path = perf_dir / "overruns.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return path
