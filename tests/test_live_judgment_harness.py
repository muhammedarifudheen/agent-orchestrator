"""scripts/live_judgment.py: the harness that runs the EXISTING live-gated contract tests against the real model and
records the outcome. Its contract is that an absent precondition is reported as SKIPPED with the exact gap (never a pass),
that it runs the very same tests the offline suite already skips (it only flips the environment), and that the pass
path itself is exercised (offline, against a loopback upstream, and then never counted as a real pass).

Not a gate module (it starts subprocesses and a loopback server). Nothing here may write to the repo's
.perf/live_judgment.jsonl: every run points LIVE_JUDGMENT_LOG at a tmp file, and a module fixture checks the real file.
"""
from __future__ import annotations

import ast
import http.server
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "live_judgment.py"
REAL_LOG = ROOT / ".perf" / "live_judgment.jsonl"
DUMMY_KEY = "sk-ant-dummy-SECRET-123456"

if not SCRIPT.exists():  # a missing harness is a failure of this module, stated once, not an import traceback per test
    pytest.fail(f"{SCRIPT} does not exist", pytrace=False)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("live_judgment", SCRIPT)
lj = importlib.util.module_from_spec(_spec)
sys.modules["live_judgment"] = lj
_spec.loader.exec_module(lj)


def _stat(p: Path):
    return None if not p.exists() else (p.stat().st_size, p.stat().st_mtime_ns)


@pytest.fixture(scope="module", autouse=True)
def repo_log_untouched():
    before = _stat(REAL_LOG)
    yield
    assert _stat(REAL_LOG) == before, "a test wrote to the repo's .perf/live_judgment.jsonl"


# -- helpers -----------------------------------------------------------------------------------------------

def clean_env(tmp_path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "LIVE_TESTS", "LIVE_JUDGMENT_LOG")}
    env["LIVE_JUDGMENT_LOG"] = str(tmp_path / "lj.jsonl")
    env.update(extra)
    return env


def run_script(args, env, timeout=120):
    p = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, cwd=ROOT, capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def records(env) -> list[dict]:
    path = Path(env["LIVE_JUDGMENT_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def only_record(env, result, exit_code, subcommand):
    recs = records(env)
    assert len(recs) == 1, recs
    r = recs[0]
    assert r["result"] == result and r["exit_code"] == exit_code and r["subcommand"] == subcommand, r
    assert r["reason"] and isinstance(r["reason"], str)
    return r


def has_pass_verdict(text: str) -> bool:
    # a verdict, not words like "passed"/"bypass": the standalone token PASS (any case-sensitive upper-case) or "result: pass"
    return bool(re.search(r"\bPASS\b", text) or re.search(r"result\W+pass\b", text, re.I))


class Loopback:
    """A real loopback HTTP server in a thread answering a fixed status with a small JSON body."""

    def __init__(self, status=200, body=b'{"ok": true, "note": "loopback"}'):
        outer = self
        self.hits = 0

        class H(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                outer.hits += 1
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _answer

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


LITERAL_KEY_MISSING = "ANTHROPIC_API_KEY absent; to close this gap, set the key and re-run"


# -- 1. missing key: skipped, exact gap, never a pass -------------------------------------------------------

@pytest.mark.parametrize("sub", ["planner", "doc"])
def test_without_a_key_the_run_is_skipped_with_the_exact_gap_and_never_a_pass(sub, tmp_path):
    env = clean_env(tmp_path)
    code, out = run_script([f"--{sub}"], env)
    assert code == 2, out
    assert lj.KEY_MISSING_MESSAGE in out
    assert LITERAL_KEY_MISSING in out, "the owner's required wording, spelled out here, not read back from the code under test"
    assert not has_pass_verdict(out), out
    r = only_record(env, "skipped", 2, sub)
    assert r["key_present"] is False and r["real"] is False and r["artifact_path"] is None
    assert r["reason"] == LITERAL_KEY_MISSING


def test_the_required_message_is_the_literal_wording_not_just_whatever_the_constant_says():
    assert lj.KEY_MISSING_MESSAGE == LITERAL_KEY_MISSING == "ANTHROPIC_API_KEY absent; to close this gap, set the key and re-run"


@pytest.mark.parametrize("sub", ["planner", "doc"])
def test_only_the_removed_auth_token_is_still_a_missing_key_and_says_it_is_unsupported(sub, tmp_path):
    env = clean_env(tmp_path, ANTHROPIC_AUTH_TOKEN="tok-should-not-count-123")
    code, out = run_script([f"--{sub}"], env)
    assert code == 2, out
    assert "no longer supported" in out
    assert "tok-should-not-count-123" not in out
    assert not has_pass_verdict(out)
    r = only_record(env, "skipped", 2, sub)
    assert r["key_present"] is False and r["real"] is False


# -- 2. apply: every precondition failure is an UPSTREAM_MISSING skip; a healthy upstream is a different skip -----

def test_apply_without_a_url_is_upstream_missing(tmp_path):
    env = clean_env(tmp_path)
    code, out = run_script(["--apply"], env)
    assert code == 3 and re.search(r"^UPSTREAM_MISSING:", out, re.M), out
    assert not has_pass_verdict(out)
    r = only_record(env, "skipped", 3, "apply")
    assert r["real"] is False


def test_apply_with_an_unreachable_host_is_upstream_missing(tmp_path):
    env = clean_env(tmp_path)
    code, out = run_script(["--apply", "http://does-not-exist.invalid/status/418"], env)
    assert code == 3 and re.search(r"^UPSTREAM_MISSING:", out, re.M), out
    only_record(env, "skipped", 3, "apply")


def test_apply_with_a_loopback_url_and_no_allow_flag_says_it_is_not_a_real_upstream(tmp_path):
    env = clean_env(tmp_path)
    with Loopback(200) as srv:
        code, out = run_script(["--apply", f"http://127.0.0.1:{srv.port}/status/418"], env)
        assert srv.hits == 0, "the harness must refuse a loopback upstream before contacting it"
    assert code == 3 and re.search(r"^UPSTREAM_MISSING:", out, re.M), out
    assert "not a real upstream" in out
    only_record(env, "skipped", 3, "apply")


def test_apply_with_allow_loopback_and_a_closed_port_is_upstream_missing(tmp_path):
    env = clean_env(tmp_path)
    code, out = run_script(["--apply", f"http://127.0.0.1:{closed_port()}/status/418", "--allow-loopback"], env)
    assert code == 3 and re.search(r"^UPSTREAM_MISSING:", out, re.M), out
    only_record(env, "skipped", 3, "apply")


def test_apply_against_a_healthy_upstream_is_a_skip_not_a_run(tmp_path):
    env = clean_env(tmp_path)
    with Loopback(200) as srv:
        code, out = run_script(["--apply", f"http://127.0.0.1:{srv.port}/", "--allow-loopback"], env)
    assert code == 4 and re.search(r"^UPSTREAM_HEALTHY:", out, re.M), out
    assert not has_pass_verdict(out)
    r = only_record(env, "skipped", 4, "apply")
    assert r["real"] is False


# -- 3. usage errors -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["--planner", "--doc"], ["--doc", "--apply"]])
def test_a_usage_error_exits_64_never_argparses_2_and_records_nothing(args, tmp_path):
    env = clean_env(tmp_path)
    code, out = run_script(args, env)
    assert code == lj.EXIT_USAGE == 64, out
    assert code != 2
    assert records(env) == []
    assert not Path(env["LIVE_JUDGMENT_LOG"]).exists()


# -- 4. --dry-run ---------------------------------------------------------------------------------------------

def _function_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node)
    raise AssertionError(f"{name} not found in {path}")


def collect_ids(file: str, select: str, env: dict) -> list[str]:
    e = dict(env, LIVE_TESTS="1", ANTHROPIC_API_KEY=DUMMY_KEY)
    p = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", file, "-k", select],
                       env=e, cwd=ROOT, capture_output=True, text=True, timeout=180)
    return [ln.strip() for ln in p.stdout.splitlines() if "::" in ln]


@pytest.fixture
def no_network(monkeypatch):
    """Scoped to THIS process only: pytest --collect-only subprocesses started by the harness are separate processes and
    are unaffected, so the plan can still collect node ids while any socket use in-process is a failure."""
    def boom(*a, **k):
        raise AssertionError("--dry-run made a network request")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


@pytest.mark.parametrize("has_key", [True, False], ids=["with-key", "no-key"])
@pytest.mark.parametrize("sub", ["planner", "doc", "apply"])
def test_dry_run_prints_the_plan_executes_nothing_records_nothing_and_never_prints_the_key(sub, has_key, tmp_path, no_network):
    env = {"LIVE_JUDGMENT_LOG": str(tmp_path / "lj.jsonl"), "PATH": os.environ.get("PATH", ""),
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    if has_key:
        env["ANTHROPIC_API_KEY"] = DUMMY_KEY
    out, err = io.StringIO(), io.StringIO()
    args = [f"--{sub}"] + (["http://does-not-exist.invalid/x"] if sub == "apply" else []) + ["--dry-run"]
    code = lj.main(args, env=env, stdout=out, stderr=err)
    text = out.getvalue() + err.getvalue()
    assert code == 0, text
    assert "DRY RUN: nothing will be executed" in text
    assert not Path(env["LIVE_JUDGMENT_LOG"]).exists(), "--dry-run must write no record"
    assert DUMMY_KEY not in text
    if sub == "planner":
        assert "check_each_intent_once_with_a_tool_of_its_declared_industry" in text
        assert _function_source(ROOT / "tests/scenarios/test_planner_contract.py",
                                "check_each_intent_once_with_a_tool_of_its_declared_industry") in text
        assert "tests/scenarios/test_planner_contract.py" in text and "tests/scenarios/test_cross_industry_planners.py" in text
        assert "claude" in text
    if sub == "doc":
        assert _function_source(ROOT / "tests/ingest/test_doc_contract.py", "check_doc_ingestion_contract") in text
        assert "tests/ingest/test_doc_contract.py" in text and "http" in text
    if sub in ("planner", "doc"):
        assert "pytest" in text
        assert "x-api-key" in text.lower()  # the wire_probe request is shown, with the key redacted


@pytest.mark.parametrize("sub", ["planner", "doc"])
def test_the_dry_run_plan_names_the_node_ids_pytest_really_collects(sub, tmp_path, no_network):
    expected = []
    for entry in lj.LIVE_TESTS[sub]:
        ids = collect_ids(entry["file"], entry["select"], os.environ)
        assert ids, f"{entry} collects nothing"
        expected += ids
    env = {"LIVE_JUDGMENT_LOG": str(tmp_path / "lj.jsonl"), "PATH": os.environ.get("PATH", ""),
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    out = io.StringIO()
    assert lj.main([f"--{sub}", "--dry-run"], env=env, stdout=out, stderr=io.StringIO()) == 0
    missing = [i for i in expected if i not in out.getvalue()]
    assert not missing, f"plan lacks node ids pytest collects: {missing}"
    if sub == "planner":
        assert all("claude" in i for i in expected)
    else:
        assert all("http" in i for i in expected)


# -- 5. SAME-CONTRACT proof ----------------------------------------------------------------------------------

ENTRIES = [(sub, e) for sub, es in lj.LIVE_TESTS.items() for e in es]
ENTRY_IDS = [f"{s}:{Path(e['file']).name}" for s, e in ENTRIES]


def test_live_tests_name_the_subcommands_and_existing_files():
    assert set(lj.LIVE_TESTS) == {"planner", "doc"} or set(lj.LIVE_TESTS) >= {"planner", "doc"}
    for sub, e in ENTRIES:
        assert (ROOT / e["file"]).is_file(), e
        assert e["select"].strip()
    assert {e["file"] for e in lj.LIVE_TESTS["planner"]} == {"tests/scenarios/test_planner_contract.py",
                                                              "tests/scenarios/test_cross_industry_planners.py"}
    assert {e["file"] for e in lj.LIVE_TESTS["doc"]} == {"tests/ingest/test_doc_contract.py"}


def test_the_live_selection_collects_the_live_gated_ids_6_planner_and_1_doc():
    counts = {}
    for sub, e in ENTRIES:
        ids = collect_ids(e["file"], e["select"], os.environ)
        assert ids, e
        assert all(i.startswith(e["file"]) for i in ids)
        needle = "claude" if sub == "planner" else "http"
        assert all(f"[{needle}]" in i or f"-{needle}]" in i or f"[{needle}-" in i for i in ids), ids
        counts[sub] = counts.get(sub, 0) + len(ids)
    assert counts == {"planner": 6, "doc": 1}, counts


def _run_junit(file, select, env, xml_path):
    subprocess.run([sys.executable, "-m", "pytest", "-q", file, "-k", select, f"--junitxml={xml_path}", "-p", "no:cacheprovider"],
                   env=env, cwd=ROOT, capture_output=True, text=True, timeout=300)
    return Path(xml_path).read_text(encoding="utf-8")


def test_without_the_env_the_very_same_ids_are_skipped_so_the_harness_only_flips_the_environment(tmp_path):
    env = clean_env(tmp_path)
    total = 0
    for n, (sub, e) in enumerate(ENTRIES):
        xml = _run_junit(e["file"], e["select"], env, tmp_path / f"j{n}.xml")
        cases = ET.fromstring(xml).findall(".//testcase")
        assert cases, e
        for c in cases:
            assert c.find("skipped") is not None, f"{c.get('name')} was not skipped without the live env"
            assert c.find("failure") is None and c.find("error") is None
        total += len(cases)
        assert lj.classify_junit(xml, len(cases))[0] == "skipped"
    assert total == 7


def _harness_tree() -> ast.Module:
    return ast.parse(SCRIPT.read_text(encoding="utf-8"))


def test_the_harness_source_has_no_check_functions_and_no_assert_statement():
    tree = _harness_tree()
    funcs = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert not [f for f in funcs if f.startswith("check_")], funcs
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Assert)], "the harness must not judge anything itself"


def test_the_harness_does_not_redefine_the_contract():
    contract = set()
    for e in (e for _, e in ENTRIES):
        for n in ast.walk(ast.parse((ROOT / e["file"]).read_text(encoding="utf-8"))):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                contract.add(n.name)
    defined = {n.name for n in ast.walk(_harness_tree()) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert len(contract) > 5
    assert not (contract & defined), contract & defined


# -- 6. classify_junit ---------------------------------------------------------------------------------------

def junit(*cases: str) -> str:
    return '<testsuites><testsuite name="pytest">' + "".join(cases) + "</testsuite></testsuites>"


OK = lambda n: f'<testcase classname="t" name="{n}"/>'  # noqa: E731
FAIL = lambda n: f'<testcase classname="t" name="{n}"><failure message="boom">x</failure></testcase>'  # noqa: E731
ERR = lambda n: f'<testcase classname="t" name="{n}"><error message="kaput">x</error></testcase>'  # noqa: E731
SKIP = lambda n: f'<testcase classname="t" name="{n}"><skipped type="pytest.skip" message="LIVE_TESTS=1 not set"/></testcase>'  # noqa: E731


def test_classify_all_pass_is_pass():
    assert lj.classify_junit(junit(OK("a"), OK("b")), 2)[0] == "pass"


def test_classify_a_failure_is_fail_and_names_the_test():
    verdict, reason = lj.classify_junit(junit(OK("a"), FAIL("test_bad")), 2)
    assert verdict == "fail" and "test_bad" in reason


def test_classify_a_skip_is_never_a_pass_and_carries_the_reason():
    verdict, reason = lj.classify_junit(junit(SKIP("a"), SKIP("b")), 2)
    assert verdict == "skipped" and "LIVE_TESTS=1 not set" in reason
    # one pass plus one skip is not a pass either
    assert lj.classify_junit(junit(OK("a"), SKIP("b")), 2)[0] != "pass"


def test_classify_expecting_zero_is_fail():
    assert lj.classify_junit(junit(), 0)[0] == "fail"
    assert lj.classify_junit(junit(OK("a")), 0)[0] == "fail"


def test_classify_a_count_mismatch_is_fail():
    assert lj.classify_junit(junit(OK("a")), 2)[0] == "fail"
    assert lj.classify_junit(junit(OK("a"), OK("b"), OK("c")), 2)[0] == "fail"


def test_classify_an_error_element_is_fail():
    verdict, reason = lj.classify_junit(junit(OK("a"), ERR("test_err")), 2)
    assert verdict == "fail" and "test_err" in reason


# -- 7. append_record / log_path -----------------------------------------------------------------------------

def test_append_record_is_append_only_valid_json_and_creates_the_directory(tmp_path):
    path = tmp_path / "deep" / "er" / "log.jsonl"
    lj.append_record(path, {"n": 1, "result": "skipped"})
    first = path.read_text(encoding="utf-8")
    lj.append_record(path, {"n": 2, "result": "pass"})
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 2 and text.startswith(first)
    assert [json.loads(x)["n"] for x in lines] == [1, 2]


def test_log_path_default_and_override(tmp_path):
    assert Path(lj.log_path({})).resolve() == (ROOT / ".perf" / "live_judgment.jsonl").resolve()
    target = tmp_path / "x.jsonl"
    assert Path(lj.log_path({"LIVE_JUDGMENT_LOG": str(target)})) == target


# -- 8. is_real_upstream -------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://localhost:8000/x", "http://127.0.0.1:9/x", "http://[::1]:80/x", "http://10.1.2.3/x",
                                 "http://192.168.1.5/x", "http://172.16.0.9/x", "http://169.254.1.1/x"])
def test_loopback_private_and_link_local_are_not_real_upstreams(url):
    real, why = lj.is_real_upstream(url)
    assert real is False and why


@pytest.mark.parametrize("url", ["https://httpbin.org/status/418", "http://93.184.216.34/x"])
def test_public_hosts_are_real_upstreams(url, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("is_real_upstream must not resolve or connect")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    assert lj.is_real_upstream(url)[0] is True


# -- 9. wire_probe: the production client classes, no network --------------------------------------------------

@pytest.mark.parametrize("kind", ["planner", "doc"])
def test_wire_probe_goes_through_the_production_client_with_the_key_redacted(kind, monkeypatch, no_network):
    from ingest import anthropic_http as ah
    calls = {"init": 0, "create": 0, "transports": []}
    orig_init, orig_create = ah.AnthropicHTTP.__init__, ah._Messages.create

    def spy_init(self, *a, **k):
        calls["init"] += 1
        calls["transports"].append(k.get("transport"))
        return orig_init(self, *a, **k)

    def spy_create(self, **body):
        calls["create"] += 1
        return orig_create(self, **body)

    monkeypatch.setattr(ah.AnthropicHTTP, "__init__", spy_init)
    monkeypatch.setattr(ah._Messages, "create", spy_create)
    key = "sk-test-123456"
    probe = lj.wire_probe(kind, env={"ANTHROPIC_API_KEY": key})
    assert calls["init"] >= 1 and calls["create"] >= 1, "the probe did not use the production AnthropicHTTP / Messages.create"
    assert any(t is not None for t in calls["transports"]), "the probe must inject a capturing transport, not the network"
    assert probe["method"] == "POST"
    assert probe["url"].endswith("/v1/messages") and "api.anthropic.com" in probe["url"]
    hdrs = {k.lower(): v for k, v in probe["headers"].items()}
    assert "anthropic-version" in hdrs
    assert "x-api-key" in hdrs and key not in hdrs["x-api-key"] and hdrs["x-api-key"]
    assert key not in json.dumps(probe, default=str)
    assert probe["key_sent_matches_env"] is True
    body = probe["body"]
    body = json.loads(body) if isinstance(body, (str, bytes)) else body
    assert body.get("model")
    names = [t["name"] for t in body["tools"]]
    assert names, body
    if kind == "planner":
        assert "submit_plan" in names
    assert body["messages"] and body["messages"][0]["role"] == "user"


def test_wire_probe_reports_a_key_mismatch_if_the_wrong_key_would_be_sent(monkeypatch, no_network):
    # the flag must be computed, not constant: a different key in the env than the one the client holds -> not True
    probe_a = lj.wire_probe("planner", env={"ANTHROPIC_API_KEY": "sk-test-AAAAAA"})
    probe_b = lj.wire_probe("planner", env={"ANTHROPIC_API_KEY": "sk-test-BBBBBB"})
    assert probe_a["key_sent_matches_env"] is True and probe_b["key_sent_matches_env"] is True
    assert "sk-test-AAAAAA" not in json.dumps(probe_a, default=str)


# -- 10. THE PASS PATH, offline ------------------------------------------------------------------------------------

def _git_status() -> str | None:
    p = subprocess.run(["git", "status", "--short"], cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return "\n".join(sorted(ln for ln in p.stdout.splitlines() if ".perf" not in ln))


def test_the_pass_path_runs_the_real_loop_offline_and_never_counts_as_a_real_pass(tmp_path):
    before = _git_status()
    if before is None:
        pytest.skip("not a git repository: cannot compare git status")
    env = clean_env(tmp_path)
    with Loopback(418, b'{"status": "I am a teapot", "code": 418}') as srv:
        code, out = run_script(["--apply", f"http://127.0.0.1:{srv.port}/status/418", "--allow-loopback"], env, timeout=600)
        assert srv.hits >= 1
    assert code == 0, out
    assert has_pass_verdict(out), out
    recs = records(env)
    assert len(recs) == 1, recs
    r = recs[0]
    assert r["result"] == "pass" and r["exit_code"] == 0 and r["subcommand"] == "apply"
    assert r["real"] is False, "a loopback run must never count as a real pass"
    # artifact_path is only set for a FAILED run (the kept throwaway project); a pass deletes it, so the record's evidence
    # is the reason (what was learned) and the upstream
    assert r["artifact_path"] is None or Path(r["artifact_path"]).name
    assert "418" in r["reason"] and "applied" in r["reason"].lower()
    assert str(srv.port) in r["upstream"]
    assert _git_status() == before, "the harness modified the repo"
