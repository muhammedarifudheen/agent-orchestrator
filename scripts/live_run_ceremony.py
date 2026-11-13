#!/usr/bin/env python
"""The ceremony driver: the ONE command a maintainer runs, once, with a real key and a real upstream, to close the three
judgment gaps (AUDIT.md section 3a rows C24, C25, C27; CLAUDE.md D25).

  python scripts/live_run_ceremony.py --allow-live --upstream https://httpbin.org/status/418
  python scripts/live_run_ceremony.py --dry-run [--upstream URL]     print the WHOLE plan; execute nothing, write nothing
  python scripts/live_run_ceremony.py --offline-mock                 the proof that the ceremony works with no key; it can
                                                                     NEVER close a gap (every row move must be refused)
  --audit PATH / --log PATH                                          passed through to scripts/move_audit_row.py
  --transcript-dir DIR                                               where the transcript goes (default .perf/ceremony)

LIVE MODE runs, in order, stopping at the first non-zero exit:
  1. scripts/live_judgment.py --planner
  2. scripts/live_judgment.py --doc
  3. scripts/live_judgment.py --apply URL --allow-live
and ONLY if all three passed (each exit 0 and each printed a RECORD_HASH line) asks scripts/move_audit_row.py to move C24,
C25 and C27, in that order, each with the record hash of its own step. A refused row stops the ceremony; rows already moved
stay moved (each is its own commit). This script never edits AUDIT.md itself: only move_audit_row.py does, and it checks the
record (real, pass, fresh, evidence) before it will.

It REFUSES before running anything (nothing is written) unless ANTHROPIC_API_KEY is set AND --allow-live AND --upstream are
given: CEREMONY_REFUSED lines, exit 20 (key absent; ANTHROPIC_AUTH_TOKEN alone does not count), 21 (--allow-live absent),
22 (upstream missing, or loopback/private: that is for the offline proof only). The exit code is that of the first missing
item in that order.

Exit codes: 0 ceremony complete / offline proof ok / dry run; 1 a step failed, a row was refused, or the offline proof failed;
20, 21, 22 refused before running; 64 usage error.

TRANSCRIPT: .perf/ceremony/<UTC YYYYMMDDTHHMMSSZ>.json (or under --transcript-dir), written at the end of every non-dry run
that started (completion, step failure, refused row, offline proof), NOT when the driver refused up front. The last printed
line is `TRANSCRIPT: <path>`. JSON keys: ceremony_id, mode (live|offline-mock), started, finished, upstream, allow_live,
key_present (bool; the key is never written), steps [{n, name, command, exit_code, output, record_hash}], audit_edits [{row,
command, exit_code, output, moved}], git_log, result (complete|stopped|refused_row|offline_proof_ok|offline_proof_failed),
next. The key and anything equal to it are redacted from every output before it is printed or written.

OFFLINE MOCK: child env ANTHROPIC_API_KEY=sk-ant-offline-mock-key and LIVE_JUDGMENT_LOG under the transcript dir (never the
repo's log); runs --planner --mock-transport, --doc --mock-transport, and --apply against its own loopback http.server
answering 418 with --allow-loopback; all must exit 0 with records real:false; then each row move is tried with --no-commit on a
COPY of AUDIT.md and must be REFUSED (exit 11 or 17). A move that is not refused means a mock could close a gap: the proof
FAILED. The repo's AUDIT.md must be byte-identical afterwards (checked).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NO_KEY = 20
EXIT_NO_ALLOW_LIVE = 21
EXIT_NO_UPSTREAM = 22
EXIT_USAGE = 64

STEP_TIMEOUT_S = 600
OFFLINE_KEY = "sk-ant-offline-mock-key"
PLACEHOLDER_URL = "<upstream-url>"
ROWS = (("C24", "planner", 0), ("C25", "doc", 1), ("C27", "apply", 2))
OFFLINE_REFUSALS = (11, 17)
HASH_RE = re.compile(r"^RECORD_HASH:\s*([0-9a-f]{64})\s*$", re.MULTILINE)
PYTEST_NEXT = "python -m pytest --strict-budgets -q -rs"
SKIP_NOTE = ("Moving rows does not change skips: in a shell with neither ANTHROPIC_API_KEY nor LIVE_TESTS set the default run "
             "stays at 26 skipped. With the key AND LIVE_TESTS=1 both set it shows 5 skipped (the 7 key-gated tests run, and so "
             "do the 14 LIVE_TESTS-gated live-API tests, which call five third-party APIs for real).")
LIVE_DOC = "docs/LIVE_RUN.md"
LIVE_APIS_ROW = "C26"
EXIT_THIRD_PARTY_UNAVAILABLE = 6  # live_judgment.py --live-apis: a third-party host did not answer (recorded skipped, not failed)
LIVE_APIS_NOTE = ("Step 4 (the live-API tests) is OPT-IN because it sends real requests to five third-party APIs; their outages "
                  "are not the ceremony's failure. Without --include-live-apis no third-party request is made.")

Runner = Callable[[list, Mapping[str, str], float], "tuple[int, str]"]


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise _UsageError(message)


def default_runner(argv: list, env: Mapping[str, str], timeout: float) -> "tuple[int, str]":
    """Run one child command (no shell) from the repo root; output is stdout and stderr merged."""
    try:
        p = subprocess.run(argv, cwd=str(ROOT), env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return 124, partial + f"\n[ceremony] timed out after {timeout:.0f} s\n"
    except OSError as exc:
        return 127, f"[ceremony] could not start {argv[0]!r}: {exc}\n"
    return p.returncode, p.stdout.decode("utf-8", "replace")


# -- helpers -----------------------------------------------------------------------------------------------------------

def _loopback_reason(url: str) -> "str | None":
    """Why this upstream is not a real one, or None."""
    parts = urlsplit(url)
    host = (parts.hostname or "").strip().lower()
    if parts.scheme not in ("http", "https") or not host:
        return f"{url!r} is not an http(s) URL with a host"
    if host == "localhost" or host.endswith(".localhost"):
        return f"{host} is loopback"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved:
        return f"{host} is loopback/private"
    return None


def _live_steps(url: str) -> "list[tuple[str, list]]":
    py = sys.executable
    return [("planner", [py, "scripts/live_judgment.py", "--planner"]),
            ("doc", [py, "scripts/live_judgment.py", "--doc"]),
            ("apply", [py, "scripts/live_judgment.py", "--apply", url, "--allow-live"])]


def _move_cmd(row: str, record_hash: str, audit: "str | None", log: "str | None", no_commit: bool = False) -> list:
    cmd = [sys.executable, "scripts/move_audit_row.py", "--row", row, "--apply", "--record-hash", record_hash]
    if no_commit:
        cmd.append("--no-commit")
    if audit:
        cmd += ["--audit", str(audit)]
    if log:
        cmd += ["--log", str(log)]
    return cmd


def _human_commands() -> "list[str]":
    return ['$env:ANTHROPIC_API_KEY = "<your real key>"',
            "python scripts/live_run_ceremony.py --allow-live --upstream https://httpbin.org/status/418",
            f"step by step instead: see {LIVE_DOC}"]


class _Run:
    """Shared state of one non-dry run: output with redaction, the transcript, the runner."""

    include_live_apis = False
    live_apis: "dict | None" = None

    def __init__(self, mode: str, env: Mapping[str, str], stdout: TextIO, stderr: TextIO, runner: Runner,
                 tdir: Path, upstream: "str | None", allow_live: bool, key: str) -> None:
        self.mode, self.env, self.stdout, self.stderr, self.runner = mode, env, stdout, stderr, runner
        self.tdir, self.upstream, self.allow_live, self.key = tdir, upstream, allow_live, key
        self.started = dt.datetime.now(dt.timezone.utc)
        self.id = self.started.strftime("%Y%m%dT%H%M%SZ")
        self.steps: list = []
        self.edits: list = []
        self.git_log: "str | None" = None
        self.next: list = []
        self.secrets = [s for s in {key, OFFLINE_KEY if mode == "offline-mock" else ""} if s]

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "[REDACTED]")
        return text

    def out(self, text: str = "") -> None:
        text = self.redact(text)
        try:
            self.stdout.write(text + "\n")
        except UnicodeEncodeError:
            self.stdout.write(text.encode("ascii", "replace").decode("ascii") + "\n")

    def show_output(self, output: str) -> None:
        for line in output.rstrip().splitlines()[-40:]:
            self.out("    | " + line)

    def run_child(self, argv: list, env: Mapping[str, str]) -> "tuple[int, str]":
        code, output = self.runner(argv, env, STEP_TIMEOUT_S)
        return int(code), self.redact(output or "")

    def finish(self, result: str) -> Path:
        path = self.tdir / f"{self.id}.json"
        doc = {"ceremony_id": self.id, "mode": self.mode, "started": self.started.isoformat(),
               "finished": dt.datetime.now(dt.timezone.utc).isoformat(), "upstream": self.upstream,
               "allow_live": self.allow_live, "key_present": bool(self.key), "steps": self.steps,
               "audit_edits": self.edits, "git_log": self.git_log, "result": result, "next": self.next}
        if self.include_live_apis:  # only when asked for: the default transcript is exactly the three-step one
            doc["live_apis"] = self.live_apis or {"requested": True, "outcome": "not_reached"}
        text = self.redact(json.dumps(doc, indent=2))
        try:
            self.tdir.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            self.stderr.write(f"could not write the transcript {path}: {exc}\n")
        self.out(f"TRANSCRIPT: {path}")
        return path


def _record_step(run: _Run, n: int, name: str, cmd: list, env: Mapping[str, str]) -> "tuple[int, str | None]":
    run.out(f"STEP {n} ({name}): {' '.join(cmd[1:])}")
    code, output = run.run_child(cmd, env)
    hashes = HASH_RE.findall(output)
    rh = hashes[-1] if hashes else None
    run.steps.append({"n": n, "name": name, "command": cmd, "exit_code": code, "output": output, "record_hash": rh})
    run.out(f"  exit {code}; RECORD_HASH: {rh or 'none'}")
    run.show_output(output)
    return code, rh


# -- the plan (dry run) ------------------------------------------------------------------------------------------------

def _print_plan(out: Callable[[str], None], mode: str, upstream: "str | None", audit: "str | None", log: "str | None",
                tdir: Path, env: Mapping[str, str], allow_live: bool, include_live_apis: bool = False) -> None:
    url = upstream or PLACEHOLDER_URL
    tpath = tdir / "<UTC YYYYMMDDTHHMMSSZ>.json"
    out(f"CEREMONY PLAN ({mode}): nothing is executed and nothing is written by --dry-run")
    if mode == "live":
        steps = _live_steps(url)
        needs = {"planner": "needs ANTHROPIC_API_KEY; sends real requests to the Anthropic API; writes one record line to "
                            ".perf/live_judgment.jsonl (real: true on pass)",
                 "doc": "needs ANTHROPIC_API_KEY; sends real requests to the Anthropic API; writes one record line to "
                        ".perf/live_judgment.jsonl",
                 "apply": f"needs --allow-live and a non-loopback upstream that FAILS with an undeclared status ({url}); "
                          "writes one record line to .perf/live_judgment.jsonl"}
    else:
        steps = [("planner", [sys.executable, "scripts/live_judgment.py", "--planner", "--mock-transport"]),
                 ("doc", [sys.executable, "scripts/live_judgment.py", "--doc", "--mock-transport"]),
                 ("apply", [sys.executable, "scripts/live_judgment.py", "--apply",
                            "http://127.0.0.1:<port>/status/418", "--allow-loopback"])]
        needs = {"planner": f"child env ANTHROPIC_API_KEY={OFFLINE_KEY}; canned replies, no network; record real: false",
                 "doc": "canned replies, no network; record real: false",
                 "apply": "its own loopback http.server answering 418; record real: false"}
        out(f"  child env: LIVE_JUDGMENT_LOG=<transcript dir>/live_judgment.offline.jsonl (never the repo's log)")
    for i, (name, cmd) in enumerate(steps, 1):
        out(f"STEP {i} ({name}): {' '.join(cmd)}")
        out(f"    {needs[name]}")
    out("  A step exiting non-zero stops the ceremony (exit 1); a step exiting 0 with no RECORD_HASH line is a failure too.")
    if mode == "live":
        out("THEN, only if all three passed, one commit per row (AUDIT.md is edited ONLY by move_audit_row.py):")
        for row, name, i in ROWS:
            cmd = _move_cmd(row, f"<record-hash of step {i + 1}>", audit, log)
            out(f"  {row} ({name}): {' '.join(cmd)}")
        if include_live_apis:
            out("STEP 4 (live_apis, opt-in): " + " ".join([sys.executable, "scripts/live_judgment.py", "--live-apis", "--allow-live"]))
            out("    sends real requests to five third-party APIs; runs AFTER the three rows above have moved, so an outage there "
                "cannot hold them up; if it passes:")
            out("  " + " ".join(["C26 (live_apis):", *_move_cmd(LIVE_APIS_ROW, "<record-hash of step 4>", audit, log)]))
            out("    an outage (exit 6, recorded skipped) leaves C26 in section 3a and is not the ceremony's failure")
        out("  " + LIVE_APIS_NOTE)
        out("  A refused row stops the ceremony; rows already moved stay moved. Then: git log --oneline -5")
        out(f"  Next manual commands: {PYTEST_NEXT}")
        out(f"  {SKIP_NOTE}")
        out("REFUSAL CONDITIONS (checked before anything runs; nothing is written):")
        out("  exit 20: ANTHROPIC_API_KEY unset or empty (ANTHROPIC_AUTH_TOKEN alone does not count)")
        out("  exit 21: --allow-live not given")
        out("  exit 22: --upstream not given, or a loopback/private URL (loopback is for the offline proof only)")
        problems = []
        if not env.get("ANTHROPIC_API_KEY"):
            problems.append("key absent (20)")
        if not allow_live:
            problems.append("--allow-live absent (21)")
        if not upstream:
            problems.append("--upstream absent (22)")
        elif _loopback_reason(upstream):
            problems.append("upstream is loopback/private (22)")
        out("  a live run RIGHT NOW would " + ("be refused: " + "; ".join(problems) if problems else "not be refused"))
    else:
        out("THEN, for each row, on a COPY of AUDIT.md (--no-commit), every move MUST be refused (exit 11 or 17):")
        for row, name, i in ROWS:
            cmd = _move_cmd(row, f"<record-hash of step {i + 1}>", "<transcript dir>/AUDIT.offline-copy.md",
                            "<transcript dir>/live_judgment.offline.jsonl", no_commit=True)
            out(f"  {row} ({name}): {' '.join(cmd)}")
        out("  A move that is NOT refused means a mock could close a gap: the proof FAILS (exit 1).")
    out(f"TRANSCRIPT would be written to: {tpath}")


# -- offline mock ------------------------------------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def _answer(self) -> None:
        body = b'{"error": "I am a teapot (offline proof upstream)"}'
        self.send_response(418)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _answer

    def log_message(self, *args: Any) -> None:
        pass


def _record_for_hash(log: Path, record_hash: str) -> "dict | None":
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        for variant in (line, line + "\n"):
            if hashlib.sha256(variant.encode("utf-8")).hexdigest() == record_hash:
                try:
                    return json.loads(line)
                except ValueError:
                    return None
    return None


def _offline(run: _Run, audit: "str | None", log_arg: "str | None") -> int:
    run.tdir.mkdir(parents=True, exist_ok=True)
    log = run.tdir / "live_judgment.offline.jsonl"
    audit_copy = run.tdir / "AUDIT.offline-copy.md"
    repo_audit = ROOT / "AUDIT.md"
    before = hashlib.sha256(repo_audit.read_bytes()).hexdigest() if repo_audit.exists() else None
    if repo_audit.exists():
        shutil.copyfile(repo_audit, audit_copy)
    else:
        audit_copy.write_text("", encoding="utf-8")
    child_env = {k: v for k, v in run.env.items() if k not in ("ANTHROPIC_AUTH_TOKEN", "LIVE_TESTS")}
    child_env["ANTHROPIC_API_KEY"] = OFFLINE_KEY
    child_env["LIVE_JUDGMENT_LOG"] = str(log)
    run.out("OFFLINE MOCK PROOF: no key, no network, nothing here can close a gap")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    failure: "str | None" = None
    hashes: list = []
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/status/418"
        run.upstream = url
        py = sys.executable
        steps = [("planner", [py, "scripts/live_judgment.py", "--planner", "--mock-transport"]),
                 ("doc", [py, "scripts/live_judgment.py", "--doc", "--mock-transport"]),
                 ("apply", [py, "scripts/live_judgment.py", "--apply", url, "--allow-loopback"])]
        for n, (name, cmd) in enumerate(steps, 1):
            code, rh = _record_step(run, n, name, cmd, child_env)
            if code != 0:
                failure = f"step {n} ({name}) exited {code}"
                break
            if not rh:
                failure = f"step {n} ({name}) exited 0 but printed no RECORD_HASH"
                break
            rec = _record_for_hash(log, rh)
            if rec is None:
                failure = f"step {n} ({name}): no record in {log} hashes to {rh}"
                break
            if rec.get("real") is not False:
                failure = f"step {n} ({name}): the record says real={rec.get('real')!r}, expected false"
                break
            hashes.append(rh)
        if failure is None:
            for row, name, i in ROWS:
                cmd = _move_cmd(row, hashes[i], str(audit_copy), str(log_arg or log), no_commit=True)
                run.out(f"MOVE {row}: {' '.join(cmd[1:])}")
                code, output = run.run_child(cmd, child_env)
                refused = code in OFFLINE_REFUSALS
                run.edits.append({"row": row, "command": cmd, "exit_code": code, "output": output, "moved": code == 0})
                run.out(f"  exit {code}: " + ("refused, as it must be" if refused else "NOT REFUSED"))
                run.show_output(output)
                if not refused:
                    failure = (f"row {row} was not refused (exit {code}): a mock record must never move a row")
                    break
    finally:
        server.shutdown()
        server.server_close()
    after = hashlib.sha256(repo_audit.read_bytes()).hexdigest() if repo_audit.exists() else None
    if after != before:
        failure = (failure + "; " if failure else "") + "the repo's AUDIT.md CHANGED"
    if failure:
        run.next = ["fix the cause, then re-run: python scripts/live_run_ceremony.py --offline-mock"]
        run.out(f"OFFLINE PROOF FAILED: {failure}")
        run.finish("offline_proof_failed")
        return EXIT_FAIL
    run.next = _human_commands()
    run.out("OFFLINE PROOF OK: all three subcommands passed on mocks; every row move was refused because the records "
            "are real:false")
    run.out("WHAT A HUMAN WOULD DO WITH A REAL KEY:")
    for c in run.next:
        run.out("  " + c)
    run.finish("offline_proof_ok")
    return EXIT_OK


# -- live --------------------------------------------------------------------------------------------------------------

def _live(run: _Run, audit: "str | None", log: "str | None") -> int:
    url = run.upstream or ""
    hashes: list = []
    env = dict(run.env)
    for n, (name, cmd) in enumerate(_live_steps(url), 1):
        code, rh = _record_step(run, n, name, cmd, env)
        if code != 0 or not rh:
            why = f"exit {code}" if code != 0 else "exit 0 but it printed no RECORD_HASH line (a pass that left no record is not a pass)"
            run.out(f"CEREMONY_STOPPED at step {n} ({name}): {why}. AUDIT.md was NOT edited. Fix the cause and re-run the "
                    "failed step; do not edit AUDIT.md by hand.")
            run.next = [" ".join(["python"] + cmd[1:])]
            run.finish("stopped")
            return EXIT_FAIL
        hashes.append(rh)
    for row, name, i in ROWS:
        cmd = _move_cmd(row, hashes[i], audit, log)
        run.out(f"MOVE {row} ({name}): {' '.join(cmd[1:])}")
        code, output = run.run_child(cmd, env)
        run.edits.append({"row": row, "command": cmd, "exit_code": code, "output": output, "moved": code == 0})
        run.out(f"  exit {code}")
        run.show_output(output)
        if code != 0:
            moved = [e["row"] for e in run.edits if e["moved"]]
            run.out(f"CEREMONY_STOPPED: row {row} was refused or failed (exit {code}). Rows moved so far: "
                    f"{', '.join(moved) or 'none'} (each is its own commit and stays moved).")
            run.next = [" ".join(["python"] + cmd[1:])]
            run.finish("refused_row")
            return EXIT_FAIL
    moved_rows = ["C24", "C25", "C27"]
    failed_after = False
    if run.include_live_apis:
        code, rh = _record_step(run, 4, "live_apis", [sys.executable, "scripts/live_judgment.py", "--live-apis", "--allow-live"], env)
        outcome = None
        if code == 0 and rh:
            cmd = _move_cmd(LIVE_APIS_ROW, rh, audit, log)
            run.out(f"MOVE {LIVE_APIS_ROW} (live_apis): {' '.join(cmd[1:])}")
            mcode, moutput = run.run_child(cmd, env)
            run.edits.append({"row": LIVE_APIS_ROW, "command": cmd, "exit_code": mcode, "output": moutput, "moved": mcode == 0})
            run.out(f"  exit {mcode}")
            run.show_output(moutput)
            outcome = "moved" if mcode == 0 else "move_refused"
            failed_after = mcode != 0
            if mcode == 0:
                moved_rows.append(LIVE_APIS_ROW)
        elif code == EXIT_THIRD_PARTY_UNAVAILABLE:
            outcome = "third_party_unavailable"
            run.out("THIRD_PARTY_UNAVAILABLE: a third-party API did not answer, so its tests could not run. C26 stays in section 3a; "
                    "this is not the ceremony's failure and C24, C25 and C27 are unaffected. Re-run `python scripts/live_judgment.py "
                    "--live-apis --allow-live` later.")
        else:
            outcome = "failed"
            failed_after = True
            run.out(f"STEP 4 (live_apis) failed or left no record (exit {code}). C24, C25 and C27 were already moved and stay moved; "
                    "C26 stays in section 3a.")
        run.live_apis = {"requested": True, "exit_code": code, "record_hash": rh, "outcome": outcome}
    gcode, gout = run.run_child(["git", "log", "--oneline", "-5"], env)
    run.git_log = gout
    run.out("git log --oneline -5:")
    run.show_output(gout)
    run.next = [PYTEST_NEXT, SKIP_NOTE]
    run.out(f"CEREMONY COMPLETE: {', '.join(moved_rows)} moved. Next manual commands:")
    run.out(f"  {PYTEST_NEXT}")
    run.out(f"  {SKIP_NOTE}")
    run.finish("complete")
    return EXIT_FAIL if failed_after else EXIT_OK


# -- entry point -------------------------------------------------------------------------------------------------------

def main(argv: "list[str] | None" = None, env: "Mapping[str, str] | None" = None, stdout: "TextIO | None" = None,
         stderr: "TextIO | None" = None, runner: "Runner | None" = None) -> int:
    env = dict(os.environ) if env is None else dict(env)
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    runner = runner or default_runner
    ap = _Parser(prog="live_run_ceremony.py", description="Run the live-judgment ceremony (see the module docstring).")
    ap.add_argument("--allow-live", action="store_true", help="explicit consent to use the real API and a real upstream")
    ap.add_argument("--upstream", help="URL of a real endpoint that FAILS with an undeclared status, e.g. https://httpbin.org/status/418")
    ap.add_argument("--dry-run", action="store_true", help="print the whole plan; execute and write nothing")
    ap.add_argument("--offline-mock", action="store_true", help="prove the ceremony on mocks; can never close a gap")
    ap.add_argument("--include-live-apis", action="store_true", dest="include_live_apis",
                    help="OPT-IN step 4: the 14 live-API tests (real requests to five third-party APIs); never part of the default")
    ap.add_argument("--audit", help="AUDIT.md path passed to move_audit_row.py")
    ap.add_argument("--log", help="record log path passed to move_audit_row.py")
    ap.add_argument("--transcript-dir", help="directory for the transcript (default .perf/ceremony)")
    try:
        a = ap.parse_args(argv)
    except _UsageError as exc:
        stderr.write(f"usage error: {exc}\n")
        return EXIT_USAGE
    if a.include_live_apis and a.offline_mock:
        stderr.write("usage error: --include-live-apis cannot be combined with --offline-mock (the offline proof makes no "
                     "third-party request)\n")
        return EXIT_USAGE
    tdir = Path(a.transcript_dir) if a.transcript_dir else ROOT / ".perf" / "ceremony"
    key = env.get("ANTHROPIC_API_KEY", "")
    mode = "offline-mock" if a.offline_mock else "live"

    if a.dry_run:
        def out(text: str) -> None:
            stdout.write(text.replace(key, "[REDACTED]") + "\n" if key else text + "\n")
        _print_plan(out, mode, a.upstream, a.audit, a.log, tdir, env, a.allow_live, a.include_live_apis)
        return EXIT_OK

    if mode == "live":
        refusals: list = []
        if not key:
            msg = "ANTHROPIC_API_KEY is not set; set it (PowerShell: $env:ANTHROPIC_API_KEY = \"<key>\") and re-run."
            if env.get("ANTHROPIC_AUTH_TOKEN"):
                msg += " ANTHROPIC_AUTH_TOKEN alone does not count: it is no longer supported (CLAUDE.md D16)."
            refusals.append((EXIT_NO_KEY, msg))
        if not a.allow_live:
            refusals.append((EXIT_NO_ALLOW_LIVE, "--allow-live is absent; pass it to confirm you mean to use the real API "
                                                 "and a real upstream."))
        if not a.upstream:
            refusals.append((EXIT_NO_UPSTREAM, "--upstream is absent; pass the URL of a real endpoint that fails with an "
                                               "undeclared status, e.g. --upstream https://httpbin.org/status/418."))
        else:
            why = _loopback_reason(a.upstream)
            if why:
                refusals.append((EXIT_NO_UPSTREAM, f"--upstream {a.upstream} is refused: {why}; loopback/private is for the "
                                                   "offline proof only (--offline-mock), a real run needs a public upstream."))
        if refusals:
            for _, msg in refusals:
                stdout.write(f"CEREMONY_REFUSED: {msg}\n")
            stdout.write("Nothing was run and nothing was written.\n")
            return refusals[0][0]

    run = _Run(mode, env, stdout, stderr, runner, tdir, a.upstream, a.allow_live, key)
    run.include_live_apis = bool(a.include_live_apis)
    if mode == "offline-mock":
        return _offline(run, a.audit, a.log)
    return _live(run, a.audit, a.log)


if __name__ == "__main__":
    sys.exit(main())
