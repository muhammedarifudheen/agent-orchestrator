"""Tests for the travel MCP server via the in-memory v2 Client."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from mcp import Client

import travel_server
from travel_server import mcp

FUTURE = (date.today() + timedelta(days=14)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()
IN_STOCK = f"AI-865_DELBOM_{FUTURE}"
SOLD = f"UK-995_DELBOM_{FUTURE}"


@pytest.fixture(autouse=True)
def _no_fail_mode(monkeypatch):
    monkeypatch.delenv("TRAVEL_FAIL_MODE", raising=False)


async def call(name, args):
    async with Client(mcp) as c:
        return await c.call_tool(name, args)


def text(r) -> str:
    return " ".join(getattr(b, "text", "") for b in r.content)


def payload(r):
    sc = getattr(r, "structured_content", None)
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(text(r))


def assert_failure(r, code, retryable):
    assert r.is_error
    t = text(r)
    assert f"[{code}] retryable={str(retryable).lower()}" in t, t
    # The SDK prefixes "Error executing tool <name>: "; the structured [CODE] part must follow it.
    assert t.split("] ", 1)[0].endswith(f"[{code}")


async def test_discovery_and_descriptions():
    async with Client(mcp) as c:
        tools = (await c.list_tools()).tools
    assert {t.name for t in tools} == {"search_flights", "book_flight", "send_confirmation"}
    for t in tools:
        assert t.description and "When to call" in t.description


async def test_search_success_shape():
    r = await call("search_flights", {"origin": "del", "destination": "BOM", "date": FUTURE})
    assert not r.is_error
    flights = payload(r)
    assert len(flights) == 4
    for f in flights:
        for k in ("flight_id", "airline", "depart", "arrive", "seats_available", "price_inr"):
            assert k in f
    assert IN_STOCK in {f["flight_id"] for f in flights}


async def test_search_unknown_route_empty():
    r = await call("search_flights", {"origin": "DEL", "destination": "XYZ", "date": FUTURE})
    assert not r.is_error
    assert payload(r) == []


async def test_search_invalid_airport():
    r = await call("search_flights", {"origin": "DELHI", "destination": "BOM", "date": FUTURE})
    assert_failure(r, "INVALID_AIRPORT", False)


async def test_search_invalid_date_past_and_malformed():
    r = await call("search_flights", {"origin": "DEL", "destination": "BOM", "date": PAST})
    assert_failure(r, "INVALID_DATE", False)
    r = await call("search_flights", {"origin": "DEL", "destination": "BOM", "date": "07/11/2030"})
    assert_failure(r, "INVALID_DATE", False)


async def test_book_flight_not_found():
    r = await call("book_flight", {"flight_id": f"ZZ-1_DELBOM_{FUTURE}", "passenger_name": "A B",
                                   "passenger_email": "a@b.com"})
    assert_failure(r, "FLIGHT_NOT_FOUND", False)
    r = await call("book_flight", {"flight_id": "garbage", "passenger_name": "A B",
                                   "passenger_email": "a@b.com"})
    assert_failure(r, "FLIGHT_NOT_FOUND", False)


async def test_book_flight_past_date_rejected():
    r = await call("book_flight", {"flight_id": f"AI-865_DELBOM_{PAST}", "passenger_name": "A B",
                                   "passenger_email": "a@b.com"})
    assert_failure(r, "FLIGHT_NOT_FOUND", False)


async def test_book_flight_sold_out():
    r = await call("book_flight", {"flight_id": SOLD, "passenger_name": "A B",
                                   "passenger_email": "a@b.com"})
    assert_failure(r, "SOLD_OUT", False)


@pytest.mark.parametrize("name,email", [("   ", "a@b.com"), ("A B", "not-an-email")])
async def test_book_flight_invalid_passenger(name, email):
    r = await call("book_flight", {"flight_id": IN_STOCK, "passenger_name": name,
                                   "passenger_email": email})
    assert_failure(r, "INVALID_PASSENGER", False)


async def test_send_confirmation_errors():
    r = await call("send_confirmation", {"email": "bad", "booking_ref": "BK000000", "details": "x"})
    assert_failure(r, "INVALID_EMAIL", False)
    r = await call("send_confirmation", {"email": "a@b.com", "booking_ref": "BK000000", "details": "x"})
    assert_failure(r, "BOOKING_NOT_FOUND", False)


async def test_book_then_confirm_happy_path():
    r = await call("book_flight", {"flight_id": IN_STOCK, "passenger_name": "Asha Rao",
                                   "passenger_email": "asha@example.com"})
    assert not r.is_error
    b = payload(r)
    assert b["status"] == "CONFIRMED" and b["booking_ref"].startswith("BK")
    assert b["total_price_inr"] == 7420 and b["flight_id"] == IN_STOCK
    r2 = await call("send_confirmation", {"email": "asha@example.com",
                                          "booking_ref": b["booking_ref"], "details": "DEL-BOM"})
    assert not r2.is_error
    assert payload(r2) is True


@pytest.mark.parametrize("mode,tool,args", [
    ("search_down", "search_flights", {"origin": "DEL", "destination": "BOM", "date": FUTURE}),
    ("booking_down", "book_flight", {"flight_id": IN_STOCK, "passenger_name": "A B",
                                     "passenger_email": "a@b.com"}),
    ("all_down", "send_confirmation", {"email": "a@b.com", "booking_ref": "BK1", "details": "x"}),
    ("all_down", "search_flights", {"origin": "DEL", "destination": "BOM", "date": FUTURE}),
])
async def test_fail_mode_service_unavailable(monkeypatch, mode, tool, args):
    monkeypatch.setenv("TRAVEL_FAIL_MODE", mode)
    assert_failure(await call(tool, args), "SERVICE_UNAVAILABLE", True)


async def test_unexpected_exception_becomes_internal_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(travel_server, "_check_outage", boom)
    r = await call("search_flights", {"origin": "DEL", "destination": "BOM", "date": FUTURE})
    assert_failure(r, "INTERNAL_ERROR", False)
    assert "RuntimeError" in text(r)
