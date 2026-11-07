"""Shared fixtures for tests that run the orchestrator against real in-memory MCP servers."""
from __future__ import annotations

import pytest

import retail_server
from errors import ToolFailure


@pytest.fixture(autouse=True)
def fresh_retail_state():
    retail_server._reset_state()
    yield
    retail_server._reset_state()


@pytest.fixture
def card_declined():
    """Monkeypatch charge_payment to raise ToolFailure(CARD_DECLINED, retryable=False, recovery="human")."""
    original = retail_server.charge_payment

    def charge_payment(order_id: str, amount: float) -> dict:
        """Charge the customer for an order. Call after create_order. (patched to fail)"""
        raise ToolFailure(code="CARD_DECLINED", message="Issuer declined the card.",
                          retryable=False, recovery="human")

    retail_server.mcp.remove_tool("charge_payment")
    retail_server.mcp.add_tool(charge_payment, name="charge_payment")
    yield
    retail_server.mcp.remove_tool("charge_payment")
    retail_server.mcp.add_tool(original, name="charge_payment")
