"""Retail MCP server: inventory check, order, payment, shipment (mock data).

Run standalone: python servers/retail_server.py   (stdio transport)

Typical flow: check_inventory -> create_order -> charge_payment -> create_shipment.
After-sales flow: verify_order -> check_refund_eligibility -> issue_refund -> notify_customer.

Set RETAIL_FAIL_MODE=payment_down to make charge_payment fail with PAYMENT_GATEWAY_TIMEOUT.
Customer C-TIMEOUT also triggers that timeout; customer C-DECLINED triggers PAYMENT_DECLINED.
"""
from __future__ import annotations

import uuid

from mcp.server.mcpserver import MCPServer

from _base import ToolFailure, make_tool, outage_active

mcp = MCPServer("retail")
tool = make_tool(mcp)

_TOLERANCE = 0.005

# TODO(real-api): replace with the warehouse / inventory management system (ERP, WMS).
_SEED_INVENTORY: dict[str, dict] = {
    "SKU-1001": {"name": "Wireless Mouse", "stock": 120, "unit_price": 24.99, "currency": "USD", "warehouse": "WH-EAST"},
    "SKU-1002": {"name": "Mechanical Keyboard", "stock": 3, "unit_price": 89.50, "currency": "USD", "warehouse": "WH-EAST"},
    "SKU-1003": {"name": "27in Monitor", "stock": 0, "unit_price": 219.00, "currency": "USD", "warehouse": "WH-WEST"},
    "SKU-1004": {"name": "USB-C Cable", "stock": 500, "unit_price": 9.99, "currency": "USD", "warehouse": "WH-WEST"},
}
# TODO(real-api): replace with the CRM / customer database.
_CUSTOMERS: dict[str, dict] = {
    "C-1001": {"name": "Asha Verma"},
    "C-1002": {"name": "Liam Chen"},
    "C-DECLINED": {"name": "Card Declined Demo"},   # payments always declined
    "C-TIMEOUT": {"name": "Gateway Timeout Demo"},  # payments always time out
}

_INVENTORY: dict[str, dict] = {}
_ORDERS: dict[str, dict] = {}
_PAYMENTS: dict[str, dict] = {}
_SHIPMENTS: dict[str, dict] = {}
_REFUNDS: dict[str, dict] = {}
_NOTIFICATIONS: list[dict] = []


def _reset_state() -> None:
    """Restore deterministic seed data and clear orders/payments/shipments (used by tests)."""
    _INVENTORY.clear()
    _INVENTORY.update({k: dict(v) for k, v in _SEED_INVENTORY.items()})
    _ORDERS.clear()
    _PAYMENTS.clear()
    _SHIPMENTS.clear()
    _REFUNDS.clear()
    _NOTIFICATIONS.clear()


_reset_state()


def _money(value: float) -> float:
    return round(float(value), 2)


def _get_sku(sku: str) -> dict:
    item = _INVENTORY.get(sku.strip().upper())
    if item is None:
        raise ToolFailure("SKU_NOT_FOUND", f"Unknown sku '{sku}'.", retryable=False, recovery="agent")
    return item


def _check_qty(qty: int) -> None:
    if qty <= 0:
        raise ToolFailure("INVALID_QUANTITY", f"qty must be a positive integer, got {qty}.", retryable=False, recovery="agent")


def _get_order(order_id: str) -> dict:
    order = _ORDERS.get(order_id)
    if order is None:
        raise ToolFailure("ORDER_NOT_FOUND", f"Unknown order_id '{order_id}'.", retryable=False, recovery="agent")
    return order


@tool(side_effect="read")
def check_inventory(sku: str, qty: int) -> dict:
    """Check whether a SKU has enough stock for a quantity, and get its price. Read-only.

    Call this first, before create_order, to confirm availability and price. Having too little
    stock is NOT an error: inspect in_stock / available_qty in the result.

    Args:
        sku: Product SKU, e.g. "SKU-1001" (case-insensitive).
        qty: Desired quantity, a positive integer.

    Returns:
        Dict with sku, requested_qty, available_qty, in_stock (True if available_qty >= requested_qty),
        unit_price (float, 2 dp), currency (ISO code), warehouse.

    Errors: SKU_NOT_FOUND (retryable=false), INVALID_QUANTITY (retryable=false).
    Idempotent; no side effects.
    """
    _check_qty(qty)
    # TODO(real-api): query live stock levels and pricing from the inventory system.
    item = _get_sku(sku)
    return {
        "sku": sku.strip().upper(), "requested_qty": qty, "available_qty": item["stock"],
        "in_stock": item["stock"] >= qty, "unit_price": _money(item["unit_price"]),
        "currency": item["currency"], "warehouse": item["warehouse"],
    }


@tool(side_effect="write", idempotent=False)
def create_order(sku: str, qty: int, customer_id: str) -> dict:
    """Create an order and reserve (decrement) stock. Status starts as PENDING_PAYMENT.

    Call after check_inventory shows in_stock, and before charge_payment. Pass the returned
    order_id and total_amount to charge_payment.

    Not idempotent: each call creates a new order and reserves more stock, so do not repeat
    a call that succeeded.

    Args:
        sku: Product SKU, e.g. "SKU-1001".
        qty: Quantity to order, a positive integer.
        customer_id: Customer identifier, e.g. "C-1001".

    Returns:
        Dict with order_id, status ("PENDING_PAYMENT"), sku, qty, customer_id, unit_price,
        total_amount (float, 2 dp), currency.

    Errors: SKU_NOT_FOUND, INVALID_QUANTITY, INSUFFICIENT_STOCK, CUSTOMER_NOT_FOUND
    (all retryable=false; change the arguments).
    """
    _check_qty(qty)
    item = _get_sku(sku)
    # TODO(real-api): look up the customer in the CRM.
    if customer_id not in _CUSTOMERS:
        raise ToolFailure("CUSTOMER_NOT_FOUND", f"Unknown customer_id '{customer_id}'.", retryable=False, recovery="agent")
    if item["stock"] < qty:
        raise ToolFailure("INSUFFICIENT_STOCK",
                          f"Only {item['stock']} of {sku} available, requested {qty}.", retryable=False, recovery="agent")

    # TODO(real-api): create the order and reserve stock in the order management system.
    item["stock"] -= qty
    order_id = "ORD-" + uuid.uuid4().hex[:8].upper()
    _ORDERS[order_id] = {
        "order_id": order_id, "status": "PENDING_PAYMENT", "sku": sku.strip().upper(), "qty": qty,
        "customer_id": customer_id, "unit_price": _money(item["unit_price"]),
        "total_amount": _money(item["unit_price"] * qty), "currency": item["currency"],
    }
    return dict(_ORDERS[order_id])


@tool(side_effect="money", idempotent=False)
def charge_payment(order_id: str, amount: float) -> dict:
    """Charge the customer for an order. Moves real money.

    Call after create_order, with total_amount from that result exactly. Call create_shipment
    afterwards.

    Not idempotent in effect, but a second charge for a paid order is rejected with ALREADY_PAID,
    so customers are never double-charged. After PAYMENT_GATEWAY_TIMEOUT the charge did not
    complete; it is safe to retry the same call.

    Args:
        order_id: Order id from create_order.
        amount: Amount to charge, in the order currency. Must equal the order total_amount.

    Returns:
        Dict with payment_id, order_id, amount (float, 2 dp), status ("PAID").

    Errors: ORDER_NOT_FOUND (retryable=false), AMOUNT_MISMATCH (retryable=false),
    ALREADY_PAID (retryable=false), PAYMENT_DECLINED (retryable=false; use another payment
    method / customer), PAYMENT_GATEWAY_TIMEOUT (retryable=true).
    """
    order = _get_order(order_id)
    if order["status"] != "PENDING_PAYMENT":
        raise ToolFailure("ALREADY_PAID", f"Order {order_id} is already {order['status']}.", retryable=False, recovery="human")
    if abs(_money(amount) - order["total_amount"]) > _TOLERANCE:
        raise ToolFailure("AMOUNT_MISMATCH",
                          f"Amount {amount} does not equal order total {order['total_amount']}.", retryable=False, recovery="agent")

    # TODO(real-api): call the payment gateway (Stripe, Adyen, ...) to authorize and capture.
    gateway_down = outage_active("RETAIL_FAIL_MODE", "charge_payment", {"payment_down": {"charge_payment"}})
    if gateway_down or order["customer_id"] == "C-TIMEOUT":
        raise ToolFailure("PAYMENT_GATEWAY_TIMEOUT", "Payment gateway timed out; the charge was not made.",
                          retryable=True, recovery="agent")
    if order["customer_id"] == "C-DECLINED":
        raise ToolFailure("PAYMENT_DECLINED", "The payment method was declined.", retryable=False, recovery="human")

    payment_id = "PAY-" + uuid.uuid4().hex[:8].upper()
    _PAYMENTS[payment_id] = {"payment_id": payment_id, "order_id": order_id,
                             "amount": order["total_amount"], "status": "PAID"}
    order["status"] = "PAID"
    return dict(_PAYMENTS[payment_id])


@tool(side_effect="write", idempotent=False)
def create_shipment(order_id: str, address: str) -> dict:
    """Create a shipment for a PAID order. Side effect: books a carrier pickup.

    Call last, after charge_payment succeeded. Orders that are not paid are rejected.
    Not idempotent: a second shipment for the same order is rejected with ALREADY_SHIPPED.

    Args:
        order_id: Order id from create_order (must be PAID).
        address: Full delivery address as one string (street, city, postal code); at least 10
            characters.

    Returns:
        Dict with shipment_id, order_id, carrier, tracking_number, eta_days (int), status.

    Errors: ORDER_NOT_FOUND, ORDER_NOT_PAID, INVALID_ADDRESS, ALREADY_SHIPPED (all retryable=false).
    """
    order = _get_order(order_id)
    if order["status"] == "SHIPPED":
        raise ToolFailure("ALREADY_SHIPPED", f"Order {order_id} is already shipped.", retryable=False, recovery="human")
    if order["status"] != "PAID":
        raise ToolFailure("ORDER_NOT_PAID", f"Order {order_id} is {order['status']}; pay it first.", retryable=False, recovery="agent")
    if len(address.strip()) < 10:
        raise ToolFailure("INVALID_ADDRESS", "Provide a full delivery address (street, city, postal code).",
                          retryable=False, recovery="agent")

    # TODO(real-api): book the shipment with the carrier API (UPS, FedEx, DHL, ...).
    shipment_id = "SHP-" + uuid.uuid4().hex[:8].upper()
    _SHIPMENTS[shipment_id] = {
        "shipment_id": shipment_id, "order_id": order_id, "carrier": "MockExpress",
        "tracking_number": "TRK" + uuid.uuid4().hex[:10].upper(), "eta_days": 3, "status": "LABEL_CREATED",
    }
    order["status"] = "SHIPPED"
    return dict(_SHIPMENTS[shipment_id])


@tool(side_effect="read")
def verify_order(order_id: str) -> dict:
    """Look up an existing order and its current status. Read-only.

    Call this first in any after-sales flow (refund, return, support) to confirm the order exists
    and see its status, customer and total. The status is returned as data, never as an error.

    Args:
        order_id: Order id from create_order, e.g. "ORD-1A2B3C4D".

    Returns:
        Dict with order_id, status ("PENDING_PAYMENT", "PAID", "SHIPPED" or "REFUNDED"), sku, qty,
        customer_id, unit_price, total_amount (float, 2 dp), currency.

    Errors: ORDER_NOT_FOUND (retryable=false). Idempotent; no side effects.
    """
    # TODO(real-api): fetch the order from the order management system.
    return dict(_get_order(order_id))


@tool(side_effect="read")
def check_refund_eligibility(order_id: str) -> dict:
    """Decide whether an order can be refunded, and for how much. Read-only.

    Call after verify_order and BEFORE issue_refund. An order that cannot be refunded is returned
    as normal data (eligible=false with a reason), not as an error.

    Args:
        order_id: Order id from create_order.

    Returns:
        Dict with order_id, eligible (bool), status (the order status), reason (why not eligible, or
        "paid order"), refundable_amount (float, 2 dp; 0.0 when not eligible), currency.

    Errors: ORDER_NOT_FOUND (retryable=false). Idempotent; no side effects.
    """
    order = _get_order(order_id)
    # TODO(real-api): apply the refund policy (return window, item condition) from the policy service.
    eligible = order["status"] in ("PAID", "SHIPPED")
    reason = {"PENDING_PAYMENT": "order was never paid", "REFUNDED": "order is already refunded"}.get(
        order["status"], "paid order")
    return {"order_id": order_id, "eligible": eligible, "status": order["status"], "reason": reason,
            "refundable_amount": order["total_amount"] if eligible else 0.0, "currency": order["currency"]}


@tool(side_effect="money", idempotent=False)
def issue_refund(order_id: str, amount: float) -> dict:
    """Refund a paid order back to the customer. Moves real money.

    Call only after check_refund_eligibility returned eligible=true, with refundable_amount exactly.
    Not idempotent in effect, but a second refund of the same order is rejected with
    ALREADY_REFUNDED, so customers are never refunded twice. After PAYMENT_GATEWAY_TIMEOUT the
    refund was not issued; it is safe to retry the same call.

    Args:
        order_id: Order id from create_order (must be PAID or SHIPPED).
        amount: Amount to refund, in the order currency. Must equal the order total_amount.

    Returns:
        Dict with refund_id, order_id, amount (float, 2 dp), status ("REFUNDED").

    Errors: ORDER_NOT_FOUND (retryable=false), NOT_REFUNDABLE (retryable=false; the order was not
    paid), ALREADY_REFUNDED (retryable=false), AMOUNT_MISMATCH (retryable=false),
    PAYMENT_GATEWAY_TIMEOUT (retryable=true).
    """
    order = _get_order(order_id)
    if order["status"] == "REFUNDED":
        raise ToolFailure("ALREADY_REFUNDED", f"Order {order_id} is already refunded.", retryable=False, recovery="human")
    if order["status"] not in ("PAID", "SHIPPED"):
        raise ToolFailure("NOT_REFUNDABLE", f"Order {order_id} is {order['status']}; nothing was paid.",
                          retryable=False, recovery="human")
    if abs(_money(amount) - order["total_amount"]) > _TOLERANCE:
        raise ToolFailure("AMOUNT_MISMATCH",
                          f"Amount {amount} does not equal order total {order['total_amount']}.", retryable=False, recovery="agent")
    # TODO(real-api): call the payment gateway to refund the captured payment.
    if outage_active("RETAIL_FAIL_MODE", "issue_refund", {"payment_down": {"issue_refund"}}):
        raise ToolFailure("PAYMENT_GATEWAY_TIMEOUT", "Payment gateway timed out; the refund was not issued.",
                          retryable=True, recovery="agent")
    refund_id = "RFD-" + uuid.uuid4().hex[:8].upper()
    _REFUNDS[refund_id] = {"refund_id": refund_id, "order_id": order_id, "amount": order["total_amount"],
                           "status": "REFUNDED"}
    order["status"] = "REFUNDED"
    return dict(_REFUNDS[refund_id])


@tool(side_effect="write", idempotent=False)
def notify_customer(customer_id: str, message: str) -> dict:
    """Send a customer a notification (email) about their order. Side effect: a message is sent.

    Call last in an after-sales flow, once the action it reports has succeeded. Not idempotent:
    every call sends another message, so do not repeat a call that succeeded.

    Args:
        customer_id: Customer identifier, e.g. "C-1001".
        message: The text to send; must not be empty.

    Returns:
        Dict with notification_id, customer_id, channel ("email"), status ("SENT").

    Errors: CUSTOMER_NOT_FOUND (retryable=false), EMPTY_MESSAGE (retryable=false).
    """
    if customer_id not in _CUSTOMERS:
        raise ToolFailure("CUSTOMER_NOT_FOUND", f"Unknown customer_id '{customer_id}'.", retryable=False, recovery="agent")
    if not message.strip():
        raise ToolFailure("EMPTY_MESSAGE", "message must not be empty.", retryable=False, recovery="agent")
    # TODO(real-api): send through the messaging provider (SES, SendGrid, ...).
    note = {"notification_id": "NTF-" + uuid.uuid4().hex[:8].upper(), "customer_id": customer_id,
            "channel": "email", "status": "SENT"}
    _NOTIFICATIONS.append({**note, "message": message})
    return note


if __name__ == "__main__":
    mcp.run()
