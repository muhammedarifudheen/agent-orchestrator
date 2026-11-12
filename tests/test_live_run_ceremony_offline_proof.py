"""The live-run ceremony, proven end to end OFFLINE (CLAUDE.md D26/D27), and the documentation rules around it.

What this proves: the whole ceremony (planner, doc, apply, then the row moves) runs on mocks and a loopback upstream, every
record comes out real=false, and every row move is REFUSED, so a mock is incapable of closing a gap. It then tries to cheat:
it forges the flags of those records (mock=false, real=true) and shows that the evidence re-check still refuses them.
What it does NOT prove: anything about a real model or a real upstream."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "LIVE_"))}


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@pytest.fixture(scope="module")
def ceremony(tmp_path_factory):
    """Run the offline ceremony ONCE (about 40 s); the tests below read its outputs."""
    out_dir = tmp_path_factory.mktemp("ceremony")
    audit, log = ROOT / "AUDIT.md", ROOT / ".perf" / "live_judgment.jsonl"
    before = (sha(audit), sha(log))
    status_before = subprocess.run(["git", "-C", str(ROOT), "status", "--short"], capture_output=True, text=True).stdout
    done = subprocess.run([sys.executable, str(SCRIPTS / "live_run_ceremony.py"), "--offline-mock", "--transcript-dir", str(out_dir)],
                          cwd=ROOT, env=clean_env(), capture_output=True, text=True, timeout=400)
    transcripts = sorted(out_dir.glob("2026*.json")) or sorted(out_dir.glob("*.json"))
    transcript = json.loads(transcripts[-1].read_text(encoding="utf-8")) if transcripts else None
    records = [json.loads(line) for line in (out_dir / "live_judgment.offline.jsonl").read_text(encoding="utf-8").splitlines()] \
        if (out_dir / "live_judgment.offline.jsonl").exists() else []
    return {"code": done.returncode, "out": done.stdout + done.stderr, "dir": out_dir, "transcript": transcript,
            "records": records, "before": before, "after": (sha(audit), sha(log)),
            "status_before": status_before,
            "status_after": subprocess.run(["git", "-C", str(ROOT), "status", "--short"], capture_output=True, text=True).stdout}


def test_the_offline_ceremony_completes_and_prints_what_a_human_would_do_with_a_real_key(ceremony):
    out = ceremony["out"]
    assert ceremony["code"] == 0, out[-1500:]
    assert "OFFLINE PROOF OK: all three subcommands passed on mocks; every row move was refused because the records are real:false" in out
    assert "WHAT A HUMAN WOULD DO WITH A REAL KEY:" in out
    assert "python scripts/live_run_ceremony.py --allow-live --upstream https://httpbin.org/status/418" in out
    assert "docs/LIVE_RUN.md" in out and "TRANSCRIPT:" in out


def test_all_three_subcommands_exit_0_and_all_three_records_are_real_false_with_the_key_present(ceremony):
    t = ceremony["transcript"]
    assert t["mode"] == "offline-mock" and t["result"] == "offline_proof_ok"
    assert [(s["name"], s["exit_code"]) for s in t["steps"]] == [("planner", 0), ("doc", 0), ("apply", 0)]
    recs = ceremony["records"][-3:]
    assert [r["subcommand"] for r in recs] == ["planner", "doc", "apply"]
    for r in recs:
        assert r["result"] == "pass" and r["exit_code"] == 0
        assert r["real"] is False, f"{r['subcommand']}: a mock or loopback run produced real=true"
        assert r["key_present"] is True  # the mocked key counts as a key present; it still cannot make a run real
        assert r["allow_live"] is False
    assert [r["mock"] for r in recs] == [True, True, False]
    assert all(r["attestation"]["transports"] == ["live_mock.OfflineAnthropicMock"] for r in recs[:2])
    assert recs[0]["attestation"]["real_requests"] == 0 and recs[1]["attestation"]["real_requests"] == 0


def test_every_row_move_is_refused_so_a_mock_cannot_close_a_gap(ceremony):
    edits = ceremony["transcript"]["audit_edits"]
    assert [e["row"] for e in edits] == ["C24", "C25", "C27"]
    assert all(e["moved"] is False for e in edits)
    assert [e["exit_code"] for e in edits] == [17, 17, 11]
    assert "REFUSED (MOCK)" in edits[0]["output"] and "REFUSED (NOT_REAL)" in edits[2]["output"]


def test_the_repo_is_untouched_by_the_offline_ceremony(ceremony):
    assert ceremony["after"] == ceremony["before"], "the offline ceremony changed the real AUDIT.md or the real live_judgment.jsonl"
    assert ceremony["status_after"] == ceremony["status_before"], "the offline ceremony changed the working tree"


def test_forged_flags_on_the_mock_records_do_not_get_a_row_moved(ceremony, tmp_path):
    """The cheat: rewrite mock/real in the mock records and ask move_audit_row to move the rows. The evidence re-check (attestation,
    upstream, allow_live) must still refuse, because a flag is not evidence."""
    recs = ceremony["records"][-3:]
    log = tmp_path / "forged.jsonl"
    hashes = {}
    with log.open("w", encoding="utf-8", newline="\n") as fh:
        for r in recs:
            forged = dict(r, mock=False, real=True)
            line = json.dumps(forged, sort_keys=True)
            fh.write(line + "\n")
            hashes[r["subcommand"]] = hashlib.sha256(line.encode()).hexdigest()
    audit_copy = tmp_path / "AUDIT.md"
    audit_copy.write_text((ROOT / "AUDIT.md").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    for row, sub in (("C24", "planner"), ("C25", "doc"), ("C27", "apply")):
        done = subprocess.run([sys.executable, str(SCRIPTS / "move_audit_row.py"), "--row", row, "--apply", "--record-hash", hashes[sub],
                               "--log", str(log), "--audit", str(audit_copy), "--no-commit"],
                              cwd=ROOT, env=clean_env(), capture_output=True, text=True, timeout=120)
        assert done.returncode == 14, (row, done.stdout)
        assert "REFUSED (EVIDENCE_MISMATCH)" in done.stdout
    assert audit_copy.read_text(encoding="utf-8") == (ROOT / "AUDIT.md").read_text(encoding="utf-8"), "a forged record edited the audit"


# -- the documentation rules --------------------------------------------------------------------------------------------------------

def audit_text() -> str:
    return (ROOT / "AUDIT.md").read_text(encoding="utf-8")


def section(text: str, start: str, end: str | None) -> str:
    a = text.index(start)
    b = text.index(end, a + 1) if end else len(text)
    return text[a:b]


def has_2b(text: str) -> bool:
    return re.search(r"^### 2b\.", text, re.M) is not None  # a heading, not a mention in prose


def rows_in(block: str) -> set[str]:
    return set(re.findall(r"^\| (C\d+) \|", block, re.M))


def test_section_5_is_one_paragraph_with_a_command_that_works_as_pasted():
    body = section(audit_text(), "## 5. HOW TO CLOSE THE JUDGMENT GAPS", "## 6.").split("\n", 1)[1].strip()
    assert "\n\n" not in body and "```" not in body, "section 5 must be a single paragraph"
    assert "python scripts/live_run_ceremony.py --allow-live --upstream https://httpbin.org/status/418" in body
    assert "refuse to move a row without a fresh `real: true` pass" in body and "docs/LIVE_RUN.md" in body
    assert (ROOT / "docs" / "LIVE_RUN.md").is_file()


def test_the_three_rows_are_each_in_exactly_one_place_and_none_moved_without_a_live_record():
    text = audit_text()
    a3 = section(text, "### 3a.", "### 3b.")
    b2 = section(text, "\n### 2b.", "\n## 3.") if has_2b(text) else ""
    for row in ("C24", "C25", "C27"):
        assert (row in rows_in(a3)) != (row in rows_in(b2)), f"{row} must be in exactly one of 3a and 2b"
    if b2:  # if a row has moved, it cites the record that proved it
        assert all(re.search(r"`[0-9a-f]{12}`", ln) for ln in b2.splitlines() if ln.startswith("| C"))


def test_section_6_lists_exactly_the_open_rows_that_remain_after_the_ceremony():
    text = audit_text()
    a3 = rows_in(section(text, "### 3a.", "### 3b."))
    s6 = section(text, "## 6. WHAT IS LEFT AFTER THE CEREMONY", None)
    listed = set(re.findall(r"\bC\d+\b", s6.split("\n", 1)[1]))
    remaining = (a3 | rows_in(section(text, "\n### 2b.", "\n## 3.") if has_2b(text) else "")) - {"C24", "C25", "C27"}
    assert listed == remaining, f"section 6 lists {sorted(listed)} but the rows left are {sorted(remaining)}"
    assert "three rows move from" in s6 and "Nothing else changes" in s6
