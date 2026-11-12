#!/usr/bin/env python
"""Run the three judgment gaps that are skipped without a key or a real upstream, and record every attempt.

Three things cannot be verified on a machine with no ANTHROPIC_API_KEY (AUDIT.md section 3a; CLAUDE.md D16, D18, D20):
the real ClaudePlanner (C24), real doc extraction by HttpDocExtractionClient (C25), and the learning loop against a REAL
upstream failure (C27). This harness runs them the moment the key or the upstream exists, FAILS HONESTLY when it does
not, and appends one line per attempt to .perf/live_judgment.jsonl.

  python scripts/live_judgment.py --planner          the EXISTING planner contract tests, claude parametrizations only
  python scripts/live_judgment.py --doc              the EXISTING doc contract test (http parametrization), then the REAL
                                                     pipeline on tests/ingest/fixtures/dogceo.doc.md in a throwaway copy
  python scripts/live_judgment.py --apply URL        the learning loop (collect -> propose -> approve -> apply) in a
                                                     throwaway git copy, against URL: an endpoint that FAILS with an HTTP
                                                     status the generated spec does not declare (e.g.
                                                     https://httpbin.org/status/418)
  --dry-run                                          print the plan (preconditions, exact commands, the exact test ids and
                                                     the SOURCE of the assertions they execute, the wire request with the key
                                                     redacted); runs nothing, no network, writes no record
  --allow-loopback                                   let a loopback/private upstream be used (tests); the record has real=false

What it does NOT do: it holds no assertion about a plan or a spec of its own for planner/doc. The live subcommands run the
SAME test files and test ids as everywhere else in a pytest subprocess with LIVE_TESTS=1 (which only unlocks their existing
skipif gates), and read the verdict from the junit XML. A skipped test is never a pass. The production pieces are used as
they are (ClaudePlanner, HttpDocExtractionClient, AnthropicHTTP, pipeline.process, Orchestrator, learning.*). No environment
variable or option was added to ingest/ or agent/: there is no ANTHROPIC_BASE_URL, so the production endpoint is the only
endpoint, and `real` in the record says whether it (or a public upstream) was used.

Exit codes
  0  pass (real tests ran, none skipped, none failed) or a dry run
  1  fail (a test failed, the selection matched the wrong number of tests, a step failed or timed out)
  2  ANTHROPIC_API_KEY absent (planner, doc): nothing was run, recorded as skipped
  3  upstream missing (apply): no URL, not reachable, or not a real upstream: nothing was run, recorded as skipped
  4  upstream healthy (apply): it answered with a status < 400, so there is nothing to learn from: recorded as skipped
  64 usage error (argparse's own exit code 2 is never used: 2 means "key missing")

THIS SCRIPT NEVER EDITS A TRACKED FILE. It writes under .perf/ (git-ignored; or beside LIVE_JUDGMENT_LOG when that is set)
and in a throwaway temp project that is deleted afterwards unless the run failed (then its path is printed and recorded as
artifact_path).
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import ipaddress
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parent.parent

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_KEY_MISSING = 2
EXIT_UPSTREAM_MISSING = 3
EXIT_UPSTREAM_HEALTHY = 4
EXIT_USAGE = 64

KEY_MISSING_MESSAGE = "ANTHROPIC_API_KEY absent; to close this gap, set the key and re-run"
SUBCOMMANDS = ("planner", "doc", "apply")

# The EXISTING live-gated tests. Entries with the same `select` run in one pytest invocation. These are selections of
# test ids, never copies of an assertion.
LIVE_TESTS: dict[str, list[dict[str, str]]] = {
    "planner": [
        {"file": "tests/scenarios/test_planner_contract.py", "select": "claude and not skip_reason"},
        {"file": "tests/scenarios/test_cross_industry_planners.py", "select": "claude and not skip_reason"},
    ],
    "doc": [
        {"file": "tests/ingest/test_doc_contract.py", "select": "http"},
    ],
    "apply": [],  # no existing test needs a real upstream; the loop's own end-to-end steps are reported by this script
}

CLOSES = {
    "planner": "AUDIT.md section 3a row C24 (real-model ClaudePlanner judgment)",
    "doc": "AUDIT.md section 3a row C25 (real doc extraction against a live model)",
    "apply": "AUDIT.md section 3a row C27 (the learning loop, apply included, against a REAL upstream failure)",
}
STEP_TIMEOUT_S = 240          # every step is under 4 minutes; a timeout is a `fail` with the reason
PROBE_TIMEOUT_S = 10
DUMMY_KEY = "sk-ant-dry-run-not-a-real-key"
REVIEW_NOTE = ("APPROVE (simulated): in real life a PERSON moves the proposal into proposals/approved/ after reading it. "
               "The harness does it here, inside the throwaway project only, so the rest of the loop can run.")
INHERITED = ("SYSTEMROOT", "SystemRoot", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "HOME", "WINDIR",
             "LOCALAPPDATA", "APPDATA", "PROGRAMDATA")


# -- small helpers -----------------------------------------------------------------------------------------------------

class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> "Any":  # argparse would exit 2, which means KEY_MISSING here
        raise _UsageError(message)


class _Ctx:
    def __init__(self, env: Mapping[str, str], stdout: TextIO, stderr: TextIO) -> None:
        self.env, self.stdout, self.stderr = env, stdout, stderr

    def out(self, text: str = "") -> None:
        try:
            self.stdout.write(text + "\n")
        except UnicodeEncodeError:
            self.stdout.write(text.encode("ascii", "replace").decode("ascii") + "\n")

    @property
    def key(self) -> str:
        return (self.env.get("ANTHROPIC_API_KEY") or "").strip()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _redact(text: str, env: Mapping[str, str]) -> str:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        secret = (env.get(name) or "").strip()
        if len(secret) >= 8:
            text = text.replace(secret, "<redacted>")
    return text


def _ensure_paths() -> None:
    """Make the production modules importable (scripts/ has no package; done lazily so importing this file has no effect)."""
    for p in (ROOT, ROOT / "servers", ROOT / "tests", ROOT / "tests" / "scenarios"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _child_env(env: Mapping[str, str], **extra: str) -> dict[str, str]:
    """The environment of a child process: `env`, plus the few non-secret variables Windows needs when `env` is a
    reduced mapping. Nothing is taken from os.environ that is not on that short list, so no key leaks in."""
    child = {k: os.environ[k] for k in INHERITED if k in os.environ}
    child.update(env)
    child.update(extra)
    return child


def log_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    given = env.get("LIVE_JUDGMENT_LOG")
    return Path(given) if given else ROOT / ".perf" / "live_judgment.jsonl"


def append_record(path: Path | str, record: dict[str, Any]) -> None:
    """Append-only: one JSON object per line; the directory is created; an existing line is never rewritten."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


# -- verdicts ----------------------------------------------------------------------------------------------------------

def _testcases(xml_text: str) -> list[ET.Element]:
    return list(ET.fromstring(xml_text).iter("testcase"))


def _case_id(case: ET.Element) -> str:
    cls, name = case.get("classname") or "", case.get("name") or "?"
    return f"{cls}::{name}" if cls else name


def classify_junit(xml_text: str, expected: int) -> tuple[str, str]:
    """("pass"|"fail"|"skipped", reason). pass ONLY if exactly `expected` (> 0) testcases ran and none failed, errored or
    was skipped. A skipped testcase (with no failure) is "skipped" with its reasons: never a pass."""
    try:
        cases = _testcases(xml_text)
    except ET.ParseError as exc:
        return "fail", f"the junit XML could not be read ({exc}); nothing can be claimed"
    failed, skipped = [], []
    for case in cases:
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                failed.append(f"{_case_id(case)}: {(node.get('message') or node.text or tag).strip()[:400]}")
                break
        else:
            node = case.find("skipped")
            if node is not None:
                skipped.append(f"{_case_id(case)}: {(node.get('message') or 'skipped').strip()[:200]}")
    if failed:
        return "fail", f"{len(failed)} of {len(cases)} test(s) failed: " + " | ".join(failed)
    if expected <= 0:
        return "fail", (f"the selection matched the wrong number of tests: expected {expected} (a selection that matches "
                        f"nothing proves nothing); the junit file holds {len(cases)}")
    if skipped:
        return "skipped", f"{len(skipped)} of {len(cases)} test(s) were skipped, which is not a pass: " + " | ".join(skipped)
    if len(cases) != expected:
        return "fail", (f"the selection matched the wrong number of tests: {expected} node id(s) were collected but the "
                        f"junit file holds {len(cases)} testcase(s)")
    return "pass", f"{len(cases)} test(s) ran and none failed or was skipped"


def _junit_counts(xml_text: str) -> tuple[int, int, list[str]]:
    """(tests that ran and passed, failed or errored, skip reasons)."""
    ran = failed = 0
    skipped: list[str] = []
    for case in _testcases(xml_text):
        if case.find("failure") is not None or case.find("error") is not None:
            failed += 1
        elif case.find("skipped") is not None:
            skipped.append((case.find("skipped").get("message") or "skipped").strip())
        else:
            ran += 1
    return ran, failed, skipped


def is_real_upstream(url: str) -> tuple[bool, str]:
    """(False, reason) for loopback / private / link-local / localhost hosts, (True, why) for a public name or address."""
    try:
        host = urlsplit(url).hostname
    except ValueError as exc:
        return False, f"not a usable URL ({exc})"
    if not host:
        return False, "the URL has no host"
    name = host.rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return False, f"{host} is localhost, not a real upstream"
    if name.endswith(".local"):
        return False, f"{host} is a .local (mDNS) name, not a real upstream"
    if "." not in name and ":" not in name:
        return False, f"{host} is a single-label host name (not a public host)"
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return True, f"{host} is a public host name"
    if addr.is_loopback:
        return False, f"{host} is a loopback address, not a real upstream"
    if addr.is_private or addr.is_link_local or addr.is_unspecified or addr.is_reserved or addr.is_multicast:
        return False, f"{host} is a private/link-local/reserved address, not a real upstream"
    return True, f"{host} is a public address"


# -- the wire probe ----------------------------------------------------------------------------------------------------

def _redact_key(key: str) -> str:
    return key[:4] + "...(redacted)"


def wire_probe(kind: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build the PRODUCTION client objects against a capturing httpx.MockTransport (no network), issue one minimal request
    through the real code path, and return what went on the wire with the key redacted."""
    import httpx

    env = os.environ if env is None else env
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    key_source = "env" if key else "dummy"
    key = key or DUMMY_KEY
    _ensure_paths()
    from ingest.anthropic_http import AnthropicHTTP

    captured: list[httpx.Request] = []
    reply: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=reply)

    def message(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "msg_probe", "type": "message", "role": "assistant", "model": "probe",
                "content": [{"type": "tool_use", "id": "toolu_probe", "name": tool, "input": payload}],
                "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}

    client = AnthropicHTTP(key, transport=httpx.MockTransport(handler))
    try:
        if kind == "planner":
            from planners import ClaudePlanner
            reply.update(message("submit_plan", {"calls": [{"intent_id": "probe", "tool": "demo__lookup"}]}))
            ClaudePlanner(client=client).plan([{"id": "probe", "intent": "look one thing up", "consumes": []}],
                                              [{"name": "demo__lookup", "description": "Look one thing up."}])
        elif kind == "doc":
            from ingest.doc import TOOL_NAME, HttpDocExtractionClient
            fixture = ROOT / "tests" / "ingest" / "fixtures" / "dogceo.stub_response.json"
            report = json.loads(fixture.read_text(encoding="utf-8")) if fixture.exists() else {}
            reply.update(message(TOOL_NAME, report))
            HttpDocExtractionClient(client).extract("GET /ping returns the text pong.")
        else:
            raise ValueError(f"unknown probe kind {kind!r} (planner or doc)")
    except ValueError:
        raise
    except Exception:  # the request is what matters; a reply the consumer rejects does not change what was sent
        pass
    if not captured:
        raise RuntimeError("the production client sent no request through the probe transport")
    request = captured[0]
    sent_key = request.headers.get("x-api-key", "")
    headers = {k: v for k, v in request.headers.items()}
    headers["x-api-key"] = _redact_key(sent_key)
    try:
        body: Any = json.loads(request.content.decode("utf-8"))
    except ValueError:
        body = request.content.decode("utf-8", "replace")
    return {"method": request.method, "url": str(request.url), "headers": headers, "body": body,
            "key_sent_matches_env": sent_key == key, "key_source": key_source}


def _truncate(value: Any, limit: int = 160) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"...[{len(value) - limit} more chars]"
    if isinstance(value, list):
        return [_truncate(v, limit) for v in value]
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in value.items()}
    return value


# -- pytest plumbing ---------------------------------------------------------------------------------------------------

def _groups(sub: str) -> list[tuple[str, list[str]]]:
    """LIVE_TESTS entries grouped by selection: [(select, [files])]."""
    groups: dict[str, list[str]] = {}
    for entry in LIVE_TESTS[sub]:
        groups.setdefault(entry["select"], []).append(entry["file"])
    return list(groups.items())


def _shown(args: list[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in args)


def _pytest_cmd(files: list[str], select: str, xml: Path | None) -> list[str]:
    cmd = [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", *files, "-k", select]
    if xml is not None:
        cmd.append(f"--junitxml={xml}")
    return cmd


def _collect(files: list[str], select: str, env: Mapping[str, str]) -> tuple[list[str], str]:
    """(node ids, problem). `pytest --collect-only -q` with LIVE_TESTS=1 and a dummy key in the child env: no network."""
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *files, "-k", select]
    child = _child_env({}, LIVE_TESTS="1", ANTHROPIC_API_KEY=DUMMY_KEY)
    try:
        done = subprocess.run(cmd, cwd=ROOT, env=child, capture_output=True, text=True, timeout=STEP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return [], f"collection timed out after {STEP_TIMEOUT_S} s"
    ids = [ln.strip() for ln in done.stdout.splitlines() if "::" in ln and ln.strip().startswith("tests/")]
    if done.returncode not in (0, 5) and not ids:
        return [], "collection failed: " + " ".join((done.stdout + done.stderr).split())[-300:]
    return ids, ""


def _run_group(ctx: _Ctx, files: list[str], select: str, xml: Path) -> tuple[str, str, list[str]]:
    """Run one selection of the existing tests for real. Returns (result, reason, node ids)."""
    ids, problem = _collect(files, select, ctx.env)
    if problem:
        return "fail", problem, ids
    xml.parent.mkdir(parents=True, exist_ok=True)
    cmd = _pytest_cmd(files, select, xml)
    ctx.out(f"  running: LIVE_TESTS=1 python {_shown(cmd[1:])}")
    try:
        done = subprocess.run(cmd, cwd=ROOT, env=_child_env(ctx.env, LIVE_TESTS="1"), capture_output=True, text=True,
                              timeout=STEP_TIMEOUT_S)
        text = (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        xml.with_suffix(".txt").write_text(_redact(partial, ctx.env), encoding="utf-8")
        return "fail", f"the step timed out after {STEP_TIMEOUT_S} s (a hung model call?); output in {xml.with_suffix('.txt')}", ids
    xml.with_suffix(".txt").write_text(_redact(text, ctx.env), encoding="utf-8")
    if not xml.exists():
        return "fail", f"pytest wrote no junit file (exit {done.returncode}); see {xml.with_suffix('.txt')}", ids
    verdict, reason = classify_junit(xml.read_text(encoding="utf-8"), len(ids))
    return verdict, _redact(reason, ctx.env), ids


def _combine(results: list[tuple[str, str]]) -> tuple[str, str]:
    for wanted in ("fail", "skipped"):
        hits = [r for v, r in results if v == wanted]
        if hits:
            return wanted, " || ".join(hits)
    return "pass", " || ".join(r for _, r in results)


# -- the throwaway project ---------------------------------------------------------------------------------------------

def _rmtree(path: Path) -> None:
    def onerror(func: Callable[..., Any], p: str, _exc: Any) -> None:
        os.chmod(p, stat.S_IWRITE)  # git objects are read-only on Windows
        func(p)
    shutil.rmtree(path, onerror=onerror)


def _git(root: Path, *args: str) -> str:
    env = _child_env(os.environ, GIT_AUTHOR_NAME="live-judgment", GIT_AUTHOR_EMAIL="live@localhost",
                     GIT_COMMITTER_NAME="live-judgment", GIT_COMMITTER_EMAIL="live@localhost")
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def make_project(prefix: str, *, git: bool = False, drop: tuple[str, ...] = ()) -> Path:
    """A throwaway copy of the repo (minus .venv, .git, __pycache__, .perf, inbox, proposals, handoffs, runs). `drop`
    names repo-relative files to delete from the copy. Never writes into the real repo."""
    top_level_skip = {".venv", ".git", ".perf", "inbox", "proposals", "handoffs", "runs"}

    def ignore(directory: str, names: list[str]) -> set[str]:
        skip = {n for n in names if n in ("__pycache__", ".pytest_cache")}
        if Path(directory).resolve() == ROOT:
            skip |= {n for n in names if n in top_level_skip}
        return skip

    proj = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copytree(ROOT, proj, ignore=ignore, dirs_exist_ok=True)
    for sub in ("inbox", "handoffs", "proposals"):
        (proj / sub).mkdir(exist_ok=True)
    (proj / "learning" / "failures.jsonl").unlink(missing_ok=True)  # the repo's own failure log is not this project's
    for rel in drop:
        (proj / rel).unlink(missing_ok=True)
    # a minimal ignore file: the repo's own ignores proposals/*.patch, and apply must be able to `git add` the moved proposal
    (proj / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    if git:
        _git(proj, "init", "-q")
        _git(proj, "add", "-A")
        _git(proj, "commit", "-q", "-m", "baseline")
    return proj


def _with_timeout(fn: Callable[[], Any], seconds: int = STEP_TIMEOUT_S) -> Any:
    """Run fn in a daemon thread; raise TimeoutError if it does not finish (a hung step must not hang the harness)."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"the step did not finish within {seconds} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# -- source of the tests a dry run will execute --------------------------------------------------------------------------

def _functions(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found: dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.setdefault(node.name, node)
    return found


def _is_fixture(node: ast.AST) -> bool:
    return any(ast.unparse(d).startswith(("pytest.fixture", "fixture")) for d in getattr(node, "decorator_list", []))


def _source_of(lines: list[str], node: Any) -> str:
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return "\n".join(lines[start - 1:node.end_lineno])


def assertion_sources(node_ids: list[str]) -> list[tuple[str, str]]:
    """[(label, verbatim source)] of the test functions the node ids execute, the same-file helpers they call that contain
    assertions, and the same-file fixtures they request. Read from the test files with ast; nothing is re-written."""
    by_file: dict[str, list[str]] = {}
    for nid in node_ids:
        file, _, rest = nid.partition("::")
        by_file.setdefault(file, []).append(rest.split("::")[-1].split("[")[0])
    out: list[tuple[str, str]] = []
    for file, names in by_file.items():
        text = (ROOT / file).read_text(encoding="utf-8")
        lines, funcs = text.splitlines(), _functions(ast.parse(text))
        wanted: list[str] = []

        def add(name: str) -> None:
            node = funcs.get(name)
            if node is None or name in wanted:
                return
            wanted.append(name)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                    callee = funcs.get(sub.func.id)
                    if callee is not None and any(isinstance(n, ast.Assert) for n in ast.walk(callee)):
                        add(sub.func.id)
            for arg in node.args.args:
                if arg.arg in funcs and _is_fixture(funcs[arg.arg]):
                    add(arg.arg)

        for name in dict.fromkeys(names):
            add(name)
        for name in wanted:
            kind = "fixture" if _is_fixture(funcs[name]) else ("test" if name.startswith("test_") else "helper")
            out.append((f"{file} :: {name} ({kind})", _source_of(lines, funcs[name])))
    return out


# -- recording ---------------------------------------------------------------------------------------------------------

def _record(ctx: _Ctx, sub: str, result: str, reason: str, code: int, *, real: bool, artifact: str | None = None,
            tests: list[str] | None = None, upstream: str | None = None) -> int:
    append_record(log_path(ctx.env), {
        "date": _now().isoformat(), "subcommand": sub, "key_present": bool(ctx.key), "result": result,
        "reason": _redact(reason, ctx.env), "exit_code": code, "real": real, "artifact_path": artifact,
        "tests": tests or [], "upstream": upstream})
    return code


def _run_dir(ctx: _Ctx) -> Path:
    return log_path(ctx.env).parent / "live_judgment"


# -- planner / doc -----------------------------------------------------------------------------------------------------

def _key_missing(ctx: _Ctx, sub: str) -> int:
    ctx.out(KEY_MISSING_MESSAGE)
    ctx.out("  needs   : the environment variable ANTHROPIC_API_KEY (a Claude API key)")
    ctx.out("  where   : set it in the shell that runs this command (cmd: set ANTHROPIC_API_KEY=...; PowerShell: "
            "$env:ANTHROPIC_API_KEY=\"...\"; bash: export ANTHROPIC_API_KEY=...). Never write it into a file of the repo.")
    ctx.out(f"  re-run  : python scripts/live_judgment.py --{sub}")
    ctx.out(f"  closes  : {CLOSES[sub]}")
    if (ctx.env.get("ANTHROPIC_AUTH_TOKEN") or "").strip():
        ctx.out("  note    : ANTHROPIC_AUTH_TOKEN is set, but it is no longer supported: the plain-HTTP client sends "
                "x-api-key only (CLAUDE.md D16). Set ANTHROPIC_API_KEY instead.")
    ctx.out("SKIPPED: nothing was run, and a skip is not a pass.")
    return _record(ctx, sub, "skipped", KEY_MISSING_MESSAGE, EXIT_KEY_MISSING, real=False)


def _print_verdict(ctx: _Ctx, verdict: str, label: str, reason: str) -> None:
    if verdict == "pass":
        ctx.out(f"PASS (live model): {label}")
    elif verdict == "fail":
        ctx.out(f"FAIL (live model): {reason}")
    else:
        ctx.out(f"SKIPPED (this is not a pass): {reason}")


def _run_contract_tests(ctx: _Ctx, sub: str, stamp: str) -> tuple[str, str, list[str], list[str]]:
    results, all_ids, artifacts = [], [], []
    for n, (select, files) in enumerate(_groups(sub), 1):
        xml = _run_dir(ctx) / f"{stamp}_{sub}{'' if n == 1 else f'_{n}'}.xml"
        verdict, reason, ids = _run_group(ctx, files, select, xml)
        results.append((verdict, reason))
        all_ids += ids
        artifacts.append(str(xml))
    verdict, reason = _combine(results)
    return verdict, reason, all_ids, artifacts


def _cmd_planner(ctx: _Ctx) -> int:
    if not ctx.key:
        return _key_missing(ctx, "planner")
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    ctx.out("Running the EXISTING planner contract tests against the real model (production endpoint).")
    verdict, reason, ids, artifacts = _run_contract_tests(ctx, "planner", stamp)
    _print_verdict(ctx, verdict, f"{len(ids)} planner-contract tests passed", reason)
    code = EXIT_PASS if verdict == "pass" else EXIT_FAIL
    return _record(ctx, "planner", verdict, reason, code, real=verdict != "skipped", artifact=artifacts[0], tests=ids)


def _pipeline_on_dogceo(ctx: _Ctx) -> tuple[str, str, Path | None]:
    """The REAL pipeline on the prose API document with the production HttpDocExtractionClient, in a throwaway project."""
    _ensure_paths()
    proj = make_project("live_doc_", drop=("specs/dogceo.yaml", "servers/dogceo_server.py", "tests/test_dogceo.py"))
    keep = True
    try:
        source = proj / "inbox" / "dogceo.doc.md"
        shutil.copyfile(ROOT / "tests" / "ingest" / "fixtures" / "dogceo.doc.md", source)

        def work() -> Any:
            import pipeline
            from ingest.anthropic_http import AnthropicHTTP
            from ingest.doc import HttpDocExtractionClient
            client = HttpDocExtractionClient(AnthropicHTTP(ctx.key))
            return pipeline.process(source, pipeline.Layout(root=proj), doc_client=client)

        try:
            outcome = _with_timeout(work)
        except TimeoutError as exc:
            return "fail", f"pipeline step: {exc}", proj
        except Exception as exc:  # noqa: BLE001 - any failure is a reportable result
            return "fail", f"pipeline step raised {type(exc).__name__}: {_redact(str(exc), ctx.env)}", proj
        for warning in outcome.warnings:
            ctx.out(f"  pipeline warning: {warning}")
        if outcome.status == "generated":
            keep = False
            ctx.out(f"  pipeline generated {outcome.name}: {', '.join(outcome.tools)}")
            return "pass", (f"pipeline.process generated '{outcome.name}' ({len(outcome.tools)} tools); its generated "
                            "tests and tests/test_contract.py passed on the staged and the promoted files"), proj
        if outcome.status == "review":
            return "fail", ("blocked_at_review_gate: the model's draft needs a person (a legitimate honest failure, not a "
                            "harness fault): " + "; ".join(outcome.findings)[:600]), proj
        return "fail", f"pipeline failed: {outcome.detail[:800]}", proj
    finally:
        if not keep:
            _rmtree(proj)


def _cmd_doc(ctx: _Ctx) -> int:
    if not ctx.key:
        return _key_missing(ctx, "doc")
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    ctx.out("Step 1: the EXISTING doc contract test, http parametrization, against the real model (production endpoint).")
    verdict, reason, ids, artifacts = _run_contract_tests(ctx, "doc", stamp)
    if verdict != "pass":
        _print_verdict(ctx, verdict, "", reason)
        return _record(ctx, "doc", verdict, "step 1 (doc contract): " + reason,
                       EXIT_PASS if verdict == "pass" else EXIT_FAIL, real=verdict != "skipped", artifact=artifacts[0], tests=ids)
    ctx.out(f"  step 1 ok: {reason}")
    ctx.out("Step 2: the real pipeline on tests/ingest/fixtures/dogceo.doc.md with HttpDocExtractionClient, in a throwaway "
            "copy of the project.")
    verdict2, reason2, proj = _pipeline_on_dogceo(ctx)
    artifact = None
    if verdict2 == "pass":
        _print_verdict(ctx, "pass", f"{len(ids)} doc-contract test(s) passed and the pipeline generated and verified the "
                       "server from the prose document", reason2)
        code = EXIT_PASS
    else:
        _print_verdict(ctx, "fail", "", "step 2 (pipeline): " + reason2)
        artifact = str(proj) if proj else None
        ctx.out(f"  the throwaway project was kept for inspection: {artifact}")
        code = EXIT_FAIL
    return _record(ctx, "doc", verdict2, f"step 1: {reason}; step 2: {reason2}", code, real=True,
                   artifact=artifact or artifacts[0], tests=ids)


# -- apply -------------------------------------------------------------------------------------------------------------

class _StepFailed(Exception):
    pass


class _Flow:
    def __init__(self, ctx: _Ctx) -> None:
        self.ctx, self.n = ctx, 0

    def do(self, label: str, fn: Callable[[], str]) -> str:
        self.n += 1
        try:
            detail = _with_timeout(fn)
        except _StepFailed as exc:
            self.ctx.out(f"STEP {self.n} FAILED  {label}: {exc}")
            raise
        except TimeoutError as exc:
            self.ctx.out(f"STEP {self.n} FAILED  {label}: {exc}")
            raise _StepFailed(f"{label}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {_redact(str(exc), self.ctx.env)}"
            self.ctx.out(f"STEP {self.n} FAILED  {label}: {msg}")
            raise _StepFailed(f"{label}: {msg}") from exc
        self.ctx.out(f"STEP {self.n} ok      {label}: {detail}")
        return detail

    def verify(self, label: str, ok: bool, detail: str) -> None:
        self.ctx.out(f"  verify {'ok    ' if ok else 'FAILED'} {label}: {detail}")
        if not ok:
            raise _StepFailed(f"{label}: {detail}")


APPLY_STEPS = (
    "write specs/liveprobe.yaml with ONE read operation `probe` GETting the URL (error_map: the default 429/5xx only), "
    "validate it with generators.from_spec and generate the server + tests with the real generator",
    "run the real Orchestrator (a scripted stand-in for the MODEL calls liveprobe__probe once and stops) TWICE against the "
    "upstream, so two real handoffs exist (learning.propose.MIN_RECURRENCE = 2)",
    "learning.collect over handoffs/, then learning.propose",
    "APPROVE: the harness moves the proposal into proposals/approved/ (a person does this in real life; simulated here)",
    "learning.apply.apply_approved with the REAL verification (not stubbed) and commit=True",
    "verify: status applied; the spec declares the upstream's status; the regenerated server differs; "
    "tests/test_contract.py -k liveprobe passes in the throwaway project; exactly one new commit; a fresh call fails with "
    "the newly declared code",
)


def _probe_spec(url: str) -> dict[str, Any]:
    parts = urlsplit(url)
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    base = f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
    request: dict[str, Any] = {"method": "GET", "path": parts.path or "/"}
    if parts.query:
        request["query_const"] = dict(parse_qsl(parts.query, keep_blank_values=True))
    return {
        "system_name": "liveprobe",
        "description": "Harness-written probe system: one read operation against the upstream under test.",
        "base_url": base,
        "timeout_s": 10,
        "auth": {"kind": "none"},
        "error_map": [
            {"when": {"status": 429}, "code": "RATE_LIMITED", "retryable": True, "recovery": "agent",
             "meaning": "Rate limit reached; retry later"},
            {"when": {"status": "5xx"}, "code": "SERVICE_UNAVAILABLE", "retryable": True, "recovery": "agent",
             "meaning": "The service is unavailable"},
        ],
        "operations": [{
            "name": "probe",
            "summary": "Send one GET request to the upstream under test.",
            "when_to_call": "Call once to see how the upstream answers.",
            "side_effect": "read", "idempotent": True,
            "request": request,
            "params": [],
            "response": {"type": "object", "description": "Whatever JSON object the upstream returns."},
            "examples": [{"name": "example", "args": {}, "mock": {"status": 200, "body": {"ok": True}}, "expect": "ok"}],
            "review": {"source": "human", "safety_fields": "reviewed"},
            "needs_review": False, "review_reasons": [],
        }],
    }


class _ScriptedModel:
    """The stand-in for the MODEL only: call liveprobe__probe once, then stop. Orchestrator, registry, servers and the error
    contract around it are the real ones."""

    def __init__(self) -> None:
        from types import SimpleNamespace as NS
        self._turns = [[NS(type="tool_use", id="1", name="liveprobe__probe", input={})],
                       [NS(type="text", text="Done.")]]
        self.messages = self

    async def create(self, **_kw: Any) -> Any:
        from types import SimpleNamespace as NS
        return NS(content=self._turns.pop(0))


def _pytest_in(proj: Path, args: list[str], xml: Path) -> tuple[int, str]:
    env = _child_env({}, PYTHONPATH=os.pathsep.join([str(proj / "offline_guard"), str(proj / "servers"), str(proj)]),
                     CONTRACT_SPEC_DIRS=str(proj / "specs"))
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args, f"--junitxml={xml}"],
                          cwd=proj, env=env, capture_output=True, text=True, timeout=STEP_TIMEOUT_S)
    return done.returncode, (done.stdout + done.stderr)


def _apply_loop(ctx: _Ctx, url: str, status: int, proj: Path) -> str:
    """The six steps of the learning loop in `proj`. Returns the success summary; raises _StepFailed otherwise."""
    import asyncio

    import yaml

    _ensure_paths()
    flow = _Flow(ctx)
    from generators.from_spec import generate, load_spec
    from learning import collect as lcollect
    from learning import propose as lpropose
    from learning.apply import apply_approved
    from pipeline import Layout

    layout = Layout(root=proj)
    server = proj / "servers" / "liveprobe_server.py"
    spec_file = proj / "specs" / "liveprobe.yaml"

    def step1() -> str:
        spec_file.write_text(yaml.safe_dump(_probe_spec(url), sort_keys=False), encoding="utf-8", newline="\n")
        load_spec(spec_file)  # the real validation: schema + semantics
        generate(spec_file, proj / "servers", proj / "tests")
        _git(proj, "add", "-A")
        _git(proj, "commit", "-q", "-m", "liveprobe baseline")
        return f"specs/liveprobe.yaml validated; {server.name} and tests/test_liveprobe.py generated and committed"

    flow.do("generate the probe system", step1)
    before_server = server.read_bytes()
    commits_before = int(_git(proj, "rev-list", "--count", "HEAD"))

    def step2() -> str:
        from agent.orchestrator import Orchestrator
        from agent.tool_registry import ToolRegistry

        async def one(user: str) -> Any:
            async with ToolRegistry(proj / "servers", pattern="liveprobe_server.py") as reg:
                if reg.connect_errors:
                    raise _StepFailed(f"the liveprobe server did not start: {reg.connect_errors}")
                orch = Orchestrator(reg, client=_ScriptedModel(), handoff_dir=proj / "handoffs", user_id=user, duties=None)
                return await orch.run("probe the upstream once")

        outcomes = [asyncio.run(one(user)) for user in ("liveprobe_a", "liveprobe_b")]
        if any(r.success for r in outcomes):
            raise _StepFailed("a run SUCCEEDED, so the upstream did not fail as expected")
        handoffs = sorted((proj / "handoffs").glob("*.json"))
        if len(handoffs) != 2:
            raise _StepFailed(f"expected 2 handoffs, found {len(handoffs)}")
        snap = json.loads(handoffs[0].read_text(encoding="utf-8")).get("error_snapshot") or {}
        return (f"2 handoffs written; failure {snap.get('error_code')} http_status={snap.get('http_status')} "
                f"retryable={snap.get('retryable')} recovery={snap.get('recovery')}")

    flow.do("orchestrator x2 against the upstream", step2)

    failures = proj / "learning" / "failures.jsonl"
    report: list[str] = []
    written: list[Path] = []

    def step3() -> str:
        records = lcollect.collect(proj / "handoffs", failures)
        mine = [r for r in records if r["system"] == "liveprobe"]
        if sum(r["recurrence"] for r in mine) < lpropose.MIN_RECURRENCE:
            raise _StepFailed(f"collect found recurrence {sum(r['recurrence'] for r in mine)}, need {lpropose.MIN_RECURRENCE}")
        written.extend(lpropose.propose(failures, proj / "specs", proj / "proposals", report=report))
        if len(written) != 1:
            raise _StepFailed(f"expected exactly one proposal, got {len(written)}: {'; '.join(report)}")
        if not written[0].name.endswith("__add_error_code.patch"):
            raise _StepFailed(f"the proposal is {written[0].name}, not an add_error_code: {'; '.join(report)}")
        return f"{len(mine)} failure record(s); proposal {written[0].name}"

    flow.do("collect, then propose", step3)

    def step4() -> str:
        approved = proj / "proposals" / "approved"
        approved.mkdir(parents=True, exist_ok=True)
        moved = approved / written[0].name
        shutil.move(str(written[0]), str(moved))
        ctx.out(f"  {REVIEW_NOTE}")
        return f"moved to {moved.relative_to(proj).as_posix()}"

    flow.do("approve (simulated)", step4)

    results: list[Any] = []

    def step5() -> str:
        saved = {k: os.environ.get(k) for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
        os.environ.update(GIT_AUTHOR_NAME="live-judgment", GIT_AUTHOR_EMAIL="live@localhost",
                          GIT_COMMITTER_NAME="live-judgment", GIT_COMMITTER_EMAIL="live@localhost")
        try:
            results.extend(apply_approved(proj / "proposals", layout, commit=True))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        if len(results) != 1:
            raise _StepFailed(f"expected one apply result, got {len(results)}")
        return f"status={results[0].status} {results[0].reason[:300]}".strip()

    flow.do("apply_approved with the real verification", step5)

    def step6() -> str:
        flow.verify("apply status", results[0].status == "applied", f"{results[0].status}: {results[0].reason[:300]}")
        spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
        rules = [r for r in spec["operations"][0].get("errors", []) if (r.get("when") or {}).get("status") == status]
        flow.verify("spec declares the upstream's status", bool(rules), f"{len(rules)} rule(s) for status {status}")
        flow.verify("server regenerated", server.read_bytes() != before_server, "servers/liveprobe_server.py changed")
        xml = _run_dir(ctx) / "_contract_tmp.xml"
        code, _text = _pytest_in(proj, ["tests/test_contract.py", "-k", "liveprobe"], xml)
        ran, failed, skipped = _junit_counts(xml.read_text(encoding="utf-8"))
        # The shared contract skips by itself what a system cannot offer (no required argument, no outage probe): those skips
        # are reported, and are not a failure, but at least one test must really have run and none may have failed.
        flow.verify("tests/test_contract.py -k liveprobe in the throwaway project", code == 0 and failed == 0 and ran > 0,
                    f"{ran} passed, {failed} failed, {len(skipped)} skipped by the contract itself"
                    + (f" ({'; '.join(skipped)[:300]})" if skipped else ""))
        commits_after = int(_git(proj, "rev-list", "--count", "HEAD"))
        flow.verify("one new commit", commits_after == commits_before + 1, f"{commits_before} -> {commits_after}")

        async def fresh() -> Any:
            from agent.tool_registry import ToolRegistry
            async with ToolRegistry(proj / "servers", pattern="liveprobe_server.py") as reg:
                return await reg.call("liveprobe__probe", {})

        outcome = asyncio.run(fresh())
        meaning = rules[0].get("meaning", "")
        flow.verify("a fresh call fails with the newly declared code",
                    (not outcome.ok) and outcome.error_code == rules[0]["code"] and bool(meaning)
                    and meaning in (outcome.error_message or "") and outcome.http_status == status,
                    f"[{outcome.error_code}] http_status={outcome.http_status} {outcome.error_message}")
        return f"all checks hold; new rule {rules[0]['code']} for HTTP {status}"

    flow.do("verify the loop's result", step6)
    return f"learned HTTP {status} -> {results[0].status}"


def _probe_upstream(url: str) -> tuple[int | None, str]:
    """(status, "") when the host answered with ANY HTTP status, (None, reason) when it could not be reached."""
    import httpx
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_S, follow_redirects=False) as client:
            return client.get(url).status_code, ""
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - e.g. a malformed URL
        return None, f"{type(exc).__name__}: {exc}"


def _upstream_missing(ctx: _Ctx, url: str | None, why: str) -> int:
    ctx.out(f"UPSTREAM_MISSING: {why}")
    ctx.out("  needs   : the full URL of an endpoint that is reachable and FAILS with an HTTP status the generated spec does "
            "not declare (not 429 or 5xx), for example https://httpbin.org/status/418")
    ctx.out("  re-run  : python scripts/live_judgment.py --apply https://httpbin.org/status/418")
    ctx.out(f"  closes  : {CLOSES['apply']}")
    ctx.out("SKIPPED: nothing was run, and a skip is not a pass.")
    return _record(ctx, "apply", "skipped", f"UPSTREAM_MISSING: {why}", EXIT_UPSTREAM_MISSING, real=False, upstream=url)


def _cmd_apply(ctx: _Ctx, url: str | None, allow_loopback: bool) -> int:
    if not url:
        return _upstream_missing(ctx, None, "no URL was given")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return _upstream_missing(ctx, url, f"{url!r} is not a full http(s) URL")
    public, why = is_real_upstream(url)
    if not public and not allow_loopback:
        return _upstream_missing(ctx, url, f"{why}; pass --allow-loopback only for a test upstream (the record then says real=false)")
    status, problem = _probe_upstream(url)
    if status is None:
        return _upstream_missing(ctx, url, f"{url} cannot be reached within {PROBE_TIMEOUT_S} s ({problem})")
    real = public
    if status < 400:
        ctx.out(f"UPSTREAM_HEALTHY: {url} answered {status}; there is nothing to learn from a healthy endpoint. Pick one that "
                "fails with an undeclared status (for example https://httpbin.org/status/418).")
        ctx.out("SKIPPED: nothing was run, and a skip is not a pass.")
        return _record(ctx, "apply", "skipped", f"UPSTREAM_HEALTHY: answered {status}", EXIT_UPSTREAM_HEALTHY, real=False,
                       upstream=url)
    if status == 429 or 500 <= status <= 599:
        reason = (f"{url} answers {status}, which the default error_map (429, 5xx) already declares, so there is nothing to "
                  "learn; use an endpoint that fails with an undeclared status, for example https://httpbin.org/status/418")
        ctx.out(f"FAIL (apply): {reason}")
        return _record(ctx, "apply", "fail", reason, EXIT_FAIL, real=real, upstream=url)
    ctx.out(f"Upstream {url} answered {status} ({'real, public' if real else 'loopback: real=false'}). Running the loop in a "
            "throwaway git copy of the project.")
    _run_dir(ctx).mkdir(parents=True, exist_ok=True)
    proj = make_project("live_apply_", git=True)
    try:
        summary = _apply_loop(ctx, url, status, proj)
    except _StepFailed as exc:
        ctx.out(f"FAIL (apply): {exc}")
        ctx.out(f"  the throwaway project was kept for inspection: {proj}")
        return _record(ctx, "apply", "fail", str(exc), EXIT_FAIL, real=real, artifact=str(proj), upstream=url)
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {_redact(str(exc), ctx.env)}"
        ctx.out(f"FAIL (apply): {reason}")
        ctx.out(f"  the throwaway project was kept for inspection: {proj}")
        return _record(ctx, "apply", "fail", reason, EXIT_FAIL, real=real, artifact=str(proj), upstream=url)
    _rmtree(proj)
    ctx.out(f"PASS ({'real upstream' if real else 'loopback upstream, real=false: not a real-upstream result'}): {summary}")
    return _record(ctx, "apply", "pass", summary, EXIT_PASS, real=real, upstream=url)


# -- dry run -----------------------------------------------------------------------------------------------------------

def _dry_run(ctx: _Ctx, sub: str, url: str | None, allow_loopback: bool) -> int:
    ctx.out("DRY RUN: nothing will be executed")
    ctx.out(f"PLAN for --{sub}")
    ctx.out("preconditions (shown, NOT enforced in a dry run):")
    if sub in ("planner", "doc"):
        ctx.out(f"  ANTHROPIC_API_KEY: {'present' if ctx.key else 'ABSENT (a real run would stop here: ' + KEY_MISSING_MESSAGE + ')'}")
        if (ctx.env.get("ANTHROPIC_AUTH_TOKEN") or "").strip() and not ctx.key:
            ctx.out("  ANTHROPIC_AUTH_TOKEN is set but no longer supported (CLAUDE.md D16)")
    else:
        if url:
            public, why = is_real_upstream(url)
            ctx.out(f"  upstream URL: given ({url}); {why}"
                    + ("" if public or allow_loopback else "; a real run would refuse it without --allow-loopback"))
        else:
            ctx.out("  upstream URL: ABSENT (a real run would stop with UPSTREAM_MISSING)")
        ctx.out(f"  a real run would probe the URL once (timeout {PROBE_TIMEOUT_S} s); it must answer with a status >= 400 that is "
                "not 429 or 5xx")
    if sub in ("planner", "doc"):
        all_ids: list[str] = []
        ctx.out("commands a real run executes (cwd = the repo, child env LIVE_TESTS=1):")
        for n, (select, files) in enumerate(_groups(sub), 1):
            xml = f".perf/live_judgment/<UTC timestamp>_{sub}{'' if n == 1 else f'_{n}'}.xml"
            ctx.out("  LIVE_TESTS=1 " + _shown(["python", *_pytest_cmd(files, select, None)[1:], f"--junitxml={xml}"]))
            ids, problem = _collect(files, select, ctx.env)
            ctx.out(f"  (collect-only for this selection: {problem or str(len(ids)) + ' node id(s)'})")
            all_ids += ids
        ctx.out(f"pytest node ids that would run ({len(all_ids)}):")
        for nid in all_ids:
            ctx.out(f"  {nid}")
        if sub == "doc":
            ctx.out("then, only if those passed: pipeline.process(inbox/dogceo.doc.md, Layout(<throwaway copy>), "
                    "doc_client=HttpDocExtractionClient(AnthropicHTTP(<key>))); required outcome status: generated "
                    "(blocked_at_review_gate or failed is a `fail` with the reason)")
        ctx.out("the source the node ids execute (verbatim, from the test files):")
        for label, src in assertion_sources(all_ids):
            ctx.out(f"--- {label}")
            ctx.out(src)
        ctx.out(f"wire probe: the request the production {sub} client sends (MockTransport, no network; key redacted):")
        probe = wire_probe(sub, ctx.env)
        ctx.out(json.dumps(_truncate({k: probe[k] for k in ("method", "url", "headers", "body")}), indent=2))
        ctx.out(f"  key sent matches the key from the environment: {probe['key_sent_matches_env']} (key source: {probe['key_source']})")
    else:
        ctx.out("existing live-gated tests: none (no existing test needs a real upstream)")
        ctx.out("the steps of the loop, run in a throwaway git copy of the project:")
        for n, text in enumerate(APPLY_STEPS, 1):
            ctx.out(f"  {n}. {text}")
        ctx.out("commands inside step 6: pytest -q -p no:cacheprovider tests/test_contract.py -k liveprobe "
                "(cwd = the throwaway project, CONTRACT_SPEC_DIRS = its specs/)")
        ctx.out("a real run also records: the URL, the spec it wrote and the commit the loop made are in the throwaway project, "
                "which is deleted on success and kept (path printed and recorded) on failure")
    ctx.out("a dry run writes no record and makes no network request.")
    return EXIT_PASS


# -- cli ---------------------------------------------------------------------------------------------------------------

def _parser() -> _Parser:
    ap = _Parser(prog="live_judgment.py", description="Run the live judgment gaps (planner, doc, apply) and record every attempt.")
    ap.add_argument("--planner", action="store_true", help="the existing planner contract tests against the real model")
    ap.add_argument("--doc", action="store_true", help="the existing doc contract test, then the real pipeline on dogceo.doc.md")
    ap.add_argument("--apply", nargs="?", const="", default=None, metavar="URL",
                    help="the learning loop against URL (an endpoint failing with an undeclared HTTP status)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; run nothing, record nothing")
    ap.add_argument("--allow-loopback", action="store_true", help="allow a loopback/private upstream (tests; real=false)")
    return ap


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    ctx = _Ctx(os.environ if env is None else env, stdout or sys.stdout, stderr or sys.stderr)
    try:
        args = _parser().parse_args(argv)
        chosen = [name for name, on in (("planner", args.planner), ("doc", args.doc), ("apply", args.apply is not None)) if on]
        if len(chosen) != 1:
            raise _UsageError("give exactly one of --planner, --doc, --apply [URL]" + (f" (got {len(chosen)})" if chosen else ""))
    except _UsageError as exc:
        ctx.stderr.write(f"usage error: {exc}\n")
        return EXIT_USAGE
    except SystemExit as exc:  # --help exits 0; anything else is a usage error, never argparse's 2
        return EXIT_PASS if exc.code in (0, None) else EXIT_USAGE
    sub = chosen[0]
    url = (args.apply or None) if sub == "apply" else None
    if args.dry_run:
        return _dry_run(ctx, sub, url, args.allow_loopback)
    if sub == "planner":
        return _cmd_planner(ctx)
    if sub == "doc":
        return _cmd_doc(ctx)
    return _cmd_apply(ctx, url, args.allow_loopback)


if __name__ == "__main__":
    raise SystemExit(main())
