"""Audit ids are looked up, never guessed (CLAUDE.md D37, "Working with audit IDs").

A prompt that names an audit row id has not seen the audit; twice a prompt named C59, which was already taken. This test makes that a
failure instead of a footnote. What it enforces, on the real AUDIT.md and its real git history:

  * an id is DEFINED by a table row (`| C<n> | ...`). A row may not repeat within a table (sections 1, 2, 2b, 3a), an id may not be defined
    as both proven (2 or 2b) and open (3a), nor in both 2 and 2b. (The same claim appears once in section 1 and once in section 2: that is
    the audit's own layout, not a duplicate.)
  * every id the audit mentions anywhere has a defining row (no dangling reference);
  * no id was ever removed and defined again, over the whole history of AUDIT.md (reuse after deletion);
  * every id named in the last 20 commit messages is defined in AUDIT.md; the failure names the commit.
Gaps are NOT a failure (a person may remove a row), and neither is the order in which ids were allocated. Existing ids are never
renumbered: the audit was clean when this was written (C1 to C68, no duplicate, no reuse), so there is no allowlist.
Does not prove: that a prompt's id is the one its author meant, or that a row's text is still the claim it started as."""
from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ID = re.compile(r"\bC\d+\b")
ROW = re.compile(r"^\| (C\d+) \|", re.M)
GIT = ["git", "-c", "user.name=ids", "-c", "user.email=ids@example.test", "-c", "commit.gpgsign=false"]
LAST = 20


def num(cid: str) -> int:
    return int(cid[1:])


def norm_id(cid: str) -> str:
    """C05 and C5 are the same id: the audit writes its first nine zero-padded, a commit message or a prompt may not. Compare by number."""
    return "C" + str(num(cid))


def _between(text: str, start: str, end: str | None) -> str:
    a = text.index(start)
    return text[a:text.index(end, a + 1)] if end else text[a:]


def tables(audit: str) -> dict[str, list[str]]:
    """The ids DEFINED (one per table row) in each table: 1 claims, 2 proven, 2b proven live, 3a open."""
    t = audit.replace("\r\n", "\n")
    s2 = _between(t, "## 2. PROVEN", "## 3. NOT PROVEN")
    cut = s2.find("\n### 2b.")
    main, live = (s2[:cut], s2[cut:]) if cut >= 0 else (s2, "")
    rows = lambda block: [norm_id(c) for c in ROW.findall(block)]  # noqa: E731
    return {"1": rows(_between(t, "## 1. CLAIMS", "## 2. PROVEN")), "2": rows(main), "2b": rows(live), "3a": rows(_between(t, "### 3a.", "### 3b."))}


def id_problems(audit: str) -> list[str]:
    """Everything wrong with how ids are defined and referenced in one AUDIT.md text; empty when it is sound."""
    out, tabs = [], tables(audit)
    for name, ids in tabs.items():
        for cid, n in sorted(Counter(ids).items(), key=lambda kv: num(kv[0])):
            if n > 1:
                out.append(f"{cid} is defined {n} times in AUDIT.md table {name}: an id names one claim, so a row cannot be repeated")
    for cid in sorted((set(tabs["2"]) | set(tabs["2b"])) & set(tabs["3a"]), key=num):
        out.append(f"{cid} is defined as proven (section 2 or 2b) AND as open (3a): two claims share one id, or a row was copied instead of moved")
    for cid in sorted(set(tabs["2"]) & set(tabs["2b"]), key=num):
        out.append(f"{cid} is defined in both section 2 and section 2b")
    defined = set().union(*tabs.values())
    for cid in sorted({norm_id(c) for c in ID.findall(audit)} - defined, key=num):
        out.append(f"{cid} is mentioned in AUDIT.md but no table row defines it (a typo, or an id a prompt supplied that was never added)")
    return out


def history_problems(versions: list[tuple[str, str]]) -> list[str]:
    """`versions` = [(label, AUDIT.md text)] oldest first. An id that vanished from every table and later came back was reused."""
    out, present, removed = [], set(), {}
    for label, text in versions:
        now = {norm_id(c) for c in ROW.findall(text.replace("\r\n", "\n"))}
        for cid in sorted(now & set(removed), key=num):
            out.append(f"{cid} was removed in {removed.pop(cid)} and defined again in {label}: an id must never be reused")
        for cid in present - now:
            removed[cid] = label
        present = now
    return out


def commit_problems(commits: list[tuple[str, str, str]], defined: set[str]) -> list[str]:
    """`commits` = [(short sha, subject, body)]. Naming an id that AUDIT.md does not define fails, naming the commit."""
    out = []
    for sha, subject, body in commits:
        missing = sorted({norm_id(c) for c in ID.findall(subject + "\n" + body)} - defined, key=num)
        if missing:
            out.append(f"commit {sha} {subject[:60]!r} names {', '.join(missing)}, which AUDIT.md does not define: look an id up before using it")
    return out


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def recent_commits(repo: Path, n: int = LAST) -> list[tuple[str, str, str]]:
    out = []
    for rec in _git(repo, "log", f"-n{n}", "--format=%h%x1f%s%x1f%b%x1e").split("\x1e"):
        if rec.strip():
            sha, subject, body = (rec.strip("\n").split("\x1f") + ["", ""])[:3]
            out.append((sha, subject, body))
    return out


def audit_history(repo: Path, name: str = "AUDIT.md") -> list[tuple[str, str]]:
    return [(sha, _git(repo, "show", f"{sha}:{name}")) for sha in _git(repo, "log", "--reverse", "--format=%h", "--", name).split()]


# -- the real audit and its real history ---------------------------------------------------------------------------------------------------

def test_audit_md_defines_every_id_once_and_every_mention_has_a_definition():
    assert id_problems((ROOT / "AUDIT.md").read_text(encoding="utf-8")) == []


def test_no_id_was_ever_removed_from_audit_md_and_defined_again():
    try:
        versions = audit_history(ROOT)
    except (RuntimeError, FileNotFoundError, subprocess.SubprocessError) as exc:
        pytest.skip(f"git history unavailable: {exc}")
    assert versions, "AUDIT.md has no history to check"
    assert history_problems(versions) == []


def test_the_last_20_commit_bodies_name_only_ids_that_audit_md_defines():
    try:
        commits = recent_commits(ROOT)
    except (RuntimeError, FileNotFoundError, subprocess.SubprocessError) as exc:
        pytest.skip(f"git history unavailable: {exc}")
    defined = set().union(*tables((ROOT / "AUDIT.md").read_text(encoding="utf-8")).values())
    assert commit_problems(commits, defined) == []


# -- the checkers must be able to fail ------------------------------------------------------------------------------------------------------

def mini(s1=(), s2=(), s2b=(), s3a=(), prose=""):
    row = lambda c: f"| {c} | text |\n"  # noqa: E731
    live = "### 2b. Proven live\n\n" + "".join(row(c) for c in s2b) if s2b else ""
    return ("## 1. CLAIMS\n\n" + "".join(row(c) for c in s1) + "\n## 2. PROVEN\n\n" + "".join(row(c) for c in s2) + "\n" + live
            + "\n## 3. NOT PROVEN / OPT-IN\n\n### 3a. Open\n\n" + "".join(row(c) for c in s3a) + "\n### 3b. Skips\n\n" + prose)


def test_a_duplicate_row_in_a_tmp_copy_of_the_real_audit_fails_naming_the_id(tmp_path):
    real = (ROOT / "AUDIT.md").read_text(encoding="utf-8")
    c58 = next(ln for ln in real.splitlines() if ln.startswith("| C58 |"))
    copy = tmp_path / "AUDIT.md"
    copy.write_text(real.replace(c58, c58 + "\n" + c58, 1), encoding="utf-8")
    found = id_problems(copy.read_text(encoding="utf-8"))
    assert found == ["C58 is defined 2 times in AUDIT.md table 3a: an id names one claim, so a row cannot be repeated"]


def test_the_c59_case_a_new_open_row_that_takes_a_proven_ones_id_fails(tmp_path):
    real = (ROOT / "AUDIT.md").read_text(encoding="utf-8")
    c58 = next(ln for ln in real.splitlines() if ln.startswith("| C58 |"))
    copy = tmp_path / "AUDIT.md"
    copy.write_text(real.replace(c58, c58 + "\n| C59 | The docs deploy workflow | never run | no remote | D36 | - |", 1), encoding="utf-8")
    found = id_problems(copy.read_text(encoding="utf-8"))
    assert len(found) == 1 and found[0].startswith("C59 is defined as proven") and "AND as open" in found[0], found


def test_one_id_in_both_section_2_and_2b_and_a_dangling_mention_fail():
    assert any("both section 2 and section 2b" in p for p in id_problems(mini(s1=["C1"], s2=["C1"], s2b=["C1"])))
    dangling = id_problems(mini(s1=["C1"], s2=["C1"], prose="see C99 for details"))
    assert dangling == ["C99 is mentioned in AUDIT.md but no table row defines it (a typo, or an id a prompt supplied that was never added)"]


def test_gaps_the_same_claim_in_sections_1_and_2_and_a_move_from_3a_to_2b_are_not_failures():
    assert id_problems(mini(s1=["C1", "C2", "C4"], s2=["C1", "C2", "C4"], s3a=["C9"])) == [], "gaps and an ordinary claim layout"
    assert id_problems(mini(s1=["C1", "C5"], s2=["C1"], s2b=["C5"])) == [], "C5 moved from open to proven live"
    assert history_problems([("a", mini(s3a=["C5"])), ("b", mini(s2b=["C5"]))]) == [], "moving a row keeps it present"
    assert history_problems([("a", mini(s1=["C1", "C2"])), ("b", mini(s1=["C1"]))]) == [], "a person removed a row: not a failure"


def test_an_id_removed_and_defined_again_is_reuse_and_names_both_commits():
    found = history_problems([("a1", mini(s1=["C1", "C2"])), ("b2", mini(s1=["C1"])), ("c3", mini(s1=["C1", "C2"]))])
    assert found == ["C2 was removed in b2 and defined again in c3: an id must never be reused"]


def test_a_commit_naming_an_id_the_audit_lacks_fails_naming_the_commit():
    defined = {"C24", "C59", "C66"}
    ok = ("abc1234", "feat: x", "prompt said C59; C59 is taken; used C66.")
    bad = ("def5678", "feat: y", "Closes C99.")
    assert commit_problems([ok], defined) == []
    found = commit_problems([ok, bad], defined)
    assert len(found) == 1 and "def5678" in found[0] and "C99" in found[0] and "abc1234" not in found[0]


def test_the_commit_check_reads_real_git_messages_on_a_throwaway_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "feat: add f", "-m", "Row C1 is fine.")
    (tmp_path / "f.txt").write_text("y", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-a", "-m", "feat: change f", "-m", "A prompt named C777 for a new row.")
    found = commit_problems(recent_commits(tmp_path), {"C1"})
    sha = _git(tmp_path, "rev-parse", "--short", "HEAD").strip()
    assert len(found) == 1 and sha in found[0] and "C777" in found[0] and "change f" in found[0]


def _commit(repo: Path, message: str, n: int) -> str:
    (repo / "f.txt").write_text(str(n), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: step", "-m", message)
    return _git(repo, "rev-parse", "--short", "HEAD").strip()


def test_a_bad_commit_older_than_head_is_found_and_only_the_last_20_are_looked_at(tmp_path):
    _git(tmp_path, "init", "-q")
    old = _commit(tmp_path, "Names C777.", 0)  # the oldest of 22 commits: outside the window of 20
    for n in range(1, 21):
        _commit(tmp_path, "Row C1 only.", n)
    inside = _commit(tmp_path, "Names C888.", 21)
    _commit(tmp_path, "Row C1 only.", 22)  # HEAD is clean; the bad commit sits one behind it
    found = commit_problems(recent_commits(tmp_path), {"C1"})
    assert len(found) == 1 and inside in found[0] and "C888" in found[0], "an older commit is found, not just HEAD"
    assert old not in "".join(found), "the 21st commit back is outside the window of 20, as for the commit-label test"
    assert len(recent_commits(tmp_path, 3)) == 3


def test_a_padded_and_an_unpadded_id_are_the_same_id():
    """The real audit writes C01..C09 padded. Found when a commit message said C1 and the check called it undefined; the worse twin,
    a duplicate written C5 and C05, would have been treated as two ids and passed."""
    assert set().union(*tables(mini(s1=["C01", "C10"], s2=["C01"])).values()) == {"C1", "C10"}
    assert commit_problems([("abc1234", "feat: x", "Uses C1 and C05.")], {"C1", "C5"}) == []
    assert any("defined 2 times" in p for p in id_problems(mini(s1=["C5", "C05"])))
    assert any("AND as open" in p for p in id_problems(mini(s1=["C5"], s2=["C05"], s3a=["C5"])))
    assert history_problems([("a", mini(s1=["C05"])), ("b", mini(s1=["C1"])), ("c", mini(s1=["C5"]))]) == [
        "C5 was removed in b and defined again in c: an id must never be reused"]
