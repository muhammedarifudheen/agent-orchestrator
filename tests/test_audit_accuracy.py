"""AUDIT.md accuracy: every PROVEN claim must name tests that exist, carry the exact command, and (opt-in, slow) PASS.

Fast checks run in the default suite (one `pytest --collect-only` subprocess, offline). The slow check runs every named
test in one nested pytest run (`--runslow` to include it). The check functions take the audit text and the set of
collected ids as parameters, so the self-tests use a synthetic document and never the real AUDIT.md."""
from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "AUDIT.md"
CONFTEST = ROOT / "tests" / "conftest.py"
HEADER = "| ID | Claim | Tests | Command | Files covered |"
NESTED = os.environ.get("AUDIT_NESTED") == "1"

pytestmark = pytest.mark.skipif(NESTED, reason="nested run")


@dataclass(frozen=True)
class Row:
    id: str
    claim: str
    tests: tuple
    commands: tuple
    files: tuple


# -- parsing ----------------------------------------------------------------------------------------------------

def _section(text: str, heading_re: str, level: int) -> str | None:
    """The body of the heading matching heading_re, up to the next heading of the same or a higher level."""
    hashes = "#" * level
    m = re.search(rf"^{hashes} {heading_re}.*$", text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    stop = re.search(rf"^#{{1,{level}}} ", rest, re.M)
    return rest[:stop.start()] if stop else rest


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _ticks(cell: str) -> tuple:
    return tuple(re.findall(r"`([^`]+)`", cell))


def parse_proven(audit_text: str) -> list[Row]:
    body = _section(audit_text, r"2\. PROVEN", 2)
    if body is None:
        pytest.fail("AUDIT.md has no '## 2. PROVEN' section (format contract: H2 headings 1-4)", pytrace=False)
    lines = body.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == HEADER), None)
    if start is None:
        pytest.fail(f"AUDIT.md section 2 has no table with the exact header {HEADER!r}", pytrace=False)
    rows = []
    for ln in lines[start + 2:]:  # skip the |---| separator
        if not ln.strip().startswith("|"):
            break
        cells = _cells(ln)
        if len(cells) != 5:
            pytest.fail(f"AUDIT.md section 2 row has {len(cells)} cells, expected 5: {ln.strip()[:120]}", pytrace=False)
        cid, claim, tests, commands, files = cells
        rows.append(Row(cid, claim, _ticks(tests), _commands(commands), _ticks(files)))
    if not rows:
        pytest.fail("AUDIT.md section 2 table has zero rows: nothing is PROVEN", pytrace=False)
    return rows


def _commands(cell: str) -> tuple:
    """Each command is `python -m pytest "<id>" -q`; the cell may wrap each in backticks. Returns the ids."""
    return tuple(re.findall(r'python -m pytest "([^"]+)" -q', cell))


def base_id(test_id: str) -> str:
    return re.sub(r"\[.*\]$", "", test_id)


# -- checks (pure: text and collected ids in, list of problems out) -----------------------------------------------

def check_structure(rows: list[Row]) -> list[str]:
    problems = []
    for r in rows:
        if not r.tests:
            problems.append(f"AUDIT.md PROVEN claim {r.id} ({r.claim}) names no test: a claim with no test is not proven")
        for t in r.tests:
            if re.match(r"tests/test_audit_[^:]*\.py", t):
                problems.append(f"AUDIT.md PROVEN claim {r.id} ({r.claim}) cites {t}: the audit tests may not prove "
                                "their own document (recursion)")
    return problems


def check_ids_exist(rows: list[Row], collected: set[str]) -> list[str]:
    return [f"AUDIT.md PROVEN claim {r.id} ({r.claim}) names {t} which no longer exists (renamed or deleted); "
            "update AUDIT.md or restore the test"
            for r in rows for t in r.tests if base_id(t) not in collected]


def check_commands(rows: list[Row]) -> list[str]:
    problems = []
    for r in rows:
        if tuple(r.commands) != tuple(r.tests):
            problems.append(f"AUDIT.md PROVEN claim {r.id} ({r.claim}): Command cell ids {list(r.commands)} do not equal "
                            f'Tests cell ids {list(r.tests)}; each test needs exactly `python -m pytest "<id>" -q`')
    return problems


def check_skip_table(audit_text: str, root: Path = ROOT) -> list[str]:
    """Section 3b, if present: each row's Test column names an existing file, and the quoted skip reason appears in that
    file's source or in tests/conftest.py."""
    body = _section(audit_text, r"3b", 3)
    if body is None:
        return []
    conftest = (root / "tests" / "conftest.py")
    conftest_src = conftest.read_text(encoding="utf-8") if conftest.exists() else ""
    problems = []
    for ln in body.splitlines():
        if not ln.strip().startswith("|"):
            continue
        cells = _cells(ln)
        path_m = next((re.search(r"tests/[\w/.\-]+?\.py", c) for c in cells if re.search(r"tests/[\w/.\-]+?\.py", c)), None)
        if path_m is None:
            continue  # header, separator or a row with no test file
        rel = path_m.group(0)
        f = root / rel
        if not f.exists():
            problems.append(f"AUDIT.md section 3b names {rel} which does not exist: {ln.strip()[:100]}")
            continue
        reasons = [q.strip("` ") for c in cells for q in re.findall(r'"([^"]+)"', c)]
        if not reasons:
            continue
        src = f.read_text(encoding="utf-8")
        if not any(r in src or r in conftest_src for r in reasons):
            problems.append(f"AUDIT.md section 3b says {rel} skips with {reasons}, but none of those appears in {rel} or "
                            "tests/conftest.py")
    return problems


# -- collection and the nested run -------------------------------------------------------------------------------

def _nested_env() -> dict:
    return {**os.environ, "AUDIT_NESTED": "1"}


@functools.lru_cache(maxsize=1)
def collected_ids() -> frozenset:
    """Node ids of every test, from one `--collect-only` subprocess (the suite itself is not run)."""
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests"]
    done = subprocess.run(cmd, cwd=ROOT, env=_nested_env(), capture_output=True, text=True, timeout=300)
    ids = {base_id(ln.strip()) for ln in done.stdout.splitlines() if ln.startswith("tests/") and "::" in ln}
    assert ids, f"collection found no tests (exit {done.returncode}):\n{done.stdout[-1500:]}\n{done.stderr[-1500:]}"
    return frozenset(ids)


def junit_outcomes(xml_path: Path) -> list[tuple]:
    """(dotted classname, test name, outcome) per testcase; outcome is passed|failed|skipped."""
    out = []
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        out.append((case.get("classname", ""), case.get("name", ""), outcome))
    return out


def judge_run(rows: list[Row], outcomes: list[tuple]) -> list[str]:
    problems = []
    for r in rows:
        for t in r.tests:
            path, *rest = base_id(t).split("::")
            dotted = path[:-3].replace("/", ".")
            func = rest[-1]
            cases = [o for o in outcomes if (o[0] == dotted or o[0].startswith(dotted + "."))
                     and re.sub(r"\[.*\]$", "", o[1]) == func]
            who = f"AUDIT.md PROVEN claim {r.id} ({r.claim}) test {t}"
            if not cases:
                problems.append(f"{who} did not run at all")
            elif any(c[2] == "skipped" for c in cases):
                problems.append(f"{who}: PROVEN test was skipped: a skipped test proves nothing")
            elif any(c[2] == "failed" for c in cases):
                problems.append(f"{who} FAILED")
    return problems


# -- the real document ---------------------------------------------------------------------------------------------

def _real_audit() -> str:
    if not AUDIT.exists():
        pytest.fail("AUDIT.md does not exist at the repo root", pytrace=False)
    return AUDIT.read_text(encoding="utf-8")


def test_every_test_named_in_audit_proven_exists():
    rows = parse_proven(_real_audit())
    problems = check_ids_exist(rows, set(collected_ids()))
    assert not problems, "\n".join(problems)


def test_audit_proven_commands_match_the_named_tests_and_never_cite_audit_tests():
    rows = parse_proven(_real_audit())
    problems = check_commands(rows) + check_structure(rows)
    assert not problems, "\n".join(problems)


def test_audit_skip_table_reasons_are_real():
    problems = check_skip_table(_real_audit())
    assert not problems, "\n".join(problems)


@pytest.mark.slow
def test_every_proven_test_named_in_audit_runs_and_passes(tmp_path):
    rows = parse_proven(_real_audit())
    ids = sorted({base_id(t) for r in rows for t in r.tests})
    xml = tmp_path / "j.xml"
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", f"--junitxml={xml}", *ids]
    done = subprocess.run(cmd, cwd=ROOT, env=_nested_env(), capture_output=True, text=True, timeout=900)
    assert xml.exists(), f"nested run produced no junit xml (exit {done.returncode}):\n{done.stdout[-2000:]}\n{done.stderr[-1000:]}"
    problems = judge_run(rows, junit_outcomes(xml))
    assert not problems, "\n".join(problems) + f"\n--- nested output tail ---\n{done.stdout[-1500:]}"


# -- self-tests on a SYNTHETIC document ----------------------------------------------------------------------------

def good_doc(tests="`tests/test_a.py::test_one`", commands='`python -m pytest "tests/test_a.py::test_one" -q`',
             extra="") -> str:
    return (f"# Audit\n\n## 1. CLAIMS\n\nx\n\n## 2. PROVEN\n\n{HEADER}\n|---|---|---|---|---|\n"
            f"| P1 | Widgets work | {tests} | {commands} | `src/a.py` |\n\n"
            f"## 3. NOT PROVEN / OPT-IN\n\n### 3a x\n\n### 3b Skips\n\n{extra}\n## 4. DECISION LOG INDEX\n\nz\n")


COLLECTED = {"tests/test_a.py::test_one", "tests/test_a.py::test_two"}


def test_parser_reads_a_good_document_and_the_checks_pass():
    rows = parse_proven(good_doc())
    assert rows == [Row("P1", "Widgets work", ("tests/test_a.py::test_one",),
                        ("tests/test_a.py::test_one",), ("src/a.py",))]
    assert check_ids_exist(rows, COLLECTED) == [] and check_commands(rows) == [] and check_structure(rows) == []


def test_multiple_tests_per_cell_are_split_on_br_and_a_parametrize_suffix_is_ignored():
    doc = good_doc("`tests/test_a.py::test_one`<br>`tests/test_a.py::test_two[x]`",
                   '`python -m pytest "tests/test_a.py::test_one" -q`<br>`python -m pytest "tests/test_a.py::test_two[x]" -q`')
    rows = parse_proven(doc)
    assert len(rows[0].tests) == 2 and check_commands(rows) == [] and check_ids_exist(rows, COLLECTED) == []


def test_a_missing_test_fails_naming_the_claim_the_text_and_the_id():
    rows = parse_proven(good_doc("`tests/test_nope.py::test_missing`",
                                 '`python -m pytest "tests/test_nope.py::test_missing" -q`'))
    problems = check_ids_exist(rows, COLLECTED)
    assert problems == ["AUDIT.md PROVEN claim P1 (Widgets work) names tests/test_nope.py::test_missing which no longer "
                        "exists (renamed or deleted); update AUDIT.md or restore the test"]


def test_a_missing_section_2_header_or_zero_rows_fail_clearly():
    with pytest.raises(pytest.fail.Exception, match="no '## 2. PROVEN' section"):
        parse_proven("# Audit\n\n## 1. CLAIMS\n\nx\n")
    with pytest.raises(pytest.fail.Exception, match="exact header"):
        parse_proven("## 2. PROVEN\n\n| ID | Claim |\n|---|---|\n| P1 | x |\n")
    with pytest.raises(pytest.fail.Exception, match="zero rows"):
        parse_proven(f"## 2. PROVEN\n\n{HEADER}\n|---|---|---|---|---|\n\n## 3. NOT PROVEN / OPT-IN\n")


def test_a_command_that_does_not_match_the_tests_fails():
    rows = parse_proven(good_doc(commands='`python -m pytest "tests/test_a.py::test_two" -q`'))
    problems = check_commands(rows)
    assert len(problems) == 1 and "P1" in problems[0] and "Widgets work" in problems[0]
    rows = parse_proven(good_doc(commands="`pytest tests/test_a.py::test_one`"))
    assert check_commands(rows), "a command that is not exactly `python -m pytest \"<id>\" -q` must fail"


def test_citing_an_audit_test_or_no_test_at_all_fails():
    rows = parse_proven(good_doc("`tests/test_audit_accuracy.py::x`", '`python -m pytest "tests/test_audit_accuracy.py::x" -q`'))
    assert any("recursion" in p and "P1" in p for p in check_structure(rows))
    rows = parse_proven(good_doc("", ""))
    assert any("names no test" in p for p in check_structure(rows))


def test_skip_table_rows_must_name_a_real_file_and_a_reason_found_in_it(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text('skip = "slow: excluded from the default run"\n', encoding="utf-8")
    (tmp_path / "tests" / "test_x.py").write_text('import pytest\nreason = "ANTHROPIC_API_KEY absent"\n', encoding="utf-8")

    def table(*rows):
        return "| Test | Skip reason |\n|---|---|\n" + "".join(f"{r}\n" for r in rows)

    ok = table('| `tests/test_x.py::t` | "`ANTHROPIC_API_KEY absent`" |',
               '| `tests/test_x.py` | "slow: excluded from the default run" |')
    assert check_skip_table(good_doc(extra=ok), tmp_path) == []
    assert check_skip_table(good_doc(extra=table('| `tests/test_gone.py::t` | "x" |')), tmp_path)[0].count("does not exist")
    bad = check_skip_table(good_doc(extra=table('| `tests/test_x.py::t` | "an invented reason" |')), tmp_path)
    assert len(bad) == 1 and "an invented reason" in bad[0]
    assert check_skip_table("## 2. PROVEN\n", tmp_path) == [], "no 3b section: nothing to check"


def test_judge_run_fails_on_skipped_failed_and_missing_tests_and_names_the_claim():
    rows = parse_proven(good_doc("`tests/test_a.py::test_one`<br>`tests/test_a.py::test_two`",
                                 '`python -m pytest "tests/test_a.py::test_one" -q`<br>`python -m pytest "tests/test_a.py::test_two" -q`'))
    assert judge_run(rows, [("tests.test_a", "test_one", "passed"), ("tests.test_a", "test_two", "passed")]) == []
    skipped = judge_run(rows, [("tests.test_a", "test_one", "skipped"), ("tests.test_a", "test_two", "passed")])
    assert len(skipped) == 1 and "P1" in skipped[0] and "Widgets work" in skipped[0] and "skipped test proves nothing" in skipped[0]
    mixed = judge_run(rows, [("tests.test_a", "test_one", "passed"), ("tests.test_a", "test_two", "failed")])
    assert len(mixed) == 1 and "FAILED" in mixed[0]
    assert "did not run" in judge_run(rows, [("tests.test_a", "test_one", "passed")])[0]
    params = [("tests.test_a", "test_one[a]", "passed"), ("tests.test_a", "test_one[b]", "failed"),
              ("tests.test_a", "test_two", "passed")]
    assert any("FAILED" in p for p in judge_run(rows, params)), "one failing parametrized case fails the test"
    cls = [("tests.test_a.TestK", "test_one", "passed"), ("tests.test_a", "test_two", "passed")]
    assert judge_run(rows, cls) == [], "a class-based test is matched by classname prefix"


def test_collection_subprocess_finds_this_module():
    assert "tests/test_audit_accuracy.py::test_parser_reads_a_good_document_and_the_checks_pass" in collected_ids()
