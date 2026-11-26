"""docs/API_BACKEND_DECISION.md must not drift from AUDIT.md (the decision page says which audit rows are open; a stale page is a false one).

Enforced, on the real document and the real AUDIT.md:
  * the page has exactly its five sections, in order;
  * every audit row it cites carries a status tag, `C24 (open)` or `C33 (proven)`, and exists in AUDIT.md; a row tagged open must still be in
    section 3a (it fails when a row moved to 2b and the page was not updated), a row tagged proven must be in section 2 (or 2b);
  * the three rows that bear on a public API (C24, C25, C27) are all cited as open: the page must say what is open NOW (C66 was a fourth until it closed on 2026-11-26);
  * the README sentence of section 5 is present verbatim, and every row it names is still open in 3a;
  * no hosting option is named without the word "free" within 40 characters of it, in the same paragraph (its free-tier limit is stated
    where it is named).
The checker is `problems(doc, audit)`; it is shown to fail on each kind of drift below. Does not prove the hosting figures are current:
they were read from the providers' pages on the date the document gives, and they change."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_audit_ids import mini, norm_id, tables

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "API_BACKEND_DECISION.md"
SECTIONS = ["## 1. WHY NOT NOW", "## 2. WHAT WOULD NEED TO BE TRUE FIRST", "## 3. IF YOU STILL WANT ONE", "## 4. THE MINIMAL SAFE VERSION",
            "## 5. WHAT TO SAY IN THE README"]
README_SENTENCE = "The public API serves AUDIT §2 proven flows only; §3a gaps (C24, C25, C27) are not exposed."
OPTIONS = ("Cloud Run", "Render", "Fly.io")
BEARING = ("C24", "C25", "C27")  # the open rows a public API would expose or depend on
CITE = re.compile(r"\b(C\d+)\b(?P<tag> \((?P<status>open|proven)\))?")


def _section_text(doc: str, heading: str) -> str:
    a = doc.index(heading)
    nxt = [doc.index(h, a + 1) for h in SECTIONS if h in doc and doc.index(h) > a]
    return doc[a:min(nxt)] if nxt else doc[a:]


def problems(doc: str, audit: str) -> list[str]:
    doc = doc.replace("\r\n", "\n")
    heads = re.findall(r"^## .*$", doc, re.M)
    if heads != SECTIONS:
        return [f"the page must have exactly these sections, in this order: {SECTIONS}; it has {heads}"]
    out = []
    tabs = tables(audit)
    open_ids, proven_ids = set(tabs["3a"]), set(tabs["2"]) | set(tabs["2b"])
    known = open_ids | proven_ids | set(tabs["1"])

    s5 = _section_text(doc, SECTIONS[4])
    if s5.count(README_SENTENCE) != 1:
        out.append("section 5 must contain the README sentence verbatim, exactly once")
    for cid in sorted({norm_id(c) for c in re.findall(r"\bC\d+\b", README_SENTENCE)}):
        if cid not in open_ids:
            out.append(f"the README sentence names {cid} as a 3a gap, but AUDIT.md no longer holds it in section 3a: update the page and the sentence")

    cited_open = set()
    for m in CITE.finditer(doc.replace(README_SENTENCE, "")):
        cid = norm_id(m.group(1))
        if cid not in known:
            out.append(f"{cid} is cited but AUDIT.md does not define it")
        elif m.group("status") is None:
            out.append(f"{cid} is cited without a status tag: write `{m.group(1)} (open)` or `{m.group(1)} (proven)`")
        elif m.group("status") == "open":
            cited_open.add(cid)
            if cid not in open_ids:
                out.append(f"{cid} is cited as open but it is not in AUDIT.md section 3a" + (" (it is proven now: update the page)" if cid in proven_ids else ""))
        elif cid not in proven_ids:
            out.append(f"{cid} is cited as proven but it is not in AUDIT.md section 2" + (" (it is still open)" if cid in open_ids else ""))
    for cid in BEARING:
        if norm_id(cid) not in cited_open:
            out.append(f"{cid} bears on a public API and must be cited as open (it is the audit's current 3a list this page has to name)")

    for para in re.split(r"\n\s*\n", doc):
        for name in OPTIONS:
            spots = [m.start() for m in re.finditer(re.escape(name), para)]
            if spots and not any("free" in para[max(0, i - 40): i + len(name) + 40].lower() for i in spots):
                out.append(f"{name} is named without its free-tier limit: the word 'free' must be within 40 characters of it, in the same paragraph")
    for name in OPTIONS:
        if name not in doc:
            out.append(f"section 3 must name {name}")
    return out


REAL_AUDIT = (ROOT / "AUDIT.md").read_text(encoding="utf-8")
REAL_DOC = DOC.read_text(encoding="utf-8") if DOC.is_file() else ""


def test_the_decision_page_exists_and_agrees_with_the_current_audit():
    assert REAL_DOC, "docs/API_BACKEND_DECISION.md is missing"
    assert problems(REAL_DOC, REAL_AUDIT) == []


# -- the checker must be able to fail -------------------------------------------------------------------------------------------------------

def audit_with(open_=("C24", "C25", "C27"), proven=("C33", "C34", "C44", "C12", "C5", "C64"), live=("C66",)):
    return mini(s1=list(proven), s2=list(proven), s2b=list(live), s3a=list(open_))


GOOD = audit_with()


def test_the_control_the_real_page_agrees_with_an_audit_shaped_like_today():
    assert problems(REAL_DOC, GOOD) == []


@pytest.mark.parametrize("label,doc_edit,audit,needle", [
    ("a row moved from 3a to 2b and the page was not updated", lambda d: d, audit_with(open_=("C24", "C25"), live=("C66", "C27")),
     "cited as open but it is not in AUDIT.md section 3a"),
    ("a row cited as proven is still open", lambda d: d.replace("C33 (proven)", "C24 (proven)", 1), GOOD, "cited as proven but it is not in AUDIT.md section 2"),
    ("a row that does not exist", lambda d: d.replace("C34 (proven)", "C999 (open)", 1), GOOD, "C999 is cited but AUDIT.md does not define it"),
    ("a citation with no status tag", lambda d: d.replace("C25 (open) and", "C25 and", 1), GOOD, "cited without a status tag"),
    ("a hosting option without its free-tier limit", lambda d: d.replace("**Render** — free tier:", "**Render** — plan:", 1), GOOD, "Render is named without its free-tier limit"),
    ("an extra section", lambda d: d + "\n## 6. EXTRA\n", GOOD, "exactly these sections"),
    ("the README sentence is missing", lambda d: d.replace(README_SENTENCE, "An API."), GOOD, "README sentence verbatim"),
    ("a bearing row is no longer cited", lambda d: d.replace("C25 (open)", "document extraction"), GOOD, "C25 bears on a public API"),
])
def test_drift_between_the_page_and_the_audit_is_caught(label, doc_edit, audit, needle):
    doc = doc_edit(REAL_DOC)
    if label != "a row moved from 3a to 2b and the page was not updated":
        assert doc != REAL_DOC, "the edit must change something"
    found = problems(doc, audit)
    assert any(needle in p for p in found), f"expected {needle!r}, got {found}"


def test_the_readme_sentence_fails_when_a_row_it_names_has_moved():
    moved = audit_with(open_=("C24", "C25"), live=("C66", "C27"))
    found = problems(REAL_DOC, moved)
    assert any("README sentence names C27 as a 3a gap" in p for p in found), found


def test_the_forty_character_rule_is_about_distance_not_just_presence():
    doc = REAL_DOC.replace("**Cloud Run** — free tier:", "**Cloud Run** " + "words " * 12 + "free tier:", 1).replace("The Cloud Run free-tier", "The first option's free-tier", 1)
    assert any("Cloud Run is named without its free-tier limit" in p for p in problems(doc, GOOD)), "a 'free' more than 40 characters away does not count"
