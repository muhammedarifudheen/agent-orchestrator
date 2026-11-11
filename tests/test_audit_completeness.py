"""AUDIT.md completeness scan (CLAUDE.md D21).

Every servers/*_server.py, specs/*.yaml, learning/*.py, ingest/*.py and scripts/*.py must appear in EXACTLY ONE of
AUDIT.md section 2 (a "Files covered" cell: a passing test exercises it) or section 3a (the "Files with NO passing test"
column). Neither = un-audited; both = contradiction. The files are DISCOVERED on every run, nothing is hardcoded, so adding
a server without updating AUDIT.md fails this test by name.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "AUDIT.md"
GLOBS = ("servers/*_server.py", "specs/*.yaml", "learning/*.py", "ingest/*.py", "scripts/*.py")
IGNORED_NAMES = {"__init__.py"}  # package markers carry no behaviour to audit
BACKTICKED = re.compile(r"`([^`]+)`")


def discover(root: Path) -> set[str]:
    found: set[str] = set()
    for pattern in GLOBS:
        found |= {p.relative_to(root).as_posix() for p in root.glob(pattern) if p.name not in IGNORED_NAMES}
    return found


def _section(text: str, heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        raise AssertionError(f"AUDIT.md has no {heading!r} section")
    nxt = re.search(r"^## ", text[start + len(heading):], re.M)
    return text[start: start + len(heading) + nxt.start()] if nxt else text[start:]


def _column_cells(block: str, header_fragment: str, column: int) -> list[str]:
    """The cells of one column of the markdown table whose header row contains `header_fragment`."""
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|") and header_fragment in line:
            cells = []
            for row in lines[i + 2:]:  # skip the |---| separator
                if not row.lstrip().startswith("|"):
                    break
                parts = [c.strip() for c in row.strip().strip("|").split("|")]
                if len(parts) > column:
                    cells.append(parts[column])
            return cells
    raise AssertionError(f"AUDIT.md has no table whose header contains {header_fragment!r}")


def _paths(cells: list[str]) -> set[str]:
    return {m.replace("\\", "/") for cell in cells for m in BACKTICKED.findall(cell)}


def audit_files(text: str) -> tuple[set[str], set[str]]:
    """(files named in section 2, files named in section 3a's NO-passing-test column)."""
    proven = _paths(_column_cells(_section(text, "## 2. PROVEN"), "Files covered", 4))
    unproven_block = _section(text, "## 3. NOT PROVEN / OPT-IN")
    unproven = _paths(_column_cells(unproven_block, "Files with NO passing test", 5))
    return proven, unproven


def problems(root: Path, text: str) -> list[str]:
    discovered = discover(root)
    proven, unproven = audit_files(text)
    out = []
    for path in sorted(discovered - proven - unproven):
        out.append(f"UN-AUDITED: {path} appears in neither AUDIT.md section 2 (PROVEN) nor section 3a (NOT PROVEN)")
    for path in sorted(discovered & proven & unproven):
        out.append(f"CONTRADICTION: {path} is in both section 2 (a passing test covers it) and section 3a (no passing test)")
    for path in sorted((proven | unproven) - discovered):
        if any(path.startswith(p.split("*")[0]) and path.endswith(p.split("*")[-1]) for p in GLOBS):
            out.append(f"STALE: AUDIT.md names {path} which no longer exists")
    return out


def assert_complete(root: Path, text: str) -> None:
    found = problems(root, text)
    assert not found, "AUDIT.md is not complete:\n  " + "\n  ".join(found)


def _mirror(tmp_path: Path) -> Path:
    """A tmp tree with an (empty) file for every real audited file, so a probe can add one more."""
    for rel in discover(ROOT):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture(scope="module")
def audit_text() -> str:
    assert AUDIT.is_file(), "AUDIT.md does not exist at the repo root"
    return AUDIT.read_text(encoding="utf-8")


def test_every_discovered_file_is_audited_exactly_once(audit_text):
    assert_complete(ROOT, audit_text)


def test_discovery_is_not_vacuous():
    """A broken glob would make the scan pass on nothing; each category must find real files."""
    found = discover(ROOT)
    for prefix in ("servers/", "specs/", "learning/", "ingest/", "scripts/"):
        assert any(p.startswith(prefix) for p in found), f"discovery found nothing under {prefix}"


def test_a_server_added_without_updating_audit_md_fails_by_name(tmp_path, audit_text):
    root = _mirror(tmp_path)
    assert_complete(root, audit_text)  # the mirror is complete before the probe
    (root / "servers" / "dummy_probe_server.py").write_text("# temporary probe, tmp_path only\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"UN-AUDITED: servers/dummy_probe_server\.py"):
        assert_complete(root, audit_text)


@pytest.mark.parametrize("new_file", ["specs/dummy_probe.yaml", "learning/dummy_probe.py", "ingest/dummy_probe.py",
                                      "scripts/dummy_probe.py"])
def test_every_discovered_category_is_scanned_dynamically(tmp_path, audit_text, new_file):
    root = _mirror(tmp_path)
    (root / new_file).write_text("", encoding="utf-8")
    with pytest.raises(AssertionError, match=f"UN-AUDITED: {re.escape(new_file)}"):
        assert_complete(root, audit_text)


def test_a_file_in_both_sections_is_a_contradiction(tmp_path, audit_text):
    root = _mirror(tmp_path)
    proven, _ = audit_files(audit_text)
    victim = sorted(proven)[0]
    # put the same file in 3a's NO-passing-test column as well
    row = f"| X99 | probe | probe | probe | probe | `{victim}` |"
    # the row must land inside the 3a table: insert it right after the last table row before "### 3b."
    lines = audit_text.splitlines()
    idx = max(i for i, ln in enumerate(lines) if ln.lstrip().startswith("|") and i < next(
        j for j, l2 in enumerate(lines) if l2.startswith("### 3b.")))
    lines.insert(idx + 1, row)
    doctored = "\n".join(lines)
    with pytest.raises(AssertionError, match=f"CONTRADICTION: {re.escape(victim)}"):
        assert_complete(root, doctored)


def test_a_citation_of_a_deleted_file_is_stale(tmp_path, audit_text):
    root = _mirror(tmp_path)
    proven, _ = audit_files(audit_text)
    victim = next(p for p in sorted(proven) if p.startswith("servers/"))
    (root / victim).unlink()
    with pytest.raises(AssertionError, match=f"STALE: AUDIT.md names {re.escape(victim)}"):
        assert_complete(root, audit_text)
