"""learning/apply.py: only human-approved, green proposals change specs/servers/tests, and only then is anything committed.

Every test builds a throwaway project (tmp Layout + git repo with a committed weather baseline). "Unchanged" is
proved by comparing the bytes of spec, server and test files before and after.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from generators.from_spec import ROOT, generate
from learning.apply import apply_approved, waiting
from learning.patchfmt import make_proposal, sha256_bytes
from pipeline_helpers import layout_in

# -- step timing (D17): which step of a test waits? ---------------------------------------------------------------
# Every subprocess call (git ...), the repo copy and the generator run are timed per test. A test that spends more than
# 75% of ITS OWN budget (1.5 s of the default 2 s; 22.5 s of an explicit 30 s) gets its breakdown appended to
# .perf/slow_apply_tests.txt, so the file stays empty unless something unusual happens; with APPLY_TIMING_LOG=<file>
# every test's breakdown is appended (JSON lines), which is how the 20-run experiment in D17 was done.
NEAR_BUDGET = 0.75


@pytest.fixture(autouse=True)
def step_timing(request, monkeypatch):
    rows: list[tuple[str, float]] = []
    me = sys.modules[__name__]

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

    def cmd_label(args):
        cmd = args[0] if args else ""
        if isinstance(cmd, list) and cmd[:1] == ["git"]:
            rest = [c for c in cmd[1:] if c not in ("-C",) and not str(c).startswith(("C:", "/"))]
            return "git " + " ".join(map(str, rest[:2]))
        return "subprocess " + (str(cmd[0]) if isinstance(cmd, list) and cmd else "?")

    monkeypatch.setattr(subprocess, "run", timed(cmd_label, subprocess.run))
    monkeypatch.setattr(shutil, "copytree", timed(lambda a: "copytree", shutil.copytree))
    monkeypatch.setattr(me, "generate", timed(lambda a: "generate", generate))
    t0 = time.perf_counter()
    yield rows
    total = time.perf_counter() - t0
    record = {"test": request.node.name, "total_s": round(total, 3), "steps": rows,
              "steps_s": round(sum(r[1] for r in rows), 3)}
    try:
        import conftest
        marker = request.node.get_closest_marker("budget")
        if total > NEAR_BUDGET * (float(marker.args[0]) if marker else 2.0):
            conftest.PERF_DIR.mkdir(parents=True, exist_ok=True)
            with open(conftest.PERF_DIR / "slow_apply_tests.txt", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + chr(10))
        if os.environ.get("APPLY_TIMING_LOG"):
            with open(os.environ["APPLY_TIMING_LOG"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + chr(10))
    except Exception:  # a diagnostic must never fail a test
        pass


NEW_RULE = ('      - {when: {status: 404}, code: NOT_FOUND, retryable: false, recovery: agent, '
            'meaning: "No weather data for these coordinates"}\n')
ANCHOR = 'meaning: "Coordinates were rejected by the weather service"}\n'


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    """One committed baseline project, copied (with its .git) for each test."""
    root = tmp_path_factory.mktemp("baseline")
    layout = layout_in(root)
    shutil.copyfile(ROOT / "specs" / "weather.yaml", layout.specs / "weather.yaml")
    generate(layout.specs / "weather.yaml", layout.servers, layout.tests)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "baseline"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)
    return root


@pytest.fixture
def project(baseline, tmp_path, monkeypatch):
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"), ("GIT_COMMITTER_NAME", "t"),
                 ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(k, v)
    root = tmp_path / "proj"
    shutil.copytree(baseline, root)
    layout = layout_in_existing(root)
    return layout


def layout_in_existing(root: Path):
    from pipeline import Layout
    (root / "inbox").mkdir(exist_ok=True)
    return Layout(root=root)


def snapshot(layout):
    files = [layout.specs / "weather.yaml", layout.servers / "weather_server.py", layout.tests / "test_weather.py"]
    return {p.name: (sha256_bytes(p.read_bytes()), p.stat().st_size) for p in files}


def commits(layout) -> int:
    return int(git(layout.root, "rev-list", "--count", "HEAD"))


def head(layout) -> str:
    """The commit HEAD points at, read straight from .git with NO subprocess: "no commit was made" is "HEAD did not
    move". (Each `git rev-list` spawn cost ~0.1-0.2 s, and the light tests called it twice; D17.)"""
    git_dir = layout.root / ".git"
    ref = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if ref.startswith("ref: "):
        loose = git_dir / ref[5:]
        if loose.exists():
            return loose.read_text(encoding="utf-8").strip()
        return git(layout.root, "rev-parse", "HEAD")  # packed ref: ask git (not the case after a fresh commit)
    return ref


def spec_text(layout) -> str:
    return (layout.specs / "weather.yaml").read_bytes().decode("utf-8").replace("\r\n", "\n")


def propose(layout, proposals: Path, where: str, new_text: str, *, kind="add_error_code", name=None, system="weather",
            spec_header=None, base=None, old_text=None) -> Path:
    raw = (layout.specs / "weather.yaml").read_bytes()
    old = old_text if old_text is not None else raw.decode("utf-8").replace("\r\n", "\n")
    header = {"proposal": kind, "system": system, "operation": "get_current_weather", "error_code": "NOT_FOUND",
              "recurrence": 3, "spec": spec_header or f"specs/{system}.yaml", "base_sha256": base or sha256_bytes(raw),
              "rationale": "seen in the failure log"}
    folder = proposals / where if where else proposals
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name or f"{system}__get_current_weather__NOT_FOUND__{kind}.patch")
    path.write_text(make_proposal(header, old, new_text), encoding="utf-8", newline="\n")
    return path


def with_rule(layout) -> str:
    old = spec_text(layout)
    assert ANCHOR in old
    return old.replace(ANCHOR, ANCHOR + NEW_RULE, 1)


def rejected_reason(proposals: Path, patch: Path) -> str:
    assert (proposals / "rejected" / patch.name).exists(), "rejected proposals move to rejected/"
    return (proposals / "rejected" / f"{patch.name}.reason.txt").read_text(encoding="utf-8")


# BUDGET: promotes through the pipeline's subprocess gates (staged + promoted, 2 pytest runs each): ~8-10 s normally; 30 s trips on a ~3x slowdown
@pytest.mark.budget(30)
def test_approved_green_proposal_is_applied_regenerated_and_committed(project, tmp_path):
    layout, proposals = project, tmp_path / "proposals"
    patch = propose(layout, proposals, "approved", with_rule(layout))
    before, sha_before = commits(layout), head(layout)

    [result] = apply_approved(proposals, layout)

    assert result.status == "applied", result.reason
    spec = yaml.safe_load(spec_text(layout))
    rules = {r["code"]: r for r in spec["operations"][0]["errors"]}
    assert rules["NOT_FOUND"]["when"] == {"status": 404}
    assert generate(layout.specs / "weather.yaml", layout.servers, layout.tests, check=True) == []
    assert "NOT_FOUND" in (layout.servers / "weather_server.py").read_text(encoding="utf-8")
    assert commits(layout) == before + 1 and head(layout) != sha_before, "exactly one new commit; HEAD moved"
    assert git(layout.root, "log", "-1", "--format=%s") == f"learn: apply {patch.name} to weather"
    assert git(layout.root, "status", "--porcelain", "--untracked-files=no", "--", "specs", "servers", "tests") == ""
    assert (proposals / "applied" / patch.name).exists() and not patch.exists()
    assert not (layout.inbox / ".staging" / "weather-learn").exists()
    changed = set(git(layout.root, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert {"specs/weather.yaml", "servers/weather_server.py", "tests/test_weather.py"} <= changed


@pytest.mark.parametrize("where", ["", "rejected"])
def test_unapproved_and_rejected_proposals_change_nothing(project, tmp_path, where):
    layout, proposals = project, tmp_path / "proposals"
    patch = propose(layout, proposals, where, with_rule(layout))
    before, count = snapshot(layout), head(layout)

    assert apply_approved(proposals, layout) == []

    assert snapshot(layout) == before and head(layout) == count
    assert patch.exists()
    assert not (proposals / "applied").exists()
    if not where:
        assert waiting(proposals) == [patch]
    assert not list((proposals / "rejected").glob("*.reason.txt")) if where else True


def test_mark_needs_review_proposal_is_rejected_per_d14(project, tmp_path):
    layout, proposals = project, tmp_path / "proposals"
    old = spec_text(layout)
    flagged = old.replace("  - name: get_current_weather\n", "  - name: get_current_weather\n    needs_review: true\n", 1)
    patch = propose(layout, proposals, "approved", flagged, kind="mark_needs_review")
    before, count = snapshot(layout), head(layout)

    [result] = apply_approved(proposals, layout)

    assert result.status == "rejected"
    reason = rejected_reason(proposals, patch)
    assert "needs_review" in reason and "D14" in reason and "by hand" in reason and "refuses" in reason
    assert snapshot(layout) == before and head(layout) == count
    assert not patch.exists() and not (proposals / "applied").exists()


def test_add_error_code_against_a_spec_with_a_needs_review_operation_is_rejected(project, tmp_path):
    layout, proposals = project, tmp_path / "proposals"
    # the spec on disk already has a flagged operation (as an ingestor would leave it)
    flagged = spec_text(layout).replace("  - name: get_daily_forecast\n",
                                        "  - name: get_daily_forecast\n    needs_review: true\n", 1)
    (layout.specs / "weather.yaml").write_text(flagged, encoding="utf-8", newline="\n")
    git(layout.root, "commit", "-qam", "spec flagged for review")
    patch = propose(layout, proposals, "approved", with_rule(layout))
    before, count = snapshot(layout), head(layout)

    [result] = apply_approved(proposals, layout)

    assert result.status == "rejected"
    reason = rejected_reason(proposals, patch)
    assert "needs_review" in reason and "get_daily_forecast" in reason and "D14" in reason
    assert snapshot(layout) == before and head(layout) == count


def test_stale_base_hash_is_rejected(project, tmp_path):
    layout, proposals = project, tmp_path / "proposals"
    patch = propose(layout, proposals, "approved", with_rule(layout), base="0" * 64)
    before, count = snapshot(layout), head(layout)

    [result] = apply_approved(proposals, layout)

    assert result.status == "rejected" and "stale" in rejected_reason(proposals, patch)
    assert snapshot(layout) == before and head(layout) == count


def test_a_diff_that_does_not_apply_is_rejected(project, tmp_path):
    layout, proposals = project, tmp_path / "proposals"
    # diff made against a different text than the spec on disk, but with the CORRECT base hash
    ANCHOR2 = ANCHOR.replace("rejected", "refused")
    other = spec_text(layout).replace(ANCHOR, ANCHOR2)
    patch = propose(layout, proposals, "approved", other.replace(ANCHOR2, ANCHOR2 + NEW_RULE, 1), old_text=other)
    before, count = snapshot(layout), head(layout)

    [result] = apply_approved(proposals, layout)

    assert result.status == "rejected" and "does not apply" in rejected_reason(proposals, patch)
    assert snapshot(layout) == before and head(layout) == count


@pytest.mark.parametrize("header_spec", ["../x.yaml", "specs/../x.yaml", "specs/other.yaml", "/etc/passwd"])
def test_path_escape_in_the_spec_header_is_rejected(project, tmp_path, header_spec):
    layout, proposals = project, tmp_path / "proposals"
    patch = propose(layout, proposals, "approved", with_rule(layout), spec_header=header_spec)
    before, count = snapshot(layout), head(layout)

    [result] = apply_approved(proposals, layout)

    assert result.status == "rejected" and "no other path" in rejected_reason(proposals, patch)
    assert snapshot(layout) == before and head(layout) == count
    assert not (layout.root.parent / "x.yaml").exists() and not (layout.specs / "x.yaml").exists()


def test_a_system_name_with_path_parts_is_rejected(project, tmp_path):
    layout, proposals = project, tmp_path / "proposals"
    patch = propose(layout, proposals, "approved", with_rule(layout), system="../weather", spec_header="specs/../weather.yaml",
                    name="escape.patch")
    [result] = apply_approved(proposals, layout)
    assert result.status == "rejected" and "plain system name" in rejected_reason(proposals, patch)


def test_missing_spec_is_rejected(project, tmp_path):
    layout, proposals = project, tmp_path / "proposals"
    patch = propose(layout, proposals, "approved", with_rule(layout), system="ghost")
    before, count = snapshot(layout), head(layout)
    [result] = apply_approved(proposals, layout)
    assert result.status == "rejected" and "does not exist" in rejected_reason(proposals, patch)
    assert snapshot(layout) == before and head(layout) == count


# BUDGET: staged verification runs the pipeline's subprocess gates (generated tests go red at the first one): ~4-5 s normally; 30 s trips on a ~6x slowdown
@pytest.mark.budget(30)
def test_red_verification_is_rejected_nothing_promoted_and_not_committed(project, tmp_path):
    layout, proposals = project, tmp_path / "proposals"
    # legitimate-looking type correction that breaks the spec's own examples: latitude 28.61 is no integer
    red = spec_text(layout).replace("- {name: latitude, type: number, in: query, required: true,\n         "
                                    'description: "Latitude in decimal degrees, -90 to 90 (e.g. 28.61 for Delhi).",',
                                    "- {name: latitude, type: integer, in: query, required: true,\n         "
                                    'description: "Latitude in decimal degrees, -90 to 90 (e.g. 28.61 for Delhi).",', 1)
    assert red != spec_text(layout)
    patch = propose(layout, proposals, "approved", red, kind="correct_param_type")
    before, count = snapshot(layout), head(layout)

    [result] = apply_approved(proposals, layout)

    assert result.status == "rejected", result.reason
    reason = rejected_reason(proposals, patch)
    assert "verification failed" in reason and "failed" in reason.lower()
    assert snapshot(layout) == before, "spec, server and tests must be byte-identical"
    assert head(layout) == count
    assert git(layout.root, "status", "--porcelain", "--untracked-files=no", "--", "specs", "servers", "tests") == ""
    assert not (proposals / "applied").exists()
    assert not (layout.inbox / ".staging" / "weather-learn").exists()


# -- restore after promotion: tested with an INJECTED failure, not assumed (D18) -------------------------

def _inject_second_verify(monkeypatch, layout, second):
    """pipeline._verify: the 1st call (staged) is the REAL gate, the 2nd (after promotion) is `second`."""
    import pipeline
    real, calls, seen = pipeline._verify, [], {}

    def verify(*args, **kw):
        calls.append(1)
        if len(calls) == 1:
            return real(*args, **kw)
        seen["spec_on_disk_during_second_verify"] = (layout.specs / "weather.yaml").read_text(encoding="utf-8")
        return second()

    monkeypatch.setattr(pipeline, "_verify", verify)
    return calls, seen


# BUDGET: the staged verification is the real subprocess gate (~4-5 s); the second verify is injected
@pytest.mark.budget(30)
def test_second_verify_red_restores_spec_server_tests_and_leaves_the_proposal_approved(project, tmp_path, monkeypatch):
    layout, proposals = project, tmp_path / "proposals"
    patch = propose(layout, proposals, "approved", with_rule(layout))
    before, count = snapshot(layout), head(layout)
    calls, seen = _inject_second_verify(monkeypatch, layout, lambda: (False, "INJECTED: contract red after promotion"))

    [result] = apply_approved(proposals, layout)

    assert len(calls) == 2, "the first verify ran for real, the second was the injected red one"
    assert "NOT_FOUND" in seen["spec_on_disk_during_second_verify"], "the spec WAS promoted before the red verify"
    assert result.status == "rolled_back" and "INJECTED" in result.reason
    assert snapshot(layout) == before, "spec, server and tests must be byte-identical to before the attempt"
    assert head(layout) == count, "no commit"
    assert git(layout.root, "status", "--porcelain", "--untracked-files=no", "--", "specs", "servers", "tests") == ""
    assert patch.exists() and (proposals / "approved" / patch.name) == patch, "the proposal must remain in approved/"
    assert not (proposals / "applied").exists() and not (proposals / "rejected").exists()
    assert "INJECTED" in (patch.with_name(patch.name + ".reason.txt")).read_text(encoding="utf-8")
    assert not (layout.inbox / ".staging" / "weather-learn").exists()


def test_second_verify_crash_also_restores_everything(project, tmp_path, monkeypatch):
    import pipeline
    layout, proposals = project, tmp_path / "proposals"
    patch = propose(layout, proposals, "approved", with_rule(layout))
    before, count = snapshot(layout), head(layout)
    calls = []

    def verify(*args, **kw):
        calls.append(1)
        if len(calls) == 1:
            return True, "staged ok (stubbed)"
        raise RuntimeError("injected crash in the second verify")

    monkeypatch.setattr(pipeline, "_verify", verify)
    [result] = apply_approved(proposals, layout)

    assert len(calls) == 2 and result.status == "rejected" and "injected crash" in result.reason
    assert snapshot(layout) == before and head(layout) == count
    assert git(layout.root, "status", "--porcelain", "--untracked-files=no", "--", "specs", "servers", "tests") == ""
    assert not (proposals / "applied").exists() and not patch.exists()
