"""The SUCCESS path of scripts/move_audit_row.py, as the CLI a maintainer runs (CLAUDE.md D27, D33).

test_move_audit_row.py drives `main()` in-process against a fake code tree. This file runs the SCRIPT: `python scripts/move_audit_row.py
--row C24 --apply --record-hash H` as a subprocess, inside a throwaway git repo that MIRRORS the real one (every file the planner's
code attestation names, AUDIT.md, CLAUDE.md, scripts/, agent/, learning/), against a synthetic `real: true` record from provider
`anthropic` written to that repo's `.perf/live_judgment.jsonl` and attested with the harness's own `repo_hash.for_subcommand`. No key, no
network, no live call: the point is that when a real Anthropic record exists, the last step (move the row, commit it) is already known to work.

Then each tamper of the record is run through the same CLI and must be refused, with the exit code that names it, and with AUDIT.md
and the git history untouched. Every tampered record differs from the valid one in ONE field (asserted), so a refusal cannot come from
some other breakage. What this does not do: the records are built here in the real schema, not written by the live harness, and nothing
here calls Anthropic."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent.provider_config import ANTHROPIC_HOST

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
ROW, SUB, N_TESTS = "C24", "planner", 6
GIT = ["git", "-c", "user.name=mirror", "-c", "user.email=mirror@example.test", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def _load(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


repo_hash = _load("repo_hash")
live_judgment = _load("live_judgment")


@pytest.fixture(scope="module", autouse=True)
def real_files_untouched():
    audit, log = ROOT / "AUDIT.md", ROOT / ".perf" / "live_judgment.jsonl"
    before = (audit.read_bytes(), log.read_bytes() if log.exists() else None)
    yield
    assert audit.read_bytes() == before[0], "a test changed the real AUDIT.md"
    assert (log.read_bytes() if log.exists() else None) == before[1], "a test changed the real live_judgment.jsonl"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr}"
    return done.stdout


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    """A mirror of the real repo at the files that matter: everything the planner attestation names, the audit documents and the
    three code directories the script lives among, committed once. Each test gets its own copy."""
    t = tmp_path_factory.mktemp("mirror")
    wanted = set(repo_hash.for_subcommand(SUB, ROOT)["files"]) | {"AUDIT.md", "CLAUDE.md"}
    wanted |= {p.relative_to(ROOT).as_posix() for d in ("scripts", "agent", "learning") for p in (ROOT / d).rglob("*.py")
               if "__pycache__" not in p.parts}
    for rel in sorted(wanted):
        dst = t / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dst)
    git(t, "init", "-q")
    git(t, "add", "-A")
    git(t, "commit", "-q", "-m", "baseline")
    return t


@pytest.fixture
def repo(template, tmp_path) -> Path:
    dest = tmp_path / "repo"
    shutil.copytree(template, dest)
    return dest


# -- building a valid record, the way the harness would have written it ---------------------------------------------------------------

def junit(n: int) -> str:
    cases = "".join(f'<testcase classname="tests.t" name="test_{i}"/>' for i in range(n))
    return f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="{n}">{cases}</testsuite></testsuites>'


def valid_record(repo: Path) -> dict:
    """A fresh, real, passing planner record from provider anthropic, attesting the code of `repo` as it is now."""
    man = repo_hash.for_subcommand(SUB, repo)
    stamp_dir = repo / ".perf" / "live_judgment"
    sidecar = stamp_dir / "20261115T000000Z_planner.manifest.json"
    sidecar_sha = repo_hash.write_sidecar(sidecar, man)
    art = stamp_dir / "20261115T000000Z_planner.xml"
    art.write_text(junit(N_TESTS), encoding="utf-8")
    return {
        "provider": "anthropic", "provider_host": ANTHROPIC_HOST, "provider_model": "claude-sonnet-5-5",
        "subcommand": SUB, "result": "pass", "reason": "6 test(s) ran and none failed or was skipped", "exit_code": 0,
        "real": True, "mock": False, "closes_gap": ROW, "key_present": True, "allow_live": False, "upstream": None,
        "date": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat(),
        "tests": [f"tests/scenarios/test_planner_contract.py::test_{i}" for i in range(N_TESTS)],
        # what the attestation hook summarises: three answered requests to the production host, all over httpx.HTTPTransport
        "attestation": {"host": ANTHROPIC_HOST, "real": True, "real_requests": 3, "non_real_requests": 0},
        "artifact_path": str(art), "artifact_sha256": hashlib.sha256(art.read_bytes()).hexdigest(),
        "repo_hash": repo_hash.digest(man), "repo_manifest_path": str(sidecar), "repo_manifest_sha256": sidecar_sha,
    }


def append(repo: Path, rec: dict) -> str:
    """Write the record as one log line; return the SHA-256 of that line, which is its name (RECORD_HASH)."""
    line = json.dumps(rec, sort_keys=True)
    log = repo / ".perf" / "live_judgment.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def run_cli(repo: Path, *args: str) -> tuple[int, str]:
    """`python scripts/move_audit_row.py ...` in the mirror, with no provider, key or log override from this process's environment."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PROVIDER_", "ANTHROPIC_", "LIVE_"))}
    env["PYTHONUTF8"] = "1"  # AUDIT.md rows hold non-ASCII characters; do not depend on the console code page
    done = subprocess.run([sys.executable, str(repo / "scripts" / "move_audit_row.py"), *args], cwd=repo, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=180)
    return done.returncode, done.stdout + done.stderr


def section_3a(text: str) -> str:
    return text[text.index("### 3a."):text.index("### 3b.")]


# -- the success path ---------------------------------------------------------------------------------------------------------------------

def test_the_mirror_attests_the_same_code_as_the_real_repo_and_the_record_is_what_the_harness_would_have_derived(repo):
    rec = valid_record(repo)
    assert rec["repo_hash"] == repo_hash.digest(repo_hash.for_subcommand(SUB, ROOT)), "the mirror is the real planner code, byte for byte"
    assert live_judgment.closes_gap(SUB, rec["provider"], rec["provider_host"], rec["result"], rec["real"]) == ROW == rec["closes_gap"]


def test_a_valid_real_anthropic_record_moves_its_row_and_commits_exactly_that_move(repo):
    baseline = (repo / "AUDIT.md").read_text(encoding="utf-8")
    old_row = next(ln for ln in section_3a(baseline).splitlines() if ln.startswith(f"| {ROW} |"))
    h = append(repo, valid_record(repo))

    code, out = run_cli(repo, "--row", ROW, "--apply", "--record-hash", h)

    assert code == 0, out
    assert f"MOVED: {ROW} -> AUDIT.md section 2b (record {h[:12]})" in out
    text = (repo / "AUDIT.md").read_text(encoding="utf-8")
    assert f"| {ROW} |" not in section_3a(text), "gone from 3a"
    block = text[text.index("### 2b. Proven live"):text.index("## 3. NOT PROVEN")]
    assert f"| {ROW} |" in block and f"`{h[:12]}`" in block, "present in 2b, citing the record"
    assert all(f"| {other} |" in section_3a(text) for other in ("C25", "C27", "C26")), "the sibling rows stay open"

    assert git(repo, "rev-list", "--count", "HEAD").strip() == "2", "exactly one new commit"
    message = git(repo, "show", "-s", "--format=%B", "HEAD")
    assert message.splitlines()[0] == f"audit: close live judgment gap {ROW} ({h[:12]})" and h in message, "the full record hash is in the message"
    assert git(repo, "diff", "--name-only", "HEAD~1", "HEAD").split() == ["AUDIT.md"], "the commit touches AUDIT.md and nothing else"
    assert git(repo, "status", "--porcelain", "--untracked-files=no").strip() == "", "nothing left uncommitted"

    # the diff is exactly the row move: the one 3a line goes, and only the 2b block (heading, text, table with the row) arrives
    diff = git(repo, "diff", "-U0", "--no-color", "HEAD~1", "HEAD", "--", "AUDIT.md").splitlines()
    removed = [ln[1:] for ln in diff if ln.startswith("-") and not ln.startswith("---")]
    added = [ln[1:] for ln in diff if ln.startswith("+") and not ln.startswith("+++")]
    assert removed == [old_row], "the only line that leaves is the 3a row"
    assert all(ln == "" or ln.startswith(("### 2b. Proven live", "These rows were closed", "| ID |", "|---|", f"| {ROW} |")) for ln in added), \
        f"only the 2b block arrives, got: {[ln[:60] for ln in added]}"
    new_row = [ln for ln in added if ln.startswith(f"| {ROW} |")]
    claim = next(ln.split("|")[2].strip() for ln in baseline[:baseline.index("### 3a.")].splitlines() if ln.startswith(f"| {ROW} |"))
    assert len(new_row) == 1 and new_row[0].split("|")[2].strip() == claim, "the row arrives with its section 1 claim text, unchanged"


# -- tampering: one field of the record changed, the CLI must refuse and change nothing ----------------------------------------------

def refuse(repo: Path, tamper, expect_fields: set[str], code: int, name: str, needle: str = "") -> None:
    rec = valid_record(repo)
    valid = copy.deepcopy(rec)
    tamper(rec)
    assert {k for k in rec if rec[k] != valid.get(k)} == expect_fields, "the tampered record differs from the valid one in exactly this field"
    h = append(repo, rec)
    before = (repo / "AUDIT.md").read_bytes()
    got, out = run_cli(repo, "--row", ROW, "--apply", "--record-hash", h)
    assert got == code and f"REFUSED ({name})" in out and needle in out, f"expected exit {code} {name}, got {got}:\n{out}"
    assert (repo / "AUDIT.md").read_bytes() == before and git(repo, "rev-list", "--count", "HEAD").strip() == "1", "nothing moved, nothing committed"


def test_mutation_a_a_forged_repo_hash_is_refused_the_record_does_not_match_its_own_manifest(repo):
    refuse(repo, lambda r: r.update(repo_hash="0" * 64), {"repo_hash"}, 14, "EVIDENCE_MISMATCH", "does not hash to the record's repo_hash")


def test_mutation_a_code_edited_after_the_run_is_refused_with_exit_18_naming_the_file(repo):
    rec = valid_record(repo)
    h = append(repo, rec)
    target = repo / "tests" / "scenarios" / "test_planner_contract.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# edited after the run\n", encoding="utf-8", newline="\n")
    before = (repo / "AUDIT.md").read_bytes()
    code, out = run_cli(repo, "--row", ROW, "--apply", "--record-hash", h)
    assert code == 18 and "REFUSED (CODE_CHANGED)" in out and "tests/scenarios/test_planner_contract.py" in out, out
    assert (repo / "AUDIT.md").read_bytes() == before and git(repo, "rev-list", "--count", "HEAD").strip() == "1"


def test_mutation_b_a_provider_that_is_not_anthropic_is_refused_with_exit_19(repo):
    refuse(repo, lambda r: r.update(provider="ollama"), {"provider"}, 19, "PROVIDER", "'ollama'")


@pytest.mark.parametrize("label,tamper,fields,code,name", [
    ("one request went over a mock transport (it poisons the attestation)",
     lambda r: r["attestation"].update(non_real_requests=1), {"attestation"}, 14, "EVIDENCE_MISMATCH"),
    ("the attestation says the transport was not the real one but the record still claims real",
     lambda r: r["attestation"].update(real=False), {"attestation"}, 14, "EVIDENCE_MISMATCH"),
    ("the record says it came from the offline mock", lambda r: r.update(mock=True), {"mock"}, 17, "MOCK"),
], ids=["poisoned-attestation", "attestation-not-real", "mock-flag"])
def test_mutation_c_a_mock_transport_is_refused(repo, label, tamper, fields, code, name):
    refuse(repo, tamper, fields, code, name)


def test_mutation_d_a_failing_result_is_refused_with_exit_12(repo):
    refuse(repo, lambda r: r.update(result="fail"), {"result"}, 12, "NOT_PASS", "'fail'")


def test_a_record_edited_after_its_hash_was_printed_is_not_found(repo):
    """The hash names the exact line. Rewrite the line and the hash the maintainer holds names nothing."""
    h = append(repo, valid_record(repo))
    log = repo / ".perf" / "live_judgment.jsonl"
    log.write_text(log.read_text(encoding="utf-8").replace('"real": true', '"real": true, "note": "edited"'), encoding="utf-8", newline="\n")
    before = (repo / "AUDIT.md").read_bytes()
    code, out = run_cli(repo, "--row", ROW, "--apply", "--record-hash", h)
    assert code == 10 and "REFUSED (NO_RECORD)" in out and (repo / "AUDIT.md").read_bytes() == before
