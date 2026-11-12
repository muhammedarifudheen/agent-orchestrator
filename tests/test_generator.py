"""Generator + HTTP runtime behaviour that the shared contract test does not cover."""
from __future__ import annotations

import copy
import importlib
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from mcp import Client

from generators.from_spec import ROOT, SpecError, generate, load_spec
from parse_helpers import parse, parse_full
from _http import HttpClient
from errors import ToolFailure, guarded

WEATHER = ROOT / "specs" / "weather.yaml"

PAYMENTS = {
    "system_name": "ledger",
    "description": "Test ledger API.",
    "base_url": "https://ledger.example.test",
    "auth": {"kind": "bearer", "env": "LEDGER_TOKEN"},
    "operations": [{
        "name": "create_payment",
        "summary": "Create a payment of the given amount.",
        "when_to_call": "Call once per payment, after the amount is confirmed.",
        "side_effect": "money",
        "idempotent": False, "reversible": False, "scope": "external",
        "request": {"method": "POST", "path": "/payments"},
        "params": [{"name": "amount", "type": "number", "in": "body", "required": True,
                    "description": "Amount to pay, greater than zero.", "constraints": {"minimum": 0.01}}],
        "response": {"type": "object", "description": "payment_id and status of the created payment."},
        "examples": [
            {"name": "ok", "args": {"amount": 5.0}, "mock": {"status": 201, "body": {"payment_id": "P1", "status": "OK"}},
             "expect": "ok", "expect_keys": ["payment_id"]},
            {"name": "read_timeout", "args": {"amount": 5.0}, "mock": {"raises": "ReadTimeout"},
             "expect": {"code": "OUTCOME_UNKNOWN", "retryable": False, "recovery": "human"}},
        ],
        "review": {"source": "human", "safety_fields": "reviewed", "reviewer": "test"},
    }],
}


def write_spec(tmp_path: Path, spec: dict) -> Path:
    path = tmp_path / f"{spec['system_name']}.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


# -- generator -----------------------------------------------------------------------------------

@pytest.mark.parametrize("spec", sorted((ROOT / "specs").glob("*.yaml")), ids=lambda p: p.stem)
def test_committed_generated_files_are_up_to_date(spec):
    assert generate(spec, ROOT / "servers", ROOT / "tests", check=True) == []


def test_unknown_spec_keys_are_rejected(tmp_path):
    bad = copy.deepcopy(PAYMENTS)
    bad["operations"][0]["typo_field"] = 1
    with pytest.raises(SpecError, match="typo_field"):
        load_spec(write_spec(tmp_path, bad))


def test_path_variables_must_match_declared_params(tmp_path):
    bad = copy.deepcopy(PAYMENTS)
    bad["operations"][0]["request"]["path"] = "/payments/{payment_id}"
    with pytest.raises(SpecError, match="path variables"):
        load_spec(write_spec(tmp_path, bad))


def test_unreviewed_money_operation_is_refused(tmp_path):
    bad = copy.deepcopy(PAYMENTS)
    bad["operations"][0]["review"]["safety_fields"] = "unreviewed"
    with pytest.raises(SpecError, match="REFUSED"):
        load_spec(write_spec(tmp_path, bad))


def test_write_operation_must_state_idempotency(tmp_path):
    bad = copy.deepcopy(PAYMENTS)
    del bad["operations"][0]["idempotent"]
    with pytest.raises(SpecError, match="idempotent"):
        load_spec(write_spec(tmp_path, bad))


def test_stale_generated_file_is_detected(tmp_path):
    servers, tests = tmp_path / "s", tmp_path / "t"
    servers.mkdir(), tests.mkdir()
    spec = write_spec(tmp_path, PAYMENTS)
    assert len(generate(spec, servers, tests)) == 2
    assert generate(spec, servers, tests, check=True) == []
    (servers / "ledger_server.py").write_text("# hand edited\n", encoding="utf-8")
    assert generate(spec, servers, tests, check=True) == [servers / "ledger_server.py"]


async def test_generated_money_server_never_calls_a_timed_out_write_retryable(tmp_path, monkeypatch):
    """End to end: spec -> generated server -> in-memory client. A read timeout on a non-idempotent
    money operation is OUTCOME_UNKNOWN (not retryable); a connect timeout never reached the server."""
    servers, tests = tmp_path / "s", tmp_path / "t"
    servers.mkdir(), tests.mkdir()
    generate(write_spec(tmp_path, PAYMENTS), servers, tests)
    monkeypatch.syspath_prepend(str(servers))
    monkeypatch.setenv("LEDGER_TOKEN", "secret-token")
    server = importlib.import_module("ledger_server")
    try:
        tool = (await _tools(server))[0]
        assert tool.annotations.idempotent_hint is False and tool.annotations.read_only_hint is False

        seen = {}

        def respond(status=201, body=None, raises=None):
            def handler(request: httpx.Request) -> httpx.Response:
                seen["auth"] = request.headers.get("authorization")
                seen["body"] = request.content
                if raises:
                    raise getattr(httpx, raises)("x", request=request)
                return httpx.Response(status, json=body)
            return handler

        async def call(handler):
            monkeypatch.setattr(server._client, "transport", httpx.MockTransport(handler))
            async with Client(server.mcp) as c:
                return await c.call_tool("create_payment", {"amount": 5.0})

        ok = await call(respond(body={"payment_id": "P1"}))
        assert not ok.is_error and seen["auth"] == "Bearer secret-token" and b"5.0" in seen["body"]
        for raises, code, retryable in [("ReadTimeout", "OUTCOME_UNKNOWN", False),
                                        ("ReadError", "OUTCOME_UNKNOWN", False),
                                        ("ConnectTimeout", "TIMEOUT", True),
                                        ("ConnectError", "NETWORK_ERROR", True)]:
            res = await call(respond(raises=raises))
            assert res.is_error
            assert parse(res) == (code, retryable), raises
            assert parse_full(res)[2] == ("human" if code == "OUTCOME_UNKNOWN" else "agent"), raises
        res = await call(respond(status=500, body={"error": "x"}))  # 5xx on a write: outcome uncertain
        assert parse_full(res) == ("SERVICE_UNAVAILABLE", False, "human")  # write + 5xx: outcome uncertain
        res = await call(respond(status=429, body={}))  # rejected before processing: safe to retry
        assert parse_full(res) == ("RATE_LIMITED", True, "agent")
    finally:
        sys.modules.pop("ledger_server", None)


async def _tools(module):
    async with Client(module.mcp) as c:
        return (await c.list_tools()).tools


# -- http runtime + base --------------------------------------------------------------------------

def _client(auth=None, transport=None, **op_extra):
    op = {"method": "GET", "path": "/x/{id}", "side_effect": "read", "idempotent": True,
          "params": [{"name": "id", "type": "string", "in": "path", "required": True,
                      "constraints": {"pattern": "[a-z]+"}}],
          "response": {"select": None}, "errors": [], **op_extra}
    return HttpClient({"base_url": "https://h.test", "auth": auth or {"kind": "none"}}, {"op": op}, transport)


def test_path_param_is_url_encoded_and_pattern_enforced():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    c = _client(transport=httpx.MockTransport(handler))
    assert c.call("op", {"id": "abc"}) == {"ok": True} and seen["url"] == "https://h.test/x/abc"
    with pytest.raises(ToolFailure) as e:
        c.call("op", {"id": "../etc"})
    assert (e.value.code, e.value.retryable) == ("INVALID_ARGUMENT", False)


def test_missing_credentials_fail_with_auth_not_configured(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    c = _client(auth={"kind": "api_key", "env": "NOPE_KEY", "header": "X-Key"},
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ToolFailure) as e:
        c.call("op", {"id": "abc"})
    assert (e.value.code, e.value.retryable) == ("AUTH_NOT_CONFIGURED", False)


def test_api_key_goes_in_header_and_never_in_error_text(monkeypatch):
    monkeypatch.setenv("K", "s3cret")
    seen = {}

    def handler(request):
        seen["h"] = request.headers.get("x-key")
        return httpx.Response(401, json={"message": "bad key"})

    c = _client(auth={"kind": "api_key", "env": "K", "header": "X-Key"}, transport=httpx.MockTransport(handler))
    with pytest.raises(ToolFailure) as e:
        c.call("op", {"id": "abc"})
    assert seen["h"] == "s3cret" and e.value.code == "AUTH_FAILED" and "s3cret" not in str(e.value)


def test_non_json_success_body_is_unexpected_response():
    c = _client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(ToolFailure) as e:
        c.call("op", {"id": "abc"})
    assert (e.value.code, e.value.retryable) == ("UNEXPECTED_RESPONSE", False)


def test_guarded_timeout_policy_depends_on_idempotency():
    @guarded(idempotent=False)
    def charge() -> None:
        raise TimeoutError("slow")

    @guarded
    def read() -> None:
        raise TimeoutError("slow")

    with pytest.raises(ToolFailure) as e:
        charge()
    assert (e.value.code, e.value.retryable) == ("OUTCOME_UNKNOWN", False)
    with pytest.raises(ToolFailure) as e:
        read()
    assert (e.value.code, e.value.retryable) == ("TIMEOUT", True)
