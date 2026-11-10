#!/usr/bin/env python
"""Measure, verify and record the time budgets of the heavy tests (CLAUDE.md D17).

Budgets are HOST-RELATIVE: a test that spawns pytest subprocesses is as fast as the machine's process creation. This
script is how a different host (CI, a laptop) re-derives them, and how any host checks today's headroom.

  python scripts/measure_budgets.py --runs 15       RE-DERIVE: run the full suite N times with per-step timing on, compute
                                                    median / p95 / max per budgeted test, recommend max(2*p95, median+2),
                                                    print it next to the CURRENT budget and flag any test whose recommendation
                                                    differs by more than 20% (the signal to update D17). With N >= 5 a one-line
                                                    summary is appended to .perf/budget_history.jsonl (RECORD).
  python scripts/measure_budgets.py --verify --allow-loaded
                                                    the same on a LOADED host: the suite runs with BUDGET_MULTIPLIER=3 (every
                                                    gate budget x3, so a loaded host does not fail the suite by name; the
                                                    slow-run capture still fires) and the report separates "over the quiet-state
                                                    budget (tolerated here)" from "over even the 3x allowance (a real problem)".
                                                    The default --verify stays strict.
  python scripts/measure_budgets.py --verify        VERIFY: run the suite ONCE; print every timed test's headroom, and for any
                                                    test above 75% of its budget: name, time, budget and how many standard
                                                    deviations above the recorded median (scripts/budget_baseline.json, the D17
                                                    table) it is. Makes the "needs a ~2.3x slowdown to fail" claim testable here.
  --from-log FILE                                   analyse an existing STEP_TIMING_LOG instead of running pytest.
  --emit-baseline                                   print the baseline JSON for scripts/budget_baseline.json to stdout.

THIS SCRIPT NEVER EDITS A TEST FILE, CLAUDE.md OR THE BASELINE. Its only writes are a temp log and
.perf/budget_history.jsonl. Changing a budget is a human decision (edit the `@pytest.mark.budget(N)` and the D17 table).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = Path(__file__).resolve().parent / "budget_baseline.json"
DEFAULT_PERF = ROOT / ".perf"
DEFAULT_BUDGET_S = 2.0   # tests without an explicit @pytest.mark.budget (the timed test_apply light tests)
NEAR = 0.75              # "near budget": above this fraction of the current budget
FLAG = 0.20              # recommendation differs from the current budget by more than this fraction
RECORD_MIN_RUNS = 5      # runs >= this are recorded in .perf/budget_history.jsonl
MIN_STDEV = 0.05         # a floor for the baseline's standard deviation (a test with no spread would divide by ~0)
SUSPEND_FACTOR = 3.0     # a run whose wall time is > this x the median run is excluded (a laptop that slept mid-run once
                         # produced a 47,090 s run and a recommended budget of 5,506 s)
LOADED_MULTIPLIER = 3.0  # --allow-loaded: budgets stay sized for a QUIET host; a loaded host gets this allowance, not new numbers
IMPLAUSIBLE = 10.0       # a single record above this x its own budget is dropped as a suspend/hang, not a measurement


def pct(values: list[float], p: float) -> float:
    """Linear interpolation (numpy's default): with 15 runs p95 sits at index 13.3, close to the max."""
    xs = sorted(values)
    pos = p * (len(xs) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def recommend(median: float, p95: float) -> float:
    """The D17 formula: max(2 * p95, median + 2 s), rounded UP to 0.1 s."""
    return math.ceil(max(2 * p95, median + 2.0) * 10) / 10


def load_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


# -- RE-DERIVE ------------------------------------------------------------------------------------------------------

def derive(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per budgeted test FUNCTION. A parametrized function has a single marker, so it takes the largest
    recommendation among its cases (the case is named in `worst`)."""
    per_item: dict[str, list[float]] = defaultdict(list)
    current: dict[str, float] = {}
    for r in records:
        if r["budget_s"] > DEFAULT_BUDGET_S:             # explicit budgets only: the tests this formula is for
            per_item[r["test"]].append(r["total_s"])
            current[r["test"].split("[")[0]] = r["budget_s"]
    rows: dict[str, dict[str, Any]] = {}
    for item, totals in per_item.items():
        func = item.split("[")[0]
        med, p95, mx = statistics.median(totals), pct(totals, 0.95), max(totals)
        rec = recommend(med, p95)
        if func not in rows or rec > rows[func]["recommended"]:
            rows[func] = {"test": func, "n": len(totals), "median": med, "p95": p95, "max": mx, "recommended": rec,
                          "worst": item.split("[", 1)[1][:-1] if "[" in item else None, "current": current[func]}
    for row in rows.values():
        row["delta"] = (row["recommended"] - row["current"]) / row["current"]
        row["flag"] = abs(row["delta"]) > FLAG
    return sorted(rows.values(), key=lambda r: -r["median"])


def table_hash(rows: list[dict[str, Any]]) -> str:
    """A short hash of the derived table (medians, p95s, maxes, recommendations to 2 decimals)."""
    flat = [[r["test"], round(r["median"], 2), round(r["p95"], 2), round(r["max"], 2), r["recommended"]] for r in rows]
    return hashlib.sha256(json.dumps(sorted(flat)).encode("utf-8")).hexdigest()[:12]


def format_table(rows: list[dict[str, Any]], runs: int) -> str:
    out = [f"{'test':64} {'runs':>4} {'median':>7} {'p95':>6} {'max':>6} {'max/p95':>7} {'current':>8} {'recommended':>11} {'delta':>7}  flag"]
    for r in rows:
        name = r["test"] + (f"[{r['worst']}]" if r["worst"] else "")
        out.append(f"{name[:64]:64} {r['n']:4} {r['median']:7.2f} {r['p95']:6.2f} {r['max']:6.2f} "
                   f"{r['max'] / r['p95']:7.2f} {r['current']:8.1f} {r['recommended']:11.1f} {r['delta']:+7.0%}  "
                   f"{'UPDATE D17: differs by more than 20%' if r['flag'] else ''}")
    flagged = sum(r["flag"] for r in rows)
    out.append(f"\nformula: budget = max(2 x p95, median + 2 s), rounded up to 0.1 s, from {runs} runs (nothing was edited).")
    out.append(f"{flagged} of {len(rows)} budgets differ from the recommendation by more than {FLAG:.0%}"
               + (": update the markers by hand and the D17 table, then re-run with --emit-baseline." if flagged else "."))
    return "\n".join(out)


def host_info() -> dict[str, Any]:
    return {"host": platform.node(), "cpu": f"{platform.processor() or platform.machine()} x{os.cpu_count()}",
            "os": platform.platform(), "python": platform.python_version()}


def record_history(perf_dir: Path, rows: list[dict[str, Any]], runs: int, suite_s: list[float],
                   notes: list[str] | None = None, runs_used: int | None = None) -> Path | None:
    """RECORD: one JSON line per derivation of >= RECORD_MIN_RUNS REQUESTED runs (`runs_used` = after exclusions)."""
    if runs < RECORD_MIN_RUNS or not rows:
        return None
    perf_dir = Path(perf_dir)
    perf_dir.mkdir(parents=True, exist_ok=True)
    line = {"date": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **host_info(), "runs": runs, "runs_used": runs_used if runs_used is not None else runs,
            "table_hash": table_hash(rows), "suite_median_s": round(statistics.median(suite_s), 1) if suite_s else None,
            "recommended": {r["test"]: r["recommended"] for r in rows},
            "flagged": [r["test"] for r in rows if r["flag"]], "notes": notes or []}
    path = perf_dir / "budget_history.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return path


# -- measurement hygiene ---------------------------------------------------------------------------------------------

def exclude_interrupted_runs(runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """`runs`: [{"wall": seconds, "records": [...]}, ...]. Needs >= 3 runs to judge; a run whose wall time is more than
    SUSPEND_FACTOR x the median of all runs is excluded WHOLE (its records are not evidence of normal speed)."""
    notes: list[str] = []
    valid = []
    for i, r in enumerate(runs, 1):  # pytest itself said the run is not valid evidence (a clock event, tests/conftest.py)
        if r.get("invalid"):
            notes.append(f"run {i} EXCLUDED: pytest reported INVALID RUN (clock event): {r['invalid']}")
        else:
            valid.append((i, r))
    if len(valid) < 3:
        return [r for _, r in valid], notes
    median = statistics.median(r["wall"] for _, r in valid)
    kept = []
    for i, r in valid:
        if r["wall"] > SUSPEND_FACTOR * median:
            notes.append(f"run {i} EXCLUDED: {r['wall']:.0f} s against a median run of {median:.0f} s "
                         f"(> {SUSPEND_FACTOR:g}x; the machine most likely slept or hung)")
        else:
            kept.append(r)
    return kept, notes


def drop_implausible(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    good = [r for r in records if r["total_s"] <= IMPLAUSIBLE * r["budget_s"]]
    return good, len(records) - len(good)


# -- VERIFY ---------------------------------------------------------------------------------------------------------

def load_baseline(path: Path) -> dict[str, dict[str, float]]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))["tests"]
    except (OSError, ValueError, KeyError):
        return {}


def verify(records: list[dict[str, Any]], baseline: dict[str, dict[str, float]],
           allow_loaded: bool = False) -> tuple[str, list[dict[str, Any]]]:
    """Headroom of every timed test today, and the near-budget ones with their distance from the recorded median.
    `allow_loaded`: the suite was run with budgets x3; additionally split the over-budget tests into "tolerated on a
    loaded host" (<= 3x the quiet-state budget) and "over even the loaded allowance"."""
    rows = []
    for r in records:
        base = baseline.get(r["test"])
        sigma = None
        if base:
            sigma = (r["total_s"] - base["median"]) / max(base["stdev"], MIN_STDEV)
        rows.append({"test": r["test"], "time": r["total_s"], "budget": r["budget_s"], "frac": r["total_s"] / r["budget_s"],
                     "headroom": r["budget_s"] / r["total_s"] if r["total_s"] else float("inf"), "sigma": sigma,
                     "median": base["median"] if base else None})
    rows.sort(key=lambda x: -x["frac"])
    near = [x for x in rows if x["frac"] > NEAR]
    out = [f"{'test':72} {'time':>7} {'budget':>7} {'used':>5} {'headroom':>9} {'sigma vs recorded median':>25}"]
    for x in rows:
        if x["budget"] <= DEFAULT_BUDGET_S and x["frac"] < 0.25:
            continue                                   # the many fast timed tests only add noise
        sg = f"{x['sigma']:+.1f} (median {x['median']:.2f}s)" if x["sigma"] is not None else "n/a (no baseline)"
        out.append(f"{x['test'][:72]:72} {x['time']:7.2f} {x['budget']:7.1f} {x['frac']:5.0%} {x['headroom']:8.1f}x {sg:>25}")
    out.append("")
    if near:
        out.append(f"NEAR BUDGET (above {NEAR:.0%} of the current budget): {len(near)}")
        for x in near:
            sg = (f"{x['sigma']:+.1f} standard deviations above the recorded median {x['median']:.2f} s (D17)"
                  if x["sigma"] is not None else "no recorded median for this test")
            out.append(f"  {x['test']}\n    time {x['time']:.2f} s of a {x['budget']:g} s budget ({x['frac']:.0%}); {sg}")
    else:
        out.append(f"No timed test is above {NEAR:.0%} of its budget. Smallest headroom today: "
                   f"{min((x['headroom'] for x in rows), default=float('nan')):.1f}x slowdown before the first failure.")
    if allow_loaded:
        over = [x for x in rows if x["frac"] > 1.0]
        beyond = [x for x in over if x["frac"] > LOADED_MULTIPLIER]
        tolerated = [x for x in over if x["frac"] <= LOADED_MULTIPLIER]
        out.append("")
        out.append(f"LOADED-HOST MODE: every gate budget was multiplied by {LOADED_MULTIPLIER:g} for this run "
                   "(BUDGET_MULTIPLIER), so the suite could not fail by name on a budget. Budgets are still sized for a "
                   "quiet host: on a normal host the tests below are bugs, on a loaded one they are expected.")
        if tolerated:
            out.append(f"  OVER THE QUIET-STATE BUDGET, within the {LOADED_MULTIPLIER:g}x allowance (tolerated here): {len(tolerated)}")
            for x in tolerated:
                out.append(f"    {x['test']}: {x['time']:.2f} s against a {x['budget']:g} s budget ({x['frac']:.1f}x)")
        if beyond:
            out.append(f"  OVER EVEN THE LOADED ALLOWANCE ({LOADED_MULTIPLIER:g}x): {len(beyond)}  <- a real problem on any host")
            for x in beyond:
                out.append(f"    {x['test']}: {x['time']:.2f} s against a {x['budget']:g} s budget ({x['frac']:.1f}x)")
        if not over:
            out.append("  No timed test exceeded its quiet-state budget in this run.")
    return "\n".join(out), near


# -- running the suite ----------------------------------------------------------------------------------------------

def run_suite(log: Path, extra: list[str], env_extra: dict[str, str] | None = None) -> tuple[int, float, str, list[str]]:
    """(exit code, wall seconds, summary line, the FAILED/ERROR lines of pytest's short summary)."""
    env = {**os.environ, "STEP_TIMING_LOG": str(log), **(env_extra or {})}
    if not (env_extra or {}).get("BUDGET_MULTIPLIER"):
        env.pop("BUDGET_MULTIPLIER", None)  # a strict run is strict even if the caller's shell exported a multiplier
    t0 = __import__("time").perf_counter()
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra], cwd=ROOT, env=env,
                          capture_output=True, text=True)
    lines = (done.stdout or "").splitlines()
    summary = next((ln for ln in reversed(lines) if " passed" in ln or " failed" in ln or "INVALID RUN" in ln), "")
    failures = [ln.strip() for ln in lines if ln.startswith(("FAILED ", "ERROR "))]
    return done.returncode, __import__("time").perf_counter() - t0, summary.strip(), failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--runs", type=int, help="RE-DERIVE: run the full suite this many times (default 15)")
    mode.add_argument("--verify", action="store_true", help="VERIFY: one run, headroom + near-budget report")
    ap.add_argument("--allow-loaded", action="store_true",
                    help=f"with --verify: run the suite with every gate budget x{LOADED_MULTIPLIER:g} (a loaded host); budgets "
                         "are NOT re-derived and the strict default is unchanged")
    ap.add_argument("--from-log", type=Path, help="analyse an existing STEP_TIMING_LOG instead of running pytest")
    ap.add_argument("--emit-baseline", action="store_true", help="print baseline JSON (stdout only, nothing is written)")
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--perf-dir", type=Path, default=DEFAULT_PERF)
    ap.add_argument("pytest_args", nargs="*", help="extra arguments passed to pytest (default: the whole suite)")
    args = ap.parse_args(argv)
    if args.allow_loaded and not args.verify:
        ap.error("--allow-loaded only applies to --verify (budgets are never re-derived on a loaded host)")

    suite_s: list[float] = []
    exit_codes: list[int] = []
    notes: list[str] = []
    if args.from_log:
        records = load_records(args.from_log)
        declared = args.runs if args.runs is not None else 0
        requested = declared
    else:
        runs = 1 if args.verify else (args.runs if args.runs is not None else 15)
        workdir = Path(tempfile.mkdtemp(prefix="measure_budgets_"))
        per_run: list[dict[str, Any]] = []
        for i in range(runs):
            log = workdir / f"run{i + 1}.jsonl"           # one log per run, so a bad run can be excluded whole
            env_extra = {"BUDGET_MULTIPLIER": f"{LOADED_MULTIPLIER:g}"} if args.allow_loaded else None
            strict = ["--strict-budgets"] if args.verify else []  # --verify is the strict check; --runs only measures
            code, secs, summary, failures = run_suite(log, [*strict, *args.pytest_args], env_extra)
            exit_codes.append(code)
            print(f"run {i + 1}/{runs}: {summary or 'no summary'} ({secs:.0f} s)", flush=True)
            for failure in failures:                      # say WHICH test: a bare "1 error" hides the evidence
                print(f"    {failure[:200]}", flush=True)
            per_run.append({"wall": secs, "records": load_records(log) if log.exists() else [],
                            "invalid": summary if "INVALID RUN" in summary else ""})
        kept, notes = exclude_interrupted_runs(per_run)
        suite_s = [r["wall"] for r in kept]
        records = [rec for r in kept for rec in r["records"]]
        declared = len(kept)
        requested = runs
    records, dropped = drop_implausible(records)
    if dropped:
        notes.append(f"{dropped} record(s) dropped as implausible (> {IMPLAUSIBLE:g}x their own budget)")
    if not records:
        print("no step-timing records were produced: is tests/conftest.py's timed_steps fixture active?")
        return 2
    for note in notes:
        print("NOTE:", note)

    if args.verify:
        text, near = verify(records, load_baseline(args.baseline), allow_loaded=args.allow_loaded)
        mode = f"LOADED-HOST (budgets x{LOADED_MULTIPLIER:g})" if args.allow_loaded else "STRICT"
        print(f"\nVERIFY [{mode}] on {host_info()['host']} ({host_info()['cpu']}), suite exit code {exit_codes[0] if exit_codes else 'n/a'}\n")
        print(text)
        return 0 if not exit_codes or exit_codes[0] == 0 else exit_codes[0]

    rows = derive(records)
    runs_seen = declared or max((r["n"] for r in rows), default=0)
    print("\n" + format_table(rows, runs_seen))
    if args.emit_baseline:
        per_item: dict[str, list[float]] = defaultdict(list)
        for r in records:
            if r["budget_s"] > DEFAULT_BUDGET_S:
                per_item[r["test"]].append(r["total_s"])
        print("\n" + json.dumps({"host": platform.node(), "date": dt.date.today().isoformat(), "runs": runs_seen, "tests": {
            t: {"median": round(statistics.median(v), 3), "stdev": round(statistics.stdev(v), 3) if len(v) > 1 else 0.0,
                "n": len(v)} for t, v in sorted(per_item.items())}}, indent=1))
    path = record_history(args.perf_dir, rows, requested, suite_s, notes, runs_used=runs_seen)
    print(f"\nrecorded in {path}" if path else f"\nnot recorded (needs --runs >= {RECORD_MIN_RUNS})")
    return 0 if all(c == 0 for c in exit_codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
