"""Tests for servers/banking_server.py via the in-memory MCP v2 transport."""
from __future__ import annotations

import json

import pytest
from mcp import Client

import banking_server
from banking_server import mcp

VERIFIED, PENDING, REJECTED, EXPIRED = ("CUST-TEST-0001", "CUST-TEST-0002", "CUST-TEST-0003",
                                        "CUST-TEST-0004")
CHK, FROZEN, PEND_INR, PEND_USD, REJ_ACCT, EXP_ACCT = (
    "ACCT-TEST-0001", "ACCT-TEST-0002", "ACCT-TEST-0003", "ACCT-TEST-0004", "ACCT-TEST-0005",
    "ACCT-TEST-0006")


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.delenv("BANKING_FAIL_MODE", raising=False)
    banking_server._seed()
    yield
    banking_server._seed()


def _text(r) -> str:
    return "".join(getattr(c, "text", "") for c in r.content)


async def call(name: str, args: dict):
    async with Client(mcp) as c:
        return await c.call_tool(name, args)


async def ok(name: str, args: dict) -> dict:
    r = await call(name, args)
    assert not r.is_error, _text(r)
    return json.loads(_text(r))


async def fails(name: str, args: dict, code: str, retryable: bool) -> None:
    r = await call(name, args)
    assert r.is_error, _text(r)
    assert f"[{code}] retryable={str(retryable).lower()}" in _text(r), _text(r)


async def bal(acct: str) -> float:
    return (await ok("get_account_balance", {"account_id": acct}))["available_balance"]


async def test_discovery():
    async with Client(mcp) as c:
        tools = (await c.list_tools()).tools
    assert sorted(t.name for t in tools) == ["get_account_balance", "transfer_funds", "verify_kyc"]
    assert all(t.description and len(t.description) > 50 for t in tools)


async def test_verify_kyc_shape():
    d = await ok("verify_kyc", {"customer_id": VERIFIED})
    assert d["customer_id"] == VERIFIED and d["kyc_status"] == "VERIFIED"
    assert d["verified_at"] and d["risk_level"] and d["account_ids"] == [CHK, FROZEN]


@pytest.mark.parametrize("cust,status", [(VERIFIED, "VERIFIED"), (PENDING, "PENDING"),
                                         (REJECTED, "REJECTED"), (EXPIRED, "EXPIRED")])
async def test_every_kyc_status_is_data(cust, status):
    assert (await ok("verify_kyc", {"customer_id": cust}))["kyc_status"] == status


async def test_pending_has_null_verified_at():
    assert (await ok("verify_kyc", {"customer_id": PENDING}))["verified_at"] is None


async def test_unknown_customer():
    await fails("verify_kyc", {"customer_id": "NOPE"}, "CUSTOMER_NOT_FOUND", False)


async def test_balance_shape():
    d = await ok("get_account_balance", {"account_id": CHK})
    assert d["account_id"] == CHK and d["customer_id"] == VERIFIED and d["currency"] == "INR"
    assert d["available_balance"] == 500000.0 and d["ledger_balance"] == 500000.0
    assert d["status"] == "ACTIVE" and d["as_of"]
    assert (await ok("get_account_balance", {"account_id": FROZEN}))["status"] == "FROZEN"
    assert (await ok("get_account_balance", {"account_id": PEND_USD}))["currency"] == "USD"


async def test_unknown_account():
    await fails("get_account_balance", {"account_id": "NOPE"}, "ACCOUNT_NOT_FOUND", False)


async def test_transfer_success_moves_both_balances_and_conserves_total():
    before = await bal(CHK), await bal(PEND_INR)
    d = await ok("transfer_funds", {"from_acct": CHK, "to_acct": PEND_INR, "amount": 1234.56})
    assert d["status"] == "COMPLETED" and d["transfer_id"] and d["timestamp"]
    assert d["from_acct"] == CHK and d["to_acct"] == PEND_INR
    assert d["amount"] == 1234.56 and d["currency"] == "INR"
    after = await bal(CHK), await bal(PEND_INR)
    assert d["from_balance_after"] == after[0] == round(before[0] - 1234.56, 2)
    assert after[1] == round(before[1] + 1234.56, 2)
    assert round(sum(before), 2) == round(sum(after), 2)


@pytest.mark.parametrize("amount", [0, -5, 10.123])
async def test_invalid_amount(amount):
    await fails("transfer_funds", {"from_acct": CHK, "to_acct": PEND_INR, "amount": amount},
                "INVALID_AMOUNT", False)


async def test_same_account():
    await fails("transfer_funds", {"from_acct": CHK, "to_acct": CHK, "amount": 1},
                "SAME_ACCOUNT", False)


async def test_transfer_account_not_found():
    await fails("transfer_funds", {"from_acct": "NOPE", "to_acct": CHK, "amount": 1},
                "ACCOUNT_NOT_FOUND", False)
    await fails("transfer_funds", {"from_acct": CHK, "to_acct": "NOPE", "amount": 1},
                "ACCOUNT_NOT_FOUND", False)


async def test_frozen_as_sender_and_receiver():
    await fails("transfer_funds", {"from_acct": FROZEN, "to_acct": CHK, "amount": 1},
                "ACCOUNT_FROZEN", False)
    await fails("transfer_funds", {"from_acct": CHK, "to_acct": FROZEN, "amount": 1},
                "ACCOUNT_FROZEN", False)


async def test_insufficient_funds():
    await fails("transfer_funds", {"from_acct": CHK, "to_acct": PEND_INR, "amount": 600000},
                "INSUFFICIENT_FUNDS", False)


async def test_currency_mismatch():
    await fails("transfer_funds", {"from_acct": CHK, "to_acct": PEND_USD, "amount": 1},
                "CURRENCY_MISMATCH", False)


@pytest.mark.parametrize("src", [PEND_INR, REJ_ACCT, EXP_ACCT])
async def test_kyc_not_verified(src):
    await fails("transfer_funds", {"from_acct": src, "to_acct": CHK, "amount": 1},
                "KYC_NOT_VERIFIED", False)


async def test_failed_transfer_changes_nothing():
    before = {a: await bal(a) for a in (CHK, PEND_INR, FROZEN)}
    await fails("transfer_funds", {"from_acct": CHK, "to_acct": PEND_INR, "amount": 600000},
                "INSUFFICIENT_FUNDS", False)
    await fails("transfer_funds", {"from_acct": CHK, "to_acct": FROZEN, "amount": 5},
                "ACCOUNT_FROZEN", False)
    assert {a: await bal(a) for a in before} == before
    assert banking_server._TRANSFERS == {} and banking_server._DAILY_SPENT == {}


async def test_daily_limit_accumulates():
    await ok("transfer_funds", {"from_acct": CHK, "to_acct": PEND_INR, "amount": 60000})
    before = await bal(CHK)
    await fails("transfer_funds", {"from_acct": CHK, "to_acct": PEND_INR, "amount": 50000},
                "DAILY_LIMIT_EXCEEDED", False)
    assert await bal(CHK) == before
    await ok("transfer_funds", {"from_acct": CHK, "to_acct": PEND_INR, "amount": 40000})
    await fails("transfer_funds", {"from_acct": CHK, "to_acct": PEND_INR, "amount": 0.01},
                "DAILY_LIMIT_EXCEEDED", False)


CALLS = [("verify_kyc", {"customer_id": VERIFIED}),
         ("get_account_balance", {"account_id": CHK}),
         ("transfer_funds", {"from_acct": CHK, "to_acct": PEND_INR, "amount": 1})]


@pytest.mark.parametrize("name,args", CALLS)
async def test_core_down(monkeypatch, name, args):
    monkeypatch.setenv("BANKING_FAIL_MODE", "core_down")
    await fails(name, args, "CORE_BANKING_UNAVAILABLE", True)
    monkeypatch.delenv("BANKING_FAIL_MODE")
    assert await bal(CHK) == 500000.0  # outage moved nothing


async def test_unexpected_exception_becomes_internal_error(monkeypatch):
    def boom(_tool: str) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(banking_server, "_check_outage", boom)
    for name, args in CALLS:
        await fails(name, args, "INTERNAL_ERROR", False)
