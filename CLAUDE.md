# Agent Orchestration System

Industry-agnostic orchestrator: a user states a goal in natural language, the Agent decides which
MCP tools to call, in what order, and how to fall back on errors. Supports travel, retail, banking, ...
by adding MCP servers only — never by changing the Agent layer.

## Architecture (three layers)

```
 user goal
    │
┌───▼──────────────────────────┐
│ Agent layer   agent/         │  Claude decides the plan; loop feeds results back.
│  orchestrator.py             │  Knows NOTHING about any industry.
└───┬──────────────────┬───────┘
    │ tool calls       │ failures
┌───▼───────────┐  ┌───▼──────────────────────────┐
│ Tool layer    │  │ Exception routing layer       │
│  servers/*.py │  │  (agent/orchestrator.py:      │
│  tool_registry│  │   ExecutionState + Handoff)   │
└───────────────┘  └──────────────────────────────┘
```

- **Tool layer** — `servers/<industry>_server.py` (one MCP server per industry system, `MCPServer`
  from `mcp.server.mcpserver`, stdio transport) and `agent/tool_registry.py` (discovers every
  `servers/*_server.py`, spawns it, connects, lists tools, exposes them to Claude as
  `<server>__<tool>`, and normalizes every call into a `ToolOutcome`).
- **Agent layer** — `agent/orchestrator.py`: Claude tool-calling loop (call → result → call ...).
  Domain-free system prompt, domain-free code.
- **Exception routing layer** — failures are never swallowed. Each failed call is fed back to
  Claude (so it can retry with other args or pick another tool) AND recorded in `ExecutionState`.
  When recovery is exhausted (consecutive-failure limit, step limit, or LLM/API failure) the
  orchestrator emits a `Handoff`: goal, every attempted operation, error codes, current state
  (what already succeeded — e.g. a booking that must not be repeated), and a reason.
  It is printed and saved to `handoffs/` for a human.

## Rules

1. **No industry logic in the Agent layer.** No "if flight", "if refund", "if account balance" in
   `agent/`. Domain knowledge lives in tool names, descriptions, and server code. If Claude needs a
   domain hint, put it in the tool docstring.
2. **All external-system capability goes through an MCP server.** No `requests`/SDK/DB calls in
   `agent/` or `demo.py`. New system = new `servers/<name>_server.py`; it is auto-discovered.
3. **Every tool has full type hints and a docstring.** The docstring is what Claude reads: say what
   the tool does, each argument's format (e.g. `date` = `YYYY-MM-DD`), what it returns, and when
   it errors. Use `Args:` / `Returns:` sections.
4. **Failures are raised, never returned.** Tools must never return a normal dict containing an
   error. Failures are raised as `ToolFailure(code, message, retryable)` from `servers/errors.py`,
   and every tool is wrapped in `@guarded` (directly under `@mcp.tool()`), which catches every
   other exception internally and re-raises it as `ToolFailure("INTERNAL_ERROR", ..., False)`.
   The agent sees `is_error=True` with the text `[CODE] retryable=<true|false> message`; the
   registry parses it into `ToolOutcome.error_code / error_message / retryable`. Codes are
   UPPER_SNAKE (`FLIGHT_NOT_FOUND`, `SERVICE_UNAVAILABLE`). `retryable=true` means the same call may
   succeed later (timeouts, 429/5xx); `false` means the arguments or state must change. The
   orchestrator counts `is_error` results only and must never inspect fields inside successful
   results to guess at failure.
5. **Side-effecting tools must be safe to reason about**: return a reference id, and say in the
   docstring whether they are idempotent.
6. **Mock first, real later.** Mock data lives behind `# TODO(real-api):` markers; replace the body,
   keep the signature and return shape.
7. Python 3.11+, `from __future__ import annotations`, async in the agent layer, no bare `except:`.
8. Secrets via environment variables (`ANTHROPIC_API_KEY`), never in code.

## Commands

```
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
set ANTHROPIC_API_KEY=...
python demo.py                                        # happy path
set TRAVEL_FAIL_MODE=all_down & python demo.py        # exercise human handoff
python -m pytest -q                                   # all tests (in-memory mcp.Client + stub LLM)
```

## Tool inventory (all mock data; every real call site is marked `TODO(real-api)`)

| Server | Tool | Side effect | Notes |
|---|---|---|---|
| travel | `search_flights(origin, destination, date)` | none | IATA codes, `YYYY-MM-DD` |
| travel | `book_flight(flight_id, passenger_name, passenger_email)` | creates booking | not idempotent |
| travel | `send_confirmation(email, booking_ref, details)` | sends email | not idempotent |
| retail | `check_inventory(sku, qty)` | none | out-of-stock is data, not an error |
| retail | `create_order(sku, qty, customer_id)` | reserves stock | then `charge_payment` |
| retail | `charge_payment(order_id, amount)` | charges customer | amount must equal order total |
| retail | `create_shipment(order_id, address)` | books carrier | order must be PAID |
| banking | `verify_kyc(customer_id)` | none | non-verified status is data; call before moving money |
| banking | `get_account_balance(account_id)` | none | |
| banking | `transfer_funds(from_acct, to_acct, amount)` | moves money | NOT idempotent: after a timeout, check balances before retrying |

Outage simulation env vars: `TRAVEL_FAIL_MODE`, `RETAIL_FAIL_MODE=payment_down`, `BANKING_FAIL_MODE=core_down`.

## Adding an industry
Create `servers/retail_server.py` with `mcp = MCPServer("retail")`, `@mcp.tool()` functions that
follow rules 3 and 4, and `if __name__ == "__main__": mcp.run()`. No other file changes.

## Later: LangGraph
`Orchestrator.run()` is the only loop. Keep `ToolRegistry`, `ExecutionState`, and `Handoff`
framework-independent so they port to LangGraph nodes unchanged.
