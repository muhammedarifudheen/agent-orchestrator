# AUDIT §2 flows API

This API serves AUDIT §2 proven flows only. C24, C25, C27 are not exposed.

It is the minimal safe version described in `docs/API_BACKEND_DECISION.md`: three POST endpoints, plus a `GET /healthz` health check, that call the same in-process functions as the offline demo (`demo/app.py`). No planner, no document extraction, no live upstream, no model, no credits. Nothing it does leaves the process, and it refuses to start if `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`, or `PROVIDER_API_KEY`) is set:

```
this service serves AUDIT §2 flows only; ANTHROPIC_API_KEY must be unset.
```

## Run

From the repository root:

```
pip install -r api/requirements.txt
python -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```

There is **no authentication and no rate limiting** in this service. The flows are deterministic and public by design, but abuse can still cost CPU: put a proxy that rate-limits in front of it before exposing it. Request bodies are limited to 64 KiB. There are exactly three flow endpoints and one health check; the automatic `/docs` and `/openapi.json` routes are off.

## Health check

`GET /healthz` returns exactly this, and names no audit row:

```
{"status":"ok","flows":["duty-match","write-check","propose"],"planner":false,"doc_extraction":false}
```

## Examples

Which duty would run for a request (the real matcher at its real threshold; `duty` is null when none would, and `write_boundary` is the tool of the write the duty stops before):

```
curl -s -X POST http://127.0.0.1:8000/v1/duty-match -H 'content-type: application/json' -d '{"intent": "refund order ORD-ABCD1234"}'
```
```
{"duty":"process_refund","confidence":1.0,"write_boundary":"retail__issue_refund"}
```

Would the write policy allow a tool (`search_flights`, `book_flight` or `transfer_funds`); when it would not, `refusal_code` and the real pending-approval record say why. Nothing is executed:

```
curl -s -X POST http://127.0.0.1:8000/v1/write-check -H 'content-type: application/json' -d '{"tool": "book_flight", "allow_writes": false}'
```
```
{"is_write":true,"allowed":false,"refusal_code":"WRITE_APPROVAL_REQUIRED","pending_approval":{"kind":"pending_write_approval", ...}}
```

The proposal the real learning loop makes for a synthetic failed-call handoff, treated as observed twice (the recurrence the proposer needs). `proposal_type` is `"none"` when it makes none (for a system with no spec, say). `diff` is the proposal file; `action_md` is its review note when it has one. It is never applied and no spec is written (`applied` and `specs_written` are always false):

```
curl -s -X POST http://127.0.0.1:8000/v1/propose -H 'content-type: application/json' -d '{"handoff": {"error_snapshot": {"step": 2, "tool": "weather__get_current_weather", "error_code": "UPSTREAM_TEAPOT", "error_message": "upstream answered an undeclared status", "retryable": false, "recovery": "agent", "arguments": {"latitude": 12.97, "longitude": 77.59}, "http_status": 418}}}'
```
```
{"proposal_type":"add_error_code","diff":"# proposal: add_error_code\n# system: weather\n...","action_md":null,"applied":false,"specs_written":false}
```

Bad input gets a 422 (an unknown tool, an error code or tool name that could escape a file name, a handoff over 20,000 characters) and an oversized body a 413.

## Deploying (Railway, from the `main` branch)

The files are in the repository: `Dockerfile`, `.dockerignore` and `.github/workflows/api.yml`. The workflow tests on every push and pull request, and deploys to Railway only from a push to `main`, only after the tests pass. **It has never run against a real Railway project** (AUDIT.md 3a, C70): the account and its token are the owner's to create, and until they exist the deploy job fails on its first step with a message that says so. The steps:

1. Create a Railway account at railway.com (sign in with GitHub). The **trial needs no credit card**: it is a one-time $5 grant that lasts 30 days, services do not sleep during it, and **when the grant or the 30 days run out the services stop** unless you upgrade to the Hobby plan ($5 a month), which needs a card. That is the point to decide, not before; nothing here enters one.
2. New project, then an empty service (the name is yours; the workflow will use it). Do not connect the GitHub repository for automatic deploys: the workflow deploys.
3. In the service's settings: set the **health check path to `/healthz`**, generate a public domain under Networking, and check that **no variable named `ANTHROPIC_*` or `PROVIDER_*` exists**: the service refuses to start with one, and none may be configured on this host. (A `railway.json` is not used: Railway's config-as-code is deprecated and stops working for existing files on 2026-12-01; a `Dockerfile` alone is built.)
4. In the project's settings, create a **project token** for the production environment.
5. In the GitHub repository (Settings, Secrets and variables, Actions): the secret `RAILWAY_TOKEN` (the token), the variable `RAILWAY_SERVICE` (the service's name) and the variable `API_URL` (the public domain, with `https://` and no trailing slash). `API_URL` switches on the smoke test, which fails the run unless `/healthz` returns exactly the body above.
6. Push to `main`, or run the `api` workflow by hand.

While the service is unauthenticated and has no rate limiting, the trial's $5 cap is also the ceiling on what abuse can cost. If you upgrade to a paid plan, set a usage limit in Railway first.
