# API backend decision: should the orchestrator be a public API?

Written 2026-11-18 for a maintainer deciding. The live list of open rows is AUDIT.md §3a; this page names only the rows that bear on a public API, and `tests/test_api_decision_accuracy.py` fails if a row cited here has moved. Hosting figures were read from the providers' own pages on the date above and change: check them before relying on them.

## 1. WHY NOT NOW

- A public API would expose the planner (C24 (open)) and document extraction (C25 (open)). Both are open in §3a and both need Anthropic credits. The one live attempt so far stopped at its first step on a billing error, and the key it used was exposed in a chat transcript and revoked (AUDIT.md §3d).
- Exposing a credit-metered path that has never passed once is the fastest way to burn credits on abuse and to publish claims the audit cannot back. Even a pass of the planner contract would show the right industries, each intent once and the right order, not that the right tool was chosen (AUDIT.md §3d).
- The learning loop against a real upstream failure (C27 (open)) is unproven in the same way.
- The docs workflow (C66 (open)) has never run: there is no remote and enabling Pages is a human step. A public API with an unverified deploy path has the same problem at a different layer.

## 2. WHAT WOULD NEED TO BE TRUE FIRST

- **Credits budgeted and rate-limited:** a hard cap of US$50 per month, set as the monthly spend limit in the Anthropic Console before any key is deployed (check that the Console offers it for the account), plus 20 planner calls per caller per day and 500 per month in total. The numbers are a bound chosen without cost data, because no live plan has been measured; replace them with figures derived from the first live run's token counts.
- **A per-caller allowlist or API key, and a refusal path that does not leak the reason:** an unknown key, an exhausted limit and a disabled caller all return the same status, body and timing to an unauthenticated caller; the reason goes to the server log only.
- **C24 (open), C25 (open) and C27 (open) closed at least once with live-run evidence,** their rows moved from §3a to §2b by the ceremony in AUDIT.md §5.
- **C66 (open) closed** by a first real CI run, after a remote exists and Pages is enabled.

## 3. IF YOU STILL WANT ONE

Three hosting options, as read on 2026-11-18. The Cloud Run free-tier quantities and Render's instance sizes and paid price come from search summaries of the providers' documentation, because those pages did not load in full; the rest (Render's free-tier limits, Fly.io's pricing, Cloud Run's timeout) was read from the providers' own pages.

**Cloud Run** — free tier: 2,000,000 requests, 180,000 vCPU-seconds and 360,000 GiB-seconds a month (request-based billing, us-central1 pricing, per billing account). After it you pay per use: requests are listed at US$0.40 per million, and CPU and memory are billed per second at rates not re-verified here. It scales to zero, so the first request after idle pays a container start, not measured for this app. The default request timeout is 5 minutes and can be raised to 60. At an assumed 0.5 vCPU-second per call the vCPU allowance is used up after about 360,000 calls a month.

**Render** — free tier: 750 instance hours per workspace per month on 0.1 CPU and 512 MB, spinning down after 15 minutes without traffic and taking about a minute to wake, with no persistent disk. After it, the smallest paid web instance listed is US$7 a month for 0.5 CPU and 512 MB, always on. No request timeout is stated on the free-tier page.

**Fly.io** — free: none for new organizations ("New organizations don't have a free tier or a monthly free usage allowance"); a US$1 trial credit lasts 7 days, and a card is required after it. The smallest machine, shared-cpu-1x, is US$2.19 a month with 256 MB and US$6.70 with 1 GB, billed per second while running; a stopped machine pays only for its root file system. Older descriptions of a Fly.io free allowance are out of date. The request timeout is not on the page read.

**The planner on any free tier.** Running it inside the service: no. Measured here, a 4B-parameter model on a 12-thread laptop CPU took 80 to 105 seconds per request, with at least ten requests per plan (AUDIT.md §7), and the free allowances are small slices of CPU time: 0.1 CPU on the smallest free instance, about 50 hours of one vCPU a month on the second, none at all on the third. Calling the Anthropic API from the service instead: the host only waits, and its timeout is then not what limits it, the credit cap is; the time per plan is unmeasured because C24 (open) has never run against Claude.

## 4. THE MINIMAL SAFE VERSION

- Serve ONLY the AUDIT §2 deterministic flows: duty matching (C33 (proven), C34 (proven)), the write-policy check (C44 (proven), C12 (proven)) and propose-from-fixture (C05 (proven)). These are the same three things the Gradio demo shows (C64 (proven)).
- It is buildable today. The demo's three functions already do the work offline. Measured on the development laptop on 2026-11-18: 70 MiB peak working set after running every flow and fixture, without Gradio; first-call latency 77 ms (duty matching), 540 ms (write-policy check) and 39 ms (proposal); warm calls 5 to 31 ms. An HTTP framework would add to the memory, not measured.
- The demo, once published, already serves these as HTTP endpoints through Gradio's built-in API (shown locally with a Gradio client on 2026-11-16). An explicit wrapper gives stable paths, per-caller keys and rate limits, and no UI. It reuses the demo's functions, adds an HTTP wrapper and adds one line to the README (§5).
- It must keep the demo's guards: refuse to start with a model key set, write only to scratch space, and cap inputs (20,000 characters; 1 to 5 observations). It calls no model, no live upstream and spends no credits. Abuse can still cost CPU, so the rate limits stay.

## 5. WHAT TO SAY IN THE README

The public API serves AUDIT §2 proven flows only; §3a gaps (C24, C25, C27, C66) are not exposed.

If one of those four rows closes, this sentence is edited in the same commit, and the accuracy test fails until it is.
