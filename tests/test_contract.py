"""The shared contract every MCP server must satisfy, hand-written or generated.

The SAME tests run against every target below. Assertions are system-independent: they say
nothing about flights, orders or weather. Per-target data is only a list of probes (a tool call and
the outcome it must produce, including who can recover: agent|human) plus an optional outage probe.

Targets:
  * hand-written: travel, retail, banking  (probes listed here)
  * generated:    every specs/*.yaml       (probes come from the spec's `examples`)

Raw exceptions: every server, hand-written or generated, is tested end to end for "an unexpected
RuntimeError inside a tool body reaches the client as INTERNAL_ERROR, not a traceback". Generated
servers are crashed through their injectable HTTP transport; hand-written ones through a named
helper their tools call (`Target.crash`). Same assertions, same shared wrapper (`@guarded`).
"""
from __future__ import annotations

import importlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from mcp import Client

from agent.tool_registry import parse_error

ROOT = Path(__file__).parent.parent
TOMORROW = (date.today() + timedelta(days=1)).isoformat()
ERROR_FORMAT = re.compile(r"\[([A-Z][A-Z0-9_]*)\] retryable=(true|false) recovery=(agent|human) \S")


@dataclass
class Target:
    name: str
    module: str
    probes: list[dict[str, Any]]
    outage: dict[str, Any] | None = None  # {"env": {..}, "tool", "args", "code"} or {"mock":..}
    crash: dict[str, Any] | None = None  # hand-written only: {"patch": helper name, "tool", "args"}
    generated: bool = False
    ids: list[str] = field(default_factory=list)


def probe(tool: str, args: dict, expect: Any = "ok", **extra: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args, "expect": expect, **extra}


def fail(code: str, retryable: bool, recovery: str) -> dict[str, Any]:
    return {"code": code, "retryable": retryable, "recovery": recovery}


HAND_WRITTEN = [
    Target("travel", "travel_server", [
        probe("search_flights", {"origin": "DEL", "destination": "BOM", "date": TOMORROW}),
        probe("search_flights", {"origin": "DEL", "destination": "BOM", "date": "not-a-date"}, fail("INVALID_DATE", False, "agent")),
        probe("book_flight", {"flight_id": "XX_DELBOM_2030-01-01", "passenger_name": "A B",
                              "passenger_email": "a@b.co"}, fail("FLIGHT_NOT_FOUND", False, "agent")),
        probe("send_confirmation", {"email": "nope", "booking_ref": "BK1", "details": "d"}, fail("INVALID_EMAIL", False, "agent")),
    ], outage={"env": {"TRAVEL_FAIL_MODE": "all_down"}, "tool": "search_flights",
               "args": {"origin": "DEL", "destination": "BOM", "date": TOMORROW}, "code": "SERVICE_UNAVAILABLE",
               "recovery": "agent"},
           crash={"patch": "_check_outage", "tool": "search_flights",
                  "args": {"origin": "DEL", "destination": "BOM", "date": TOMORROW}}),
    Target("retail", "retail_server", [
        probe("check_inventory", {"sku": "SKU-1001", "qty": 1}),
        probe("check_inventory", {"sku": "SKU-NOPE", "qty": 1}, fail("SKU_NOT_FOUND", False, "agent")),
        probe("create_order", {"sku": "SKU-1001", "qty": 0, "customer_id": "C-1001"}, fail("INVALID_QUANTITY", False, "agent")),
        probe("charge_payment", {"order_id": "ORD-NOPE", "amount": 1.0}, fail("ORDER_NOT_FOUND", False, "agent")),
        probe("create_shipment", {"order_id": "ORD-NOPE", "address": "12 MG Road, Pune 411001"}, fail("ORDER_NOT_FOUND", False, "agent")),
    ], crash={"patch": "_get_sku", "tool": "check_inventory", "args": {"sku": "SKU-1001", "qty": 1}}),
    Target("banking", "banking_server", [
        probe("verify_kyc", {"customer_id": "CUST-TEST-0001"}),
        probe("verify_kyc", {"customer_id": "CUST-NOPE"}, fail("CUSTOMER_NOT_FOUND", False, "agent")),
        probe("get_account_balance", {"account_id": "ACCT-TEST-0001"}),
        probe("transfer_funds", {"from_acct": "ACCT-TEST-0001", "to_acct": "ACCT-TEST-0001", "amount": 1.0},
              fail("SAME_ACCOUNT", False, "agent")),
        probe("transfer_funds", {"from_acct": "ACCT-TEST-0001", "to_acct": "ACCT-TEST-0002", "amount": -5.0},
              fail("INVALID_AMOUNT", False, "agent")),
        probe("transfer_funds", {"from_acct": "ACCT-TEST-0002", "to_acct": "ACCT-TEST-0001", "amount": 1.0},
              fail("ACCOUNT_FROZEN", False, "human")),  # a business refusal: a person must decide
    ], outage={"env": {"BANKING_FAIL_MODE": "core_down"}, "tool": "get_account_balance",
               "args": {"account_id": "ACCT-TEST-0001"}, "code": "CORE_BANKING_UNAVAILABLE", "recovery": "agent"},
           crash={"patch": "_check_outage", "tool": "get_account_balance", "args": {"account_id": "ACCT-TEST-0001"}}),
]


def spec_dirs() -> list[Path]:
    """Where generated targets come from: specs/ by default. The ingestion pipeline sets
    CONTRACT_SPEC_DIRS (os.pathsep separated) to run THIS SAME test on a staged spec before promoting it.
    Only discovery changes; no assertion depends on it."""
    raw = os.environ.get("CONTRACT_SPEC_DIRS", "")
    return [Path(p) for p in raw.split(os.pathsep) if p] or [ROOT / "specs"]


def generated_targets() -> list[Target]:
    targets = []
    for spec_path in sorted(p for d in spec_dirs() for p in d.glob("*.yaml")):
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        probes = [{"tool": op["name"], "args": ex["args"], "expect": ex["expect"], "mock": ex.get("mock")}
                  for op in spec["operations"] for ex in op["examples"]]
        outage = next(({"mock": p["mock"], "tool": p["tool"], "args": p["args"], "code": p["expect"]["code"],
                        "recovery": p["expect"].get("recovery")}
                       for p in probes if p["expect"] != "ok" and p["expect"]["retryable"] and p.get("mock")), None)
        targets.append(Target(spec["system_name"], f"{spec['system_name']}_server", probes, outage, generated=True))
    return targets


TARGETS = HAND_WRITTEN + generated_targets()
TARGET_IDS = [("generated:" if t.generated else "hand:") + t.name for t in TARGETS]
ALL_PROBES = [(t, p) for t in TARGETS for p in t.probes]
PROBE_IDS = [f"{'generated' if t.generated else 'hand'}:{t.name}:{p['tool']}:"
             f"{'ok' if p['expect'] == 'ok' else p['expect']['code']}" for t, p in ALL_PROBES]


def load(target: Target):
    module = importlib.import_module(target.module)
    for reset in ("_reset_state", "_seed"):  # mock backends: start every test from seed data
        if hasattr(module, reset):
            getattr(module, reset)()
    if hasattr(module, "_BOOKINGS"):
        module._BOOKINGS.clear()
    return module


def mock_transport(mock: dict | None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if mock is None:
            raise AssertionError("probe has no mock but the request reached the transport")
        if "raises" in mock:
            raise getattr(httpx, mock["raises"])("simulated", request=request)
        return httpx.Response(mock.get("status", 200), json=mock.get("body"))

    return httpx.MockTransport(handler)


def arm(module, target: Target, probe_: dict, monkeypatch) -> None:
    if target.generated:
        monkeypatch.setattr(module._client, "transport", mock_transport(probe_.get("mock")))


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


async def run(module, tool: str, args: dict):
    async with Client(module.mcp) as client:
        return await client.call_tool(tool, args)


@pytest.fixture(params=TARGETS, ids=TARGET_IDS)
def target(request):
    return request.param


# -- static contract: how tools describe themselves --------------------------------------------

async def tool_list(module):
    async with Client(module.mcp) as client:
        return (await client.list_tools()).tools


async def test_exposes_tools_with_descriptions(target):
    tools = await tool_list(load(target))
    assert tools, "server exposes no tools"
    assert all((t.description or "").strip() for t in tools)


async def test_descriptions_state_when_to_call_args_and_errors(target):
    for t in await tool_list(load(target)):
        d = t.description
        assert re.search(r"(?im)^\s*(when to call:|call )", d), f"{t.name}: no 'when to call' sentence"
        assert re.search(r"(?m)^\s*Errors:", d), f"{t.name}: no 'Errors:' line"
        assert "retryable" in d, f"{t.name}: Errors line must state retryable flags"
        if t.input_schema.get("properties"):
            assert re.search(r"(?m)^\s*Args:", d), f"{t.name}: has parameters but no 'Args:' section"


async def test_every_tool_declares_safety_annotations(target):
    for t in await tool_list(load(target)):
        a = t.annotations
        assert a is not None, f"{t.name}: no annotations"
        assert a.read_only_hint is not None and a.idempotent_hint is not None, f"{t.name}: safety hints missing"
        if a.read_only_hint is False and a.idempotent_hint is False:
            assert re.search(r"(?i)not idempotent|do not retry|moves money|changes state|side effect", t.description), \
                f"{t.name}: non-idempotent tool must warn in its description"


async def test_every_tool_is_wrapped_by_guarded(target):
    module = load(target)
    for t in await tool_list(module):
        fn = module.mcp._tool_manager.get_tool(t.name).fn
        assert getattr(fn, "__guarded__", False), f"{t.name} is not wrapped by @guarded"


# -- behavioural contract: probes --------------------------------------------------------------

@pytest.mark.parametrize("target_probe", ALL_PROBES, ids=PROBE_IDS)
async def test_probe_outcome_follows_the_contract(target_probe, monkeypatch):
    target, p = target_probe
    module = load(target)
    arm(module, target, p, monkeypatch)
    result = await run(module, p["tool"], p["args"])
    text = text_of(result)
    if p["expect"] == "ok":
        assert not result.is_error, text
        payload = result.structured_content if result.structured_content is not None else json.loads(text)
        if isinstance(payload, dict):  # never a normal result that carries an error
            assert not payload.get("error") and not payload.get("error_code"), payload
    else:
        assert result.is_error, f"expected failure {p['expect']} but call succeeded: {text}"
        m = ERROR_FORMAT.search(text)
        assert m, f"failure text does not follow '[CODE] retryable=<bool> message': {text!r}"
        exp = p["expect"]
        assert (m.group(1), m.group(2) == "true") == (exp["code"], exp["retryable"]), text
        if "recovery" in exp:  # mandatory for hand-written probes; optional in spec examples
            assert m.group(3) == exp["recovery"], text
        parsed = parse_error(text)  # and the registry parses it identically
        assert (parsed.code, parsed.retryable, parsed.recovery) == (m.group(1), m.group(2) == "true", m.group(3))
        assert parsed.message


async def test_missing_arguments_are_a_failure_the_registry_normalizes(target):
    module = load(target)
    tool = next((t for t in await tool_list(module) if t.input_schema.get("required")), None)
    if tool is None:
        pytest.skip(f"{target.name}: no tool has required arguments")
    result = await run(module, tool.name, {})
    assert result.is_error
    assert parse_error(text_of(result))[:3] == ("INVALID_ARGUMENTS", False, "agent")


async def test_outage_is_a_retryable_failure(target, monkeypatch):
    if target.outage is None:
        pytest.skip(f"{target.name}: no outage probe")
    module = load(target)
    for k, v in (target.outage.get("env") or {}).items():
        monkeypatch.setenv(k, v)
    if target.generated:
        monkeypatch.setattr(module._client, "transport", mock_transport(target.outage["mock"]))
    result = await run(module, target.outage["tool"], target.outage["args"])
    assert result.is_error
    parsed = parse_error(text_of(result))
    assert (parsed.code, parsed.retryable) == (target.outage["code"], True)
    if target.outage.get("recovery"):
        assert parsed.recovery == target.outage["recovery"]


async def test_unexpected_exception_becomes_internal_error(target, monkeypatch):
    """A raw RuntimeError inside a tool body must reach the client as INTERNAL_ERROR (human), never a traceback."""
    module = load(target)
    if target.generated:
        ok = next(p for p in target.probes if p["expect"] == "ok")
        tool, args = ok["tool"], ok["args"]

        def boom_transport(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(module._client, "transport", httpx.MockTransport(boom_transport))
    else:
        assert target.crash, f"{target.name}: hand-written targets must declare a crash hook (no skipping)"
        tool, args = target.crash["tool"], target.crash["args"]

        def boom(*_a, **_k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(module, target.crash["patch"], boom)  # the tool body calls this helper
    result = await run(module, tool, args)
    text = text_of(result)
    assert result.is_error, text
    assert parse_error(text)[:3] == ("INTERNAL_ERROR", False, "human"), text
    assert ERROR_FORMAT.search(text), text
    assert "Traceback" not in text and 'File "' not in text, text
