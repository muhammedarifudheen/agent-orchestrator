"""Tests for scripts/live_run_ceremony.py: offline, no network, an injected runner and fake child commands."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("live_run_ceremony", ROOT / "scripts" / "live_run_ceremony.py")
cer = importlib.util.module_from_spec(_spec)
sys.modules["live_run_ceremony"] = cer
_spec.loader.exec_module(cer)

KEY = "sk-ant-FAKEKEY-9f8e7d6c5b4a-do-not-leak"
URL = "https://httpbin.org/status/418"
HASHES = {"--planner": "a" * 64, "--doc": "b" * 64, "--apply": "c" * 64}


def _flag(argv):
    for f in ("--planner", "--doc", "--apply"):
        if f in argv:
            return f
    return None


class Fake:
    """Scripted runner. `step_exit` maps a live_judgment flag to its exit code; `row_exit` maps a row to move's exit code."""

    def __init__(self, step_exit=None, row_exit=None, no_hash=(), echo_key=None, offline_real=False):
        self.calls = []
        self.step_exit = step_exit or {}
        self.row_exit = row_exit or {}
        self.no_hash = set(no_hash)
        self.echo_key = echo_key
        self.offline_real = offline_real

    def __call__(self, argv, env, timeout):
        self.calls.append((list(argv), dict(env)))
        script = Path(argv[1]).name if len(argv) > 1 else argv[0]
        if script == "live_judgment.py":
            flag = _flag(argv)
            code = self.step_exit.get(flag, 0)
            out = "running " + flag + "\n"
            if self.echo_key:
                out += f"debug: key was {self.echo_key}\n"
            if flag in self.no_hash or code != 0:
                return code, out
            log = env.get("LIVE_JUDGMENT_LOG")
            h = HASHES[flag]
            if log:  # offline: write a record that hashes to H' (the hash of the line we write)
                line = json.dumps({"subcommand": flag, "real": self.offline_real, "result": "pass"})
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                h = hashlib.sha256(line.encode()).hexdigest()
                HASHES_SEEN[flag] = h
            return code, out + f"RECORD_HASH: {h}\n"
        if script == "move_audit_row.py":
            row = argv[argv.index("--row") + 1]
            code = self.row_exit.get(row, 0)
            return code, (f"MOVED: {row} ok\n" if code == 0 else f"REFUSED (X): {row}\n")
        if script == "git" or argv[0] == "git":
            return 0, "abc1234 move C27\n"
        raise AssertionError(f"unexpected command {argv}")


HASHES_SEEN: dict = {}


def run(argv, env=None, runner=None, tmp=None):
    out, err = io.StringIO(), io.StringIO()
    if tmp is not None and "--transcript-dir" not in argv:
        argv = list(argv) + ["--transcript-dir", str(tmp)]
    code = cer.main(argv, env={} if env is None else env, stdout=out, stderr=err, runner=runner or Fake())
    return code, out.getvalue(), err.getvalue()


LIVE = ["--allow-live", "--upstream", URL]
GOODENV = {"ANTHROPIC_API_KEY": KEY}


def transcripts(tmp):
    return sorted(Path(tmp).glob("*.json"))


# -- refusals ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv,env,code,needles", [
    (LIVE, {}, 20, ["ANTHROPIC_API_KEY"]),
    (["--upstream", URL], {"ANTHROPIC_API_KEY": KEY}, 21, ["--allow-live"]),
    (["--allow-live"], {"ANTHROPIC_API_KEY": KEY}, 22, ["--upstream"]),
    (["--allow-live", "--upstream", "http://127.0.0.1:8000/status/418"], GOODENV, 22, ["loopback", "offline proof only"]),
    (["--allow-live", "--upstream", "http://localhost/x"], GOODENV, 22, ["loopback"]),
    (["--allow-live", "--upstream", "http://10.0.0.5/x"], GOODENV, 22, ["private"]),
    (LIVE, {"ANTHROPIC_AUTH_TOKEN": "tok"}, 20, ["ANTHROPIC_AUTH_TOKEN alone does not count"]),
    ([], {}, 20, ["ANTHROPIC_API_KEY", "--allow-live", "--upstream"]),
    ([], {"ANTHROPIC_API_KEY": ""}, 20, []),
])
def test_refusals_run_nothing_and_write_nothing(tmp_path, argv, env, code, needles):
    fake = Fake()
    got, out, err = run(argv, env, fake, tmp_path)
    assert got == code
    assert "CEREMONY_REFUSED" in out
    for n in needles:
        assert n in out
    assert fake.calls == []
    assert transcripts(tmp_path) == []
    assert "TRANSCRIPT:" not in out


def test_every_missing_item_is_reported_and_the_first_decides_the_code(tmp_path):
    got, out, _ = run([], {}, Fake(), tmp_path)
    assert got == 20
    assert out.count("CEREMONY_REFUSED") == 3


def test_refusal_message_never_contains_a_key(tmp_path):
    _, out, _ = run(["--upstream", URL], GOODENV, Fake(), tmp_path)
    assert KEY not in out


def test_usage_error_is_64(tmp_path):
    got, _, err = run(["--nope"], GOODENV, Fake(), tmp_path)
    assert got == 64 and "usage error" in err


# -- live success ------------------------------------------------------------------------------------------------------

def test_three_steps_then_three_moves_in_order_with_the_right_hashes(tmp_path):
    fake = Fake()
    got, out, _ = run(LIVE, GOODENV, fake, tmp_path)
    assert got == 0
    py = sys.executable
    cmds = [c for c, _ in fake.calls]
    assert cmds[0] == [py, "scripts/live_judgment.py", "--planner"]
    assert cmds[1] == [py, "scripts/live_judgment.py", "--doc"]
    assert cmds[2] == [py, "scripts/live_judgment.py", "--apply", URL, "--allow-live"]
    assert cmds[3] == [py, "scripts/move_audit_row.py", "--row", "C24", "--apply", "--record-hash", "a" * 64]
    assert cmds[4] == [py, "scripts/move_audit_row.py", "--row", "C25", "--apply", "--record-hash", "b" * 64]
    assert cmds[5] == [py, "scripts/move_audit_row.py", "--row", "C27", "--apply", "--record-hash", "c" * 64]
    assert cmds[6] == ["git", "log", "--oneline", "-5"]
    assert len(cmds) == 7
    assert "python -m pytest --strict-budgets -q -rs" in out
    assert "stays at 26 skipped" in out and "shows 5 skipped" in out and "19 skipped" not in out
    (t,) = transcripts(tmp_path)
    assert f"TRANSCRIPT: {t}" in out.strip().splitlines()[-3] or f"TRANSCRIPT: {t}" in out
    doc = json.loads(t.read_text(encoding="utf-8"))
    assert doc["result"] == "complete" and doc["mode"] == "live"
    assert [e["row"] for e in doc["audit_edits"]] == ["C24", "C25", "C27"]
    assert all(e["moved"] for e in doc["audit_edits"])
    assert "abc1234" in doc["git_log"]


def test_audit_and_log_are_passed_through_to_the_move_commands(tmp_path):
    fake = Fake()
    run(LIVE + ["--audit", "A.md", "--log", "L.jsonl"], GOODENV, fake, tmp_path)
    move = fake.calls[3][0]
    assert move[-4:] == ["--audit", "A.md", "--log", "L.jsonl"]


def test_child_env_carries_the_key_and_runs_from_the_given_env(tmp_path):
    fake = Fake()
    run(LIVE, GOODENV, fake, tmp_path)
    assert fake.calls[0][1]["ANTHROPIC_API_KEY"] == KEY


# -- live failure ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("failing,ran", [("--planner", 1), ("--doc", 2), ("--apply", 3)])
def test_stops_at_the_first_failed_step_and_never_touches_audit(tmp_path, failing, ran):
    fake = Fake(step_exit={failing: 1})
    got, out, _ = run(LIVE, GOODENV, fake, tmp_path)
    assert got == 1
    assert len(fake.calls) == ran
    assert not any("move_audit_row" in " ".join(c) for c, _ in fake.calls)
    assert "CEREMONY_STOPPED at step" in out and "AUDIT.md was NOT edited" in out
    (t,) = transcripts(tmp_path)
    doc = json.loads(t.read_text(encoding="utf-8"))
    assert doc["result"] == "stopped" and len(doc["steps"]) == ran and doc["audit_edits"] == []
    assert doc["steps"][-1]["exit_code"] == 1


def test_exact_stop_message(tmp_path):
    _, out, _ = run(LIVE, GOODENV, Fake(step_exit={"--doc": 2}), tmp_path)
    assert ("CEREMONY_STOPPED at step 2 (doc): exit 2. AUDIT.md was NOT edited. Fix the cause and re-run the failed step; "
            "do not edit AUDIT.md by hand.") in out


def test_a_zero_exit_without_a_record_hash_is_a_failure(tmp_path):
    fake = Fake(no_hash={"--planner"})
    got, out, _ = run(LIVE, GOODENV, fake, tmp_path)
    assert got == 1
    assert len(fake.calls) == 1
    assert "RECORD_HASH" in out and "AUDIT.md was NOT edited" in out


def test_the_last_record_hash_line_wins(tmp_path):
    class Two(Fake):
        def __call__(self, argv, env, timeout):
            code, out = super().__call__(argv, env, timeout)
            if "--planner" in argv:
                out = "RECORD_HASH: " + "9" * 64 + "\n" + out
            return code, out
    fake = Two()
    run(LIVE, GOODENV, fake, tmp_path)
    assert fake.calls[3][0][-1] == "a" * 64


@pytest.mark.parametrize("bad,moves", [("C24", 1), ("C25", 2), ("C27", 3)])
def test_a_refused_row_stops_the_ceremony(tmp_path, bad, moves):
    fake = Fake(row_exit={bad: 12})
    got, out, _ = run(LIVE, GOODENV, fake, tmp_path)
    assert got == 1
    moved = [c for c, _ in fake.calls if "move_audit_row" in " ".join(c)]
    assert len(moved) == moves
    assert not any(c[0] == "git" for c, _ in fake.calls)
    assert bad in out
    (t,) = transcripts(tmp_path)
    doc = json.loads(t.read_text(encoding="utf-8"))
    assert doc["result"] == "refused_row"
    assert [e["moved"] for e in doc["audit_edits"]][-1] is False
    assert [e["row"] for e in doc["audit_edits"] if e["moved"]] == ["C24", "C25", "C27"][:moves - 1]


# -- the key never leaks -----------------------------------------------------------------------------------------------

def test_the_key_never_appears_in_output_or_transcript(tmp_path):
    fake = Fake(echo_key=KEY)
    got, out, err = run(LIVE, GOODENV, fake, tmp_path)
    assert got == 0
    (t,) = transcripts(tmp_path)
    text = t.read_text(encoding="utf-8")
    assert KEY not in text and KEY not in out and KEY not in err
    assert "[REDACTED]" in text
    doc = json.loads(text)
    assert doc["key_present"] is True


def test_the_key_is_redacted_from_a_failed_step_too(tmp_path):
    fake = Fake(step_exit={"--planner": 1}, echo_key=KEY)
    _, out, _ = run(LIVE, GOODENV, fake, tmp_path)
    (t,) = transcripts(tmp_path)
    assert KEY not in t.read_text(encoding="utf-8") and KEY not in out


# -- dry run -----------------------------------------------------------------------------------------------------------

def test_dry_run_prints_the_whole_plan_and_executes_nothing(tmp_path):
    fake = Fake()
    got, out, _ = run(["--dry-run"], {}, fake, tmp_path)
    assert got == 0
    assert fake.calls == [] and transcripts(tmp_path) == []
    py = sys.executable
    assert f"{py} scripts/live_judgment.py --planner" in out
    assert f"{py} scripts/live_judgment.py --doc" in out
    assert f"scripts/live_judgment.py --apply {cer.PLACEHOLDER_URL} --allow-live" in out
    for i, row in enumerate(("C24", "C25", "C27"), 1):
        assert f"--row {row} --apply --record-hash <record-hash of step {i}>" in out
    assert "exit 20" in out and "exit 21" in out and "exit 22" in out
    assert "TRANSCRIPT would be written to" in out and str(tmp_path) in out
    assert "git log --oneline -5" in out and "--strict-budgets" in out


def test_dry_run_with_an_upstream_shows_it(tmp_path):
    _, out, _ = run(["--dry-run", "--upstream", URL, "--audit", "A.md"], GOODENV, Fake(), tmp_path)
    assert f"--apply {URL} --allow-live" in out and "--audit A.md" in out


def test_dry_run_offline_plan(tmp_path):
    fake = Fake()
    got, out, _ = run(["--dry-run", "--offline-mock"], {}, fake, tmp_path)
    assert got == 0 and fake.calls == [] and transcripts(tmp_path) == []
    assert "--mock-transport" in out and "--allow-loopback" in out and "--no-commit" in out


def test_dry_run_never_prints_the_key(tmp_path):
    _, out, _ = run(["--dry-run", "--upstream", URL], GOODENV, Fake(), tmp_path)
    assert KEY not in out


# -- offline mock ------------------------------------------------------------------------------------------------------

def test_offline_mock_ok_when_every_move_is_refused(tmp_path):
    audit = ROOT / "AUDIT.md"
    before = audit.read_bytes() if audit.exists() else None
    fake = Fake(row_exit={"C24": 11, "C25": 17, "C27": 11})
    got, out, _ = run(["--offline-mock"], {"ANTHROPIC_AUTH_TOKEN": "x"}, fake, tmp_path)
    assert got == 0, out
    assert "OFFLINE PROOF OK: all three subcommands passed on mocks; every row move was refused because the records are real:false" in out
    assert "WHAT A HUMAN WOULD DO WITH A REAL KEY:" in out
    assert "python scripts/live_run_ceremony.py --allow-live --upstream https://httpbin.org/status/418" in out
    assert "docs/LIVE_RUN.md" in out
    cmds = [c for c, _ in fake.calls]
    assert "--mock-transport" in cmds[0] and "--planner" in cmds[0]
    assert "--mock-transport" in cmds[1] and "--doc" in cmds[1]
    assert cmds[2][2] == "--apply" and cmds[2][3].startswith("http://127.0.0.1:") and cmds[2][3].endswith("/status/418")
    assert cmds[2][-1] == "--allow-loopback"
    env0 = fake.calls[0][1]
    assert env0["ANTHROPIC_API_KEY"] == cer.OFFLINE_KEY and "ANTHROPIC_AUTH_TOKEN" not in env0
    assert Path(env0["LIVE_JUDGMENT_LOG"]).parent == tmp_path
    for row, c in zip(("C24", "C25", "C27"), cmds[3:6]):
        assert c[c.index("--row") + 1] == row and "--no-commit" in c
        assert Path(c[c.index("--audit") + 1]).parent == tmp_path
        assert Path(c[c.index("--log") + 1]) == Path(env0["LIVE_JUDGMENT_LOG"])
    assert len(cmds) == 6
    (t,) = [p for p in transcripts(tmp_path)]
    doc = json.loads(t.read_text(encoding="utf-8"))
    assert doc["result"] == "offline_proof_ok" and doc["mode"] == "offline-mock"
    assert (audit.read_bytes() if audit.exists() else None) == before


def test_offline_mock_fails_when_a_move_succeeds(tmp_path):
    fake = Fake(row_exit={"C24": 11, "C25": 0, "C27": 11})
    got, out, _ = run(["--offline-mock"], {}, fake, tmp_path)
    assert got == 1
    assert "OFFLINE PROOF FAILED" in out and "C25" in out and "OFFLINE PROOF OK" not in out
    (t,) = transcripts(tmp_path)
    assert json.loads(t.read_text(encoding="utf-8"))["result"] == "offline_proof_failed"


def test_offline_mock_fails_when_the_first_move_succeeds_and_stops(tmp_path):
    fake = Fake(row_exit={"C24": 0})
    got, _, _ = run(["--offline-mock"], {}, fake, tmp_path)
    assert got == 1
    assert len([c for c, _ in fake.calls if "move_audit_row" in " ".join(c)]) == 1


def test_offline_mock_fails_on_a_real_true_record(tmp_path):
    fake = Fake(row_exit={"C24": 11, "C25": 11, "C27": 11}, offline_real=True)
    got, out, _ = run(["--offline-mock"], {}, fake, tmp_path)
    assert got == 1 and "real=True" in out


def test_offline_mock_fails_on_a_failed_step(tmp_path):
    fake = Fake(step_exit={"--doc": 1})
    got, _, _ = run(["--offline-mock"], {}, fake, tmp_path)
    assert got == 1 and len(fake.calls) == 2


def test_offline_mock_needs_no_key_and_no_allow_live(tmp_path):
    got, _, _ = run(["--offline-mock"], {}, Fake(row_exit={"C24": 11, "C25": 11, "C27": 11}), tmp_path)
    assert got == 0


def test_offline_mock_loopback_server_really_answers_418(tmp_path):
    import urllib.error
    import urllib.request

    seen = {}

    class Probe(Fake):
        def __call__(self, argv, env, timeout):
            if "--apply" in argv and "live_judgment.py" in argv[1]:
                try:
                    urllib.request.urlopen(argv[3], timeout=5)
                except urllib.error.HTTPError as exc:
                    seen["status"] = exc.code
                    seen["body"] = exc.read()
            return super().__call__(argv, env, timeout)

    run(["--offline-mock"], {}, Probe(row_exit={"C24": 11, "C25": 11, "C27": 11}), tmp_path)
    assert seen["status"] == 418 and json.loads(seen["body"])


def test_offline_mock_never_uses_the_repo_log(tmp_path):
    fake = Fake(row_exit={"C24": 11, "C25": 11, "C27": 11})
    run(["--offline-mock"], {"LIVE_JUDGMENT_LOG": str(ROOT / ".perf" / "live_judgment.jsonl")}, fake, tmp_path)
    assert all(Path(e["LIVE_JUDGMENT_LOG"]).parent == tmp_path for _, e in fake.calls)


# -- transcript schema -------------------------------------------------------------------------------------------------

def test_transcript_schema(tmp_path):
    run(LIVE, GOODENV, Fake(), tmp_path)
    (t,) = transcripts(tmp_path)
    assert t.name.endswith("Z.json") and len(t.stem) == 16
    doc = json.loads(t.read_text(encoding="utf-8"))
    assert set(doc) == {"ceremony_id", "mode", "started", "finished", "upstream", "allow_live", "key_present", "steps",
                        "audit_edits", "git_log", "result", "next"}
    assert doc["ceremony_id"] == t.stem and doc["upstream"] == URL and doc["allow_live"] is True
    for i, s in enumerate(doc["steps"], 1):
        assert set(s) == {"n", "name", "command", "exit_code", "output", "record_hash"} and s["n"] == i
    assert [s["name"] for s in doc["steps"]] == ["planner", "doc", "apply"]
    assert [s["record_hash"] for s in doc["steps"]] == ["a" * 64, "b" * 64, "c" * 64]
    for e in doc["audit_edits"]:
        assert set(e) == {"row", "command", "exit_code", "output", "moved"}
    assert isinstance(doc["next"], list) and doc["next"]


def test_default_transcript_dir_is_perf_ceremony():
    assert cer.ROOT / ".perf" / "ceremony" == cer.Path(cer.ROOT) / ".perf" / "ceremony"
    out = io.StringIO()
    cer.main(["--dry-run"], env={}, stdout=out, stderr=io.StringIO(), runner=Fake())
    assert str(ROOT / ".perf" / "ceremony") in out.getvalue()
