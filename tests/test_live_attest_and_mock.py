"""Attestation, the offline mock, and the rule that a mock can never produce `real: true` (CLAUDE.md D26).

`real` is what scripts/live_attest.py observed (which transport carried each request, and what answered), never what a flag
says. The mock (tests/live_mock.py) injects a transport that is not httpx's real one, so by construction it cannot attest as
real; and the harness additionally forces real=false whenever mock mode is on. These tests try to make a fake look real."""
from __future__ import annotations

import hashlib
import http.server
import importlib.util
import io
import json
import subprocess
import sys
import threading
from pathlib import Path

import httpx
from agent.provider_config import ANTHROPIC_HOST as _HOST
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
MOCK_KEY = "sk-ant-offline-mock-key"
for p in (SCRIPTS, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import live_attest  # noqa: E402
import live_mock  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lj = _load("live_judgment")


class Upstream:
    """A loopback http.server answering a fixed status; the 'host' under test is 127.0.0.1."""

    def __init__(self, status: int) -> None:
        outer = self
        self.hits = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                body = b'{"ok": true}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/x"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    servers = []

    def make(status=200):
        servers.append(Upstream(status))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


# -- the attestation itself -------------------------------------------------------------------------------------------------------

def test_a_request_over_the_real_transport_that_is_answered_2xx_attests_as_real(tmp_path, upstream):
    srv = upstream(200)
    f = tmp_path / "att.jsonl"
    with live_attest.attesting(f), httpx.Client() as c:
        assert c.get(srv.url).status_code == 200
    att = live_attest.summarize(f, "127.0.0.1")
    assert att["real"] is True and att["real_requests"] == 1 and att["non_real_requests"] == 0
    assert att["transports"] == [live_attest.REAL_TRANSPORT]


def test_a_mock_transport_can_never_attest_as_real_however_it_answers(tmp_path):
    f = tmp_path / "att.jsonl"
    with live_attest.attesting(f), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as c:
        c.get(f"https://{_HOST}/v1/messages")
    att = live_attest.summarize(f, _HOST)
    assert att["real"] is False and att["real_requests"] == 0 and att["non_real_requests"] == 1
    assert att["transports"] == ["httpx.MockTransport"]


def test_the_offline_mock_transport_is_not_the_real_transport_and_attests_as_not_real(tmp_path):
    f = tmp_path / "att.jsonl"
    with live_attest.attesting(f), live_mock.mocked():
        from ingest.anthropic_http import AnthropicHTTP
        client = AnthropicHTTP(MOCK_KEY)
        assert type(client._http._transport).__name__ == "OfflineAnthropicMock"
        client._http.post("/v1/messages", json={"tools": [], "messages": []})
    att = live_attest.summarize(f, _HOST)
    assert att["real"] is False and att["non_real_requests"] == 1 and att["transports"] == ["live_mock.OfflineAnthropicMock"]


def test_a_wrong_key_is_answered_with_401_and_is_not_real_for_the_api_but_is_for_an_upstream_expected_to_fail(tmp_path, upstream):
    srv = upstream(401)
    f = tmp_path / "att.jsonl"
    with live_attest.attesting(f), httpx.Client() as c:
        c.get(srv.url)
    assert live_attest.summarize(f, "127.0.0.1")["real"] is False                       # an API answer must be a 2xx
    assert live_attest.summarize(f, "127.0.0.1", require_success=False)["real"] is True  # --apply: any real answer is evidence


def test_one_request_over_another_transport_poisons_the_hosts_attestation(tmp_path, upstream):
    srv = upstream(200)
    f = tmp_path / "att.jsonl"
    with live_attest.attesting(f):
        with httpx.Client() as c:
            c.get(srv.url)
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
            c.get(srv.url)
    att = live_attest.summarize(f, "127.0.0.1")
    assert att["real_requests"] == 1 and att["non_real_requests"] == 1 and att["real"] is False


def test_a_failed_connection_is_recorded_but_is_not_a_real_answer(tmp_path):
    f = tmp_path / "att.jsonl"
    with live_attest.attesting(f), httpx.Client(timeout=2) as c:
        with pytest.raises(httpx.HTTPError):
            c.get("http://127.0.0.1:1/x")
    att = live_attest.summarize(f, "127.0.0.1")
    assert att["requests"] == 1 and att["real_requests"] == 0 and att["real"] is False


def test_uninstalling_restores_httpx_exactly():
    before = httpx.Client.send
    with _tmp_attesting():
        assert getattr(httpx.Client.send, "_live_attest", False)
    assert httpx.Client.send is before and not getattr(httpx.Client.send, "_live_attest", False)


def _tmp_attesting():
    import tempfile
    return live_attest.attesting(Path(tempfile.mkdtemp()) / "a.jsonl")


def test_the_sitecustomize_makes_a_child_python_attest_through_the_environment(tmp_path, upstream):
    srv = upstream(200)
    f = tmp_path / "child.jsonl"
    env = {**live_attest.child_environment(f, ROOT), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    code = f"import httpx; print(httpx.Client().get({srv.url!r}).status_code)"
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "200", done.stderr
    assert live_attest.summarize(f, "127.0.0.1")["real"] is True


def test_without_the_environment_variable_the_hook_does_nothing(tmp_path):
    done = subprocess.run([sys.executable, "-c", "import httpx; print(getattr(httpx.Client.send, '_live_attest', False))"],
                          env={"PYTHONPATH": str(SCRIPTS / "live_attest_site"), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
                          capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "False"


# -- the mock's replies ------------------------------------------------------------------------------------------------------------

def test_the_real_transport_constant_is_httpx_own_class_name():
    """If httpx ever renames its transport, attestation would silently stop recognising real traffic: fail loudly instead."""
    t = httpx.HTTPTransport()
    assert f"{type(t).__module__}.{type(t).__qualname__}" == live_attest.REAL_TRANSPORT
    m = httpx.MockTransport(lambda r: httpx.Response(200))
    assert f"{type(m).__module__}.{type(m).__qualname__}" != live_attest.REAL_TRANSPORT


def test_the_mock_answers_a_planner_request_with_a_valid_plan_and_a_doc_request_with_the_report(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", MOCK_KEY)
    from fake_anthropic import tool_use_response  # noqa: F401  (the reply shapes are the shared fake's)
    from ingest.anthropic_http import AnthropicHTTP
    from ingest.doc import TOOL_NAME, HttpDocExtractionClient

    with live_mock.mocked():
        from planners import ClaudePlanner
        from test_planner_contract import INTENTS
        import asyncio
        from inmem import InMemoryRegistry
        async def load():
            async with InMemoryRegistry(ROOT / "servers") as reg:
                return [dict(t) for t in reg.anthropic_tools()]
        plan = ClaudePlanner().plan(INTENTS, asyncio.run(load()))
        assert sorted(c.intent_id for c in plan) == sorted(i["id"] for i in INTENTS)
        report = HttpDocExtractionClient(AnthropicHTTP(MOCK_KEY)).extract((ROOT / "tests/ingest/fixtures/dogceo.doc.md").read_text(encoding="utf-8"))
        assert report and TOOL_NAME


def test_the_mock_refuses_a_request_it_has_no_canned_reply_for():
    t = live_mock.OfflineAnthropicMock()
    r = t.handle_request(httpx.Request("POST", f"https://{_HOST}/v1/messages", json={"tools": [{"name": "other"}], "messages": []}))
    assert r.status_code == 400 and "no canned reply" in r.text


def test_installing_the_mock_is_undone_and_leaves_no_trace_in_other_tests():
    from ingest import anthropic_http as ah
    original = ah.AnthropicHTTP.__init__
    with live_mock.mocked():
        assert ah.AnthropicHTTP.__init__ is not original
    assert ah.AnthropicHTTP.__init__ is original


# -- the harness: a mock run can never be real ---------------------------------------------------------------------------------------

def run_cli(args, env_extra, log):
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "LIVE_"))}  # a normal environment, minus any real key
    env.update({"LIVE_JUDGMENT_LOG": str(log), **env_extra})
    done = subprocess.run([sys.executable, str(SCRIPTS / "live_judgment.py"), *args], env=env, cwd=ROOT, capture_output=True,
                          text=True, timeout=240)
    return done.returncode, done.stdout + done.stderr


def last_record(log):
    return json.loads(log.read_text(encoding="utf-8").splitlines()[-1])


@pytest.mark.parametrize("sub", ["planner", "doc"])
def test_a_mock_run_passes_but_is_recorded_mock_true_real_false_with_the_key_present(sub, tmp_path):
    log = tmp_path / "log.jsonl"
    code, out = run_cli([f"--{sub}", "--mock-transport"], {"ANTHROPIC_API_KEY": MOCK_KEY}, log)
    assert code == 0, out
    assert "PASS (OFFLINE MOCK, real=false" in out and "PASS (live model)" not in out
    r = last_record(log)
    assert (r["result"], r["key_present"], r["mock"], r["real"], r["exit_code"]) == ("pass", True, True, False, 0)
    assert r["attestation"]["real"] is False and r["attestation"]["real_requests"] == 0
    assert "live_mock.OfflineAnthropicMock" in r["attestation"]["transports"]
    line = log.read_text(encoding="utf-8").splitlines()[-1]
    assert f"RECORD_HASH: {hashlib.sha256(line.encode()).hexdigest()}" in out
    assert r["artifact_sha256"] == hashlib.sha256(Path(r["artifact_path"]).read_bytes()).hexdigest()


def test_a_forged_attestation_file_in_the_environment_cannot_make_a_mock_run_real(tmp_path):
    forged = tmp_path / "forged.jsonl"
    forged.write_text(json.dumps({"host": _HOST, "transport": live_attest.REAL_TRANSPORT, "status": 200}) + "\n" * 1,
                      encoding="utf-8")
    log = tmp_path / "log.jsonl"
    code, out = run_cli(["--planner", "--mock-transport"], {"ANTHROPIC_API_KEY": MOCK_KEY, "LIVE_ATTEST_FILE": str(forged)}, log)
    assert code == 0, out
    assert last_record(log)["real"] is False


def test_even_if_the_attestation_said_real_mock_mode_forces_real_false(tmp_path, monkeypatch):
    """Belt and braces: _record ignores a `real` that mock mode produced, whatever the caller computed."""
    monkeypatch.setenv("LIVE_JUDGMENT_LOG", str(tmp_path / "log.jsonl"))
    ctx = lj._Ctx({"ANTHROPIC_API_KEY": MOCK_KEY, "LIVE_JUDGMENT_LOG": str(tmp_path / "log.jsonl")}, io.StringIO(), io.StringIO())
    ctx.mock = True
    lj._record(ctx, "planner", "pass", "ok", 0, real=True)
    assert last_record(tmp_path / "log.jsonl")["real"] is False
    assert lj._is_real(ctx, {"real": True}) is False
    ctx.mock = False
    assert lj._is_real(ctx, {"real": True}) is True and lj._is_real(ctx, {"real": True}, "skipped") is False
    assert lj._is_real(ctx, {"real": False}) is False


def test_mock_mode_does_not_replace_the_key_requirement(tmp_path):
    log = tmp_path / "log.jsonl"
    code, out = run_cli(["--planner", "--mock-transport"], {}, log)
    assert code == 2 and "ANTHROPIC_API_KEY absent" in out
    assert last_record(log)["result"] == "skipped"


@pytest.mark.parametrize("argv", [["--planner", "--allow-live"], ["--doc", "--allow-live"], ["--apply", "http://x.test/s", "--mock-transport"]])
def test_the_new_flags_are_only_valid_with_their_own_subcommand(argv, tmp_path):
    code, out = run_cli(argv, {"ANTHROPIC_API_KEY": MOCK_KEY}, tmp_path / "log.jsonl")
    assert code == 64 and "applies to" in out and not (tmp_path / "log.jsonl").exists()


def test_apply_against_a_public_upstream_without_allow_live_is_exit_5_and_nothing_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(lj, "_probe_upstream", lambda url: (418, ""))
    monkeypatch.setattr(lj, "_apply_loop", lambda *a, **k: pytest.fail("the loop must not start without --allow-live"))
    log = tmp_path / "log.jsonl"
    out = io.StringIO()
    code = lj.main(["--apply", "https://upstream.example/status/418"], env={"LIVE_JUDGMENT_LOG": str(log)}, stdout=out, stderr=io.StringIO())
    assert code == lj.EXIT_LIVE_NOT_ALLOWED == 5
    assert "LIVE_NOT_ALLOWED" in out.getvalue() and "--allow-live" in out.getvalue()
    r = last_record(log)
    assert (r["result"], r["exit_code"], r["real"], r["allow_live"]) == ("skipped", 5, False, False)


def test_the_dry_run_describes_the_new_requirements(tmp_path):
    code, out = run_cli(["--apply", "https://httpbin.org/status/418", "--dry-run"], {}, tmp_path / "log.jsonl")
    assert code == 0 and "--allow-live" in out
    code, out = run_cli(["--planner", "--dry-run"], {}, tmp_path / "log.jsonl")
    assert code == 0 and "attests every HTTP request" in out and not (tmp_path / "log.jsonl").exists()
