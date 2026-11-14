"""Commit hygiene (CLAUDE.md D30): a `docs:` or `chore:` commit must not touch code paths.

The commit message is what a maintainer reads to decide what a commit can have changed. A commit that says "docs" and edits
`scripts/` or `agent/` breaks that trust, whether or not the change is disclosed elsewhere (it was, once: efdb147). This test makes
the rule enforced rather than promised: for the last 20 commits, a `docs:` / `chore:` subject with a diff under CODE_PATHS fails the
test, naming the commit and the files.

History is never rewritten (an old commit may be cited elsewhere). A past mislabel is excused ONLY by a line in CHANGELOG.md that starts
with the commit's hash and says it "contained code changes ... behind a docs-only message", so the exception is itself in the audit trail.
The checker is exercised on throwaway repositories below, so it is shown to fail, not just to pass on today's tree."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CODE_PATHS = ("scripts/", "agent/", "ingest/", "learning/")  # extend here; servers/, generators/ and specs/ are NOT covered yet
LABELS = re.compile(r"^(docs|chore)(\([^)]*\))?!?:")
LAST = 20
EXCUSE = re.compile(r"contained code changes.*behind a docs-only message", re.I)
GIT = ["git", "-c", "user.name=hygiene", "-c", "user.email=hygiene@example.test", "-c", "commit.gpgsign=false"]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def excused(changelog: str) -> set[str]:
    """Hashes (as written, 7 or more hex characters) that CHANGELOG.md records as mislabelled code commits."""
    out = set()
    for line in changelog.splitlines():
        m = re.match(r"^\W*([0-9a-f]{7,40})\b", line.strip())
        if m and EXCUSE.search(line):
            out.add(m.group(1))
    return out


def violations(repo: Path, changelog: str = "", last: int = LAST) -> list[str]:
    """One message per commit among the last `last` whose subject is docs:/chore: and whose diff touches CODE_PATHS, minus the excused."""
    recorded = excused(changelog)
    found = []
    for line in _git(repo, "log", f"-n{last}", "--format=%H%x09%s").splitlines():
        sha, _, subject = line.partition("\t")
        if not LABELS.match(subject):
            continue
        files = [f for f in _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha).splitlines()
                 if f.startswith(CODE_PATHS)]
        if files and not any(sha.startswith(h) for h in recorded):
            found.append(f"{sha[:7]} {subject!r} touches code: {', '.join(files)}")
    return found


# -- the real history -------------------------------------------------------------------------------------------------------------------

def test_no_docs_or_chore_commit_among_the_last_20_touches_code_unless_changelog_records_it():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8") if (ROOT / "CHANGELOG.md").is_file() else ""
    try:
        found = violations(ROOT, changelog)
    except (RuntimeError, FileNotFoundError, subprocess.SubprocessError) as exc:  # no git, or not a repository
        pytest.skip(f"git history unavailable: {exc}")
    assert not found, ("a docs:/chore: commit touches " + ", ".join(CODE_PATHS) + " (D30). Relabel future commits by what they change; "
                       "for a commit that is already made, record it in CHANGELOG.md. Found:\n  " + "\n  ".join(found))


# -- the checker itself, on throwaway repositories --------------------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    return tmp_path


def commit(repo: Path, message: str, *files: str) -> str:
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.read_text(encoding="utf-8") + "x\n" if path.exists() else "x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("message", ["docs: tidy", "chore: bump", "docs(audit): add a section", "chore!: drop a thing"])
@pytest.mark.parametrize("path", ["scripts/a.py", "agent/b.py", "ingest/c.py", "learning/d.py", "scripts/sub/e.py"])
def test_a_docs_or_chore_commit_touching_code_fails_and_names_the_commit_and_file(repo, message, path):
    sha = commit(repo, message, "README.md", path)
    found = violations(repo)
    assert len(found) == 1 and found[0].startswith(sha[:7]) and path in found[0] and message in found[0]


@pytest.mark.parametrize("message,path", [
    ("docs: only docs", "docs/a.md"), ("chore: only tests", "tests/test_x.py"), ("docs: the changelog", "CHANGELOG.md"),
    ("feat: code under a feat", "scripts/a.py"), ("fix: code under a fix", "agent/b.py"), ("test: tests", "tests/t.py"),
    ("document: not a label we police", "scripts/a.py"), ("docsx: not the label", "scripts/a.py"),
])
def test_honest_commits_pass(repo, message, path):
    commit(repo, message, path)
    assert violations(repo) == []


def test_a_recorded_mislabel_is_excused_but_only_by_its_own_hash_and_only_with_the_phrase(repo):
    bad = commit(repo, "docs: sneaky", "scripts/a.py")
    other = commit(repo, "docs: another", "agent/b.py")
    good_line = f"{bad[:7]} contained code changes (x) behind a docs-only message; recorded here for the audit trail."
    assert [v[:7] for v in violations(repo, good_line)] == [other[:7]], "excuses exactly the recorded commit"
    assert len(violations(repo, f"{bad[:7]} was fine, honest")) == 2, "a line without the phrase excuses nothing"
    assert len(violations(repo, f"see {bad[:7]} contained code changes behind a docs-only message")) == 2, "the hash must start the line"
    assert len(violations(repo, f"{bad[:6]} contained code changes behind a docs-only message")) == 2, "a short prefix is not a hash"
    both = good_line + "\n" + f"{other[:7]} contained code changes behind a docs-only message"
    assert violations(repo, both) == []


def test_only_the_last_20_commits_are_looked_at(repo):
    commit(repo, "docs: old sneaky", "scripts/a.py")
    for i in range(20):
        commit(repo, f"feat: {i}", f"src/{i}.txt")
    assert violations(repo) == [] and len(violations(repo, last=21)) == 1


def test_a_root_commit_is_checked_too(repo):
    commit(repo, "docs: the root commit touches code", "scripts/root.py")
    assert len(violations(repo)) == 1, "--root: the first commit's files are seen too"
