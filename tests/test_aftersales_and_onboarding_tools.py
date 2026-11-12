"""The seven mock tools added for the duty library (D22): retail after-sales (verify_order, check_refund_eligibility,
issue_refund, notify_customer) and banking onboarding (create_customer, open_account, send_welcome).

Same style as test_retail.py / test_banking.py; the shared contract test (tests/test_contract.py) already checks their
docstrings, annotations and error format, so these assert behaviour: results, the state they change, and each error code
with its retryable/recovery pair."""
from __future__ import annotations

import json

import pytest
from mcp import Client

import banking_server
import retail_server


@pytest.fixture(autouse=True)
def _worlds(monkeypatch):
    for var in ("RETAIL_FAIL_MODE", "BANKING_FAIL_MODE"):
        monkeypatch.delenv(var, raising=False)
    retail_server._reset_state()
    banking_server._seed()
    yield
    retail_server._reset_state()
    banking_server._seed()


async def call(module, name: str, args: dict):
    async with Client(module.mcp) as c:
        return await c.call_tool(name, args)


def text(r) -> str:
    return "".join(getattr(b, "text", "") for b in r.content)


def data(r) -> dict:
    assert not r.is_error, text(r)
    sc = getattr(r, "structured_content", None)
    if sc:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(text(r))


async def failure(module, name: str, args: dict, code: str, retryable: bool, recovery: str):
    r = await call(module, name, args)
    assert r.is_error, "expected a failure"
    assert f"[{code}] retryable={str(retryable).lower()} recovery={recovery}" in text(r), text(r)


async def paid_order(cust="C-1001") -> dict:
    order = data(await call(retail_server, "create_order", {"sku": "SKU-1001", "qty": 2, "customer_id": cust}))
    data(await call(retail_server, "charge_payment", {"order_id": order["order_id"], "amount": order["total_amount"]}))
    return order


# -- retail after-sales ----------------------------------------------------------------------------------------------

async def test_verify_order_returns_the_order_with_its_current_status():
    order = await paid_order()
    got = data(await call(retail_server, "verify_order", {"order_id": order["order_id"]}))
    assert got["status"] == "PAID" and got["customer_id"] == "C-1001" and got["total_amount"] == 49.98


async def test_verify_order_unknown_id_is_an_agent_recoverable_failure():
    await failure(retail_server, "verify_order", {"order_id": "ORD-NOPE"}, "ORDER_NOT_FOUND", False, "agent")


@pytest.mark.parametrize("setup,eligible,amount", [("paid", True, 49.98), ("pending", False, 0.0)])
async def test_refund_eligibility_is_data_not_an_error(setup, eligible, amount):
    order = await paid_order() if setup == "paid" else data(
        await call(retail_server, "create_order", {"sku": "SKU-1001", "qty": 2, "customer_id": "C-1001"}))
    got = data(await call(retail_server, "check_refund_eligibility", {"order_id": order["order_id"]}))
    assert got["eligible"] is eligible and got["refundable_amount"] == amount
    assert got["status"] == ("PAID" if eligible else "PENDING_PAYMENT") and got["reason"]


async def test_issue_refund_refunds_once_and_marks_the_order():
    order = await paid_order()
    refund = data(await call(retail_server, "issue_refund", {"order_id": order["order_id"], "amount": order["total_amount"]}))
    assert refund["status"] == "REFUNDED" and refund["amount"] == 49.98 and refund["refund_id"].startswith("RFD-")
    assert retail_server._ORDERS[order["order_id"]]["status"] == "REFUNDED"
    # a second refund must never pay out twice
    await failure(retail_server, "issue_refund", {"order_id": order["order_id"], "amount": 49.98}, "ALREADY_REFUNDED", False, "human")
    assert len(retail_server._REFUNDS) == 1
    # and an order that was refunded is no longer eligible
    again = data(await call(retail_server, "check_refund_eligibility", {"order_id": order["order_id"]}))
    assert again["eligible"] is False and again["refundable_amount"] == 0.0


async def test_issue_refund_refuses_an_unpaid_order_a_wrong_amount_and_an_unknown_order():
    pending = data(await call(retail_server, "create_order", {"sku": "SKU-1001", "qty": 1, "customer_id": "C-1001"}))
    await failure(retail_server, "issue_refund", {"order_id": pending["order_id"], "amount": 24.99}, "NOT_REFUNDABLE", False, "human")
    order = await paid_order()
    await failure(retail_server, "issue_refund", {"order_id": order["order_id"], "amount": 1.0}, "AMOUNT_MISMATCH", False, "agent")
    await failure(retail_server, "issue_refund", {"order_id": "ORD-NOPE", "amount": 1.0}, "ORDER_NOT_FOUND", False, "agent")
    assert retail_server._ORDERS[order["order_id"]]["status"] == "PAID" and not retail_server._REFUNDS


async def test_issue_refund_gateway_outage_is_retryable_and_refunds_nothing(monkeypatch):
    order = await paid_order()
    monkeypatch.setenv("RETAIL_FAIL_MODE", "payment_down")
    await failure(retail_server, "issue_refund", {"order_id": order["order_id"], "amount": order["total_amount"]},
                  "PAYMENT_GATEWAY_TIMEOUT", True, "agent")
    assert retail_server._ORDERS[order["order_id"]]["status"] == "PAID" and not retail_server._REFUNDS


async def test_notify_customer_sends_and_records_the_message():
    got = data(await call(retail_server, "notify_customer", {"customer_id": "C-1001", "message": "Your refund has been issued."}))
    assert got["status"] == "SENT" and got["channel"] == "email" and got["notification_id"].startswith("NTF-")
    assert retail_server._NOTIFICATIONS[-1]["message"] == "Your refund has been issued."


async def test_notify_customer_rejects_an_unknown_customer_and_an_empty_message():
    await failure(retail_server, "notify_customer", {"customer_id": "C-NOPE", "message": "hi"}, "CUSTOMER_NOT_FOUND", False, "agent")
    await failure(retail_server, "notify_customer", {"customer_id": "C-1001", "message": "   "}, "EMPTY_MESSAGE", False, "agent")
    assert not retail_server._NOTIFICATIONS


# -- banking onboarding ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("document,status", [("DOC-VALID-1", "VERIFIED"), ("doc-valid-2", "VERIFIED"),
                                             ("DOC-REJECT-1", "REJECTED"), ("PASSPORT-9", "PENDING")])
async def test_create_customer_runs_the_mock_document_check(document, status):
    got = data(await call(banking_server, "create_customer", {"name": "Priya Nair", "document_id": document}))
    assert got["kyc_status"] == status and got["customer_id"].startswith("CUST-NEW-") and got["name"] == "Priya Nair"
    read_back = data(await call(banking_server, "verify_kyc", {"customer_id": got["customer_id"]}))
    assert read_back["kyc_status"] == status and read_back["account_ids"] == []


async def test_create_customer_mints_distinct_ids_and_validates_its_arguments():
    a = data(await call(banking_server, "create_customer", {"name": "A", "document_id": "DOC-VALID-1"}))
    b = data(await call(banking_server, "create_customer", {"name": "B", "document_id": "DOC-VALID-2"}))
    assert a["customer_id"] != b["customer_id"]
    await failure(banking_server, "create_customer", {"name": " ", "document_id": "DOC-VALID-1"}, "INVALID_NAME", False, "agent")
    await failure(banking_server, "create_customer", {"name": "C", "document_id": ""}, "INVALID_DOCUMENT", False, "agent")


async def test_open_account_needs_a_verified_customer_and_a_supported_currency():
    verified = data(await call(banking_server, "create_customer", {"name": "Priya", "document_id": "DOC-VALID-1"}))
    pending = data(await call(banking_server, "create_customer", {"name": "Ravi", "document_id": "PASSPORT-9"}))
    account = data(await call(banking_server, "open_account", {"customer_id": verified["customer_id"], "currency": "inr"}))
    assert account["status"] == "ACTIVE" and account["currency"] == "INR" and account["available_balance"] == 0.0
    balance = data(await call(banking_server, "get_account_balance", {"account_id": account["account_id"]}))
    assert balance["customer_id"] == verified["customer_id"] and balance["available_balance"] == 0.0
    assert data(await call(banking_server, "verify_kyc", {"customer_id": verified["customer_id"]}))["account_ids"] == [account["account_id"]]
    await failure(banking_server, "open_account", {"customer_id": pending["customer_id"], "currency": "INR"}, "KYC_NOT_VERIFIED", False, "human")
    await failure(banking_server, "open_account", {"customer_id": verified["customer_id"], "currency": "XYZ"}, "INVALID_CURRENCY", False, "agent")
    await failure(banking_server, "open_account", {"customer_id": "CUST-NOPE", "currency": "INR"}, "CUSTOMER_NOT_FOUND", False, "agent")


async def test_send_welcome_requires_the_account_to_belong_to_the_customer():
    one = data(await call(banking_server, "create_customer", {"name": "One", "document_id": "DOC-VALID-1"}))
    two = data(await call(banking_server, "create_customer", {"name": "Two", "document_id": "DOC-VALID-2"}))
    acct = data(await call(banking_server, "open_account", {"customer_id": one["customer_id"], "currency": "USD"}))
    sent = data(await call(banking_server, "send_welcome", {"customer_id": one["customer_id"], "account_id": acct["account_id"]}))
    assert sent["status"] == "SENT" and sent["message_id"].startswith("MSG-") and banking_server._WELCOMES == [sent]
    await failure(banking_server, "send_welcome", {"customer_id": two["customer_id"], "account_id": acct["account_id"]}, "ACCOUNT_MISMATCH", False, "agent")
    await failure(banking_server, "send_welcome", {"customer_id": one["customer_id"], "account_id": "ACCT-NOPE"}, "ACCOUNT_NOT_FOUND", False, "agent")
    await failure(banking_server, "send_welcome", {"customer_id": "CUST-NOPE", "account_id": acct["account_id"]}, "CUSTOMER_NOT_FOUND", False, "agent")


async def test_the_new_banking_tools_respect_the_core_outage_switch(monkeypatch):
    monkeypatch.setenv("BANKING_FAIL_MODE", "core_down")
    await failure(banking_server, "create_customer", {"name": "A", "document_id": "DOC-VALID-1"}, "CORE_BANKING_UNAVAILABLE", True, "agent")
    assert not any(k.startswith("CUST-NEW") for k in banking_server._CUSTOMERS)


async def test_reseeding_clears_everything_the_new_tools_created():
    c = data(await call(banking_server, "create_customer", {"name": "A", "document_id": "DOC-VALID-1"}))
    data(await call(banking_server, "open_account", {"customer_id": c["customer_id"], "currency": "INR"}))
    banking_server._seed()
    assert not any(k.startswith(("CUST-NEW", "ACCT-NEW")) for k in [*banking_server._CUSTOMERS, *banking_server._ACCOUNTS])
    assert not banking_server._WELCOMES and not banking_server._COUNTERS
