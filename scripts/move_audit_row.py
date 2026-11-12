#!/usr/bin/env python
"""Move ONE judgment-gap row from AUDIT.md section 3a to section 2b, only from fresh, real, intact evidence (CLAUDE.md D27).

  python scripts/move_audit_row.py --row C24 [--record-hash H]            read-only: run every check, print the diff it WOULD make
  python scripts/move_audit_row.py --row C24 --apply --record-hash H      the same checks, then edit AUDIT.md and commit it

Rows and the subcommand of scripts/live_judgment.py that closes each: C24 planner, C25 doc, C27 apply. The record is one line
of .perf/live_judgment.jsonl, named by (a prefix of) its SHA-256 (live_judgment.py prints `RECORD_HASH: <hex>`).

A row moves ONLY when ALL of these hold; the first that does not hold refuses with `REFUSED (<NAME>): <reason>`:
  17 MOCK              the record is not from the offline mock (a mock run can never close a gap)
  11 NOT_REAL          real is true: attested over the real transport (scripts/live_attest.py), not merely asserted
  12 NOT_PASS          result is pass and the exit code is 0
  16 WRONG_SUBCOMMAND  the record is from the subcommand that closes this row
  13 STALE             the run is at most 30 minutes old: a historical record is not evidence. There is no option to
                       extend the window, and re-using an old record is exactly what this check exists to stop
  14 EVIDENCE_MISMATCH the stored evidence still says what the record says: the artifact exists and its SHA-256 equals the
                       one recorded; planner/doc: the junit report is re-classified here and must be a pass with the recorded
                       number of tests, none skipped, and a key was present; apply: a public upstream, --allow-live, and an
                       attested answer from that host
  15 ROW_NOT_OPEN      the row is still in section 3a (a row already moved is not moved twice)
  18 DIRTY_AUDIT       (with --apply and committing) AUDIT.md has no uncommitted changes and nothing else is staged
  10 NO_RECORD         the hash names exactly one record in the log
On success it prints the diff, removes the row from 3a, adds it to `### 2b. Proven live` (the record hash, date, command and the
evidence hash), and commits `audit: close live judgment gap <row> (<first 12 hex of the hash>)` (the full hash and the evidence
are in the commit body). `--no-commit` edits the file only; `--audit` and `--log` point at another AUDIT.md / log (tests).

This is a PROCEDURE with evidence, not a security boundary: whoever can edit this script, the log or an artifact can fake a
record. What it stops is the accidental and the lazy: a mock, a wrong key, a loopback upstream, a skip, an old run or a
hand-edit cannot move a row by themselves, and the commit names the evidence a reviewer can check.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

ROWS = {"C24": "planner", "C25": "doc", "C27": "apply"}
FRESH_MINUTES = 30
CODES = {"NO_RECORD": 10, "NOT_REAL": 11, "NOT_PASS": 12, "STALE": 13, "EVIDENCE_MISMATCH": 14, "ROW_NOT_OPEN": 15,
         "WRONG_SUBCOMMAND": 16, "MOCK": 17, "DIRTY_AUDIT": 18}
EXIT_OK, EXIT_USAGE = 0, 64
MIN_HASH = 12
HEADING_2B = "### 2b. Proven live"
COMMANDS = {"planner": "python scripts/live_judgment.py --planner", "doc": "python scripts/live_judgment.py --doc",
            "apply": "python scripts/live_judgment.py --apply <URL> --allow-live"}


class Refusal(Exception):
    def __init__(self, name: str, reason: str) -> None:
        super().__init__(reason)
        self.name, self.reason, self.code = name, reason, CODES[name]


def _log_default() -> Path:
    return ROOT / ".perf" / "live_judgment.jsonl"


def read_log(path: Path) -> list[tuple[int, str, dict[str, Any]]]:
    """(line number, the line, its record) for every parseable line."""
    out = []
    if path.is_file():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                out.append((n, line, json.loads(line)))
            except ValueError:
                continue
    return out


def find_record(path: Path, wanted: str) -> tuple[int, str, dict[str, Any], str]:
    wanted = wanted.strip().lower()
    if len(wanted) < MIN_HASH or not re.fullmatch(r"[0-9a-f]+", wanted):
        raise Refusal("NO_RECORD", f"a record hash is at least {MIN_HASH} hex characters (got {wanted!r})")
    hits = [(n, line, rec, hashlib.sha256(line.encode("utf-8")).hexdigest()) for n, line, rec in read_log(path)]
    hits = [h for h in hits if h[3].startswith(wanted)]
    if not hits:
        raise Refusal("NO_RECORD", f"no record in {path} has a SHA-256 starting {wanted}")
    if len(hits) > 1:
        raise Refusal("NO_RECORD", f"{len(hits)} records start with {wanted}: give more characters")
    return hits[0]


def _parse_date(text: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _sha256_file(path: str | Path | None) -> str | None:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path and Path(path).is_file() else None


def check_record(row: str, record: dict[str, Any], now: dt.datetime) -> None:
    """Raise Refusal unless the record can close `row`. The ORDER is part of the contract: mock and not-real first."""
    sub = ROWS[row]
    if record.get("mock") is True:
        raise Refusal("MOCK", "the record is from the offline mock: a mock run can never close a gap")
    if record.get("real") is not True:
        raise Refusal("NOT_REAL", f"real is {record.get('real')!r}: only a run attested over the real transport (a real key for "
                      "planner/doc, a public upstream with --allow-live for apply) is real; a skipped, loopback, wrong-key or "
                      "mock run never is")
    if record.get("result") != "pass" or record.get("exit_code") != 0:
        raise Refusal("NOT_PASS", f"result is {record.get('result')!r} with exit code {record.get('exit_code')!r}: only a pass "
                      f"(exit 0) closes a gap. Reason recorded: {str(record.get('reason'))[:200]}")
    if record.get("subcommand") != sub:
        raise Refusal("WRONG_SUBCOMMAND", f"row {row} is closed by --{sub}, but this record is from --{record.get('subcommand')}")
    try:
        age = now - _parse_date(str(record.get("date")))
    except ValueError:
        raise Refusal("STALE", f"the record's date {record.get('date')!r} cannot be read, so its age is unknown") from None
    if age > dt.timedelta(minutes=FRESH_MINUTES) or age < -dt.timedelta(minutes=1):
        raise Refusal("STALE", f"the record is {int(age.total_seconds() // 60)} minutes old (limit {FRESH_MINUTES}): a historical "
                      "record is not evidence. Re-run the subcommand and use its new RECORD_HASH")
    _check_evidence(sub, record)


def _check_evidence(sub: str, record: dict[str, Any]) -> None:
    def bad(why: str) -> Refusal:
        return Refusal("EVIDENCE_MISMATCH", why)

    att = record.get("attestation") or {}
    if not att.get("real") or att.get("non_real_requests"):
        raise bad("the attestation does not show answered requests over the real transport and none over another one")
    artifact, recorded = record.get("artifact_path"), record.get("artifact_sha256")
    actual = _sha256_file(artifact)
    if not artifact or actual is None:
        raise bad(f"the evidence artifact {artifact!r} is missing")
    if actual != recorded:
        raise bad(f"the artifact {artifact} has SHA-256 {actual[:12]}..., the record says {str(recorded)[:12]}...: it changed")
    if sub in ("planner", "doc"):
        if record.get("key_present") is not True:
            raise bad("no key was present for a planner/doc run")
        from live_judgment import classify_junit  # the harness's own classifier: the same rule, applied again here

        tests = record.get("tests") or []
        verdict, why = classify_junit(Path(artifact).read_text(encoding="utf-8"), len(tests))
        if not tests or verdict != "pass":
            raise bad(f"re-classifying the junit report gives {verdict!r} for {len(tests)} recorded test(s): {why[:160]}")
    else:
        from live_judgment import is_real_upstream

        public, why = is_real_upstream(str(record.get("upstream") or ""))
        if not public:
            raise bad(f"the upstream is not a real, public one ({why})")
        if record.get("allow_live") is not True:
            raise bad("the run was not made with --allow-live")
        try:
            transcript = json.loads(Path(artifact).read_text(encoding="utf-8"))
        except ValueError:
            raise bad("the apply transcript is not readable JSON") from None
        if transcript.get("public") is not True or transcript.get("allow_live") is not True or not (transcript.get("attestation") or {}).get("real"):
            raise bad("the apply transcript does not say: public upstream, --allow-live, attested answers")


# -- the AUDIT.md edit ---------------------------------------------------------------------------------------------------------

def _claim_text(lines: list[str], row: str) -> str:
    for ln in lines:
        if ln.startswith(f"| {row} |"):
            return ln.split("|")[2].strip()
    return row


def plan_edit(text: str, row: str, record: dict[str, Any], full_hash: str) -> str:
    """The new AUDIT.md: `row` removed from 3a, added to 2b. Raises Refusal ROW_NOT_OPEN if it is not an open 3a row."""
    lines = text.replace("\r\n", "\n").split("\n")
    try:
        a = next(i for i, ln in enumerate(lines) if ln.startswith("### 3a."))
        b = next(i for i, ln in enumerate(lines) if ln.startswith("### 3b.") and i > a)
    except StopIteration:
        raise Refusal("ROW_NOT_OPEN", "AUDIT.md has no section 3a to take the row from") from None
    idx = [i for i in range(a, b) if lines[i].startswith(f"| {row} |")]
    if not idx:
        raise Refusal("ROW_NOT_OPEN", f"row {row} is not an open row of AUDIT.md section 3a (already moved, or never there)")
    claim = _claim_text(lines[:a], row)
    del lines[idx[0]]
    att = record.get("attestation") or {}
    new_row = (f"| {row} | {claim} | `{full_hash[:MIN_HASH]}` ({record['date'][:19]}Z), real: true, {att.get('real_requests')} answered "
               f"request(s) to {att.get('host')} over the real transport | `{COMMANDS[ROWS[row]]}` | "
               f"`{Path(str(record['artifact_path'])).name}` sha256 `{str(record['artifact_sha256'])[:MIN_HASH]}` |")
    heading = next((i for i, ln in enumerate(lines) if ln.startswith(HEADING_2B)), None)
    if heading is None:
        three = next(i for i, ln in enumerate(lines) if ln.startswith("## 3. NOT PROVEN"))
        block = [HEADING_2B + " (opt-in: each row cites the fresh, real record that proved it)", "",
                 "These rows were closed by `scripts/move_audit_row.py` from a live run (CLAUDE.md D27). The evidence is the record "
                 "named in each row (`.perf/live_judgment.jsonl`, SHA-256 of its line) and the artifact it hashes.", "",
                 "| ID | Claim | Live record | Command | Evidence |", "|---|---|---|---|---|", new_row, ""]
        lines[three:three] = block
    else:
        end = heading + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        last_table = max(i for i in range(heading, end) if lines[i].startswith("|"))
        lines.insert(last_table + 1, new_row)
    return "\n".join(lines)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _commit(repo: Path, audit: Path, row: str, record: dict[str, Any], full_hash: str) -> str:
    rel = str(audit.resolve().relative_to(repo.resolve()))
    if _git(repo, "status", "--porcelain", "--", rel).stdout.strip() or _git(repo, "diff", "--cached", "--name-only").stdout.strip():
        raise Refusal("DIRTY_AUDIT", f"{rel} has uncommitted changes, or something else is staged: commit or stash them first, "
                      "so this commit contains exactly the row move")
    return rel


def finish_commit(repo: Path, rel: str, row: str, record: dict[str, Any], full_hash: str) -> str:
    _git(repo, "add", "--", rel)
    subject = f"audit: close live judgment gap {row} ({full_hash[:MIN_HASH]})"
    body = (f"Record: {full_hash}\nDate: {record['date']}\nSubcommand: --{record['subcommand']} (real: true, attested)\n"
            f"Evidence: {record['artifact_path']}\nEvidence sha256: {record['artifact_sha256']}\n"
            "Moved from AUDIT.md section 3a to section 2b by scripts/move_audit_row.py (CLAUDE.md D27).")
    cfg: list[str] = []
    if not _git(repo, "config", "user.name").stdout.strip():
        cfg += ["-c", "user.name=move-audit-row"]
    if not _git(repo, "config", "user.email").stdout.strip():
        cfg += ["-c", "user.email=move-audit-row@localhost"]
    done = subprocess.run(["git", "-C", str(repo), *cfg, "commit", "-q", "-m", subject, "-m", body, "--", rel],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"git commit failed: {(done.stderr or done.stdout).strip()}")
    return _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()


# -- cli ----------------------------------------------------------------------------------------------------------------------------

def _latest(path: Path, sub: str) -> tuple[int, str, dict[str, Any], str] | None:
    rows = [(n, line, rec) for n, line, rec in read_log(path) if rec.get("subcommand") == sub]
    if not rows:
        return None
    n, line, rec = rows[-1]
    return n, line, rec, hashlib.sha256(line.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None, stdout: TextIO | None = None, now: Callable[[], dt.datetime] | None = None,
         env: Mapping[str, str] | None = None) -> int:
    out = stdout or sys.stdout
    say = lambda text="": out.write(text + "\n")  # noqa: E731
    ap = argparse.ArgumentParser(prog="move_audit_row.py", description=__doc__.splitlines()[0])
    ap.add_argument("--row", required=True, choices=sorted(ROWS))
    ap.add_argument("--record-hash", help=f"the SHA-256 (at least {MIN_HASH} hex characters) of the record line")
    ap.add_argument("--apply", action="store_true", help="edit AUDIT.md and commit (needs --record-hash)")
    ap.add_argument("--audit", type=Path, default=ROOT / "AUDIT.md")
    ap.add_argument("--log", type=Path, default=None)
    ap.add_argument("--no-commit", action="store_true", help="edit the file but do not commit")
    ap.add_argument("--repo", type=Path, default=None, help="the git work tree to commit in (default: the repo)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    if args.apply and not args.record_hash:
        say("usage error: --apply needs --record-hash (the RECORD_HASH line the live run printed)")
        return EXIT_USAGE
    log = args.log or Path((env if env is not None else os.environ).get("LIVE_JUDGMENT_LOG") or _log_default())
    moment = (now or (lambda: dt.datetime.now(dt.timezone.utc)))()
    sub = ROWS[args.row]
    say(f"ROW {args.row}: closed only by a fresh, real, passing `{COMMANDS[sub]}` (record freshness limit {FRESH_MINUTES} min)")
    try:
        if args.record_hash:
            n, line, record, full = find_record(log, args.record_hash)
        else:
            latest = _latest(log, sub)
            if latest is None:
                raise Refusal("NO_RECORD", f"{log} has no --{sub} record at all")
            n, line, record, full = latest
            say(f"(no --record-hash given: showing the checks for the latest --{sub} record, which would be moved only with "
                "its hash and --apply)")
        say(f"record {full[:MIN_HASH]} (line {n} of {log}): subcommand={record.get('subcommand')} result={record.get('result')} "
            f"real={record.get('real')} mock={record.get('mock')} date={record.get('date')}")
        check_record(args.row, record, moment)
        old = args.audit.read_text(encoding="utf-8")
        new = plan_edit(old, args.row, record, full)
        diff = "".join(difflib.unified_diff(old.replace("\r\n", "\n").splitlines(True), new.splitlines(True),
                                            "AUDIT.md (before)", "AUDIT.md (after)"))
        say(diff.rstrip("\n") if diff else "(no change)")
        if not args.apply:
            say(f"ALLOWED (dry run): every check passed; nothing was changed. To apply: python scripts/move_audit_row.py --row "
                f"{args.row} --apply --record-hash {full[:MIN_HASH]}")
            return EXIT_OK
        rel = None
        if not args.no_commit:
            repo = args.repo or (args.audit.resolve().parent)
            rel = _commit(repo, args.audit, args.row, record, full)
        args.audit.write_text(new if new.endswith("\n") else new + "\n", encoding="utf-8", newline="\n")
        if args.no_commit:
            say(f"MOVED: {args.row} -> AUDIT.md section 2b (record {full[:MIN_HASH]}); not committed (--no-commit)")
        else:
            sha = finish_commit(args.repo or args.audit.resolve().parent, rel or "", args.row, record, full)
            say(f"MOVED: {args.row} -> AUDIT.md section 2b (record {full[:MIN_HASH]}); committed {sha}: audit: close live "
                f"judgment gap {args.row} ({full[:MIN_HASH]})")
        return EXIT_OK
    except Refusal as exc:
        say(f"REFUSED ({exc.name}): {exc.reason}")
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
