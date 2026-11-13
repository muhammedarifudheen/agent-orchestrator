"""scripts/move_audit_row.py (D27): a row moves from AUDIT.md 3a to 2b ONLY from a fresh, real, intact, passing record.

Everything runs against a THROWAWAY git repo holding a copy of AUDIT.md and synthetic records written by these tests; the real
AUDIT.md and the real .perf/live_judgment.jsonl are never touched (a module fixture asserts it). The synthetic records are good
only because these tests construct them: whoever can write a log line can fake one, which is stated in the script's docstring."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path

from agent.provider_config import ANTHROPIC_HOST as _HOST
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    import sys
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mar = _load("move_audit_row")
repo_hash = _load("repo_hash")
NOW = dt.datetime(2026, 11, 13, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module", autouse=True)
def real_files_untouched():
    audit = ROOT / "AUDIT.md"
    log = ROOT / ".perf" / "live_judgment.jsonl"
    before = (audit.read_bytes(), log.read_bytes() if log.exists() else None)
    yield
    assert audit.read_bytes() == before[0], "a test changed the real AUDIT.md"
    assert (log.read_bytes() if log.exists() else None) == before[1], "a test changed the real live_judgment.jsonl"


JUNIT = ('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="{n}">{cases}</testsuite></testsuites>')


def junit(n: int, skipped: int = 0) -> str:
    cases = "".join(f'<testcase classname="tests.t" name="test_{i}"/>' for i in range(n - skipped))
    cases += "".join(f'<testcase classname="tests.t" name="test_s{i}"><skipped message="ANTHROPIC_API_KEY absent"/></testcase>' for i in range(skipped))
    return JUNIT.format(n=n, cases=cases)


class Lab:
    """A throwaway git repo with AUDIT.md and a log; builds records, runs the script."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.repo.mkdir()
        (self.repo / "AUDIT.md").write_text((ROOT / "AUDIT.md").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        self.log = tmp / "live_judgment.jsonl"
        self.code = tmp / "code"  # the throwaway tree whose code every synthetic record attests (scripts/repo_hash.py)
        for rel, body in {"tests/test_a.py": "import helper\n\ndef test_a():\n    assert helper.ONE == 1\n",
                          "tests/helper.py": "ONE = 1\n", "servers/x_server.py": "NAME = 'x'\n", "pytest.ini": "[pytest]\n"}.items():
            (self.code / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.code / rel).write_text(body, encoding="utf-8", newline="\n")
        for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t"], ["add", "AUDIT.md"],
                     ["commit", "-q", "-m", "baseline"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.n = 0

    def record(self, sub: str = "planner", *, minutes_old: float = 1, tamper=None, artifact_text: str | None = None, **over) -> str:
        """Write an artifact and a record line; return the record's full hash."""
        self.n += 1
        if sub == "live_apis":
            art = self.tmp / f"{self.n}_live_apis.xml"
            art.write_text(artifact_text or junit(14), encoding="utf-8")
            hosts = {h: {"real": True, "real_requests": 2, "non_real_requests": 0}
                     for h in ("api.open-meteo.com", "api.zippopotam.us", "catfact.ninja", "dog.ceo", "jsonplaceholder.typicode.com")}
            att = {"hosts": hosts, "real": True, "real_requests": 10, "non_real_requests": 0, "host": "five"}
            base = {"key_present": False, "upstream": None, "allow_live": True, "tests": [f"tests/t.py::test_live[{i}]" for i in range(14)]}
        elif sub == "apply":
            art = self.tmp / f"{self.n}_apply.json"
            payload = {"upstream": "https://httpbin.org/status/418", "public": True, "allow_live": True,
                       "attestation": {"real": True, "non_real_requests": 0}}
            art.write_text(artifact_text or json.dumps(payload), encoding="utf-8")
            att = {"host": "httpbin.org", "real": True, "real_requests": 3, "non_real_requests": 0}
            base = {"key_present": False, "upstream": "https://httpbin.org/status/418", "allow_live": True, "tests": []}
        else:
            art = self.tmp / f"{self.n}_{sub}.xml"
            art.write_text(artifact_text or junit(6 if sub == "planner" else 1), encoding="utf-8")
            att = {"host": _HOST, "real": True, "real_requests": 4, "non_real_requests": 0}
            base = {"key_present": True, "upstream": None, "allow_live": False,
                    "tests": [f"tests/t.py::test_{i}" for i in range(6 if sub == "planner" else 1)]}
        man = repo_hash.manifest(["tests/test_a.py"], ["servers/*.py"], self.code, harness=())
        sidecar = self.tmp / f"{self.n}_{sub}.manifest.json"
        sidecar_sha = repo_hash.write_sidecar(sidecar, man)
        rec = {"repo_hash": repo_hash.digest(man), "repo_manifest_path": str(sidecar), "repo_manifest_sha256": sidecar_sha,
               "date": (NOW - dt.timedelta(minutes=minutes_old)).isoformat(), "subcommand": sub, "result": "pass", "reason": "ok",
               "exit_code": 0, "real": True, "mock": False, "artifact_path": str(art),
               "artifact_sha256": hashlib.sha256(art.read_bytes()).hexdigest(), "attestation": att, **base}
        rec.update({"provider": "anthropic", "provider_host": _HOST})
        rec.update(over)
        if tamper:
            tamper(rec, art)
        if "closes_gap" not in over:  # what live_judgment.py would have written for these fields
            from live_judgment import closes_gap  # scripts/ is on sys.path once move_audit_row is loaded

            rec["closes_gap"] = closes_gap(rec["subcommand"], rec.get("provider"), rec.get("provider_host"), rec.get("result"),
                                           rec.get("real") is True)
        line = json.dumps(rec, sort_keys=True)
        with self.log.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        return hashlib.sha256(line.encode("utf-8")).hexdigest()

    def run(self, row: str, h: str | None, *extra: str, now: dt.datetime = NOW) -> tuple[int, str]:
        argv = ["--row", row, "--log", str(self.log), "--audit", str(self.repo / "AUDIT.md"), "--repo", str(self.repo),
                "--code-root", str(self.code), *extra]
        if h:
            argv += ["--record-hash", h]
        out = io.StringIO()
        code = mar.main(argv, stdout=out, now=lambda: now)
        return code, out.getvalue()

    def audit(self) -> str:
        return (self.repo / "AUDIT.md").read_text(encoding="utf-8")

    def commits(self) -> list[str]:
        done = subprocess.run(["git", "-C", str(self.repo), "log", "--format=%s"], capture_output=True, text=True)
        return done.stdout.splitlines()


@pytest.fixture
def lab(tmp_path) -> Lab:
    return Lab(tmp_path)


def section_3a(text: str) -> str:
    a = text.index("### 3a.")
    return text[a:text.index("### 3b.")]


# -- the happy path: a fresh, real, passing record moves its row, with a commit that names the evidence ----------------------------

def test_a_dry_run_runs_every_check_prints_the_diff_and_changes_nothing(lab):
    h = lab.record("planner")
    before = lab.audit()
    code, out = lab.run("C24", h)
    assert code == 0 and "ALLOWED (dry run)" in out and "-| C24 |" in out and "+| C24 |" in out
    assert lab.audit() == before and lab.commits() == ["baseline"]


def test_apply_moves_the_row_from_3a_to_2b_and_commits_with_the_record_hash(lab):
    h = lab.record("planner")
    code, out = lab.run("C24", h, "--apply")
    assert code == 0, out
    assert f"MOVED: C24 -> AUDIT.md section 2b (record {h[:12]})" in out
    text = lab.audit()
    assert "| C24 |" not in section_3a(text) and "| C25 |" in section_3a(text) and "| C27 |" in section_3a(text)
    block = text[text.index("### 2b. Proven live"):text.index("## 3. NOT PROVEN")]
    assert "| C24 |" in block and f"`{h[:12]}`" in block and "python scripts/live_judgment.py --planner" in block
    assert lab.commits() == [f"audit: close live judgment gap C24 ({h[:12]})", "baseline"]
    body = subprocess.run(["git", "-C", str(lab.repo), "log", "-1", "--format=%B"], capture_output=True, text=True).stdout
    assert h in body and "Evidence sha256:" in body


def test_the_edited_audit_still_satisfies_the_audit_tests_own_parsers(lab):
    """The row moves must not break test_audit_accuracy / test_audit_completeness (which parse AUDIT.md)."""
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    import test_audit_accuracy as accuracy
    import test_audit_completeness as completeness
    for sub, row in (("planner", "C24"), ("doc", "C25"), ("apply", "C27")):
        h = lab.record(sub)
        assert lab.run(row, h, "--apply")[0] == 0
    text = lab.audit()
    assert accuracy.parse_proven(text), "section 2's table must still parse with the 2b table beside it"
    assert completeness.problems(ROOT, text) == []
    block = text[text.index("### 2b. Proven live"):text.index("## 3. NOT PROVEN")]
    assert all(f"| {r} |" in block for r in ("C24", "C25", "C27"))
    assert all(f"| {r} |" not in section_3a(text) for r in ("C24", "C25", "C27"))
    assert len(lab.commits()) == 4  # baseline + one commit per row


def test_without_a_record_hash_it_shows_the_checks_for_the_latest_record_and_applies_nothing(lab):
    lab.record("planner")
    code, out = lab.run("C24", None)
    assert code == 0 and "no --record-hash given" in out and lab.commits() == ["baseline"]


def test_apply_without_a_record_hash_is_a_usage_error(lab):
    code, out = lab.run("C24", None, "--apply")
    assert code == 64 and "--apply needs --record-hash" in out and lab.commits() == ["baseline"]


# -- every refusal, by code, and none of them edits or commits anything ----------------------------------------------------------

def _sub(**kv):
    def tamper(rec, art):
        rec.update(kv)
    return tamper


REFUSALS = [
    ("mock", dict(mock=True), 17), ("not real", dict(real=False), 11), ("real missing", dict(real=None), 11),
    ("skipped", dict(result="skipped", exit_code=2), 12), ("failed", dict(result="fail", exit_code=1), 12),
    ("pass with a non-zero exit", dict(exit_code=1), 12), ("wrong subcommand", dict(subcommand="doc"), 16),
    ("no key for planner", dict(key_present=False), 14), ("attestation not real", dict(attestation={"real": False}), 14),
    ("attestation poisoned by another transport", dict(attestation={"real": True, "non_real_requests": 1}), 14),
    ("no attestation", dict(attestation=None), 14), ("artifact missing", dict(artifact_path="nope.xml"), 14),
    ("artifact hash differs", dict(artifact_sha256="0" * 64), 14), ("an unreadable date is stale", dict(date="yesterday"), 13),
]


@pytest.mark.parametrize("label,over,code", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_a_record_that_is_not_fresh_real_intact_evidence_is_refused_and_nothing_changes(lab, label, over, code):
    h = lab.record("planner", **over)
    before = lab.audit()
    got, out = lab.run("C24", h, "--apply")
    assert got == code, out
    assert f"REFUSED ({next(k for k, v in mar.CODES.items() if v == code)})" in out
    assert lab.audit() == before and lab.commits() == ["baseline"], "a refusal must change nothing"


def test_a_historical_record_is_not_evidence_the_window_is_exactly_30_minutes(lab):
    old = lab.record("planner", minutes_old=31)
    just = lab.record("planner", minutes_old=29)
    code, out = lab.run("C24", old, "--apply")
    assert code == 13 and "a historical record is not evidence" in out and lab.commits() == ["baseline"]
    assert lab.run("C24", just, "--apply")[0] == 0


def test_a_record_from_the_future_is_refused_too(lab):
    h = lab.record("planner", minutes_old=-10)
    assert lab.run("C24", h)[0] == 13


def test_the_skipped_test_inside_an_otherwise_passing_report_makes_the_evidence_a_mismatch(lab):
    h = lab.record("planner", artifact_text=junit(6, skipped=1))
    code, out = lab.run("C24", h, "--apply")
    assert code == 14 and "re-classifying the junit report" in out


def test_a_report_with_a_different_number_of_tests_than_recorded_is_a_mismatch(lab):
    h = lab.record("planner", artifact_text=junit(5))
    assert lab.run("C24", h)[0] == 14


def test_a_failing_test_in_the_stored_report_is_a_mismatch_even_if_the_record_says_pass(lab):
    bad = junit(6).replace('<testcase classname="tests.t" name="test_0"/>',
                           '<testcase classname="tests.t" name="test_0"><failure message="boom"/></testcase>')
    h = lab.record("planner", artifact_text=bad)
    assert lab.run("C24", h)[0] == 14


@pytest.mark.parametrize("label,over", [("loopback upstream", dict(upstream="http://127.0.0.1:8000/status/418")),
                                        ("no --allow-live", dict(allow_live=False))])
def test_an_apply_record_needs_a_public_upstream_and_allow_live(lab, label, over):
    h = lab.record("apply", **over)
    code, out = lab.run("C27", h, "--apply")
    assert code == 14 and lab.commits() == ["baseline"], out


def test_an_apply_transcript_that_does_not_say_public_and_attested_is_a_mismatch(lab):
    h = lab.record("apply", artifact_text=json.dumps({"public": False, "allow_live": True, "attestation": {"real": True}}))
    assert lab.run("C27", h)[0] == 14


def test_the_order_of_the_checks_mock_and_not_real_come_first(lab):
    both = lab.record("planner", mock=True, real=False, minutes_old=500, result="fail")
    assert lab.run("C24", both)[0] == 17
    not_real_and_stale = lab.record("planner", real=False, minutes_old=500)
    assert lab.run("C24", not_real_and_stale)[0] == 11


def test_a_row_cannot_be_moved_twice_and_an_unknown_or_short_hash_is_refused(lab):
    first = lab.record("planner")
    assert lab.run("C24", first, "--apply")[0] == 0
    second = lab.record("planner")
    code, out = lab.run("C24", second, "--apply")
    assert code == 15 and "not an open row" in out
    assert lab.run("C25", "abcdef012345")[0] == 10
    assert lab.run("C25", "abc")[0] == 10


def test_a_dirty_audit_md_refuses_to_commit_and_writes_nothing(lab):
    h = lab.record("planner")
    path = lab.repo / "AUDIT.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nsomeone's uncommitted edit\n", encoding="utf-8")
    dirty = lab.audit()
    code, out = lab.run("C24", h, "--apply")
    assert code == 23 and lab.audit() == dirty and lab.commits() == ["baseline"]


def test_no_commit_edits_the_file_only(lab):
    h = lab.record("planner")
    code, out = lab.run("C24", h, "--apply", "--no-commit")
    assert code == 0 and "not committed" in out and "| C24 |" not in section_3a(lab.audit()) and lab.commits() == ["baseline"]


def test_there_is_no_option_that_extends_the_freshness_window_or_skips_a_check():
    help_text = io.StringIO()
    src = (SCRIPTS / "move_audit_row.py").read_text(encoding="utf-8")
    for forbidden in ("--max-age", "--force", "--skip", "--ignore", "--allow-stale", "--allow-mock", "--allow-fake"):
        assert forbidden not in src, forbidden
    assert help_text is not None and mar.FRESH_MINUTES == 30


def test_the_script_never_starts_a_live_run_or_touches_the_network():
    import ast
    tree = ast.parse((SCRIPTS / "move_audit_row.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"httpx", "requests", "urllib", "socket", "anthropic", "http"}, imported
    # the only subprocess it starts is git
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "run" and getattr(n.func.value, "id", "") == "subprocess":
            first = n.args[0]
            assert isinstance(first, ast.List) and getattr(first.elts[0], "value", None) == "git", ast.dump(first)[:120]


def test_the_sibling_row_is_untouched_when_one_row_moves(lab):
    before = section_3a(lab.audit())
    h = lab.record("doc")
    assert lab.run("C25", h, "--apply")[0] == 0
    after = section_3a(lab.audit())
    assert [ln for ln in before.splitlines() if ln not in after.splitlines()] == [ln for ln in before.splitlines() if ln.startswith("| C25 |")]
    assert copy.copy(after).count("| C24 |") == 1 and after.count("| C27 |") == 1


# -- D27/Q2: the record attests the CODE under test; a move refuses if that code changed since --------------------------------------

def test_unchanged_code_lets_the_row_move(lab):
    h = lab.record("planner")
    code, out = lab.run("C24", h, "--apply")
    assert code == 0, out
    assert "| C24 |" not in section_3a(lab.audit())


def test_an_edited_test_file_refuses_with_exit_18_and_names_the_file(lab):
    h = lab.record("planner")
    (lab.code / "tests" / "test_a.py").write_text("import helper\n\ndef test_a():\n    assert True  # weakened after the run\n",
                                                  encoding="utf-8", newline="\n")
    before = lab.audit()
    code, out = lab.run("C24", h, "--apply")
    assert code == 18 == mar.CODES["CODE_CHANGED"], out
    assert "REFUSED (CODE_CHANGED)" in out and "changed: tests/test_a.py" in out
    assert lab.audit() == before and lab.commits() == ["baseline"], "a refusal must change nothing"


def test_an_edit_to_a_file_the_test_exercises_is_a_code_change_too(lab):
    h = lab.record("planner")
    (lab.code / "tests" / "helper.py").write_text("ONE = 2\n", encoding="utf-8", newline="\n")
    code, out = lab.run("C24", h)
    assert code == 18 and "changed: tests/helper.py" in out


def test_a_deleted_file_refuses_with_exit_18_and_names_it(lab):
    h = lab.record("planner")
    (lab.code / "tests" / "helper.py").unlink()
    before = lab.audit()
    code, out = lab.run("C24", h, "--apply")
    assert code == 18, out
    assert "deleted: tests/helper.py" in out
    assert lab.audit() == before and lab.commits() == ["baseline"]


def test_a_deleted_test_file_is_named_too(lab):
    h = lab.record("planner")
    (lab.code / "tests" / "test_a.py").unlink()
    code, out = lab.run("C24", h)
    assert code == 18 and "deleted:" in out and "tests/test_a.py" in out.split("deleted:")[1]  # (its import closure goes with it)


def test_a_newly_imported_file_is_reported_as_added(lab):
    h = lab.record("planner")
    (lab.code / "tests" / "test_a.py").write_text("import helper\nimport extra\n", encoding="utf-8", newline="\n")
    (lab.code / "tests" / "extra.py").write_text("X = 1\n", encoding="utf-8", newline="\n")
    code, out = lab.run("C24", h)
    assert code == 18 and "changed: tests/test_a.py" in out and "added: tests/extra.py" in out


def test_a_data_file_loaded_at_run_time_is_part_of_the_code(lab):
    h = lab.record("planner")
    (lab.code / "servers" / "x_server.py").write_text("NAME = 'changed'\n", encoding="utf-8", newline="\n")
    code, out = lab.run("C24", h)
    assert code == 18 and "changed: servers/x_server.py" in out


def test_a_line_ending_only_change_is_not_a_code_change(lab):
    h = lab.record("planner")
    path = lab.code / "tests" / "test_a.py"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    code, out = lab.run("C24", h)
    assert code == 0 and "ALLOWED" in out


def test_a_record_with_no_code_attestation_cannot_close_a_row(lab):
    def strip(rec, art):
        for k in ("repo_hash", "repo_manifest_path", "repo_manifest_sha256"):
            rec.pop(k)
    h = lab.record("planner", tamper=strip)
    code, out = lab.run("C24", h)
    assert code == 14 and "no repo_hash" in out


def test_a_tampered_manifest_is_refused(lab):
    h = lab.record("planner")
    record = json.loads(lab.log.read_text(encoding="utf-8").splitlines()[-1])
    path = Path(record["repo_manifest_path"])
    man = json.loads(path.read_text(encoding="utf-8"))
    man["files"]["tests/test_a.py"] = "0" * 64
    path.write_text(json.dumps(man), encoding="utf-8")
    code, out = lab.run("C24", h)
    assert code == 14 and "manifest" in out


def test_the_exit_codes_are_distinct_and_dirty_audit_moved_to_23():
    assert mar.CODES["CODE_CHANGED"] == 18 and mar.CODES["PROVIDER"] == 19 and mar.CODES["DIRTY_AUDIT"] == 23
    assert len(set(mar.CODES.values())) == len(mar.CODES)


# -- C26: the opt-in live-API row -----------------------------------------------------------------------------------------------------

def test_the_live_apis_row_moves_only_from_14_passing_tests_and_five_hosts_attested_real(lab):
    h = lab.record("live_apis")
    code, out = lab.run("C26", h, "--apply")
    assert code == 0, out
    assert "| C26 |" not in section_3a(lab.audit())
    assert "python scripts/live_judgment.py --live-apis --allow-live" in lab.audit()


@pytest.mark.parametrize("label,over", [
    ("only 13 tests", dict(artifact_text=junit(13), tests=[f"t{i}" for i in range(13)])),
    ("a skipped test", dict(artifact_text=junit(14, skipped=1))),
    ("no --allow-live", dict(allow_live=False)),
    ("one host not real", dict(attestation={"hosts": {"a": {"real": True}, "b": {"real": True}, "c": {"real": True}, "d": {"real": True},
                                                     "e": {"real": False}}, "real": True, "non_real_requests": 0})),
    ("fewer than five hosts", dict(attestation={"hosts": {"a": {"real": True}}, "real": True, "non_real_requests": 0})),
])
def test_a_live_apis_record_that_is_not_all_14_real_is_refused(lab, label, over):
    h = lab.record("live_apis", **over)
    code, out = lab.run("C26", h, "--apply")
    assert code == 14 and lab.commits() == ["baseline"], out


# -- D27/D29: closes_gap is provider-gated; a non-Anthropic pass is recorded but cannot move an audit row --------------------------------------

def test_an_ollama_pass_record_cannot_move_c24(lab):
    # exactly what the harness writes for an Ollama run that passed: provider ollama, real false (never attested to the production host),
    # closes_gap null. It is refused for its PROVIDER (exit 19), before NOT_REAL could even be asked.
    h = lab.record("planner", provider="ollama", provider_host="localhost", real=False)
    before = lab.audit()
    got, out = lab.run("C24", h, "--apply")
    assert got == 19 and "REFUSED (PROVIDER)" in out and "'ollama'" in out and "Claude-specific" in out, out
    assert lab.audit() == before and lab.commits() == ["baseline"], "a refusal changes nothing"
    # the same record with every other field forged to look perfect (real true, closes_gap C24) is still refused
    forged = lab.record("planner", provider="ollama", provider_host="localhost", real=True, closes_gap="C24")
    got, out = lab.run("C24", forged, "--apply")
    assert got == 19 and "REFUSED (PROVIDER)" in out and lab.audit() == before and lab.commits() == ["baseline"]


@pytest.mark.parametrize("provider,host", [("ollama", "localhost"), ("freellmpool", "pool.test"), ("openrouter", "openrouter.ai"), ("mystery", "x")])
@pytest.mark.parametrize("row,sub", [("C24", "planner"), ("C25", "doc"), ("C27", "apply")])
def test_no_non_anthropic_provider_can_move_any_claude_specific_row(lab, provider, host, row, sub):
    h = lab.record(sub, provider=provider, provider_host=host)
    got, out = lab.run(row, h, "--apply")
    assert got == 19 and "REFUSED (PROVIDER)" in out, out
    assert lab.commits() == ["baseline"]


PROVIDER_FIELD_CASES = [
    ("closes_gap null", dict(closes_gap=None)), ("closes_gap names another row", dict(closes_gap="C25")),
    ("no provider field", dict(provider=None)), ("production host replaced by a proxy", dict(provider_host="gateway.example.test")),
    ("closes_gap forged on a pass the fields do not support", dict(provider_host="gateway.example.test", closes_gap="C24")),
]


@pytest.mark.parametrize("label,over", PROVIDER_FIELD_CASES, ids=[c[0] for c in PROVIDER_FIELD_CASES])
def test_an_anthropic_record_whose_closes_gap_does_not_match_the_row_is_refused_with_19(lab, label, over):
    h = lab.record("planner", **over)
    got, out = lab.run("C24", h, "--apply")
    assert got == 19 and "REFUSED (PROVIDER)" in out, out
    assert lab.commits() == ["baseline"]


def test_an_anthropic_real_pass_still_moves_its_row_and_the_provider_is_in_the_record(lab):
    h = lab.record("planner")
    rec = json.loads(lab.log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["provider"] == "anthropic" and rec["closes_gap"] == "C24"
    got, out = lab.run("C24", h, "--apply")
    assert got == 0, out
    assert len(lab.commits()) == 2 and "| C24 |" not in lab.audit().split("### 2b. Proven live")[0].split("3a")[-1]


def test_the_third_party_api_row_is_not_provider_gated_but_still_needs_its_own_closes_gap(lab):
    bad = lab.record("live_apis", closes_gap=None)
    got, out = lab.run("C26", bad, "--apply")
    assert got == 19 and "REFUSED (PROVIDER)" in out and lab.commits() == ["baseline"]
    h = lab.record("live_apis", provider="ollama", provider_host="localhost")  # C26 involves no LLM, so the provider is irrelevant
    got, out = lab.run("C26", h, "--apply")
    assert got == 0, out


def test_the_provider_check_runs_before_not_real_so_a_real_ollama_run_is_told_why():
    assert mar.CODES["MOCK"] == 17  # the mock check is still first
    import inspect
    src = inspect.getsource(mar.check_record)
    assert src.index('"MOCK"') < src.index('"PROVIDER"') < src.index('"NOT_REAL"') < src.index('"NOT_PASS"') < src.index('"WRONG_SUBCOMMAND"')
