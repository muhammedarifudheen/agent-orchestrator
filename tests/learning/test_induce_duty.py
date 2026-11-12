"""Phase C: duty induction (learning/induce_duty.py) and the approval flow for duty proposals (learning/apply.py), CLAUDE.md D23.

Three kinds of test:
  * `flow` (module fixture): ONE real end-to-end run. Three identical planner runs go through the real Orchestrator and the
    real in-memory servers with a scripted model; induction proposes a duty; a person approves it in a copy of the repo;
    apply installs it (the generated contract test really runs, twice, in pytest subprocesses) and commits; a fresh
    orchestrator over the installed duties then serves the same request WITHOUT consulting the model.
  * synthetic run records (fast): every edge of induction (evidence rules, binding, skips, idempotence).
  * apply's decisions with the contract-test runner stubbed (`apply._run_contract_test`), where only the decision matters.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

import pipeline
from agent.duties import DutyLibrary, load_duty_file
from agent.orchestrator import Orchestrator
from duty_helpers import ROOT, SERVERS, ScriptedLLM, reset_worlds
from inmem import SpyRegistry
from learning import apply as apply_mod
from learning import induce_duty
from learning.apply import apply_approved_duties
from learning.induce_duty import MIN_RUNS, UNREVIEWED, contract_test_source, induce

import travel_server

REAL_DUTIES = ROOT / "duties"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
PROPOSAL_SUFFIXES = (".duty.yaml", ".duty.diff", ".contract_test.py", ".evidence.json")


def future(days: int = 30) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def goal_for(origin="DEL", destination="BOM", on=None, name="Asha Verma", email="asha@example.com") -> str:
    """A plain sentence naming a route, a date and a passenger. It deliberately does NOT fit the shipped book_flight duty."""
    return f"get {name} onto a flight from {origin} to {destination} on {on or future()} and email {email}"


def tree_hash(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


# -- real runs (the orchestrator, the in-memory servers, a scripted model) -------------------------------------------------

def plan_for(v: dict):
    """The scripted model's calls for one booking: the flight id and the booking reference come from earlier RESULTS."""
    return [
        [("call", "travel__search_flights", {"origin": v["origin"], "destination": v["destination"], "date": v["date"]})],
        [("call", "travel__book_flight", lambda r: {"flight_id": r[0][0]["flight_id"], "passenger_name": v["name"],
                                                     "passenger_email": v["email"]})],
        [("call", "travel__send_confirmation", lambda r: {"email": v["email"], "booking_ref": r[1]["booking_ref"],
                                                           "details": "Your flight booking is confirmed."})],
        [("text", "Booked and confirmed.")]]


def variant(**over) -> dict:
    v = {"origin": "DEL", "destination": "BOM", "date": future(), "name": "Asha Verma", "email": "asha@example.com"}
    v.update(over)
    v.setdefault("goal", goal_for(v["origin"], v["destination"], v["date"], v["name"], v["email"]))
    return v


async def drive(tmp: Path, runs_dir: Path, variants: list[dict]) -> list:
    """Run each variant through the REAL Orchestrator on the planner path (duties=None) and record the runs."""
    results = []
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        for v in variants:
            llm = ScriptedLLM(*plan_for(v))
            orch = Orchestrator(reg, client=llm, handoff_dir=tmp / "handoffs", user_id="tester", duties=None,
                                run_log_dir=runs_dir)
            results.append(await orch.run(v["goal"]))
    return results


# -- a copy of the repo that is a git project (like tests/learning/test_apply.py's `project`) ------------------------------

IGNORE = shutil.ignore_patterns(".venv", ".git", "__pycache__", ".perf", "handoffs", "inbox", "proposals", "runs", "*.pyc",
                                ".pytest_cache")


@pytest.fixture(scope="module", autouse=True)
def git_env():
    mp = pytest.MonkeyPatch()
    for k, v in GIT_ENV.items():
        mp.setenv(k, v)
    yield
    mp.undo()


def _commit_baseline(root: Path) -> None:
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "baseline"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    """The whole repo, committed: what the generated contract test needs to run (agent/, servers/, tests/, duties/)."""
    root = tmp_path_factory.mktemp("baseline") / "repo"
    shutil.copytree(ROOT, root, ignore=IGNORE)
    _commit_baseline(root)
    return root


@pytest.fixture(scope="module")
def light_baseline(tmp_path_factory):
    """A tiny committed project: only duties/schema.json and an empty tests/ (for apply decisions with a stubbed runner)."""
    root = tmp_path_factory.mktemp("light") / "repo"
    (root / "duties").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "inbox").mkdir()
    shutil.copyfile(REAL_DUTIES / "schema.json", root / "duties" / "schema.json")
    (root / "tests" / "keep.txt").write_text("x\n")
    (root / "inbox" / ".gitkeep").write_text("")
    _commit_baseline(root)
    return root


def _copy(baseline_dir: Path, tmp_path: Path) -> pipeline.Layout:
    root = tmp_path / "proj"
    shutil.copytree(baseline_dir, root)
    return pipeline.Layout(root=root)


@pytest.fixture
def light(light_baseline, tmp_path):
    return _copy(light_baseline, tmp_path)


@pytest.fixture
def project(baseline, tmp_path):
    return _copy(baseline, tmp_path)


def approve(proposals: Path, name: str, reviewer: str = "Ada Lovelace") -> Path:
    """What a person does: put their name in the duty and move it into approved/."""
    pending = proposals / "duties" / f"{name}.duty.yaml"
    data = yaml.safe_load(pending.read_text(encoding="utf-8"))
    data["review"]["reviewer"] = reviewer
    target = proposals / "duties" / "approved" / f"{name}.duty.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, width=1000), encoding="utf-8")
    pending.unlink()
    return target


# -- THE end-to-end flow, once ----------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def flow(tmp_path_factory, baseline):
    work = tmp_path_factory.mktemp("flow")
    runs, proposals = work / "runs", work / "proposals"
    goal = goal_for()
    reset_worlds()
    try:
        results = asyncio.run(drive(work, runs, [variant(), variant(), variant()]))
        before = (tree_hash(REAL_DUTIES), tree_hash(ROOT / "tests"))
        outcomes = induce(runs, proposals, REAL_DUTIES)
        after = (tree_hash(REAL_DUTIES), tree_hash(ROOT / "tests"))

        root = work / "proj"
        shutil.copytree(baseline, root)
        layout = pipeline.Layout(root=root)
        name = next(o.name for o in outcomes if o.status == "proposed")
        installed_before = (root / "duties" / f"{name}.yaml").exists()
        proposed_text = (proposals / "duties" / f"{name}.duty.yaml").read_text(encoding="utf-8")
        pending = {p.name: p.read_text(encoding="utf-8") for p in (proposals / "duties").iterdir() if p.is_file()}
        files_pending = sorted(pending)
        approve(proposals, name)
        commits_before = int(git(root, "rev-list", "--count", "HEAD"))
        applied = apply_approved_duties(proposals, layout)
        commits_after = int(git(root, "rev-list", "--count", "HEAD"))

        # the NEXT identical intent: a fresh orchestrator over the installed duties, a model that must not be asked
        reset_worlds()
        llm = ScriptedLLM()

        async def again():
            async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
                orch = Orchestrator(reg, client=llm, handoff_dir=work / "h2", user_id="tester",
                                    duties=DutyLibrary(root / "duties"))
                return await orch.run(goal), list(reg.calls)

        second, calls = asyncio.run(again())
        bookings = [b for b in travel_server._BOOKINGS.values() if b["status"] == "CONFIRMED"]
    finally:
        reset_worlds()
    return NS(work=work, runs=runs, proposals=proposals, layout=layout, root=root, name=name, goal=goal, results=results,
              outcomes=outcomes, before=before, after=after, installed_before=installed_before, proposed_text=proposed_text,
              files_pending=files_pending, pending=pending, applied=applied, commits_before=commits_before, commits_after=commits_after,
              second=second, llm=llm, calls=calls, bookings=bookings)


# (a) ---------------------------------------------------------------------------------------------------------------------

def test_three_identical_successful_planner_runs_induce_exactly_one_proposal_with_four_files(flow):
    assert all(r.success and r.duty_used is None for r in flow.results), "the runs must be planner runs"
    assert len(list(flow.runs.glob("*.json"))) == 3
    assert len([o for o in flow.outcomes if o.status == "proposed"]) == 1
    assert flow.files_pending == sorted(f"{flow.name}{s}" for s in PROPOSAL_SUFFIXES)
    assert flow.pending[f"{flow.name}.contract_test.py"] == contract_test_source(flow.name)
    evidence = json.loads(flow.pending[f"{flow.name}.evidence.json"])
    assert len(evidence["runs"]) == 3 and evidence["goals"] == [flow.goal] * 3
    assert evidence["tool_sequence"] == ["travel__search_flights", "travel__book_flight", "travel__send_confirmation"]


def test_the_proposed_duty_validates_loads_and_matches_its_own_goal_at_full_confidence(flow, tmp_path):
    lib_dir = tmp_path / "duties"
    lib_dir.mkdir()
    shutil.copyfile(REAL_DUTIES / "schema.json", lib_dir / "schema.json")
    (lib_dir / f"{flow.name}.yaml").write_text(flow.proposed_text, encoding="utf-8")
    duty = load_duty_file(lib_dir / f"{flow.name}.yaml")  # schema + cross-field rules
    data = yaml.safe_load(flow.proposed_text)
    assert data["review"] == {"status": "reviewed", "reviewer": UNREVIEWED}
    assert data["write_boundary"] == {"before": "book_flight"} and data["success"] == {"name": "send_confirmation_succeeded", "check": "$send_confirmation", "equals": True}
    assert [s.args.get("flight_id") for s in duty.steps][1] == "$search_flights.0.flight_id"
    match, ambiguous = DutyLibrary(lib_dir).best_match(flow.goal)
    assert match is not None and match.confidence == 1.0 and match.duty.name == flow.name and not ambiguous


def test_the_diff_is_a_new_file_and_names_the_runs(flow):
    diff = flow.pending[f"{flow.name}.duty.diff"]
    lines = diff.splitlines()
    assert lines[0].startswith("# proposed from 3 runs: ") and all(r.name in lines[0] for r in flow.runs.glob("*.json"))
    assert f"+++ duties/{flow.name}.yaml" in lines and "--- /dev/null" in lines
    assert all(l.startswith("+") for l in lines[4:] if not l.startswith("@@"))


# (b) ---------------------------------------------------------------------------------------------------------------------

def test_induction_changes_nothing_in_duties_or_tests_and_installs_nothing(flow):
    assert flow.before[0] == flow.after[0], "induction wrote into duties/"
    assert flow.before[1] == flow.after[1], "induction wrote into tests/"
    assert not flow.installed_before and not (ROOT / "duties" / f"{flow.name}.yaml").exists()
    assert not (ROOT / "tests" / "duties" / f"test_duty_{flow.name}.py").exists()


# (c) ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.budget(30)
def test_an_approved_proposal_is_installed_with_its_test_in_one_commit_and_the_test_passes_alone(flow):
    [result] = flow.applied
    assert result.status == "applied", result.reason
    root, name = flow.root, flow.name
    assert (root / "duties" / f"{name}.yaml").is_file()
    generated = root / "tests" / "duties" / f"test_duty_{name}.py"
    assert generated.read_text(encoding="utf-8") == contract_test_source(name)
    assert generated.read_text(encoding="utf-8").startswith(
        f"# GENERATED by learning/induce_duty.py for duties/{name}.yaml: DO NOT EDIT (CLAUDE.md D23)\n")
    assert flow.commits_after == flow.commits_before + 1
    assert git(root, "log", "-1", "--format=%s") == f"learn: install duty {name} (reviewed by Ada Lovelace)"
    changed = set(git(root, "show", "--name-status", "--format=", "HEAD").splitlines())
    assert {l.split("\t")[1] for l in changed} >= {f"duties/{name}.yaml", f"tests/duties/test_duty_{name}.py"}
    assert git(root, "status", "--porcelain", "--untracked-files=all", "--", "duties", "tests/duties", "proposals").replace(
        "?? ", "") .count("\n") == 0 or "__pycache__" in git(root, "status", "--porcelain", "--untracked-files=all")
    applied_dir = flow.proposals / "duties" / "applied"
    assert sorted(p.name for p in applied_dir.iterdir()) == sorted(f"{name}{s}" for s in PROPOSAL_SUFFIXES)
    assert not [p for p in (flow.proposals / "duties").iterdir() if p.is_file()]
    # the generated test passes when run on its own, in that project
    env = {k: v for k, v in os.environ.items() if k != "DUTIES_DIR"}
    done = subprocess.run([sys.executable, "-m", "pytest", str(generated), "-q", "-p", "no:cacheprovider"], cwd=root,
                          env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-1000:]


# (d) ---------------------------------------------------------------------------------------------------------------------

def test_the_next_identical_intent_is_served_by_the_installed_duty_without_asking_the_model(flow):
    assert flow.second.success and flow.second.duty_used == flow.name, flow.second.final_message
    assert flow.llm.calls == 0, "the model was consulted although the installed duty matched"
    assert flow.calls == ["travel__search_flights", "travel__book_flight", "travel__send_confirmation"]
    assert len(flow.bookings) == 1 and flow.bookings[0]["passenger_email"] == "asha@example.com"


# -- synthetic run records -----------------------------------------------------------------------------------------------

def flight_ops(origin="DEL", dest="BOM", on="2031-01-10", name="Asha Verma", email="asha@example.com",
               details="Your flight booking is confirmed.", ref="BK-1", book_ok=True, status="CONFIRMED") -> list[dict]:
    fid = f"6E-2041_{origin}{dest}_{on}"
    flights = [{"flight_id": fid, "airline": "IndiGo", "seats_available": 9},
               {"flight_id": f"AI-865_{origin}{dest}_{on}", "airline": "Air India", "seats_available": 4}]
    return [
        {"tool": "travel__search_flights", "arguments": {"origin": origin, "destination": dest, "date": on}, "ok": True,
         "result": flights, "read_only": True},
        {"tool": "travel__book_flight", "arguments": {"flight_id": fid, "passenger_name": name, "passenger_email": email},
         "ok": book_ok, "result": {"booking_ref": ref, "status": status, "passenger_email": email}, "read_only": False},
        {"tool": "travel__send_confirmation", "arguments": {"email": email, "booking_ref": ref, "details": details}, "ok": True,
         "result": True, "read_only": False}]


def write_run(runs: Path, index: int, goal: str, ops: list[dict], **over) -> str:
    runs.mkdir(parents=True, exist_ok=True)
    record = {"goal": goal, "success": True, "user": "t", "duty_used": None, "duty_contract_failed": None,
              "created_at": "2031-01-01T00:00:00+00:00", "operations": ops}
    record.update(over)
    name = f"20310101_0000{index:02d}_t.json"
    (runs / name).write_text(json.dumps(record), encoding="utf-8")
    return name


def three_identical(runs: Path, **kw) -> None:
    for i in range(3):
        write_run(runs, i, goal_for(on="2031-01-10"), flight_ops(ref=f"BK-{i}", **kw))


@pytest.fixture
def world(tmp_path):
    duties = tmp_path / "duties"
    duties.mkdir()
    shutil.copyfile(REAL_DUTIES / "schema.json", duties / "schema.json")
    for f in REAL_DUTIES.glob("*.yaml"):
        shutil.copyfile(f, duties / f.name)
    return NS(runs=tmp_path / "runs", proposals=tmp_path / "proposals", duties=duties, tmp=tmp_path)


def proposal_files(world) -> list[str]:
    folder = world.proposals / "duties"
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []


def proposed(outcomes):
    return [o for o in outcomes if o.status == "proposed"]


def test_min_runs_agrees_with_claude_md_d23():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    recorded = re.search(r"MIN_RUNS = (\d+)", text)
    assert recorded, "CLAUDE.md D23 must state `MIN_RUNS = <number>`"
    assert int(recorded.group(1)) == MIN_RUNS, "edit CLAUDE.md D23 and learning/induce_duty.py in the same commit"


def test_fewer_than_min_runs_produce_nothing(world):
    for i in range(MIN_RUNS - 1):
        write_run(world.runs, i, goal_for(on="2031-01-10"), flight_ops(ref=f"BK-{i}"))
    outcomes = induce(world.runs, world.proposals, world.duties)
    assert proposed(outcomes) == [] and proposal_files(world) == []
    assert outcomes[0].reason == f"skipped: {MIN_RUNS - 1} of {MIN_RUNS} runs"


def test_two_identical_runs_and_one_with_another_sequence_produce_nothing(world):
    for i in range(2):
        write_run(world.runs, i, goal_for(on="2031-01-10"), flight_ops(ref=f"BK-{i}"))
    write_run(world.runs, 2, goal_for(on="2031-01-10"), flight_ops(ref="BK-2")[:2])  # no confirmation: another sequence
    assert proposed(induce(world.runs, world.proposals, world.duties)) == [] and proposal_files(world) == []


def test_a_run_with_a_failed_operation_is_not_evidence(world):
    write_run(world.runs, 0, goal_for(on="2031-01-10"), flight_ops(ref="BK-0"))
    write_run(world.runs, 1, goal_for(on="2031-01-10"), flight_ops(ref="BK-1"))
    write_run(world.runs, 2, goal_for(on="2031-01-10"), flight_ops(ref="BK-2", book_ok=False))
    outcomes = induce(world.runs, world.proposals, world.duties)
    assert proposed(outcomes) == [] and outcomes[0].reason == f"skipped: 2 of {MIN_RUNS} runs"


def test_a_run_completed_by_a_duty_is_not_evidence(world):
    for i in range(3):
        write_run(world.runs, i, goal_for(on="2031-01-10"), flight_ops(ref=f"BK-{i}"), duty_used="book_flight" if i == 2 else None)
    assert proposed(induce(world.runs, world.proposals, world.duties)) == []
    write_run(world.runs, 5, goal_for(on="2031-01-10"), flight_ops(ref="BK-5"), success=False)  # nor is an unsuccessful one
    assert proposed(induce(world.runs, world.proposals, world.duties)) == []


def test_variation_in_city_date_name_and_email_becomes_placeholders_and_the_duty_matches_a_fourth_goal(world):
    cases = [("DEL", "BOM", "2031-01-10", "Asha Verma", "asha@example.com"),
             ("BLR", "GOI", "2031-02-11", "Ravi Kumar Singh", "ravi.k@mail.test"),
             ("MAA", "HYD", "2031-03-12", "Meera Iyer", "meera@corp.test")]
    for i, (o, d, on, name, email) in enumerate(cases):
        write_run(world.runs, i, goal_for(o, d, on, name, email), flight_ops(o, d, on, name, email, ref=f"BK-{i}"))
    [out] = proposed(induce(world.runs, world.proposals, world.duties))
    data = yaml.safe_load((world.proposals / "duties" / f"{out.name}.duty.yaml").read_text(encoding="utf-8"))
    assert data["intent_patterns"] == ["get {passenger_name} onto a flight from {origin} to {destination} on {date} "
                                       "and email {passenger_email}"]
    assert set(data["parameters"]) == {"origin", "destination", "date", "passenger_name", "passenger_email"}
    assert all(spec == {"type": "string"} for spec in data["parameters"].values())
    steps = {s["id"]: s["args"] for s in data["steps"]}
    assert steps["search_flights"] == {"origin": "$intent.origin", "destination": "$intent.destination", "date": "$intent.date"}
    assert steps["book_flight"] == {"flight_id": "$search_flights.0.flight_id", "passenger_name": "$intent.passenger_name",
                                    "passenger_email": "$intent.passenger_email"}
    assert steps["send_confirmation"] == {"email": "$intent.passenger_email", "booking_ref": "$book_flight.booking_ref",
                                          "details": "Your flight booking is confirmed."}
    assert data["example"] == {"intent": goal_for(*cases[0]), "expect": "success"}
    lib = tmp_library(world, out.name)
    fourth = goal_for("CCU", "PNQ", "2031-04-13", "Oleg Tanaka Brown", "oleg@x.test")  # not in any run
    match, _ = lib.best_match(fourth)
    assert match.duty.name == out.name and match.confidence == 1.0
    assert match.captures == {"origin": "CCU", "destination": "PNQ", "date": "2031-04-13", "passenger_name": "Oleg Tanaka Brown",
                              "passenger_email": "oleg@x.test"}


def tmp_library(world, name: str) -> DutyLibrary:
    lib = world.tmp / "lib"
    lib.mkdir(exist_ok=True)
    shutil.copyfile(REAL_DUTIES / "schema.json", lib / "schema.json")
    shutil.copyfile(world.proposals / "duties" / f"{name}.duty.yaml", lib / f"{name}.yaml")
    library = DutyLibrary(lib)
    assert name in library.duties, library.errors
    return library


def test_a_multi_word_placeholder_is_bound_by_string_comparison_in_every_run(world):
    for i, name in enumerate(["Asha Verma", "Ravi Kumar Singh", "Meera Iyer"]):
        write_run(world.runs, i, goal_for(name=name), flight_ops(name=name, ref=f"BK-{i}"))
    [out] = proposed(induce(world.runs, world.proposals, world.duties))
    text = (world.proposals / "duties" / f"{out.name}.duty.yaml").read_text(encoding="utf-8")
    assert "{passenger_name}" in text and "passenger_name: $intent.passenger_name" in text


def test_an_argument_that_is_neither_in_the_goal_nor_in_a_result_nor_constant_means_no_proposal_and_names_it(world):
    for i in range(3):
        write_run(world.runs, i, goal_for(on="2031-01-10"), flight_ops(ref=f"BK-{i}", details=f"free text number {i}"))
    [out] = induce(world.runs, world.proposals, world.duties)
    assert out.status == "skipped" and out.name is None
    assert "send_confirmation" in out.reason and "'details'" in out.reason
    assert proposal_files(world) == []


def test_a_read_only_sequence_is_skipped_because_a_duty_needs_a_write_boundary(world):
    for i in range(3):
        ops = flight_ops(ref=f"BK-{i}")[:1]
        write_run(world.runs, i, "show flights DEL BOM", ops)
    [out] = induce(world.runs, world.proposals, world.duties)
    assert out.status == "skipped" and "write boundary" in out.reason and proposal_files(world) == []


def test_a_read_after_the_write_is_fine_and_the_boundary_is_the_first_write(world):
    for i in range(3):
        ops = flight_ops(ref=f"BK-{i}")
        ops.append({"tool": "travel__search_flights", "arguments": {"origin": "DEL", "destination": "BOM", "date": "2031-01-10"},
                    "ok": True, "result": ops[0]["result"], "read_only": True})
        write_run(world.runs, i, goal_for(on="2031-01-10"), ops)
    [out] = proposed(induce(world.runs, world.proposals, world.duties))
    data = yaml.safe_load((world.proposals / "duties" / f"{out.name}.duty.yaml").read_text(encoding="utf-8"))
    assert data["write_boundary"] == {"before": "book_flight"}
    assert [s["id"] for s in data["steps"]] == ["search_flights", "book_flight", "send_confirmation", "search_flights_2"]
    assert data["success"]["check"] == "$send_confirmation" and data["success"]["equals"] is True


def test_induction_is_idempotent_in_every_proposal_state(world):
    three_identical(world.runs)
    [first] = proposed(induce(world.runs, world.proposals, world.duties))
    snapshot = tree_hash(world.proposals)
    again = induce(world.runs, world.proposals, world.duties)
    assert proposed(again) == [] and "already proposed" in again[0].reason
    assert tree_hash(world.proposals) == snapshot, "a second induction made or changed files"
    for state in ("approved", "rejected", "applied"):  # a person moved it: still not proposed again
        folder = world.proposals / "duties" / state
        folder.mkdir()
        for p in list((world.proposals / "duties").glob(f"{first.name}.*")):
            shutil.move(str(p), str(folder / p.name))
        assert proposed(induce(world.runs, world.proposals, world.duties)) == [], state
        for p in list(folder.iterdir()):
            shutil.move(str(p), str(world.proposals / "duties" / p.name))
        folder.rmdir()


def test_an_existing_duty_that_already_matches_the_goal_means_no_proposal(world):
    book = "book a flight from DEL to BOM on 2031-01-10 for Asha Verma at asha@example.com"
    for i in range(3):
        write_run(world.runs, i, book, flight_ops(ref=f"BK-{i}"))
    [out] = induce(world.runs, world.proposals, world.duties)
    assert out.status == "skipped" and "book_flight" in out.reason and "already matches" in out.reason
    assert proposal_files(world) == []


def test_a_name_that_collides_with_a_duty_or_a_proposal_gets_a_numeric_suffix(world):
    three_identical(world.runs)
    [first] = proposed(induce(world.runs, world.proposals, world.duties))
    assert first.name == "book_flight_2"  # duties/book_flight.yaml exists
    for i in range(3):  # another sequence ending in the same write tool: a different group, same bare name
        write_run(world.runs, 10 + i, "reserve seats on the morning service", flight_ops(ref=f"BK-9{i}")[1:])
    [second] = proposed(induce(world.runs, world.proposals, world.duties))
    assert second.name == "book_flight_3"


def test_generated_contract_test_source_has_the_header_and_the_named_check():
    text = contract_test_source("fly_me")
    assert text.splitlines()[0] == "# GENERATED by learning/induce_duty.py for duties/fly_me.yaml: DO NOT EDIT (CLAUDE.md D23)"
    assert "import duty_contract" in text and "async def test_duty_contract_fly_me():" in text
    assert 'await duty_contract.check_duty_contract("fly_me")' in text
    compile(text, "x", "exec")


def test_the_command_line_prints_one_line_per_group(world, capsys):
    three_identical(world.runs)
    write_run(world.runs, 9, "other", flight_ops()[:1])
    rc = induce_duty.main(["--runs", str(world.runs), "--proposals", str(world.proposals), "--duties", str(world.duties)])
    lines = capsys.readouterr().out.strip().splitlines()
    assert rc == 0 and len(lines) == 2
    assert lines[0].startswith("[PROPOSED] book_flight_2") and lines[1].startswith("[SKIPPED ]") and "1 of 3" in lines[1]


# -- apply: decisions (the contract-test runner is stubbed; the real runner is in `flow` and the red-example test) ---------

def make_proposal(light_or_root: Path, proposals: Path, name="fly_me", state="approved", reviewer="Ada Lovelace", mutate=None) -> Path:
    data = yaml.safe_load((REAL_DUTIES / "book_flight.yaml").read_text(encoding="utf-8"))
    data["name"] = name
    data["review"]["reviewer"] = reviewer
    data["intent_patterns"] = [p.replace("book a flight", "reserve a seat") for p in data["intent_patterns"]]
    data["example"]["intent"] = data["example"]["intent"].replace("book a flight", "reserve a seat")
    if mutate:
        mutate(data)
    folder = proposals / "duties" / state if state else proposals / "duties"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.duty.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, width=1000) if not isinstance(data, str) else data, encoding="utf-8")
    for suffix in PROPOSAL_SUFFIXES[1:]:
        (proposals / "duties" / f"{name}{suffix}").write_text("sibling\n", encoding="utf-8")
    return path


class Runner:
    """Stands in for apply._run_contract_test: records each call (and what the project looked like at that moment)."""

    def __init__(self, layout, staged=True, installed=True) -> None:
        self.layout, self.verdicts, self.calls = layout, (staged, installed), []

    def __call__(self, layout, test_path, duties_dir):
        staged = duties_dir is not None
        self.calls.append({"staged": staged, "test": Path(test_path), "test_exists": Path(test_path).exists(),
                           "duties_dir": duties_dir, "staged_files": sorted(p.name for p in duties_dir.iterdir()) if staged else None,
                           "installed": (layout.root / "duties" / Path(test_path).name[len("test_duty_"):].replace(".py", ".yaml")).exists()})
        return self.verdicts[0 if staged else 1], "" if self.verdicts[0 if staged else 1] else "forced red output"


def head(layout) -> str:
    return git(layout.root, "rev-parse", "HEAD")


def test_an_approved_proposal_is_staged_tested_installed_verified_and_committed(light, tmp_path, monkeypatch):
    runner = Runner(light)
    monkeypatch.setattr(apply_mod, "_run_contract_test", runner)
    proposals = tmp_path / "proposals"
    path = make_proposal(light.root, proposals)
    approved_bytes = path.read_bytes()
    before = head(light)
    [result] = apply_approved_duties(proposals, light)
    assert result.status == "applied", result.reason
    first, second = runner.calls
    assert first["staged"] and first["test_exists"] and first["test"].parent == light.root / "tests" / "duties"
    assert first["staged_files"] == ["fly_me.yaml", "schema.json"] and not first["installed"], "staged, not installed yet"
    assert not second["staged"] and second["installed"], "the second run is on the installed files"
    assert (light.root / "duties" / "fly_me.yaml").read_bytes() == approved_bytes
    assert (light.root / "tests" / "duties" / "test_duty_fly_me.py").read_text(encoding="utf-8") == contract_test_source("fly_me")
    assert git(light.root, "rev-list", "--count", f"{before}..HEAD") == "1"
    assert git(light.root, "log", "-1", "--format=%s") == "learn: install duty fly_me (reviewed by Ada Lovelace)"
    names = {l.split("\t")[1] for l in git(light.root, "show", "--name-status", "--format=", "HEAD").splitlines()}
    assert names == {"duties/fly_me.yaml", "tests/duties/test_duty_fly_me.py"}  # the proposal folder is untracked in this tmp tree
    assert sorted(p.name for p in (proposals / "duties" / "applied").iterdir()) == sorted(f"fly_me{s}" for s in PROPOSAL_SUFFIXES)
    assert not (proposals / "duties" / "approved" / "fly_me.duty.yaml").exists()


@pytest.mark.parametrize("why, reviewer", [("PROPOSED-UNREVIEWED", UNREVIEWED), ("an empty reviewer", ""), ("blank", "   ")])
def test_a_duty_without_a_named_reviewer_is_rejected_and_nothing_is_installed(light, tmp_path, monkeypatch, why, reviewer):
    runner = Runner(light)
    monkeypatch.setattr(apply_mod, "_run_contract_test", runner)
    proposals = tmp_path / "proposals"
    make_proposal(light.root, proposals, reviewer=reviewer)
    snapshot, commit = tree_hash(light.root / "duties"), head(light)
    [result] = apply_approved_duties(proposals, light)
    assert result.status == "rejected" and "reviewer" in result.reason
    assert runner.calls == [], "the contract test must not even run for an unreviewed duty"
    assert tree_hash(light.root / "duties") == snapshot and not (light.root / "tests" / "duties").exists()
    assert head(light) == commit
    rejected = proposals / "duties" / "rejected"
    assert (rejected / "fly_me.duty.yaml").exists() and "reviewer" in (rejected / "fly_me.reason.txt").read_text(encoding="utf-8")
    assert (proposals / "duties" / "fly_me.duty.diff").exists(), "siblings are left alone"


def test_a_name_that_already_exists_is_rejected_and_the_existing_files_are_untouched(light, tmp_path, monkeypatch):
    runner = Runner(light)
    monkeypatch.setattr(apply_mod, "_run_contract_test", runner)
    proposals = tmp_path / "proposals"
    existing = light.root / "duties" / "fly_me.yaml"
    existing.write_text("name: fly_me\n# somebody else's duty\n", encoding="utf-8")
    make_proposal(light.root, proposals)
    before = existing.read_bytes()
    [result] = apply_approved_duties(proposals, light)
    assert result.status == "rejected" and "already exists" in result.reason and runner.calls == []
    assert existing.read_bytes() == before
    # an existing generated test alone is a collision too
    existing.unlink()
    (light.root / "tests" / "duties").mkdir()
    (light.root / "tests" / "duties" / "test_duty_fly_me.py").write_text("# mine\n", encoding="utf-8")
    make_proposal(light.root, proposals)
    [again] = apply_approved_duties(proposals, light)
    assert again.status == "rejected" and "already exists" in again.reason
    assert (light.root / "tests" / "duties" / "test_duty_fly_me.py").read_text(encoding="utf-8") == "# mine\n"
    assert not (light.root / "duties" / "fly_me.yaml").exists()


def test_a_duty_that_does_not_load_is_rejected_with_the_reason(light, tmp_path, monkeypatch):
    runner = Runner(light)
    monkeypatch.setattr(apply_mod, "_run_contract_test", runner)
    proposals = tmp_path / "proposals"

    def schema_break(data):
        data["write_boundary"] = {"before": "book", "extra": 1}  # not allowed by duties/schema.json

    def cross_field(data):
        data["steps"][1]["args"]["flight_id"] = "$confirm.nothing"  # a step may only reference EARLIER steps

    make_proposal(light.root, proposals, name="bad_schema", mutate=schema_break)
    make_proposal(light.root, proposals, name="bad_cross", mutate=cross_field)
    (proposals / "duties" / "approved" / "bad_yaml.duty.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    by_name = {r.proposal: r for r in apply_approved_duties(proposals, light)}
    assert {r.status for r in by_name.values()} == {"rejected"} and len(by_name) == 3
    assert "does not load" in by_name["bad_schema.duty.yaml"].reason and "write_boundary" in by_name["bad_schema.duty.yaml"].reason
    assert "EARLIER" in by_name["bad_cross.duty.yaml"].reason
    assert runner.calls == [] and not list((light.root / "duties").glob("bad_*")) and not (light.root / "tests" / "duties").exists()


def test_a_red_staged_contract_test_rejects_and_removes_the_test_file(light, tmp_path, monkeypatch):
    runner = Runner(light, staged=False)
    monkeypatch.setattr(apply_mod, "_run_contract_test", runner)
    proposals = tmp_path / "proposals"
    make_proposal(light.root, proposals)
    snapshot, commit = tree_hash(light.root), head(light)
    [result] = apply_approved_duties(proposals, light)
    assert result.status == "rejected" and "contract test is red" in result.reason and "forced red output" in result.reason
    assert len(runner.calls) == 1 and runner.calls[0]["staged"], "no post-install run: nothing was installed"
    assert tree_hash(light.root) == snapshot, "the project must be byte-identical (no leftover tests/duties/)"
    assert head(light) == commit
    assert "forced red output" in (proposals / "duties" / "rejected" / "fly_me.reason.txt").read_text(encoding="utf-8")


@pytest.mark.budget(30)
def test_an_example_that_does_not_replay_on_fresh_mocks_is_rejected_by_the_real_contract_test(project, tmp_path):
    """The REAL runner: the example asks for the reverse route, which has no flights in the mock world, so the duty cannot
    complete alone. D23: it is rejected, never installed on faith."""
    proposals = tmp_path / "proposals"
    make_proposal(project.root, proposals, mutate=lambda d: d["example"].update(
        intent="reserve a seat from BOM to DEL on $today+60 for Asha Verma at asha@example.com"))
    snapshot, commit = tree_hash(project.root), head(project)
    [result] = apply_approved_duties(proposals, project)
    assert result.status == "rejected" and "contract test is red" in result.reason, result.reason
    assert "did not complete as this duty" in result.reason or "model was consulted" in result.reason
    assert tree_hash(project.root) == snapshot and head(project) == commit
    assert (proposals / "duties" / "rejected" / "fly_me.reason.txt").exists()


def test_a_red_post_install_verification_rolls_back_and_the_proposal_stays_approved(light, tmp_path, monkeypatch):
    runner = Runner(light, staged=True, installed=False)  # the staged test is green, the one on the installed files red
    monkeypatch.setattr(apply_mod, "_run_contract_test", runner)
    proposals = tmp_path / "proposals"
    path = make_proposal(light.root, proposals)
    snapshot, commit = tree_hash(light.root), head(light)
    [result] = apply_approved_duties(proposals, light)
    assert result.status == "rolled_back" and "post-install" in result.reason and "forced red output" in result.reason
    assert [c["staged"] for c in runner.calls] == [True, False]
    assert tree_hash(light.root) == snapshot, "the duty and its generated test must both be gone, byte for byte"
    assert not (light.root / "tests" / "duties").exists()
    assert head(light) == commit, "no commit on a rollback"
    assert path.exists() and not (proposals / "duties" / "rejected").exists(), "the proposal STAYS approved"
    assert "post-install" in (proposals / "duties" / "approved" / "fly_me.reason.txt").read_text(encoding="utf-8")


def test_a_crash_while_testing_leaves_nothing_behind(light, tmp_path, monkeypatch):
    def boom(*a):
        raise OSError("disk on fire")

    monkeypatch.setattr(apply_mod, "_run_contract_test", boom)
    proposals = tmp_path / "proposals"
    make_proposal(light.root, proposals)
    snapshot = tree_hash(light.root)
    [result] = apply_approved_duties(proposals, light)
    assert result.status == "rejected" and "disk on fire" in result.reason
    assert tree_hash(light.root) == snapshot


def test_proposals_that_are_not_approved_never_install_or_commit(light, tmp_path, monkeypatch):
    runner = Runner(light)
    monkeypatch.setattr(apply_mod, "_run_contract_test", runner)
    proposals = tmp_path / "proposals"
    make_proposal(light.root, proposals, name="pending_one", state="")
    make_proposal(light.root, proposals, name="rejected_one", state="rejected")
    make_proposal(light.root, proposals, name="applied_one", state="applied")
    snapshot, commit, files = tree_hash(light.root), head(light), tree_hash(proposals)
    assert apply_approved_duties(proposals, light) == []
    assert runner.calls == [] and tree_hash(light.root) == snapshot and head(light) == commit
    assert tree_hash(proposals) == files, "unapproved proposals are not even moved"
    assert not (light.root / "duties" / "pending_one.yaml").exists() and not (light.root / "tests" / "duties").exists()


def test_a_spec_proposal_in_approved_is_not_touched_by_the_duty_flow(light, tmp_path, monkeypatch):
    monkeypatch.setattr(apply_mod, "_run_contract_test", Runner(light))
    proposals = tmp_path / "proposals"
    (proposals / "approved").mkdir(parents=True)
    (proposals / "approved" / "x__op__CODE__t.patch").write_text("not a duty\n", encoding="utf-8")
    assert apply_approved_duties(proposals, light) == []
    assert (proposals / "approved" / "x__op__CODE__t.patch").exists()


def test_the_apply_command_reports_duty_results_and_the_proposals_waiting(light, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(apply_mod, "_run_contract_test", Runner(light))
    monkeypatch.setattr(pipeline, "Layout", lambda: light)
    proposals = tmp_path / "proposals"
    make_proposal(light.root, proposals, name="fly_me")
    make_proposal(light.root, proposals, name="later_one", state="")
    rc = apply_mod.main(["--proposals", str(proposals)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fly_me.duty.yaml" in out and "APPLIED" in out
    assert "1 approved duty proposal(s) processed; 1 duty proposal(s) waiting" in out
