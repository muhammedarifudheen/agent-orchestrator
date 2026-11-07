"""Tests for the retail MCP server via the in-memory v2 transport."""
from __future__ import annotations

import json

import pytest
from mcp import Client

import retail_server
from retail_server import mcp


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    retail_server._reset_state()
    monkeypatch.delenv("RETAIL_FAIL_MODE", raising=False)
    yield
    retail_server._reset_state()


async def call(name: str, args: dict):
    async with Client(mcp) as c:
        return await c.call_tool(name, args)


def text(r) -> str:
    return "".join(getattr(b, "text", "") for b in r.content)


def data(r) -> dict:
    assert not r.is_error, text(r)
    if getattr(r, "structured_content", None):
        sc = r.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(text(r))


async def fail(name: str, args: dict, code: str, retryable: bool):
    r = await call(name, args)
    assert r.is_error
    assert f"[{code}] retryable={str(retryable).lower()}" in text(r)


async def make_order(sku="SKU-1001", qty=2, cust="C-1001") -> dict:
    return data(await call("create_order", {"sku": sku, "qty": qty, "customer_id": cust}))


async def test_discovery():
    async with Client(mcp) as c:
        tools = (await c.list_tools()).tools
    assert sorted(t.name for t in tools) == ["charge_payment", "check_inventory", "create_order", "create_shipment"]
    assert all(t.description for t in tools)


async def test_check_inventory_success_and_not_enough_is_data():
    d = data(await call("check_inventory", {"sku": "SKU-1001", "qty": 5}))
    assert d["sku"] == "SKU-1001" and d["requested_qty"] == 5 and d["available_qty"] == 120
    assert d["in_stock"] is True and d["unit_price"] == 24.99 and d["currency"] == "USD" and d["warehouse"]
    low = data(await call("check_inventory", {"sku": "SKU-1002", "qty": 10}))
    assert low["in_stock"] is False and low["available_qty"] == 3
    zero = data(await call("check_inventory", {"sku": "SKU-1003", "qty": 1}))
    assert zero["in_stock"] is False and zero["available_qty"] == 0


async def test_check_inventory_errors():
    await fail("check_inventory", {"sku": "NOPE", "qty": 1}, "SKU_NOT_FOUND", False)
    await fail("check_inventory", {"sku": "SKU-1001", "qty": 0}, "INVALID_QUANTITY", False)
    await fail("check_inventory", {"sku": "SKU-1001", "qty": -3}, "INVALID_QUANTITY", False)


async def test_create_order_success_decrements_stock():
    o = await make_order(qty=4)
    assert o["status"] == "PENDING_PAYMENT" and o["order_id"] and o["sku"] == "SKU-1001"
    assert o["qty"] == 4 and o["customer_id"] == "C-1001" and o["unit_price"] == 24.99
    assert o["total_amount"] == 99.96 and o["currency"] == "USD"
    inv = data(await call("check_inventory", {"sku": "SKU-1001", "qty": 1}))
    assert inv["available_qty"] == 116


async def test_create_order_errors():
    await fail("create_order", {"sku": "NOPE", "qty": 1, "customer_id": "C-1001"}, "SKU_NOT_FOUND", False)
    await fail("create_order", {"sku": "SKU-1001", "qty": 0, "customer_id": "C-1001"}, "INVALID_QUANTITY", False)
    await fail("create_order", {"sku": "SKU-1002", "qty": 4, "customer_id": "C-1001"}, "INSUFFICIENT_STOCK", False)
    await fail("create_order", {"sku": "SKU-1003", "qty": 1, "customer_id": "C-1001"}, "INSUFFICIENT_STOCK", False)
    await fail("create_order", {"sku": "SKU-1001", "qty": 1, "customer_id": "C-NOPE"}, "CUSTOMER_NOT_FOUND", False)
    # failed orders must not reserve stock
    assert data(await call("check_inventory", {"sku": "SKU-1002", "qty": 1}))["available_qty"] == 3


async def test_charge_payment_success_and_errors():
    o = await make_order()
    await fail("charge_payment", {"order_id": "ORD-NOPE", "amount": 1.0}, "ORDER_NOT_FOUND", False)
    await fail("charge_payment", {"order_id": o["order_id"], "amount": 1.0}, "AMOUNT_MISMATCH", False)
    p = data(await call("charge_payment", {"order_id": o["order_id"], "amount": o["total_amount"] + 0.004}))
    assert p["status"] == "PAID" and p["payment_id"] and p["order_id"] == o["order_id"]
    assert p["amount"] == o["total_amount"]
    await fail("charge_payment", {"order_id": o["order_id"], "amount": o["total_amount"]}, "ALREADY_PAID", False)


async def test_payment_declined_not_retryable():
    o = await make_order(cust="C-DECLINED")
    await fail("charge_payment", {"order_id": o["order_id"], "amount": o["total_amount"]}, "PAYMENT_DECLINED", False)


async def test_payment_gateway_timeout_customer_and_env(monkeypatch):
    o = await make_order(cust="C-TIMEOUT")
    await fail("charge_payment", {"order_id": o["order_id"], "amount": o["total_amount"]},
               "PAYMENT_GATEWAY_TIMEOUT", True)
    o2 = await make_order()
    monkeypatch.setenv("RETAIL_FAIL_MODE", "payment_down")
    await fail("charge_payment", {"order_id": o2["order_id"], "amount": o2["total_amount"]},
               "PAYMENT_GATEWAY_TIMEOUT", True)
    monkeypatch.delenv("RETAIL_FAIL_MODE")
    p = data(await call("charge_payment", {"order_id": o2["order_id"], "amount": o2["total_amount"]}))
    assert p["status"] == "PAID"  # retry after outage succeeds


async def test_create_shipment_errors():
    await fail("create_shipment", {"order_id": "ORD-NOPE", "address": "12 Long Street, Springfield"},
               "ORDER_NOT_FOUND", False)
    o = await make_order()
    await fail("create_shipment", {"order_id": o["order_id"], "address": "12 Long Street, Springfield"},
               "ORDER_NOT_PAID", False)
    await call("charge_payment", {"order_id": o["order_id"], "amount": o["total_amount"]})
    await fail("create_shipment", {"order_id": o["order_id"], "address": ""}, "INVALID_ADDRESS", False)
    await fail("create_shipment", {"order_id": o["order_id"], "address": "abc"}, "INVALID_ADDRESS", False)


async def test_full_flow_and_double_actions():
    inv = data(await call("check_inventory", {"sku": "SKU-1004", "qty": 10}))
    assert inv["in_stock"]
    o = await make_order(sku="SKU-1004", qty=10, cust="C-1002")
    assert o["total_amount"] == 99.9
    p = data(await call("charge_payment", {"order_id": o["order_id"], "amount": o["total_amount"]}))
    assert p["status"] == "PAID"
    s = data(await call("create_shipment", {"order_id": o["order_id"], "address": "12 Long Street, Springfield"}))
    assert s["order_id"] == o["order_id"] and s["shipment_id"] and s["carrier"]
    assert s["tracking_number"] and isinstance(s["eta_days"], int) and s["status"]
    await fail("create_shipment", {"order_id": o["order_id"], "address": "12 Long Street, Springfield"},
               "ALREADY_SHIPPED", False)
    await fail("charge_payment", {"order_id": o["order_id"], "amount": o["total_amount"]}, "ALREADY_PAID", False)
    assert data(await call("check_inventory", {"sku": "SKU-1004", "qty": 1}))["available_qty"] == 490


async def test_unexpected_exception_becomes_internal_error(monkeypatch):
    def boom(_sku):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(retail_server, "_get_sku", boom)
    await fail("check_inventory", {"sku": "SKU-1001", "qty": 1}, "INTERNAL_ERROR", False)
