"""Per-step timing for the tests that carry a time budget (D17): which step of a test waits?

Active (via the `timed_steps` fixture in conftest.py) for every test with a `@pytest.mark.budget(N)` and for the
whole of tests/learning/test_apply.py. It times every subprocess call (git, and the pytest subprocesses the pipeline's
gates start, numbered in the order they run), the repo copy, and every call of the generator. A test that spends more
than 75% of ITS OWN budget has its breakdown appended to `.perf/slow_tests.txt`; with STEP_TIMING_LOG=<file> every test's
breakdown is appended as JSON lines (how the measured budgets in D17 were obtained). A diagnostic only: it can never
fail a test.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

NEAR_BUDGET = 0.75
DEFAULT_BUDGET_S = 2.0


def _label_subprocess(counter: list[int]):
    def label(args) -> str:
        cmd = args[0] if args else ""
        if isinstance(cmd, list) and cmd[:1] == ["git"]:
            rest = [c for c in cmd[1:] if c != "-C" and not str(c).startswith(("C:", "/"))]
            return "git " + " ".join(map(str, rest[:2]))
        if isinstance(cmd, list) and "pytest" in cmd:
            counter[0] += 1
            files = [Path(str(c).split("::")[0]).name for c in cmd if ".py" in str(c) and not str(c).endswith("pytest.ini")]
            return f"pytest#{counter[0]} {' '.join(files) or '?'}"
        return "subprocess " + (Path(str(cmd[0])).name if isinstance(cmd, list) and cmd else "?")
    return label


def install(monkeypatch) -> list[tuple[str, float]]:
    """Wrap subprocess.run, shutil.copytree and every module-level reference to the generator; returns the rows list."""
    rows: list[tuple[str, float]] = []

    def timed(label, fn):
        depth = [0]  # shutil.copytree calls itself for every sub-directory: record only the outermost call

        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            depth[0] += 1
            try:
                return fn(*args, **kwargs)
            finally:
                depth[0] -= 1
                if depth[0] == 0:
                    rows.append((label(args), round(time.perf_counter() - t0, 3)))
        return wrapper

    monkeypatch.setattr(subprocess, "run", timed(_label_subprocess([0]), subprocess.run))
    monkeypatch.setattr(shutil, "copytree", timed(lambda a: "copytree", shutil.copytree))
    from generators import from_spec
    original = from_spec.generate
    wrapped = timed(lambda a: "generate", original)
    for module in list(sys.modules.values()):  # the test module, pipeline, the generator itself: whoever holds the name
        if getattr(module, "generate", None) is original:
            monkeypatch.setattr(module, "generate", wrapped)
    return rows


def finish(request, rows: list[tuple[str, float]], total: float, perf_dir: Path) -> None:
    try:
        marker = request.node.get_closest_marker("budget")
        budget = float(marker.args[0]) if marker else DEFAULT_BUDGET_S
        record = {"test": request.node.name, "total_s": round(total, 3), "budget_s": budget, "steps": rows,
                  "steps_s": round(sum(r[1] for r in rows), 3)}
        if total > NEAR_BUDGET * budget:
            perf_dir.mkdir(parents=True, exist_ok=True)
            with open(perf_dir / "slow_tests.txt", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + chr(10))
        log = os.environ.get("STEP_TIMING_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + chr(10))
    except Exception:  # a diagnostic must never fail a test
        pass
