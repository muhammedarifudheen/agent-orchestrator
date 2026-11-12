# Live run ceremony: closing the three judgment gaps (C24, C25, C27)

This page is the exact procedure a maintainer follows ONCE, with a real `ANTHROPIC_API_KEY` and a real upstream, to close the three judgment gaps in `AUDIT.md` section 3a: C24 (the real `ClaudePlanner`), C25 (real doc extraction) and C27 (the learning loop against a real upstream failure). Background: `AUDIT.md` sections 3a and 5, `CLAUDE.md` D25.

Who runs it: a maintainer who owns the API key and may send requests to a third party. It was NOT run when this page was written (there was no key and no upstream), so the expected output below is taken from the scripts' source and their contracts, not from a live run. If a real run prints something different, the script wins: fix this page in the same commit.

What it costs: real Claude API calls (the planner tests, the doc contract test, and one real doc-ingestion call) and real HTTP requests to the upstream you name. Windows, PowerShell, from the repo root; every command spells out `.venv\Scripts\python`.

NOTHING in `AUDIT.md` changes unless every step passes. A row is moved only by `scripts/move_audit_row.py`, only from a fresh real pass. A historical record is not evidence. Never edit `AUDIT.md` by hand for this.

## 1. Preconditions

1. `ANTHROPIC_API_KEY` is set in the shell that runs the commands. Never write it into a file. The old `ANTHROPIC_AUTH_TOKEN` is not accepted (CLAUDE.md D16): if only that is set, the harness treats the key as absent.

   ```powershell
   $env:ANTHROPIC_API_KEY = "<your key>"
   ```

   bash: `export ANTHROPIC_API_KEY=<your key>`
2. The upstream URL is reachable and FAILS with an HTTP status the generated spec does not declare: not 429 and not 5xx. Example: `https://httpbin.org/status/418`. A loopback or private address is neither required nor accepted: it is not a real upstream.
3. The git tree is clean, on the branch you will commit to (each row move makes a commit there):

   ```powershell
   git status --short
   ```

   Expected: no output. If anything is listed, commit or stash it first; `move_audit_row.py` also refuses when `AUDIT.md` itself has uncommitted changes (`DIRTY_AUDIT`, step 5).
4. The default suite is green:

   ```powershell
   .venv\Scripts\python -m pytest -q
   ```

   Expected: `0 failed`, exit code 0 (the skip count is 26 without the key; see step 7).
5. Read the plan before you run anything. This needs no key and sends nothing:

   ```powershell
   .venv\Scripts\python scripts\live_run_ceremony.py --dry-run
   ```

   Expected: the whole plan (every command of steps 2 to 6), nothing executed. Per-step plans are also available: `.venv\Scripts\python scripts\live_judgment.py --planner --dry-run` prints the exact pytest command, the node ids, the verbatim assertion source and the redacted wire request.

If you stop at any point, nothing needs undoing: the harness never edits a tracked file, and `AUDIT.md` has not been touched until step 5.

## 2. C24: the real planner

```powershell
.venv\Scripts\python scripts\live_judgment.py --planner
```

It runs the EXISTING `claude` parametrizations of `tests/scenarios/test_planner_contract.py` and `tests/scenarios/test_cross_industry_planners.py` in a pytest subprocess with `LIVE_TESTS=1` (the only thing it changes), and reads the verdict from the junit XML. Expected on success, last lines:

```
PASS (live model): 6 planner-contract tests passed
  attestation: <n> answered request(s) to api.anthropic.com over the real transport, 0 over another transport; real=True
RECORD_HASH: <64 hex characters>
```

Exit code 0. Copy the 64-character `RECORD_HASH:` value: step 5 needs it. The run also leaves `.perf\live_judgment\<UTC>_planner.xml` and one appended line in `.perf\live_judgment.jsonl`; the hash is the SHA-256 of that line. The record says `real: true` only if at least one HTTP request really went to `api.anthropic.com` through httpx's real transport and none went through a mock.

What a failure looks like: `FAIL (live model): <n> of <m> test(s) failed: <test id>: <message> | ...` and exit code 1. A skipped test prints `SKIPPED (this is not a pass): ...`. A missing key prints `ANTHROPIC_API_KEY absent; to close this gap, set the key and re-run` and exits 2.

IF IT FAILS, STOP. Paste the failure output into the audit trail (the issue or the PR for this ceremony). Do NOT edit `AUDIT.md`. A `fail` record is the honest state: the row stays in 3a with its reason. Do not run step 5 for this row, and do not retry until you have read why it failed.

## 3. C25: real doc extraction

```powershell
.venv\Scripts\python scripts\live_judgment.py --doc
```

Step 1 runs the existing `http` parametrization of `tests/ingest/test_doc_contract.py` against the real model. Step 2 runs the production `pipeline.process` on `tests/ingest/fixtures/dogceo.doc.md` with the real `HttpDocExtractionClient`, in a throwaway project copy. Expected on success: the two `Step` lines, then

```
PASS (live model): 1 doc-contract test(s) passed and the pipeline generated and verified the server from the prose document
  attestation: <n> answered request(s) to api.anthropic.com over the real transport, 0 over another transport; real=True
RECORD_HASH: <64 hex characters>
```

Exit code 0. (The count in that line is the number of collected doc-contract tests.) Copy the hash.

What a failure looks like: `FAIL (live model): step 1 ...` or `FAIL (live model): step 2 (pipeline): ...`, exit 1. For a step 2 failure the throwaway project is kept and its path is printed (`the throwaway project was kept for inspection: <path>`).

A review-gate block (`blocked_at_review_gate: the model's draft needs a person ...`) is a LEGITIMATE HONEST FAILURE, not a harness fault: the model flagged operations for review (CLAUDE.md D12, D15). `AUDIT.md` stays as it is.

IF IT FAILS, STOP. Paste the failure into the audit trail and do NOT edit `AUDIT.md`.

## 4. C27: the learning loop against a real upstream

```powershell
.venv\Scripts\python scripts\live_judgment.py --apply https://httpbin.org/status/418 --allow-live
```

`--allow-live` is REQUIRED for a non-loopback upstream. It is your explicit confirmation that real requests will go to a third party (here, httpbin.org). It is never a default; without it the command exits 5 with `LIVE_NOT_ALLOWED` and sends nothing.

The harness probes the URL once, then runs the real loop in a throwaway git copy: generate a one-operation spec, run the real Orchestrator twice against the upstream, `learning.collect`, `learning.propose`, an approval that the HARNESS SIMULATES (it says so in its output; the human gate is tested elsewhere), `learning.apply` with the real staged and promoted verification, and then checks the new rule, the regenerated server, the contract test, one new commit and a fresh call. Expected output: `Upstream <url> answered 418 (real, public). ...`, then `STEP 1 ok ...` through `STEP 6 ok ...`, then

```
  attestation: <n> answered request(s) to <the upstream host> over the real transport, 0 over another transport; real=True
PASS (real upstream): learned HTTP 418 -> applied
RECORD_HASH: <64 hex characters>
```

Exit code 0. Copy the hash.

Other outcomes, none of which is a pass:

| Output | Exit code | Meaning |
|---|---|---|
| `UPSTREAM_MISSING: ...` | 3 | no URL, not a full http(s) URL, a loopback or private host, or not reachable within 10 s |
| `UPSTREAM_HEALTHY: ...` | 4 | the endpoint answered below 400; there is nothing to learn from it |
| `LIVE_NOT_ALLOWED` | 5 | `--allow-live` was not given |
| `FAIL (apply): ...` | 1 | a step failed, or the status was 429 or 5xx (already declared); the throwaway project path is printed |

IF IT FAILS, STOP. Paste the failure into the audit trail and do NOT edit `AUDIT.md`.

## 5. Move each passed row (C24, then C25, then C27)

Only for a row whose step passed. In this order, one row at a time. First WITHOUT `--apply`, to read what would change:

```powershell
.venv\Scripts\python scripts\move_audit_row.py --row C24 --record-hash <hash from step 2>
```

Expected: the diff it WOULD make to `AUDIT.md` (the row removed from section 3a; the row added to a new `### 2b. Proven live` table with the record hash, the date, the command and the evidence hash) and the verdict of its checks. Exit 0 means the move is allowed. Any other exit code is a refusal (table below), and nothing is written.

When the diff reads right, run the same command with `--apply`:

```powershell
.venv\Scripts\python scripts\move_audit_row.py --row C24 --record-hash <hash from step 2> --apply
```

Expected: the diff, then `MOVED: C24 -> AUDIT.md section 2b (record <hash12>); committed <git sha>: audit: close live judgment gap C24 (<hash12>)`, and a commit whose message is

```
audit: close live judgment gap C24 (<first 12 hex of the record hash>)
```

with the full 64-character hash in the commit body. Repeat for `C25` (hash from step 3) and `C27` (hash from step 4). The hash is the one the matching step printed on its `RECORD_HASH:` line. Row to subcommand: C24 `--planner`, C25 `--doc`, C27 `--apply`.

Freshness: the record must be no older than 30 minutes (`STALE`). If you took a long break, rerun the step and use its new hash.

Refusals print `REFUSED (<NAME>): <reason>`. On every one of them: do not edit `AUDIT.md` by hand; fix the cause and, where the table says so, rerun the step.

| Exit code | Name | Action |
|---|---|---|
| 10 | `NO_RECORD` | The hash matches no line of `.perf\live_judgment.jsonl`. Check the hash you pasted; if the record is not there, rerun the step. |
| 11 | `NOT_REAL` | The record is not `real: true`. Rerun the step with the real key (or the public upstream and `--allow-live`). |
| 12 | `NOT_PASS` | The record is a `fail` or `skipped`. Go back to the step's STOP rule; do not retry blindly. |
| 13 | `STALE` | The record is older than 30 minutes. Rerun the step and use the new hash. |
| 14 | `EVIDENCE_MISMATCH` | The evidence does not match the record (the hash, or the evidence file it points to). Rerun the step; investigate if it repeats, because it means something changed the evidence. |
| 15 | `ROW_NOT_OPEN` | The row is not in section 3a (already moved, or never there). Check `AUDIT.md` and `git log`; nothing to do if it is already in 2b. |
| 16 | `WRONG_SUBCOMMAND` | The record belongs to another subcommand than the row (C24 planner, C25 doc, C27 apply). Use the hash from the right step. |
| 17 | `MOCK` | The record came from the offline mock transport and can never close a gap. Run the real step. |
| 18 | `DIRTY_AUDIT` | `AUDIT.md` has uncommitted changes. Commit or revert them (not as part of this ceremony), then rerun the move. |

## 6. Check the three commits

```powershell
git log --oneline -5
```

Expected: three new commits, newest first C27, C25, C24 (if you moved them in that order, as step 5 says):

```
<hash> audit: close live judgment gap C27 (<hash12>)
<hash> audit: close live judgment gap C25 (<hash12>)
<hash> audit: close live judgment gap C24 (<hash12>)
```

`AUDIT.md` section 3a no longer lists the moved rows and the new `### 2b. Proven live` table does.

## 7. The suite afterwards

```powershell
.venv\Scripts\python -m pytest --strict-budgets -q -rs
```

The arithmetic, stated plainly: moving rows from 3a to 2b does NOT change which tests are skipped. Skips are decided by environment variables, not by `AUDIT.md`. A brief that says "the skip count falls by 3" is wrong.

- The seven tests skipped with the reason `ANTHROPIC_API_KEY absent` (1 doc-contract test, 2 cross-industry planner tests, 4 planner-contract tests) run only when BOTH `ANTHROPIC_API_KEY` and `LIVE_TESTS=1` are set, and they make real API calls. BUT `LIVE_TESTS=1` also unlocks the 14 live-API tests (weather, jsonplaceholder, catfact, dogceo, zippopotamus), which have no other gate and call five third-party APIs for real. So in a shell with both set the default run shows **5 skipped** instead of 26 (26 - 7 - 14; the five left are the two `slow` tests, the retail outage probe and the two probe tests). Do this only if you want those 21 real-network tests too; set it for one shell with `$env:LIVE_TESTS = "1"`.
- In a shell with neither variable (the normal case, and the right one for this step), the run stays at **26 skipped** and passes: that is the expected result of step 7.
- C27 (the apply gap) changes no skip: no existing test needs a real upstream.

If a strict budget error appears (`GATE TEST TOO SLOW`, or a bare `N errors`), it is the budget guard on a slow host, not a regression by itself: re-run once, alone, with nothing else running (CLAUDE.md D17). `INVALID RUN (clock event)` also means re-run alone, see the table in section 10.

## 8. One commit per row, not one ceremony commit

The commit format is fixed: each row has its own commit, `audit: close live judgment gap <row> (<record-hash>)`, where the hash in the subject is the first 12 hex characters of the record hash. Do not squash the three into one ceremony commit and do not rename them; each commit ties one row to the one record that proves it.

## 9. The one-command version

Steps 2 to 6 in one go:

```powershell
.venv\Scripts\python scripts\live_run_ceremony.py --allow-live --upstream https://httpbin.org/status/418
```

It REFUSES to run without BOTH `ANTHROPIC_API_KEY` set and `--allow-live`: exit 20 (key absent), 21 (`--allow-live` absent), 22 (upstream missing). It stops at the first failing subcommand and never edits `AUDIT.md` after a failure. Add `--dry-run` to print the whole plan without executing anything and without a key (step 1.5).

The transcript is written to `.perf\ceremony\<UTC timestamp>.json`: every command, every output and every audit edit. To read it:

```powershell
Get-ChildItem .perf\ceremony | Sort-Object Name | Select-Object -Last 1
Get-Content (Get-ChildItem .perf\ceremony | Sort-Object Name | Select-Object -Last 1).FullName
```

Attach it to the PR or issue as the audit trail. The same STOP rules apply: if it stops at a failing step, paste the failure and the transcript into the audit trail and leave `AUDIT.md` alone.

## 10. When something goes wrong

| Symptom | Meaning | Action |
|---|---|---|
| `ANTHROPIC_API_KEY absent; to close this gap, set the key and re-run`, exit 2 | The key is not set in this shell (or only the unsupported `ANTHROPIC_AUTH_TOKEN` is). Recorded `skipped`. | Set `ANTHROPIC_API_KEY` in this shell (step 1.1) and rerun. Nothing to undo. |
| `FAIL (live model): ...` with an HTTP 401 from the API | The key is wrong or revoked. The record says `fail` and `real: false`: a 401 is not a successful answer, so the attestation does not count it as a real result. | Do not move any row. Fix the key and rerun the step. The `fail` record stays in the log; that is expected. |
| `FAIL (live model): step 2 (pipeline): blocked_at_review_gate ...` (`--doc`) | The model flagged operations for review. A legitimate honest failure. | `AUDIT.md` stays. Paste the findings into the audit trail. Do not clear the flags to make it pass. |
| `FAIL (live model): ... test(s) failed ...` (`--planner`) | The real model's plan broke the contract (wrong industry, missing intent, wrong order). | STOP. Paste into the audit trail. `AUDIT.md` stays. |
| `UPSTREAM_HEALTHY` (exit 4) | The upstream answered below 400. | Pick an endpoint that fails with an undeclared status, e.g. `https://httpbin.org/status/418`. |
| `UPSTREAM_MISSING` (exit 3) | No URL, not a public host, or not reachable. | Check the URL and your network; a loopback address is never accepted. |
| `LIVE_NOT_ALLOWED` (exit 5) | `--apply URL` without `--allow-live`. | Add `--allow-live` only if you mean to send real requests to that host. |
| `FAIL (apply): ... answers 418/429/5xx ...` with 429 or 5xx | The status is already declared by the default error map; nothing to learn. | Use an endpoint failing with a status other than 429 and 5xx. |
| `REFUSED (<NAME>): ...`, exit 10 to 18 | `move_audit_row.py` refused the move; nothing was written. | See the table in step 5. Never edit `AUDIT.md` by hand. |
| `INVALID RUN (clock event)` or a bare `N errors` in the suite | The machine paused or was loaded; the budget guard or clock guard fired (CLAUDE.md D17 (9), (12)). | Re-run the suite once, alone. Do not raise a budget to get it green. |
| A step timed out (`the step timed out after 240 s`) | A hung model call or a slow upstream. A `fail`. | STOP, paste into the audit trail, then rerun once. |

## 11. What the ceremony does NOT prove

It proves the planner contract and the doc contract on one scenario and one document, and the learning loop on one upstream failure, on one day, with one model. It is not a quality guarantee: a model can plan or extract differently tomorrow, a different document can be flagged for review, and the apply step uses a scripted stand-in for the MODEL and a simulated human approval (D25). `AUDIT.md` 2b records that a contract held once, live; it does not say it will hold. It also does not cover C26 (live behaviour of the five real-API servers), which stays open.

Never use the offline `--mock-transport` flag of `live_judgment.py` for the ceremony: it is offline proof of the harness only, can never produce `real: true`, and `move_audit_row.py` refuses its records (`MOCK`).
