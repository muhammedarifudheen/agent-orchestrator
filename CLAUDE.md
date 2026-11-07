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
  Every failure states two facts: `retryable` (may the same call succeed later?) and `recovery`
  (`agent` = the agent can fix it; `human` = it needs a person). Policy, evaluated after every call:
  1. `recovery=human`, or an error that states no recovery (fail safe): hand off at once.
  2. `retryable=true` failures in a row >= 2: hand off (a success or any other outcome resets the run).
  3. `recovery=agent`: the agent gets ONE self-correction per tool. The first failure is fed back to
     Claude with the error text plus that tool's parameter hints (its `Args:` docs + schema); the
     second failure of the same tool (since it last succeeded) hands off.
  On handoff the run stops at once: remaining calls of that turn are skipped (recorded, not executed)
  and the LLM is not called again. It also fires on step limit or LLM/API failure, and when the
  agent stops while its last call was still failing. It writes
  `handoffs/YYYYMMDD_HHMMSS_<user_id>.json` with: goal, user, reason, `error_snapshot` (the
  triggering failure incl. retryable/recovery), `steps` (thought, tool, args, result per call),
  `current_state` (what already succeeded - e.g. an order that must not be created twice - plus
  skipped calls and the failure counters), and the text decision log.

### Recorded decisions (error contract + handoff)
These were NOT written down when first made; this is the record, reconstructed in the turn that
implemented them. Change them here first, then in code.
- D1. Failure text is `[CODE] retryable=<true|false> recovery=<agent|human> message`. `recovery` is
  mandatory on every `ToolFailure`; there is no default.
- D2. `recovery=agent`: bad/missing/mistyped arguments, an id the agent can look up again, wrong call
  order, a transient fault worth one retry, a sold-out choice it can replace.
  `recovery=human`: business declines (card declined, insufficient funds, frozen account, KYC, limit),
  state conflicts that could duplicate a side effect (ALREADY_PAID, ALREADY_SHIPPED), auth/permission,
  unknown outcomes (OUTCOME_UNKNOWN), internal faults, unparseable responses.
- D3. `retryable` and `recovery` are independent: INVALID_DATE is retryable=false, recovery=agent.
- D4. The consecutive-failure limit (2) applies to `retryable=true` failures only.
- D5. The `<user_id>` in the handoff filename is `Orchestrator(user_id=...)`, defaulting to the OS
  username (sanitized). Pass the real end-user id from the calling application.

### Recorded decisions (generator)
- D6. The spec format keeps the field names in `specs/schema.json` rather than the shorter names in
  the genericity-engine brief (`auth.kind/env` not `auth{type, env_var}`; `request.method/path` and
  `params[].in` not flat `method/path`; `response{type,select,description}` not `response_shape`;
  `errors[]`/`error_map[]` with `{when, code, retryable, recovery}` not `error_codes[]{code, recovery}`).
  Same information, a superset; renaming would have broken the working generator and its tests.
- D7. `servers/__init__.py` puts `servers/` on `sys.path`, so both `python -c "import servers.<name>_server"`
  and script launch (`python servers/<name>_server.py`, how the registry and `mcp dev` start servers)
  work. `tests/test_import.py` asserts both for every server.
- D8. Specs are read-only unless a human has reviewed the safety fields. `jsonplaceholder.yaml` is
  deliberately read-only: its API fakes writes, but the fail-closed rule (`review.safety_fields:
  reviewed`) must be signed by a person, and an agent must not self-approve one.
- D9. `tests/test_contract.py` assertions are identical for every server. Per-server data is only the
  probes. Two asymmetries are stated, not hidden: (a) the end-to-end "raw exception becomes
  INTERNAL_ERROR" check runs only for generated servers (their HTTP layer is injectable); hand-written
  ones get the structural check that every tool is wrapped by `@guarded`; (b) retail has no outage probe.
- D10. A spec's mocked-only behaviour is allowed but must be labelled in the YAML (e.g. the
  `list_comments_for_post` 404 rule: the real API returns 200 `[]`).

## Architecture: genericity

Goal: a pipeline that turns ANY reachable system into tools with the same contract, without a
hand-written server per industry. Status: the spec -> server generator is **built and proven on one system**
(`weather`); ingestion (OpenAPI/HAR/doc -> spec) is **not built**. The three industry servers are
hand-written mock simulators that the design was checked against.

### 1. Separation: what is shared, what is per-system

Measured on the current servers (non-blank lines, `ast`-counted):

| File | tool signatures + docstrings | tool bodies + module data/helpers | `TODO(real-api)` |
|---|---|---|---|
| travel_server.py | 59 (40%) | 90 (60%) | 4 |
| retail_server.py | 75 (42%) | 104 (58%) | 7 |
| banking_server.py | 59 (32%) | 125 (68%) | 7 |

The "80% copy-pasted boilerplate" premise is **false**. True boilerplate (imports, `MCPServer()`,
decorator pair, `__main__` guard, an outage helper) is roughly 15-25 lines per file (estimate).
The bulk is two different things:

- **Interface layer (~40%)**: names, typed params, docstrings, error codes + retryable flags,
  ordering ("call after create_order"), side-effect and idempotency notes. This is data about the
  system, fully expressible in a spec, and is what a generator can emit.
- **Simulation layer (~60%)**: in-memory state machines and validation (stock decrement, ORDER_NOT_PAID,
  daily limit, KYC re-check). This exists only because there is no real backend. It is NOT
  generatable and NOT shared. When the real API is connected it is deleted, and the real system
  enforces those rules. It survives only as a test double.

Classification:

| File | Class | Notes |
|---|---|---|
| `servers/errors.py` | generic infrastructure | identical for every system (`ToolFailure`, `@guarded`) |
| `agent/tool_registry.py`, `agent/orchestrator.py` | generic infrastructure | already industry-free |
| `servers/{travel,retail,banking}_server.py` | **hybrid** | interface layer = adapter (generatable); simulation layer = test double (system-specific, hand-written, disposable) |
| `tests/test_{travel,retail,banking}.py` | system-specific | assert simulator behavior; stay with the test double |
| `tests/test_handoff.py`, `tests/stdio_smoke.py` | generic mechanism, retail/travel fixtures | fixtures should move to spec `examples` |

Consequence: the extraction is not "pull out shared boilerplate", it is **split interface from
simulation**. Interface -> spec. Simulation -> mock backend behind the same transport interface.

### 2. Core insight: generate the server from a spec

Minimal system spec (YAML, JSON-Schema validated at load):

```yaml
system: <name>                       # tool prefix
transport: {kind: http|mock, base_url_env: X_BASE_URL, timeout_s: 10}
auth: {kind: none|api_key|bearer|basic|oauth_client_credentials, env: [..]}
error_map:                           # system-wide defaults, per-operation overrides win
  - {when: {status: 429}, code: RATE_LIMITED, retryable: true, recovery: agent}
  - {when: {status: 5xx}, code: SERVICE_UNAVAILABLE, retryable: true, recovery: agent}
operations:
  - name: query_account_balance
    summary: one sentence             # docstring line 1
    when_to_call: one sentence        # docstring "Call ..." line
    side_effect: read|write|money     # drives safety policy below
    idempotent: true|false
    requires: [verify_kyc]            # ordering hints -> docstring
    request: {method: GET, path: /accounts/{account_id}}
    params: [{name, type, in: path|query|body, required, description, constraints: {pattern, enum, minimum, multiple_of}}]
    response: {description, select: "$.data", fields: [..]}
    errors: [{when: {status: 409, body: {code: FROZEN}}, code: ACCOUNT_FROZEN, retryable: false, recovery: human, meaning: ".."}]
    examples: [{args: {..}, expect: ok}, {args: {..}, expect: {code: INVALID_AMOUNT}}]
    review: {source: openapi|har|llm|human, safety_fields: reviewed|unreviewed}
```

Can all three existing servers be expressed as spec + one generator?

- **Interface layer: yes, all of it.** Every signature, docstring section, error code, retryable flag
  and ordering hint of the 10 tools fits the schema. Input checks like IATA pattern, email pattern,
  positive amount, 2-dp amount map to `constraints`.
- **Simulation layer: no, and it should not be generated.** It is replaced by (a) the real system
  over `transport: http`, or (b) for offline tests, a mock backend that serves the spec's
  `examples`. Stateful behavior (stock drops after an order, a paid order can ship) is NOT
  reproduced by example-serving mocks; tests that need it keep a hand-written stateful double
  that plugs in as `transport: mock` (this is what the current servers become).
- **Exactly what resists generation:**
  1. Cross-operation business rules and state (enforced by the real backend; the spec can only
     document them through `requires`).
  2. Composite operations (`transfer_funds` re-checks KYC then moves money; `book_flight`
     derives `flight_id`). Escape hatch: `hooks:` Python module for named operations. Every hook is
     a per-system hand-written cost and is counted as such.
  3. Response derivation (`in_stock = available >= qty`). Small `select`/`derive` expressions
     cover simple cases; the rest is a hook.
  4. Auth flows beyond static credentials / client-credentials: MFA, interactive OAuth, session
     cookies, request signing (SigV4), mTLS. These are transports/auth plugins written once per
     *kind*, not per system, but they are not free.
  5. Semantic judgment: whether `PAYMENT_DECLINED` is retryable, whether an operation is
     idempotent. See guardrails in section 3.

So the generator is the product for the interface layer, and the spec is the per-system artifact.
The generator emits a normal `servers/<name>_server.py` (committed, with a `GENERATED - DO NOT EDIT`
header + spec hash; CI regenerates and diffs) so registry discovery, `mcp dev`, and review stay
unchanged. Rejected alternative: one runtime-interpreting server, since dynamic signatures lose
the type-hint-derived schemas and make diffs unreviewable.

**Contract defect this exposed - FIXED (da57843, extended in 8c53a30):** `@guarded` used to map every
`TimeoutError` to `TIMEOUT retryable=true`. For a non-idempotent `write`/`money` operation a timeout is
an UNKNOWN OUTCOME, and telling the agent to retry invites a double charge. Now: idempotent ->
`TIMEOUT retryable=true recovery=agent`; otherwise -> `OUTCOME_UNKNOWN retryable=false recovery=human`.
The generated HTTP runtime applies the same rule (read timeout vs connect timeout vs 5xx), covered by
`tests/test_generator.py`.

### 3. Learning loop: what "study a system" can honestly mean

(The phrase "the AI should study every system" does not appear in earlier turns of this project as
I have it; the answer below addresses the idea, not a quoted commitment.)

Auto-discoverable (deterministic where possible):
- OpenAPI/Swagger, GraphQL introspection, gRPC reflection, Postman collections: operations, params,
  types, constraints, status codes, descriptions. Quality varies; descriptions are usually thin and
  never say "when to call" or ordering.
- Existing vendor MCP servers: wrap/proxy them, do not regenerate.
- Recorded HTTP traces (HAR): endpoints, observed request/response shapes, auth header shape.
  Only what was exercised: no constraints, no error cases that did not occur.
- Prose docs / pasted text: LLM extraction. Lossy; output is always a *draft*.

Not discoverable:
- Undocumented business rules, hidden preconditions, rate limits not published.
- Safety semantics: idempotency, retryability of business errors, blast radius, which calls move money.
- Units, timezones, rounding, sandbox-vs-prod differences.
- Auth that needs a human (MFA, CAPTCHA, device approval), systems with no API (legacy GUI,
  mainframe, email-driven) which need a different transport (browser/RPA/SFTP).
- Whether the business is *allowed* to automate an action.

Verdict on the proposed pipeline `{OpenAPI url | pasted doc | trace} -> system spec -> generator ->
server + tests`: **confirmed, with three amendments; "any system" is rejected as stated.**
1. Ingestion emits a *draft* spec with per-operation provenance (`review.source`) and
   `safety_fields: unreviewed`. LLM use is confined to ingestion; the generator is deterministic
   templating so it can be tested and diffed.
2. Fail closed: the generator refuses to expose any `write`/`money` operation whose safety fields
   (`side_effect`, `idempotent`, error `retryable`, `requires`) are unreviewed. Read operations may
   ship unreviewed.
3. A reviewer (human) signs off the safety fields; that is the irreducible per-system human cost.

Honest scope: "any system reachable through a supported transport with an observable API surface".
The list of supported transports/auth kinds is the real boundary of "any".

### 4. Guardrail: proving genericity without writing 10 servers

A single shared `tests/test_contract.py`, parametrized over every `servers/*_server.py`, asserting
only system-independent properties: tools listed with non-empty docstrings containing a
"Call"/when-to-call sentence and an `Errors:` line; every failing probe is `is_error=True` with text
`[CODE] retryable=<bool> recovery=<agent|human> message`; a raw exception becomes `INTERNAL_ERROR retryable=false recovery=human`;
`write`/`money` tools declare idempotency; the outage switch yields a retryable code; nothing returns
a normal result containing an error. Probes come from the spec's `examples` (for hand-written
servers, from `specs/<name>.probes.yaml`).

Proof plan, strongest evidence first:
1. **Round trip on travel**: express travel's interface as `specs/travel.yaml`, generate the
   server against the existing travel simulator as `transport: mock`, and require BOTH
   `test_contract.py` and the existing `tests/test_travel.py` to pass unchanged.
2. **Hold-out system**: ingest an OpenAPI document nobody tuned the generator for (e.g. the public
   Swagger Petstore) with no hand edits except safety review; generated server must pass
   `test_contract.py` against its `examples`-serving mock, then a real call against the public
   sandbox.
3. **The contract test must be able to fail**: mutation-check it (break `[CODE] retryable=` format,
   drop a docstring section, return an error dict, mark a write tool idempotent=missing); each
   mutation must turn it red. A contract test that cannot fail proves nothing.

What this does and does not prove: conformance to the contract (error format, schemas, docs,
safety declarations) by construction. It does NOT prove the tools do the right thing on the real
system; that needs per-system sandbox tests (`tests/test_<name>.py`, generated from `examples`,
credentials required).

### File plan and status

```
specs/
  system_spec.schema.json        # JSON Schema for the spec
  travel.yaml                    # phase 1: interface layer of the existing server
  travel.probes.yaml             # probes for hand-written servers (pre-migration)
  petstore.yaml                  # phase 3: ingested + reviewed
generators/
  spec.py                        # load, validate, normalize -> dataclasses; fail-closed safety check
  from_spec.py                   # CLI: specs/<n>.yaml -> servers/<n>_server.py (+ tests)
  ingest/openapi.py              # OpenAPI 3 -> draft spec (deterministic)
  ingest/har.py                  # trace -> draft spec (infers params/shapes)
  ingest/doc_llm.py              # pasted docs -> draft spec via Claude; always unreviewed
  templates/server.py.j2         # tool functions, docstrings, @guarded wiring
  templates/test_system.py.j2    # per-tool sandbox tests from examples
servers/
  errors.py                      # existing; add OUTCOME_UNKNOWN policy
  _runtime/http.py               # httpx client, auth kinds, timeouts, error_map application
  _runtime/mock.py               # serves examples; pluggable stateful double
tests/
  test_contract.py               # shared, parametrized over all servers
```

Original phase plan (P0 and the generator core below are now done; see "What is built"): P0 fix the timeout/idempotency defect in `@guarded` + write
`test_contract.py` against the three existing servers (expect it to find real gaps); P1 spec schema +
travel round trip; P2 OpenAPI ingestion + Petstore hold-out; P3 retail/banking migration (hooks needed
for composites; count them); P4 HAR and doc ingestion.

### What is built (and what is not)

Built and tested:
- `servers/_base.py`: shared core for ALL servers (ToolFailure re-export, `make_tool` registration
  decorator with `side_effect`/`idempotent` safety annotations, `check_outage`, `auth_from_env`,
  `standard_http_error` (code, retryable, recovery) mapping). The three hand-written servers use it.
- `servers/errors.py`: `@guarded` now takes `idempotent`. A timeout is `TIMEOUT retryable=true` for
  idempotent tools and `OUTCOME_UNKNOWN retryable=false` otherwise (fixes the double-charge risk).
- `servers/_http.py`: generic HTTP runtime (arg constraints, auth from env, error_map, timeouts,
  response select). Every failure becomes `ToolFailure`.
- `specs/schema.json`, `specs/weather.yaml`, `generators/from_spec.py`: spec -> `servers/<name>_server.py`
  + `tests/test_<name>.py`. Refuses to generate non-read operations unless `idempotent` is stated and
  `review.safety_fields: reviewed`. `--check` detects stale generated files.
- `tests/test_contract.py`: ONE contract test, run identically against travel, retail, banking
  (hand-written) and weather + jsonplaceholder (generated, two APIs with different response shapes). Mutation-checked: 5 deliberate contract breaks are each caught.
- `agent/tool_registry.py: parse_error` normalizes SDK argument-validation errors to
  `INVALID_ARGUMENTS retryable=false`.

NOT built: OpenAPI/HAR/doc ingestion; OAuth/MFA/signed-request auth; `hooks:` for composite operations;
migrating travel/retail/banking to specs (their simulators are stateful and not expressible as a spec,
see section 1); a retail outage probe in the contract test. "Proven" means: the generated `weather`
server (spec only, zero hand-written server code) passes the same contract test as hand-written
`travel`, and its real API calls work (`LIVE_TESTS=1`). It does not yet show the generator copes with
a messier system than a two-operation read-only API.

## Rules

1. **No industry logic in the Agent layer.** No "if flight", "if refund", "if account balance" in
   `agent/`. Domain knowledge lives in tool names, descriptions, and server code. If Claude needs a
   domain hint, put it in the tool docstring.
2. **All external-system capability goes through an MCP server.** No `requests`/SDK/DB calls in
   `agent/` or `demo.py`. New system = new `servers/<name>_server.py`; it is auto-discovered.
3. **Every tool has full type hints and a docstring.** The docstring is what Claude reads: say what
   the tool does, each argument's format (e.g. `date` = `YYYY-MM-DD`), what it returns, and when
   it errors. Use `Args:` / `Returns:` sections.
4. **Failures are raised, never returned.** (Use `_base.make_tool` so registration, `@guarded` and
   the safety annotations come together.) Tools must never return a normal dict containing an
   error. Failures are raised as `ToolFailure(code, message, retryable, recovery)` from `servers/errors.py`,
   and every tool is wrapped in `@guarded` (directly under `@mcp.tool()`), which catches every
   other exception internally and re-raises it as `ToolFailure("INTERNAL_ERROR", ..., False, "human")`.
   The agent sees `is_error=True` with the text `[CODE] retryable=<true|false> recovery=<agent|human> message` (see D1-D4); the
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
8. Secrets via environment variables (`ANTHROPIC_API_KEY`, per-system keys named in the spec's
   `auth.env`), never in code, never in error text.
9. **New systems are added by writing a spec + running `generators/from_spec.py`. Hand-writing
   servers is deprecated except when a spec cannot capture the system; document why in the file's
   module docstring.** Generated files carry a `GENERATED ... DO NOT EDIT` header: change the spec
   and regenerate. The shared contract test (`tests/test_contract.py`) must pass for every server.

## Commands

```
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
set ANTHROPIC_API_KEY=...
python demo.py                                        # happy path
set TRAVEL_FAIL_MODE=all_down & python demo.py        # exercise human handoff
python -m pytest -q                                   # all tests (in-memory mcp.Client + stub LLM)
python -m generators.from_spec specs/weather.yaml     # (re)generate servers/weather_server.py + tests
python -m generators.from_spec specs/weather.yaml --check   # CI: exit 1 if generated files are stale
set LIVE_TESTS=1 & python -m pytest tests/test_weather.py   # also call the real Open-Meteo API
```

## Tool inventory (travel/retail/banking are mock data with `TODO(real-api)` markers; weather calls a real API)

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
| weather (generated, real API) | `get_current_weather(latitude, longitude)` | none | Open-Meteo, no key |
| weather (generated, real API) | `get_daily_forecast(latitude, longitude, days=3)` | none | Open-Meteo, no key |
| jsonplaceholder (generated, real API) | `get_post(post_id)`, `get_user(user_id)` | none | object / nested object responses |
| jsonplaceholder (generated, real API) | `list_posts_by_user(user_id)`, `list_comments_for_post(post_id)` | none | array responses; unknown id = empty list, not an error |

Outage simulation env vars: `TRAVEL_FAIL_MODE`, `RETAIL_FAIL_MODE=payment_down`, `BANKING_FAIL_MODE=core_down`.

## Adding a system
1. Write `specs/<name>.yaml` (see `specs/schema.json`, `specs/weather.yaml`). For any operation that
   writes or moves money, state `idempotent` and have a human set `review.safety_fields: reviewed`.
2. `python -m generators.from_spec specs/<name>.yaml` writes `servers/<name>_server.py` and
   `tests/test_<name>.py`. The registry discovers the server automatically.
3. `python -m pytest -q`. `tests/test_contract.py` picks the new spec up by itself (probes come from
   the spec's `examples`).
Hand-written server (exception only, see rule 9): `mcp = MCPServer("x")`, `tool = make_tool(mcp)`,
`@tool(side_effect=..., idempotent=...)` functions, `if __name__ == "__main__": mcp.run()`, plus a
probe entry in `tests/test_contract.py`.

## Later: LangGraph
`Orchestrator.run()` is the only loop. Keep `ToolRegistry`, `ExecutionState`, and `Handoff`
framework-independent so they port to LangGraph nodes unchanged.
