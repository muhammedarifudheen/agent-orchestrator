"""The minimal API (api/app.py, CLAUDE.md D38): three endpoints over the demo's own functions, nothing else.

  * each endpoint returns its documented shape on real fixtures (the real matcher, the real write policy, the real proposer);
  * the service has exactly three routes, all POST (the automatic documentation routes are off);
  * /v1/propose, and every other endpoint, leaves specs/, servers/, handoffs/ (and learning/, proposals/, duties/, agent/) byte-identical;
  * the service refuses to start when a model key is set: in-process, as `import api.app`, and under uvicorn;
  * no outbound network: the real network transports and any non-loopback socket raise while every endpoint runs (the test client is
    in-process and unaffected), importing the service loads no Anthropic SDK and no HTTP client, and api/ imports neither;
  * the README's curl examples are run, and the rows its "not exposed" line names are still open in AUDIT.md section 3a.
Does not prove: anything about a deployed service (none exists), authentication or rate limiting (the service has neither)."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from test_audit_ids import norm_id, tables

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "demo" / "fixtures"
WATCHED = ("specs", "servers", "handoffs", "learning", "proposals", "duties", "agent")
KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PROVIDER_API_KEY")
REFUSAL = "this service serves AUDIT §2 flows only; ANTHROPIC_API_KEY must be unset."


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def tree() -> dict[str, str]:
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for d in WATCHED
            for p in sorted((ROOT / d).rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


# Taken at COLLECTION time, before any test has called an endpoint. Comparing each call with the state just before it would miss a service that
# writes the same file every time (an earlier test would already have created it); comparing with this cannot.
BASELINE = tree()


@pytest.fixture(scope="module", autouse=True)
def repo_untouched_by_the_whole_module():
    yield
    assert tree() == BASELINE, "a test in this module left a watched directory changed"


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The REAL network paths raise; the test client's in-process transport is a different class and keeps working."""
    attempts: list[str] = []
    loopback = {"127.0.0.1", "::1", "localhost", None, ""}
    real_connect, real_gai = socket.socket.connect, socket.getaddrinfo

    def boom(name):
        def _raise(*a, **k):
            attempts.append(name)
            raise AssertionError("network call attempted")
        return _raise

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom("httpx.HTTPTransport"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom("httpx.AsyncHTTPTransport"))
    try:  # mcp 2.x loads httpx2 when the write check builds its in-process client; patch that library's network paths too
        import httpx2
        monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", boom("httpx2.HTTPTransport"))
        monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", boom("httpx2.AsyncHTTPTransport"))
    except (ImportError, AttributeError):
        pass

    def guarded_connect(self, address):  # asyncio's Windows event loop connects to 127.0.0.1 for its own self-pipe
        host = address[0] if isinstance(address, tuple) else address
        return real_connect(self, address) if host in loopback else boom("socket.connect")()

    def guarded_gai(host, *a, **k):
        return real_gai(host, *a, **k) if host in loopback else boom("socket.getaddrinfo")()

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_gai)
    yield attempts
    assert attempts == [], f"network attempts: {attempts}"


@pytest.fixture(scope="module")
def api_module():
    with pytest.MonkeyPatch.context() as mp:  # a developer's shell must not stop the import (the check runs at import); restored afterwards
        for var in KEY_VARS:
            mp.delenv(var, raising=False)
        return importlib.import_module("api.app")


@pytest.fixture
def client(api_module):
    return TestClient(api_module.app)


# -- (1) shapes on real fixtures ---------------------------------------------------------------------------------------------------------------

def test_duty_match_returns_the_duty_that_would_run(client):
    r = client.post("/v1/duty-match", json={"intent": "refund order ORD-ABCD1234"})
    assert r.status_code == 200
    assert r.json() == {"duty": "process_refund", "confidence": 1.0, "write_boundary": "retail__issue_refund"}


def test_a_near_miss_has_a_confidence_but_no_duty_and_no_boundary(client):
    """`refund ORD-ABCD1234` has a real candidate (process_refund, 0.5) that is below the threshold, so it would NOT run: the service must not name it."""
    body = client.post("/v1/duty-match", json={"intent": "refund ORD-ABCD1234"}).json()
    assert body == {"duty": None, "confidence": 0.5, "write_boundary": None}


def test_duty_match_without_a_duty_is_null_not_a_guess(client):
    body = client.post("/v1/duty-match", json={"intent": "what is the weather in Pune"}).json()
    assert body == {"duty": None, "confidence": 0.0, "write_boundary": None}


@pytest.mark.parametrize("tool,allow,expect", [
    ("search_flights", False, dict(is_write=False, allowed=True, refusal_code=None, pending_approval=None)),
    ("book_flight", False, dict(is_write=True, allowed=False, refusal_code="WRITE_APPROVAL_REQUIRED")),
    ("transfer_funds", False, dict(is_write=True, allowed=False, refusal_code="WRITE_APPROVAL_REQUIRED")),
    ("book_flight", True, dict(is_write=True, allowed=True, refusal_code=None, pending_approval=None)),
    ("transfer_funds", True, dict(is_write=True, allowed=True, refusal_code=None, pending_approval=None)),
])
def test_write_check_follows_the_real_policy(client, tool, allow, expect):
    r = client.post("/v1/write-check", json={"tool": tool, "allow_writes": allow})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"is_write", "allowed", "refusal_code", "pending_approval"}
    for key, value in expect.items():
        assert body[key] == value, (key, body)
    if not body["allowed"]:
        assert body["pending_approval"]["kind"] == "pending_write_approval"
        assert body["pending_approval"]["writes"][0]["status"] == "pending"


@pytest.mark.parametrize("name,kind,has_action", [
    ("handoff_undeclared_status", "add_error_code", False),
    ("handoff_wrong_param_type", "correct_param_type", False),
    ("handoff_declared_code", "mark_needs_review", True),
])
def test_propose_returns_the_real_proposal_and_never_applies_it(client, name, kind, has_action):
    r = client.post("/v1/propose", json={"handoff": fixture(name)})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"proposal_type", "diff", "action_md", "applied", "specs_written"}
    assert body["proposal_type"] == kind and body["diff"].startswith(f"# proposal: {kind}")
    assert (body["action_md"] is not None) is has_action
    assert body["applied"] is False and body["specs_written"] is False
    if has_action:
        assert "INVALID_COORDINATES" in body["action_md"]


def test_propose_for_a_system_with_no_spec_is_none(client):
    body = client.post("/v1/propose", json={"handoff": fixture("handoff_no_spec_system")}).json()
    assert body == {"proposal_type": "none", "diff": "", "action_md": None, "applied": False, "specs_written": False}


def test_bad_input_is_refused_and_nothing_executes(client):
    evil = fixture("handoff_undeclared_status")
    evil["error_snapshot"]["error_code"] = "../../evil"
    assert client.post("/v1/propose", json={"handoff": evil}).status_code == 422
    assert client.post("/v1/propose", json={"handoff": {"error_snapshot": "nope"}}).status_code == 422
    assert client.post("/v1/propose", json={"handoff": "not an object"}).status_code == 422
    assert client.post("/v1/write-check", json={"tool": "drop_tables"}).status_code == 422
    assert client.post("/v1/duty-match", json={"intent": "x" * 501}).status_code == 422
    assert client.post("/v1/duty-match", json={}).status_code == 422
    assert client.post("/v1/propose", content=b"x" * 70000, headers={"content-type": "application/json"}).status_code == 413


def test_the_service_has_exactly_three_flow_routes_all_post_and_one_health_route(api_module):
    routes = {(r.path, tuple(sorted(r.methods))) for r in api_module.app.routes}
    assert routes == {("/v1/duty-match", ("POST",)), ("/v1/write-check", ("POST",)), ("/v1/propose", ("POST",)), ("/healthz", ("GET",))}


def test_healthz_returns_200_and_exactly_the_three_flows(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "flows": ["duty-match", "write-check", "propose"], "planner": False, "doc_extraction": False}


def test_the_public_health_response_names_no_audit_row_and_claims_no_unproven_capability(client):
    text = client.get("/healthz").text
    assert not re.search(r"\bC\d+\b", text), "no audit row id of any kind"
    for row in ("C24", "C25", "C27"):
        assert row not in text
    body = client.get("/healthz").json()
    assert body["planner"] is False and body["doc_extraction"] is False, "the capabilities that are open in AUDIT.md 3a are stated as absent"
    assert set(body) == {"status", "flows", "planner", "doc_extraction"}, "nothing else is said"
    assert not any(word in text.lower() for word in ("claude", "anthropic", "model", "live", "real upstream")), text


def test_the_health_check_calls_no_flow_and_needs_no_input(client, monkeypatch):
    import sys
    demo = sys.modules["api_demo_flows"]
    for name in ("match_duty", "write_safety", "learning_proposal"):
        monkeypatch.setattr(demo, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("healthz called a flow")))
    assert client.get("/healthz").status_code == 200
    assert client.post("/healthz").status_code == 405


# -- (2) nothing is written to the repo ---------------------------------------------------------------------------------------------------------

def test_no_endpoint_changes_a_file_under_specs_servers_handoffs_or_anything_it_imports(client):
    calls = [("duty-match", "/v1/duty-match", {"intent": "refund order ORD-ABCD1234"})]
    calls += [(f"write-check {t} {a}", "/v1/write-check", {"tool": t, "allow_writes": a})
              for t in ("search_flights", "book_flight", "transfer_funds") for a in (False, True)]
    calls += [(f"propose {p.stem}", "/v1/propose", {"handoff": json.loads(p.read_text(encoding="utf-8"))}) for p in sorted(FIXTURES.glob("*.json"))]
    evil = fixture("handoff_undeclared_status")
    evil["error_snapshot"]["error_code"] = "../../evil"
    calls.append(("propose rejected", "/v1/propose", {"handoff": evil}))
    assert len(calls) == 1 + 6 + 4 + 1
    for label, path, payload in calls:
        client.post(path, json=payload)
        assert tree() == BASELINE, f"{label} changed the repo (compared with the state before any endpoint was called)"


# -- (3) the startup refusal --------------------------------------------------------------------------------------------------------------------

def _load_with(env_key: str, value: str, monkeypatch):
    monkeypatch.setenv(env_key, value)
    spec = importlib.util.spec_from_file_location("api_app_refusal_probe", ROOT / "api" / "app.py")
    return importlib.util.module_from_spec(spec), spec


def test_the_mandated_sentence_is_exact_when_anthropic_api_key_is_set(monkeypatch):
    mod, spec = _load_with("ANTHROPIC_API_KEY", "sk-test-not-a-real-key", monkeypatch)
    with pytest.raises(SystemExit) as exc:
        spec.loader.exec_module(mod)
    assert str(exc.value) == REFUSAL


@pytest.mark.parametrize("var", KEY_VARS)
def test_any_model_key_variable_stops_the_service_and_names_itself(var, monkeypatch):
    mod, spec = _load_with(var, "x", monkeypatch)
    with pytest.raises(SystemExit) as exc:
        spec.loader.exec_module(mod)
    assert str(exc.value) == f"this service serves AUDIT §2 flows only; {var} must be unset."


def test_an_empty_value_is_not_a_key(api_module):
    api_module.assert_flows_only({"ANTHROPIC_API_KEY": "  "})
    api_module.assert_flows_only({})


def _run(args: list[str], extra_env: dict[str, str], timeout: int = 120):
    env = {**{k: v for k, v in os.environ.items() if k not in KEY_VARS}, "PYTHONUTF8": "1", **extra_env}
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout)


def test_importing_the_service_as_a_subprocess_refuses_with_a_key_and_works_without():
    refused = _run(["-c", "import api.app"], {"ANTHROPIC_API_KEY": "sk-test-not-a-real-key"})
    assert refused.returncode != 0 and REFUSAL in refused.stderr
    assert _run(["-c", "import api.app"], {}).returncode == 0, "the control: the same import without a key"


def test_uvicorn_refuses_to_start_with_a_key():
    done = _run(["-m", "uvicorn", "api.app:app", "--port", "18766"], {"ANTHROPIC_API_KEY": "sk-test-not-a-real-key"}, timeout=90)
    assert done.returncode != 0 and REFUSAL in done.stderr, done.stderr[-600:]


# -- (4) no outbound network ---------------------------------------------------------------------------------------------------------------------

def test_the_guard_is_live_a_real_outbound_request_raises(no_network):
    with pytest.raises(AssertionError, match="network call attempted"):
        httpx.get("http://example.invalid/")
    no_network.clear()  # that attempt was the control, not the service's


def test_importing_the_service_loads_no_anthropic_sdk_and_no_http_client():
    code = ("import sys, api.app\n"
            "print('LOADED:' + ','.join(n for n in ('anthropic', 'httpx', 'gradio', 'requests') if n in sys.modules))\n")
    done = _run(["-c", code], {})
    assert done.returncode == 0, done.stderr[-500:]
    assert done.stdout.strip().splitlines()[-1] == "LOADED:", done.stdout


def test_nothing_under_api_imports_the_anthropic_sdk_or_httpx_or_lists_them_as_requirements():
    for p in (ROOT / "api").glob("*.py"):
        assert not re.search(r"^\s*(import|from)\s+(anthropic|httpx|requests|gradio)\b", p.read_text(encoding="utf-8"), re.M), p
    reqs = (ROOT / "api" / "requirements.txt").read_text(encoding="utf-8").lower()
    names = {re.split(r"[<>=\[\s]", ln.strip(), maxsplit=1)[0] for ln in reqs.splitlines() if ln.strip() and not ln.startswith("#")}
    assert {"fastapi", "uvicorn", "mcp", "pyyaml", "jsonschema"} <= names
    assert not names & {"anthropic", "httpx", "requests", "gradio"}, names


# -- (5) the README ---------------------------------------------------------------------------------------------------------------------------------

README = (ROOT / "api" / "README.md").read_text(encoding="utf-8")


def test_the_readme_curl_examples_are_real_and_run(client):
    shots = re.findall(r"curl -s -X POST http://127\.0\.0\.1:8000(/v1/[a-z-]+) -H 'content-type: application/json' -d '(.*)'\n", README)
    assert [p for p, _ in shots] == ["/v1/duty-match", "/v1/write-check", "/v1/propose"], "one example per endpoint"
    for path, body in shots:
        r = client.post(path, json=json.loads(body))
        assert r.status_code == 200, (path, r.text[:200])
    assert client.post(shots[2][0], json=json.loads(shots[2][1])).json()["proposal_type"] == "add_error_code", "the documented propose example does what it says"


def test_the_readme_names_the_rows_that_are_not_exposed_and_they_are_still_open():
    m = re.search(r"This API serves AUDIT §2 proven flows only\. (C\d+(?:, C\d+)*) are not exposed\.", README)
    assert m, "the README must carry its one line"
    named = {norm_id(c) for c in m.group(1).split(", ")}
    open_rows = set(tables((ROOT / "AUDIT.md").read_text(encoding="utf-8"))["3a"])
    assert named == {"C24", "C25", "C27"}
    assert named <= open_rows, f"rows the README says are not exposed but that are no longer open: {sorted(named - open_rows)}"
