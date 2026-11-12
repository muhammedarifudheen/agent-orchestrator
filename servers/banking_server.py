"""Banking MCP server: KYC check, balance lookup, fund transfer, customer onboarding (mock data).

Run standalone: python servers/banking_server.py   (stdio transport)

Set BANKING_FAIL_MODE to simulate outages (for testing the exception routing layer):
  core_down -> every tool fails with CORE_BANKING_UNAVAILABLE (retryable)

All ids and balances are obviously fake. State lives in module-level dicts, so transfers
change balances for the lifetime of the server process.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from mcp.server.mcpserver import MCPServer

from _base import ToolFailure, check_outage, make_tool

mcp = MCPServer("banking")
tool = make_tool(mcp)

_CENT = Decimal("0.01")
DAILY_LIMIT = Decimal("100000.00")  # per sending account, per UTC day, in the account's currency

# TODO(real-api): replace with the core-banking / KYC provider customer master.
_CUSTOMERS: dict[str, dict] = {}
# TODO(real-api): replace with the core-banking ledger (account master + balances).
_ACCOUNTS: dict[str, dict] = {}
# TODO(real-api): replace with the core-banking transaction log / limits engine.
_TRANSFERS: dict[str, dict] = {}
_DAILY_SPENT: dict[tuple[str, str], Decimal] = {}  # (from_acct, YYYY-MM-DD) -> total sent
_COUNTERS: dict[str, int] = {}  # sequence numbers for ids minted by create_customer / open_account
_WELCOMES: list[dict] = []


def _seed() -> None:
    """(Re)load deterministic seed data into the module-level state."""
    _CUSTOMERS.clear()
    _ACCOUNTS.clear()
    _TRANSFERS.clear()
    _DAILY_SPENT.clear()
    _COUNTERS.clear()
    _WELCOMES.clear()
    _CUSTOMERS.update({
        "CUST-TEST-0001": {"kyc_status": "VERIFIED", "verified_at": "2025-01-15T10:00:00+00:00",
                           "risk_level": "LOW", "account_ids": ["ACCT-TEST-0001", "ACCT-TEST-0002"]},
        "CUST-TEST-0002": {"kyc_status": "PENDING", "verified_at": None,
                           "risk_level": "MEDIUM", "account_ids": ["ACCT-TEST-0003", "ACCT-TEST-0004"]},
        "CUST-TEST-0003": {"kyc_status": "REJECTED", "verified_at": None,
                           "risk_level": "HIGH", "account_ids": ["ACCT-TEST-0005"]},
        "CUST-TEST-0004": {"kyc_status": "EXPIRED", "verified_at": "2022-03-01T09:30:00+00:00",
                           "risk_level": "MEDIUM", "account_ids": ["ACCT-TEST-0006"]},
    })

    def acct(customer: str, currency: str, balance: str, status: str = "ACTIVE") -> dict:
        b = Decimal(balance)
        return {"customer_id": customer, "currency": currency, "available": b, "ledger": b, "status": status}

    _ACCOUNTS.update({
        "ACCT-TEST-0001": acct("CUST-TEST-0001", "INR", "500000.00"),
        "ACCT-TEST-0002": acct("CUST-TEST-0001", "INR", "25000.00", "FROZEN"),
        "ACCT-TEST-0003": acct("CUST-TEST-0002", "INR", "10000.00"),
        "ACCT-TEST-0004": acct("CUST-TEST-0002", "USD", "1500.00"),
        "ACCT-TEST-0005": acct("CUST-TEST-0003", "INR", "5000.00"),
        "ACCT-TEST-0006": acct("CUST-TEST-0004", "INR", "20000.00"),
    })


_seed()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _money(value: Decimal) -> float:
    return float(value.quantize(_CENT))


def _check_outage(tool: str) -> None:
    check_outage("BANKING_FAIL_MODE", tool, {"core_down": None}, code="CORE_BANKING_UNAVAILABLE",
                 message=f"Core banking system is down during {tool}")


def _get_account(account_id: str) -> dict:
    # TODO(real-api): fetch the account from the core-banking system.
    account = _ACCOUNTS.get(account_id)
    if account is None:
        raise ToolFailure("ACCOUNT_NOT_FOUND", f"No account '{account_id}'.", retryable=False, recovery="agent")
    return account


@tool(side_effect="read")
def verify_kyc(customer_id: str) -> dict:
    """Look up a customer's KYC (know-your-customer) status and list their accounts. Read-only.

    Call this BEFORE moving money: transfer_funds requires the sender's owner to be VERIFIED.
    Use the returned account_ids with get_account_balance and transfer_funds. A customer who is
    not VERIFIED is returned as normal data (not an error); decide from kyc_status.

    Args:
        customer_id: Customer identifier (e.g. "CUST-TEST-0001").

    Returns:
        Dict with customer_id, kyc_status ("VERIFIED", "PENDING", "REJECTED" or "EXPIRED"),
        verified_at (ISO-8601 timestamp, or null if never verified), risk_level ("LOW", "MEDIUM",
        "HIGH") and account_ids (list of the customer's account ids).

    Errors: CUSTOMER_NOT_FOUND (retryable=false), CORE_BANKING_UNAVAILABLE (retryable=true).
    Idempotent: yes, no side effects.
    """
    _check_outage("verify_kyc")
    # TODO(real-api): call the KYC provider / customer master.
    customer = _CUSTOMERS.get(customer_id.strip())
    if customer is None:
        raise ToolFailure("CUSTOMER_NOT_FOUND", f"No customer '{customer_id}'.", retryable=False, recovery="agent")
    return {"customer_id": customer_id.strip(), "kyc_status": customer["kyc_status"],
            "verified_at": customer["verified_at"], "risk_level": customer["risk_level"],
            "account_ids": list(customer["account_ids"])}


@tool(side_effect="read")
def get_account_balance(account_id: str) -> dict:
    """Get the current balances and status of one account. Read-only.

    Call this to check funds before transfer_funds, and AFTER any transfer whose outcome is
    unknown (timeout, outage) to find out whether the money actually moved.

    Args:
        account_id: Account identifier (e.g. "ACCT-TEST-0001"), from verify_kyc's account_ids.

    Returns:
        Dict with account_id, customer_id, currency (ISO code), available_balance and
        ledger_balance (floats, 2 decimals), status ("ACTIVE", "FROZEN" or "CLOSED"), and as_of
        (ISO-8601 UTC timestamp).

    Errors: ACCOUNT_NOT_FOUND (retryable=false), CORE_BANKING_UNAVAILABLE (retryable=true).
    Idempotent: yes, no side effects.
    """
    _check_outage("get_account_balance")
    account = _get_account(account_id.strip())
    return {"account_id": account_id.strip(), "customer_id": account["customer_id"],
            "currency": account["currency"], "available_balance": _money(account["available"]),
            "ledger_balance": _money(account["ledger"]), "status": account["status"],
            "as_of": _now()}


@tool(side_effect="money", idempotent=False)
def transfer_funds(from_acct: str, to_acct: str, amount: float) -> dict:
    """Move money between two accounts of the same currency. Executes IMMEDIATELY.

    NOT idempotent and has side effects: every successful call moves money again, so calling it
    twice transfers twice. Do NOT retry after a timeout or any unknown outcome without first
    calling get_account_balance on both accounts to see whether the transfer already happened.
    Call only after verify_kyc (the sender's owner must be VERIFIED) and get_account_balance
    (check funds/status); call get_account_balance again afterwards to confirm.

    Args:
        from_acct: Sending account id. Must be ACTIVE, and its owner's KYC must be VERIFIED.
        to_acct: Receiving account id. Must be ACTIVE, same currency, different from from_acct.
        amount: Amount in the accounts' currency; greater than 0 with at most 2 decimal places.
            A per-sending-account daily limit of 100000.00 applies across all transfers that day.

    Returns:
        Dict with transfer_id, status ("COMPLETED"), from_acct, to_acct, amount (float),
        currency, from_balance_after (float, 2 decimals) and timestamp (ISO-8601 UTC).

    Errors: all retryable=false unless noted: INVALID_AMOUNT, SAME_ACCOUNT, ACCOUNT_NOT_FOUND,
    ACCOUNT_FROZEN (either account not ACTIVE), CURRENCY_MISMATCH, KYC_NOT_VERIFIED,
    INSUFFICIENT_FUNDS, DAILY_LIMIT_EXCEEDED, CORE_BANKING_UNAVAILABLE (retryable=true, but
    the outcome may be unknown: check balances before retrying).
    """
    _check_outage("transfer_funds")
    from_acct, to_acct = from_acct.strip(), to_acct.strip()
    try:
        value = Decimal(str(amount))
    except ArithmeticError:
        raise ToolFailure("INVALID_AMOUNT", f"'{amount}' is not a valid amount.", retryable=False, recovery="agent") from None
    if not value.is_finite() or value <= 0:
        raise ToolFailure("INVALID_AMOUNT", "Amount must be greater than 0.", retryable=False, recovery="agent")
    if value != value.quantize(_CENT):
        raise ToolFailure("INVALID_AMOUNT", "Amount may have at most 2 decimal places.", retryable=False, recovery="agent")
    value = value.quantize(_CENT)
    if from_acct == to_acct:
        raise ToolFailure("SAME_ACCOUNT", "from_acct and to_acct must differ.", retryable=False, recovery="agent")

    src, dst = _get_account(from_acct), _get_account(to_acct)
    for acct_id, acct in ((from_acct, src), (to_acct, dst)):
        if acct["status"] != "ACTIVE":
            raise ToolFailure("ACCOUNT_FROZEN", f"Account '{acct_id}' is {acct['status']}.", retryable=False, recovery="human")
    if src["currency"] != dst["currency"]:
        raise ToolFailure("CURRENCY_MISMATCH",
                          f"{from_acct} is {src['currency']} but {to_acct} is {dst['currency']}.",
                          retryable=False, recovery="human")
    # TODO(real-api): re-check KYC against the live KYC provider.
    kyc = _CUSTOMERS[src["customer_id"]]["kyc_status"]
    if kyc != "VERIFIED":
        raise ToolFailure("KYC_NOT_VERIFIED", f"Sender's KYC status is {kyc}; must be VERIFIED.",
                          retryable=False, recovery="human")
    if src["available"] < value:
        raise ToolFailure("INSUFFICIENT_FUNDS",
                          f"Available balance {_money(src['available'])} is less than {value}.",
                          retryable=False, recovery="human")
    now = _now()
    key = (from_acct, now[:10])
    spent = _DAILY_SPENT.get(key, Decimal("0"))
    if spent + value > DAILY_LIMIT:
        raise ToolFailure("DAILY_LIMIT_EXCEEDED",
                          f"Daily limit {DAILY_LIMIT} exceeded (already sent {spent} today).",
                          retryable=False, recovery="human")

    # TODO(real-api): post the debit/credit pair to the core-banking ledger atomically.
    src["available"] -= value
    src["ledger"] -= value
    dst["available"] += value
    dst["ledger"] += value
    _DAILY_SPENT[key] = spent + value
    transfer_id = "TRF-" + uuid.uuid4().hex[:10].upper()
    _TRANSFERS[transfer_id] = {"from": from_acct, "to": to_acct, "amount": value}
    return {"transfer_id": transfer_id, "status": "COMPLETED", "from_acct": from_acct,
            "to_acct": to_acct, "amount": _money(value), "currency": src["currency"],
            "from_balance_after": _money(src["available"]), "timestamp": now}


def _next_id(kind: str, prefix: str) -> str:
    _COUNTERS[kind] = _COUNTERS.get(kind, 0) + 1
    return f"{prefix}{_COUNTERS[kind]:04d}"


@tool(side_effect="write", idempotent=False)
def create_customer(name: str, document_id: str) -> dict:
    """Create a new customer record from a name and a document number. Side effect: a customer is created.

    Call first when onboarding someone who is not yet a customer. Afterwards read the KYC outcome with
    the returned customer_id before open_account. Not idempotent: each call creates another customer.

    Args:
        name: The customer's full name; must not be empty.
        document_id: Identity document number. (Mock KYC provider: ids starting "DOC-VALID" verify,
            "DOC-REJECT" are rejected, anything else stays PENDING.)

    Returns:
        Dict with customer_id, name, kyc_status ("VERIFIED", "PENDING" or "REJECTED").

    Errors: INVALID_NAME (retryable=false), INVALID_DOCUMENT (retryable=false),
    CORE_BANKING_UNAVAILABLE (retryable=true; the customer may not have been created).
    """
    _check_outage("create_customer")
    if not name.strip():
        raise ToolFailure("INVALID_NAME", "name must not be empty.", retryable=False, recovery="agent")
    if not document_id.strip():
        raise ToolFailure("INVALID_DOCUMENT", "document_id must not be empty.", retryable=False, recovery="agent")
    # TODO(real-api): create the customer in the customer master and submit the document to the KYC provider.
    doc = document_id.strip().upper()
    status = "VERIFIED" if doc.startswith("DOC-VALID") else "REJECTED" if doc.startswith("DOC-REJECT") else "PENDING"
    customer_id = _next_id("customer", "CUST-NEW-")
    _CUSTOMERS[customer_id] = {"name": name.strip(), "kyc_status": status,
                               "verified_at": _now() if status == "VERIFIED" else None,
                               "risk_level": "HIGH" if status == "REJECTED" else "LOW", "account_ids": []}
    return {"customer_id": customer_id, "name": name.strip(), "kyc_status": status}


@tool(side_effect="write", idempotent=False)
def open_account(customer_id: str, currency: str) -> dict:
    """Open a new, empty account for a KYC-VERIFIED customer. Side effect: an account is created.

    Call after create_customer and verify_kyc showed kyc_status VERIFIED. Not idempotent: each
    call opens another account.

    Args:
        customer_id: Customer id from create_customer.
        currency: ISO currency code: "INR", "USD" or "EUR".

    Returns:
        Dict with account_id, customer_id, currency, status ("ACTIVE"), available_balance (0.0).

    Errors: CUSTOMER_NOT_FOUND (retryable=false), KYC_NOT_VERIFIED (retryable=false; a person must
    resolve it), INVALID_CURRENCY (retryable=false), CORE_BANKING_UNAVAILABLE (retryable=true; the
    account may not have been opened: check verify_kyc's account_ids before retrying).
    """
    _check_outage("open_account")
    customer = _CUSTOMERS.get(customer_id.strip())
    if customer is None:
        raise ToolFailure("CUSTOMER_NOT_FOUND", f"No customer '{customer_id}'.", retryable=False, recovery="agent")
    if currency.strip().upper() not in ("INR", "USD", "EUR"):
        raise ToolFailure("INVALID_CURRENCY", f"Unsupported currency '{currency}'.", retryable=False, recovery="agent")
    if customer["kyc_status"] != "VERIFIED":
        raise ToolFailure("KYC_NOT_VERIFIED", f"Customer KYC status is {customer['kyc_status']}; must be VERIFIED.",
                          retryable=False, recovery="human")
    # TODO(real-api): open the account in the core-banking ledger.
    account_id = _next_id("account", "ACCT-NEW-")
    _ACCOUNTS[account_id] = {"customer_id": customer_id.strip(), "currency": currency.strip().upper(),
                             "available": Decimal("0.00"), "ledger": Decimal("0.00"), "status": "ACTIVE"}
    customer["account_ids"].append(account_id)
    return {"account_id": account_id, "customer_id": customer_id.strip(), "currency": currency.strip().upper(),
            "status": "ACTIVE", "available_balance": 0.0}


@tool(side_effect="write", idempotent=False)
def send_welcome(customer_id: str, account_id: str) -> dict:
    """Send the new customer a welcome message with their account details. Side effect: a message is sent.

    Call last in onboarding, after open_account succeeded. Not idempotent: every call sends another message.

    Args:
        customer_id: Customer id from create_customer.
        account_id: Account id from open_account; it must belong to that customer.

    Returns:
        Dict with message_id, customer_id, account_id, status ("SENT").

    Errors: CUSTOMER_NOT_FOUND (retryable=false), ACCOUNT_NOT_FOUND (retryable=false),
    ACCOUNT_MISMATCH (retryable=false; the account is not this customer's).
    """
    customer = _CUSTOMERS.get(customer_id.strip())
    if customer is None:
        raise ToolFailure("CUSTOMER_NOT_FOUND", f"No customer '{customer_id}'.", retryable=False, recovery="agent")
    account = _get_account(account_id.strip())
    if account["customer_id"] != customer_id.strip():
        raise ToolFailure("ACCOUNT_MISMATCH", f"Account '{account_id}' does not belong to '{customer_id}'.",
                          retryable=False, recovery="agent")
    # TODO(real-api): send through the messaging provider.
    message = {"message_id": _next_id("message", "MSG-"), "customer_id": customer_id.strip(),
               "account_id": account_id.strip(), "status": "SENT"}
    _WELCOMES.append(message)
    return dict(message)


if __name__ == "__main__":
    mcp.run()
