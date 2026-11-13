"""`live_judgment.py --live-apis`, the opt-in step for the 14 live-API tests (CLAUDE.md D28), and the code attestation every record
now carries (D27).

Nothing here sends a request to a third party: the real 14 tests run against a loopback stand-in (tests/fake_live_apis.py), and the
outage / contract-failure / attestation logic is driven with crafted junit and attestation files. A loopback run attests as NOT real,
which is what the end-to-end test asserts."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (SCRIPTS, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import live_attest  # noqa: E402
import repo_hash  # noqa: E402
from fake_live_apis import FakeLiveApis  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lj = _load("live_judgment")
mar = _load("move_audit_row")
HOSTS = {"tests/test_weather.py": "api.open-meteo.com", "tests/test_jsonplaceholder.py": "jsonplaceholder.typicode.com",
         "tests/test_catfact.py": "catfact.ninja", "tests/test_dogceo.py": "dog.ceo", "tests/test_zippopotamus.py": "api.zippopotam.us"}


def clean_env(**extra) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "LIVE_"))}
    env.update(extra)
    return env


def run_cli(args, log: Path, **extra):
    env = clean_env(LIVE_JUDGMENT_LOG=str(log), **extra)
    done = subprocess.run([sys.executable, str(SCRIPTS / "live_judgment.py"), *args], env=env, cwd=ROOT, capture_output=True,
                          text=True, timeout=300)
    return done.returncode, done.stdout + done.stderr


def last(log: Path) -> dict:
    return json.loads(log.read_text(encoding="utf-8").splitlines()[-1])


# -- consent: nothing runs, and nothing is sent, without --allow-live ----------------------------------------------------------------

def test_live_apis_without_allow_live_is_exit_5_runs_nothing_and_records_skipped(tmp_path):
    log = tmp_path / "log.jsonl"
    code, out = run_cli(["--live-apis"], log)
    assert code == lj.EXIT_LIVE_NOT_ALLOWED == 5
    assert "LIVE_NOT_ALLOWED" in out and "third-party" in out
    r = last(log)
    assert (r["subcommand"], r["result"], r["exit_code"], r["real"], r["allow_live"]) == ("live_apis", "skipped", 5, False, False)
    assert not (tmp_path / "live_judgment").exists() or not list((tmp_path / "live_judgment").glob("*.xml")), "no test may have run"


def test_without_allow_live_not_one_network_call_is_made_and_no_test_is_started(tmp_path, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("a network call was attempted")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(lj, "_run_contract_tests", lambda *a, **k: pytest.fail("the tests must not start without --allow-live"))
    out = io.StringIO()
    code = lj.main(["--live-apis"], env={"LIVE_JUDGMENT_LOG": str(tmp_path / "log.jsonl")}, stdout=out, stderr=io.StringIO())
    assert code == 5 and calls == []


@pytest.mark.parametrize("argv", [["--live-apis", "--mock-transport"], ["--live-apis", "--planner"], ["--live-apis", "--doc"]])
def test_live_apis_flag_combinations_that_make_no_sense_are_usage_errors(argv, tmp_path):
    code, out = run_cli(argv, tmp_path / "log.jsonl", ANTHROPIC_API_KEY="x")
    assert code == 64 and not (tmp_path / "log.jsonl").exists()


def test_the_dry_run_names_the_14_tests_and_the_five_hosts_and_records_nothing(tmp_path):
    log = tmp_path / "log.jsonl"
    code, out = run_cli(["--live-apis", "--dry-run"], log)
    assert code == 0 and "DRY RUN" in out and not log.exists()
    assert "pytest node ids that would run (14)" in out
    for host in HOSTS.values():
        assert host in out
    assert "THIRD_PARTY_UNAVAILABLE" in out and "skipped, exit 6" in out


def test_the_selection_is_exactly_the_14_existing_live_gated_tests():
    ids, problem = lj._collect([e["file"] for e in lj.LIVE_TESTS["live_apis"]], "test_live", {})
    assert problem == "" and len(ids) == 14
    assert sorted({i.split("::")[0] for i in ids}) == sorted(HOSTS)  # five files, no copies of any assertion
    assert all("::test_live[" in i for i in ids)


# -- the whole step, end to end, on the REAL tests against a loopback stand-in ------------------------------------------------------------

def test_the_real_14_tests_run_and_pass_against_a_loopback_stand_in_and_are_recorded_not_real(tmp_path):
    log = tmp_path / "log.jsonl"
    with FakeLiveApis() as fake:
        code, out = run_cli(["--live-apis", "--allow-live"], log, **fake.env())
        assert fake.hits >= 14, f"the 14 tests should have called the stand-in ({fake.hits} hits)\n{out[-800:]}"
    assert code == 0, out[-1500:]
    assert "PASS (live third-party APIs): 14 live-API tests passed against 5 hosts" in out
    r = last(log)
    assert (r["subcommand"], r["result"], r["exit_code"], r["mock"], r["allow_live"]) == ("live_apis", "pass", 0, False, True)
    assert len(r["tests"]) == 14
    assert r["real"] is False, "traffic that went to 127.0.0.1 can never be a real third-party result"
    att = r["attestation"]
    assert att["real"] is False and set(att["hosts"]) == set(HOSTS.values())
    assert all(h["real_requests"] == 0 for h in att["hosts"].values())
    assert r["artifact_sha256"] == hashlib.sha256(Path(r["artifact_path"]).read_bytes()).hexdigest()
    # and the move script will not take it
    audit = tmp_path / "AUDIT.md"
    audit.write_text((ROOT / "AUDIT.md").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    done = subprocess.run([sys.executable, str(SCRIPTS / "move_audit_row.py"), "--row", "C26", "--apply", "--record-hash",
                           hashlib.sha256(log.read_text(encoding="utf-8").splitlines()[-1].encode()).hexdigest(), "--log", str(log),
                           "--audit", str(audit), "--no-commit"], cwd=ROOT, env=clean_env(), capture_output=True, text=True, timeout=120)
    assert done.returncode == 11 and "REFUSED (NOT_REAL)" in done.stdout


def test_every_record_carries_the_code_attestation_and_it_is_consistent(tmp_path):
    log = tmp_path / "log.jsonl"
    with FakeLiveApis() as fake:
        run_cli(["--live-apis", "--allow-live"], log, **fake.env())
    r = last(log)
    man = json.loads(Path(r["repo_manifest_path"]).read_text(encoding="utf-8"))
    assert r["repo_hash"] == repo_hash.digest(man)
    assert r["repo_manifest_sha256"] == hashlib.sha256(Path(r["repo_manifest_path"]).read_bytes()).hexdigest()
    assert {"scripts/live_judgment.py", "tests/test_weather.py", "specs/weather.yaml", "servers/_http.py"} <= set(man["files"])
    assert repo_hash.diff(man) == {"changed": [], "deleted": [], "added": []}


# -- the outcomes, driven with crafted junit and attestation -----------------------------------------------------------------------------

def junit_for(failed_files: list[str], total: int = 14) -> str:
    cases = []
    for i in range(total):
        file = list(HOSTS)[i % 5]
        cls = file[:-3].replace("/", ".")
        bad = file in failed_files
        cases.append(f'<testcase classname="{cls}" name="test_live[{i}]">' + ('<failure message="boom"/>' if bad else "") + "</testcase>")
    return f'<testsuites><testsuite tests="{total}">{"".join(cases)}</testsuite></testsuites>'


def drive(tmp_path, monkeypatch, *, failed_files=(), attest: dict | None = None, verdict=None, ids=14):
    """Run `--live-apis --allow-live` in-process with `_run_contract_tests` replaced by a crafted outcome."""
    attest = attest if attest is not None else {h: [200, 200] for h in HOSTS.values()}

    def fake_run(ctx, sub, stamp):
        xml = Path(ctx.attest_file).parent / f"{stamp}_live_apis.xml"
        xml.write_text(junit_for(list(failed_files)), encoding="utf-8")
        lines = []
        for host, statuses in attest.items():
            for st in statuses:
                lines.append(json.dumps({"host": host, "transport": live_attest.REAL_TRANSPORT, "status": st}))
        Path(ctx.attest_file).write_text("\n".join(lines) + "\n", encoding="utf-8")
        v = verdict or ("fail" if failed_files else "pass")
        return v, "crafted reason", [f"tests/t.py::test_live[{i}]" for i in range(ids)], [str(xml)]

    monkeypatch.setattr(lj, "_run_contract_tests", fake_run)
    log = tmp_path / "log.jsonl"
    out = io.StringIO()
    code = lj.main(["--live-apis", "--allow-live"], env={"LIVE_JUDGMENT_LOG": str(log)}, stdout=out, stderr=io.StringIO())
    return code, out.getvalue(), last(log)


def test_all_pass_and_all_five_hosts_attested_real_is_a_real_pass(tmp_path, monkeypatch):
    code, out, r = drive(tmp_path, monkeypatch)
    assert code == 0 and (r["result"], r["real"]) == ("pass", True) and r["attestation"]["real"] is True


def test_all_pass_but_one_host_never_attested_is_a_pass_that_is_not_real(tmp_path, monkeypatch):
    attest = {h: [200] for h in list(HOSTS.values())[:4]}
    code, out, r = drive(tmp_path, monkeypatch, attest=attest)
    assert code == 0 and (r["result"], r["real"]) == ("pass", False)


def test_all_pass_but_a_host_answered_only_401_is_not_real(tmp_path, monkeypatch):
    attest = {h: [200] for h in HOSTS.values()}
    attest["dog.ceo"] = [401]
    code, out, r = drive(tmp_path, monkeypatch, attest=attest)
    assert r["real"] is False


@pytest.mark.parametrize("statuses", [[503], [500, 502], [None]])
def test_a_third_party_outage_is_skipped_exit_6_and_never_marked_failed(tmp_path, monkeypatch, statuses):
    attest = {h: [200] for h in HOSTS.values()}
    attest["api.open-meteo.com"] = statuses
    code, out, r = drive(tmp_path, monkeypatch, failed_files=["tests/test_weather.py"], attest=attest)
    assert code == lj.EXIT_THIRD_PARTY_UNAVAILABLE == 6
    assert (r["result"], r["real"], r["exit_code"]) == ("skipped", False, 6)
    assert r["reason"].startswith("THIRD_PARTY_UNAVAILABLE") and "api.open-meteo.com" in r["reason"]
    assert "C26 stays in section 3a" in out and "nothing is marked failed" in out and "FAIL" not in out.split("THIRD_PARTY_UNAVAILABLE")[0]


def test_two_hosts_down_are_both_named_and_still_skipped(tmp_path, monkeypatch):
    attest = {h: [200] for h in HOSTS.values()}
    attest["dog.ceo"], attest["catfact.ninja"] = [503], [None]
    code, out, r = drive(tmp_path, monkeypatch, failed_files=["tests/test_dogceo.py", "tests/test_catfact.py"], attest=attest)
    assert code == 6 and "dog.ceo" in r["reason"] and "catfact.ninja" in r["reason"] and r["result"] == "skipped"


def test_a_host_that_ANSWERED_but_whose_test_failed_is_a_real_failure_not_an_outage(tmp_path, monkeypatch):
    """The API is up and the contract broke: that is this code's failure, recorded as fail (exit 1)."""
    code, out, r = drive(tmp_path, monkeypatch, failed_files=["tests/test_weather.py"])
    assert code == 1 and r["result"] == "fail" and r["exit_code"] == 1 and "THIRD_PARTY" not in r["reason"]


def test_an_outage_on_one_host_does_not_hide_a_contract_failure_on_another(tmp_path, monkeypatch):
    attest = {h: [200] for h in HOSTS.values()}
    attest["api.open-meteo.com"] = [503]
    code, out, r = drive(tmp_path, monkeypatch, failed_files=["tests/test_weather.py", "tests/test_catfact.py"], attest=attest)
    assert code == 1 and r["result"] == "fail"


def test_tests_that_were_skipped_despite_the_opt_in_are_a_failure_not_a_skip(tmp_path, monkeypatch):
    code, out, r = drive(tmp_path, monkeypatch, verdict="skipped")
    assert code == 1 and r["result"] == "fail"


# -- the code attestation catches an edit made WHILE the run was going ------------------------------------------------------------------------

def test_a_pass_while_the_code_changed_during_the_run_is_recorded_as_fail(tmp_path, monkeypatch):
    ctx = lj._Ctx({"LIVE_JUDGMENT_LOG": str(tmp_path / "log.jsonl")}, io.StringIO(), io.StringIO())
    lj._attest_start(ctx, "planner", "20261113T000000Z")
    changed = json.loads(json.dumps(ctx.manifest))
    changed["files"]["tests/scenarios/test_planner_contract.py"] = "0" * 64
    monkeypatch.setattr(repo_hash, "for_subcommand", lambda sub, root=repo_hash.ROOT: changed)
    code = lj._record(ctx, "planner", "pass", "ok", 0, real=True)
    r = last(tmp_path / "log.jsonl")
    assert code == 1 and r["result"] == "fail" and r["real"] is False
    assert r["reason"].startswith("CODE CHANGED DURING THE RUN") and "tests/scenarios/test_planner_contract.py" in r["reason"]
    assert r["repo_hash"] == repo_hash.digest(ctx.manifest), "the record attests the code as it was when the run STARTED"
