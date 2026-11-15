"""PROJECT_STATUS.md must say what AUDIT.md says (CLAUDE.md D34). It is the one-page answer to "what is this and is it done?", so it is
checked the way AUDIT.md is (tests/test_audit_accuracy.py), by tests and not by trust:

  * section 2: every line is a claim of AUDIT section 2 copied VERBATIM, cites a test AUDIT cites for that claim, carries the command that
    runs it, and every cited test is RUN here and must pass (a skip is a failure). 10 to 15 lines.
  * section 3: every line names rows that are still open in AUDIT section 3a, gives a reason and a command that AUDIT itself contains;
    C24, C25 and C27 cannot be dropped while they are open.
  * a row that moves from 3a to 2b makes this test fail until PROJECT_STATUS.md is updated: it may no longer be listed as unproven, and it
    must appear in section 2.
  * section 4: the two commands are the ones AUDIT section 5 and section 7 give.
The checker is `problems(status, audit)`; it is shown to fail on drift below, so a green result means something."""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agent.provider_config import ANTHROPIC_HOST  # the production host is written in one file only (D29)

ROOT = Path(__file__).resolve().parent.parent
HEADINGS = ["## 1. WHAT THIS SYSTEM IS", "## 2. WHAT IS PROVEN", "## 3. WHAT IS NOT PROVEN", "## 4. TO CLOSE THE REMAINING GAPS"]
PROVEN_LINE = re.compile(r"^- \*\*(?P<cid>C\d+)\*\* (?P<claim>.+) Test: `(?P<tid>[^`]+)`\. Run: `(?P<cmd>[^`]+)`$")
NEEDLES = ("Anthropic credits required", "GPU inference required for planner within budget")


def norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def between(text: str, start: str, end: str | None) -> str:
    a = text.index(start)
    return text[a:text.index(end, a + 1)] if end else text[a:]


def audit_facts(audit: str) -> dict:
    """What AUDIT.md says: the proven claims (id -> (claim, cited tests)), the rows proven live (2b), the open rows (3a)."""
    s2 = between(audit, "## 2. PROVEN", "## 3. NOT PROVEN")
    cut = s2.find("\n### 2b.")
    main, live = (s2[:cut], s2[cut:]) if cut >= 0 else (s2, "")
    proven = {}
    for ln in main.splitlines():
        if re.match(r"^\| C\d+ \|", ln):
            cells = [c.strip() for c in ln.split("|")[1:-1]]
            proven[cells[0]] = (cells[1], re.findall(r"`([^`]+::[^`]+)`", cells[2]))
    open_rows = set(re.findall(r"^\| (C\d+) \|", between(audit, "### 3a.", "### 3b."), re.M))
    s7 = between(audit, "## 7. FREE-PROVIDER WIRING VERIFICATION", None)
    block = re.search(r"```powershell\n(.*?)```", s7, re.S)
    return {"proven": proven, "moved": set(re.findall(r"^\| (C\d+) \|", live, re.M)), "open": open_rows,
            "s5": between(audit, "## 5. HOW TO CLOSE", "## 6."), "s7_block": block.group(1) if block else ""}


def problems(status: str, audit: str) -> list[str]:
    """Every way PROJECT_STATUS.md disagrees with AUDIT.md or with its own rules; empty when it agrees."""
    status, audit = norm(status), norm(audit)
    heads = re.findall(r"^## .*$", status, re.M)
    if heads != HEADINGS:
        return [f"PROJECT_STATUS.md must have exactly these four sections, in this order: {HEADINGS}; it has {heads}"]
    parts = {h: between(status, h, HEADINGS[i + 1] if i + 1 < len(HEADINGS) else None).split("\n", 1)[1]
             for i, h in enumerate(HEADINGS)}
    facts, out = audit_facts(audit), []

    s1 = [ln for ln in parts[HEADINGS[0]].splitlines() if ln.strip()]
    if len(s1) != 5 or not all(ln.startswith("- ") for ln in s1):
        out.append(f"section 1 must be exactly five bullet lines, it has {len(s1)}")

    lines = [ln for ln in parts[HEADINGS[1]].splitlines() if ln.startswith("- **C")]
    seen, tested = set(), 0
    for ln in lines:
        m = PROVEN_LINE.match(ln)
        cid = re.match(r"- \*\*(C\d+)\*\*", ln).group(1)
        seen.add(cid)
        if not m:
            if not (cid in facts["moved"] and re.search(r"`[0-9a-f]{12}`", ln)):  # a row proven live cites its record instead of a test
                out.append(f"section 2, {cid}: not 'claim. Test: `id`. Run: `command`' (or, for a row proven live, a `12-hex` record)")
            continue
        tested += 1
        claim, tid, cmd = m["claim"], m["tid"], m["cmd"]
        if cid not in facts["proven"]:
            out.append(f"section 2, {cid}: is not a proven claim of AUDIT.md section 2")
            continue
        if claim != facts["proven"][cid][0]:
            out.append(f"section 2, {cid}: the claim text is not AUDIT.md's, verbatim (it must be copied, not summarized)")
        if tid not in facts["proven"][cid][1]:
            out.append(f"section 2, {cid}: {tid} is not a test AUDIT.md cites for this claim")
        if cmd != f'python -m pytest "{tid}" -q':
            out.append(f"section 2, {cid}: the command is not `python -m pytest \"{tid}\" -q`")
        if re.match(r"tests/test_(audit_|project_status)", tid):
            out.append(f"section 2, {cid}: {tid} is a test of the documents themselves (recursion)")
    if not 10 <= tested <= 15:
        out.append(f"section 2 must have 10 to 15 proven lines, it has {tested}")
    if len({ln.split("**")[1] for ln in lines}) != len(lines):
        out.append("section 2 names a claim twice")
    for cid in sorted(facts["moved"] - seen):
        out.append(f"{cid} moved to AUDIT.md section 2b: update PROJECT_STATUS.md (add it to section 2, remove it from section 3)")

    bullets = [ln for ln in parts[HEADINGS[2]].splitlines() if ln.startswith("- **C")]
    named: set[str] = set()
    if not 5 <= len(bullets) <= 8:
        out.append(f"section 3 must have 5 to 8 bullets, it has {len(bullets)}")
    for ln in bullets:
        ids = set(re.findall(r"\bC\d+\b", ln))
        named |= ids
        for cid in sorted(ids - facts["open"]):
            out.append(f"section 3 names {cid}, which is not open in AUDIT.md section 3a: update PROJECT_STATUS.md"
                       + (" (it moved to 2b)" if cid in facts["moved"] else ""))
        if "Reason:" not in ln:
            out.append(f"section 3 bullet without a 'Reason:': {ln[:60]}")
        cmd = re.search(r"Command: `([^`]+)`", ln)
        if not cmd:
            out.append(f"section 3 bullet without a 'Command: `...`': {ln[:60]}")
        elif cmd.group(1) not in audit:
            out.append(f"section 3: the command `{cmd.group(1)}` does not appear in AUDIT.md")
    leading = {re.match(r"- \*\*(C\d+)\*\*", ln).group(1) for ln in bullets}  # a bullet of its own, not a passing mention in another
    for cid in ("C24", "C25", "C27"):
        if cid in facts["open"] and cid not in leading:
            out.append(f"section 3 does not name {cid}, which is still open in AUDIT.md section 3a")

    s4 = parts[HEADINGS[3]]
    blocks = re.findall(r"```[a-z]*\n(.*?)```", s4, re.S)
    if len(blocks) != 2:
        out.append(f"section 4 must hold exactly two command blocks, it has {len(blocks)}")
    else:
        first = [ln for ln in blocks[0].splitlines() if ln.strip()]
        if len(first) != 1 or first[0] not in facts["s5"]:
            out.append("section 4: the Anthropic command is not the one AUDIT.md section 5 gives")
        for ln in (l.strip() for l in blocks[1].splitlines() if l.strip()):
            if ln not in facts["s7_block"]:
                out.append(f"section 4: `{ln[:70]}` is not in the command block of AUDIT.md section 7")
    for needle in NEEDLES:
        if needle not in s4:
            out.append(f"section 4 must say: {needle}")
    return out


STATUS = norm((ROOT / "PROJECT_STATUS.md").read_text(encoding="utf-8")) if (ROOT / "PROJECT_STATUS.md").is_file() else ""
AUDIT = norm((ROOT / "AUDIT.md").read_text(encoding="utf-8"))


# -- the real files -----------------------------------------------------------------------------------------------------------------------

def test_project_status_exists_and_agrees_with_the_audit():
    assert STATUS, "PROJECT_STATUS.md is missing"
    assert problems(STATUS, AUDIT) == []


def test_every_proven_line_names_a_test_that_passes(tmp_path):
    ids = [m["tid"] for ln in STATUS.splitlines() if (m := PROVEN_LINE.match(ln))]
    assert 10 <= len(ids) <= 15
    xml = tmp_path / "proven.xml"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LIVE_", "PROVIDER_", "ANTHROPIC_"))}
    done = subprocess.run([sys.executable, "-m", "pytest", *ids, "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"a test named in PROJECT_STATUS.md section 2 did not pass:\n{done.stdout[-1500:]}"
    cases = list(ET.parse(xml).getroot().iter("testcase"))
    assert len(cases) >= len(ids) and not [c for c in cases if c.find("skipped") is not None], "a named test was skipped, which is not a pass"


# -- the checker must be able to fail --------------------------------------------------------------------------------------------------

def _first_claim_line() -> str:
    return next(ln for ln in STATUS.splitlines() if PROVEN_LINE.match(ln))


DRIFT = [
    ("a claim summarized instead of copied", lambda s: s.replace(PROVEN_LINE.match(_first_claim_line())["claim"], "Generation works.", 1), "verbatim"),
    ("a test AUDIT does not cite for that claim", lambda s: s.replace("tests/test_generator.py::test_committed_generated_files_are_up_to_date",
                                                                       "tests/test_generator.py::test_something_else"), "not a test AUDIT.md cites"),
    ("a proven line that is not a claim of section 2", lambda s: s.replace("- **C01**", "- **C99**", 1), "not a proven claim"),
    ("section 3 names a row that is proven, not open", lambda s: s.replace("- **C26** Live", "- **C01** Live", 1), "not open in AUDIT.md section 3a"),
    ("section 3 drops C24 while it is open", lambda s: re.sub(r"^- \*\*C24\*\*.*\n", "", s, flags=re.M), "does not name C24"),
    ("a section 3 command AUDIT does not contain", lambda s: s.replace("python scripts/live_judgment.py --doc", "python scripts/live_judgment.py --docs"), "does not appear in AUDIT.md"),
    ("a fifth section", lambda s: s + "\n## 5. EXTRA\n", "exactly these four sections"),
    ("a section 4 command that is not AUDIT's", lambda s: s.replace("--upstream https://httpbin.org/status/418", "--upstream https://example.org"), "section 5 gives"),
    ("the GPU line removed", lambda s: s.replace("GPU inference required for planner within budget", "later"), "GPU inference required"),
]


@pytest.mark.parametrize("label,edit,needle", DRIFT, ids=[d[0] for d in DRIFT])
def test_drift_between_the_page_and_the_audit_is_caught(label, edit, needle):
    assert problems(STATUS, AUDIT) == [], "the control: the unedited page agrees"
    changed = edit(STATUS)
    assert changed != STATUS, "the edit must change something"
    found = problems(changed, AUDIT)
    assert any(needle in p for p in found), f"expected a problem mentioning {needle!r}, got {found}"


# -- a row that moves must make the page change ----------------------------------------------------------------------------------------------

def _load(name: str):
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(name, scripts / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_row_that_moves_from_3a_to_2b_fails_until_the_status_page_is_updated():
    mar = _load("move_audit_row")
    record = {"date": "2026-11-15T00:00:00+00:00", "artifact_path": "x_planner.xml", "artifact_sha256": "0" * 64,
              "attestation": {"host": ANTHROPIC_HOST, "real_requests": 3}}
    moved_audit = mar.plan_edit(AUDIT, "C24", record, "a" * 64)  # the real edit the ceremony makes
    assert "C24" in audit_facts(moved_audit)["moved"] and "C24" not in audit_facts(moved_audit)["open"]
    stale = problems(STATUS, moved_audit)
    assert any("C24" in p and "update PROJECT_STATUS.md" in p for p in stale), stale
    assert any("section 3 names C24" in p for p in stale) and any("C24 moved to AUDIT.md section 2b" in p for p in stale)

    updated = re.sub(r"^- \*\*C24\*\*.*\n", "", STATUS, flags=re.M)  # no longer unproven ...
    updated = updated.replace("could never close C24", "could never close the planner row")  # ... and no passing mention of it either
    updated = updated.replace("\n## 3. WHAT IS NOT PROVEN", "- **C24** Real-model planner judgment, proven live. Record: `aaaaaaaaaaaa`.\n\n## 3. WHAT IS NOT PROVEN", 1)
    assert problems(updated, moved_audit) == [], "... and proven: the page that follows the move agrees again"
