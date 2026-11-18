# Onboarding: a 30-minute walkthrough

Windows, PowerShell, from the repo root. Python 3.11+ (built on 3.12). Every command below was run on a machine whose `.venv` already existed; building the venv from nothing was not re-tested.
Work on a throwaway branch so nothing you try lands in the project (step 0 and the last step). Run ONE test command at a time (the copied `mycat.yaml` keeps catfact's description and base URL; that is harmless, the tests are offline): two suites at once slow each other down enough to trigger budget warnings, an `INVALID RUN`, or a script that looks hung (steps vii and ix).

## 0. Set up and take a practice branch

```powershell
if (-not (Test-Path .venv)) { python -m venv .venv }
.venv\Scripts\python -m pip install -r requirements.txt
git switch -c onboarding-practice
.venv\Scripts\python -m pytest -q
```

The first command creates `.venv` only if it is missing (re-running venv creation with the venv's own interpreter fails with `Permission denied`). If you have uncommitted changes, `git switch -c` carries them to the new branch and `git switch main` at the end brings them back; the Finish step touches only the files this page creates.

Expected last line: `1311 passed, 27 skipped in <about 150-300> s`, exit code 0. (The count changes when tests are added;
what must not change is `0 failed`.) pip may print a "new release of pip is available" notice; it is harmless.
You do NOT need an `ANTHROPIC_API_KEY` for anything on this page. Every command below spells out `.venv\Scripts\python` on purpose: bare `python` is
your system Python, which does not have the project's packages (`ModuleNotFoundError: No module named 'jsonschema'`).

Three files describe the project: `README.md` (how to run the tests), `AUDIT.md` (what is claimed, proven and not proven)
and `CLAUDE.md` (architecture and the numbered decisions D1-D21).

## i. Add a new spec and generate its server

A spec is a YAML file describing one system's operations. Start from a working one:

```powershell
(Get-Content specs\catfact.yaml) -replace 'system_name: catfact','system_name: mycat' -replace 'CATFACT_BASE_URL','MYCAT_BASE_URL' | Set-Content -Encoding utf8 specs\mycat.yaml
.venv\Scripts\python -m generators.from_spec specs\mycat.yaml
```

Expected: two `wrote:` lines, `servers\mycat_server.py` and `tests\test_mycat.py`. Both are GENERATED and carry a
`DO NOT EDIT` header: to change them, change the spec and regenerate. Check nothing is stale with
`.venv\Scripts\python -m generators.from_spec specs\mycat.yaml --check` (exit 0).

## ii. Run the contract test

`tests/test_contract.py` is one file, identical for every server; it discovers `specs\mycat.yaml` by itself.

```powershell
.venv\Scripts\python -m pytest tests\test_contract.py tests\test_mycat.py -q -k mycat
```

Expected: `13 passed` for the contract test alone (a read-only spec). With `tests\test_mycat.py` included: `20 passed, 3 skipped`; the 3
skips say `set LIVE_TESTS=1 to call the real API` (the opt-in live tests, which call the real internet).

## iii. Run the cross-industry scenario

```powershell
.venv\Scripts\python -m pytest tests\scenarios -q
```

It drives one goal through travel, banking and retail with a stand-in for the model and injects a real outage, then checks
the human handoff (a JSON file in a temp dir, not in `handoffs\`). Expected: `31 passed, 6 skipped`, every skip with the reason
`ANTHROPIC_API_KEY absent` (the real-model planner tests). `.venv\Scripts\python -m pytest tests\scenarios\test_cross_industry.py -q`
is the core 4-test version.

## iv. Run the learning loop end to end

The loop turns repeated failures in `handoffs\` into proposed spec fixes that a person approves.

```powershell
.venv\Scripts\python -m learning.collect      # handoffs\*.json -> learning\failures.jsonl
.venv\Scripts\python -m learning.propose      # recurrence >= 2 -> proposals\*.patch (never touches specs\)
.venv\Scripts\python -m learning.apply        # applies ONLY what a person moved into proposals\approved\
```

On this repo's two sample handoffs, `propose` ends `0 proposal(s) written` and `apply` prints `0 approved proposal(s) processed; 0 waiting for approval` because each failure has recurrence 1 and
`MIN_RECURRENCE` is 2 (that is the loop refusing to act on a one-off, not a fault). `collect` also prints
`systems without specs (no proposals possible): banking, retail, travel.` The full approve-apply-verify-commit path, with the
real verification, is one `slow` test (skipped by default, see step vi; `--runslow` enables it and `-k end_to_end` selects just it):

```powershell
.venv\Scripts\python -m pytest tests\learning\test_apply.py --runslow -q -k end_to_end
```

Expected: 1 passed (about 6-20 s). The same flow with the verification stubbed runs in the normal suite.

## v. Add a new write tool (safety metadata)

The write-safety metadata is `side_effect` (`read|write|money`), `idempotent` (true/false), `reversible` (true/false: can the
system itself undo one call?), `scope` (`single|batch|external`: the blast radius of one call) and `review.safety_fields`
(`reviewed|unreviewed`) plus `review.reviewer` (a named person). The orchestrator refuses to run any write tool unless the run was
started with `allow_writes=True` or an approver said yes (`python demo.py --offline` stops at a pending-approval record; add
`--allow-writes` to let it book), and it refuses a write tool that did not declare `reversible` and `scope`.
Append this operation to the END of `specs\mycat.yaml` (its last section is `operations:`; the snippet continues that list) and generate:

```powershell
@'
- name: submit_fact
  summary: Submit a new cat fact.
  when_to_call: Call when the user wants to contribute a fact; not repeatable, a second call creates a duplicate.
  side_effect: write
  idempotent: false
  reversible: false
  scope: single
  request:
    method: POST
    path: /facts
  params:
  - name: text
    type: string
    in: body
    required: true
    description: The fact text.
  response:
    type: object
    description: The created fact.
  examples:
  - name: created
    args: {text: Cats sleep a lot}
    mock:
      status: 201
      body: {id: 1}
    expect: ok
  review:
    source: human
    safety_fields: unreviewed
'@ | Add-Content -Encoding utf8 specs\mycat.yaml
.venv\Scripts\python -m generators.from_spec specs\mycat.yaml
```

Expected, exit code 2: `REFUSED. side_effect=write operations need review.safety_fields: reviewed ... signed off by a human`. That refusal
is the safety layer. Now sign it off (a real person does this after reading the operation; here any name works, the generator checks that one
is present, not who it is), regenerate, and test:

```powershell
(Get-Content specs\mycat.yaml -Raw) -replace '(source: human\s+safety_fields: )unreviewed','${1}reviewed' | Set-Content -Encoding utf8 specs\mycat.yaml
Add-Content -Encoding utf8 specs\mycat.yaml "    reviewer: Your Name"
.venv\Scripts\python -m generators.from_spec specs\mycat.yaml
.venv\Scripts\python -m pytest tests\test_contract.py -q -k mycat
```

Expected: two `wrote:` lines, then `14 passed`. (Leave out `reversible` or `scope` from the snippet and the generator refuses with
`must state reversible and scope ...`; that is the write policy's metadata rule, checked at generation time.) ALWAYS regenerate after editing a spec and before running the contract test: the test reads the
new spec but the committed server is stale, and fails with `submit_fact:ok` until you do. Ingestion never signs off a write: ingested specs
(`pipeline.py`) flag every write `needs_review: true` and the generator refuses it until a named person clears it.

## vi. The full suite, the 26 skips, and warnings vs `--strict-budgets`

```powershell
.venv\Scripts\python -m pytest -q -rs         # -rs prints every skip with its reason
```

The 26 skips are all deliberate and each is listed with its reason in `AUDIT.md` section 3b: 14 need `LIVE_TESTS=1` (real
internet), 7 need `ANTHROPIC_API_KEY` (a real model) (`.venv\Scripts\python scripts\live_judgment.py --planner --dry-run` shows what closing those would run, without a key), 2 are `slow` tests (`--runslow`: the end-to-end apply test and the audit's
run-every-cited-test), 1 is "retail: no outage probe", and 2 are probes started only by another test.

Counts change as you add tests: after steps i-v the suite shows about `1335 passed, 31 skipped` (the practice system adds tests and 4 live
skips) and `2 failed`: `test_every_discovered_file_is_audited_exactly_once` and `test_a_server_added_without_updating_audit_md_fails_by_name` in
`tests/test_audit_completeness.py`, which is expected until `AUDIT.md` lists `mycat` (step viii).

A full run takes 80-170 s; many shells move a command that runs longer than about 3 minutes to the background. Wait for it, do not start another command meanwhile (see step ix).

Some tests have a time budget sized for a quiet machine. Two modes:

- `.venv\Scripts\python -m pytest -q` (default): an overrun is a WARNING (section "budget overrun warnings"), exit code 0, and a line is
  appended to `.perf\overruns.jsonl`. Use this locally. If you see one, re-run on a quiet machine; if it persists it is a real regression.
- `.venv\Scripts\python -m pytest -q --strict-budgets`: the same overrun FAILS the test by name (`GATE TEST TOO SLOW`). Use this in CI.

## vii. Re-check the budgets on a new host

```powershell
.venv\Scripts\python scripts\measure_budgets.py --verify                 # one strict suite run (about 80-170 s, silent until it ends); headroom per budgeted test
.venv\Scripts\python scripts\measure_budgets.py --verify --allow-loaded  # same, with a 3x allowance for a busy machine
.venv\Scripts\python scripts\measure_budgets.py --runs 5                 # re-derive budgets (5 full suite runs, about 10-15 minutes)
```

Run it BEFORE step i or AFTER the Finish step: while the practice files exist the two audit tests fail and the script exits 1. Run it alone, on an otherwise idle machine, and give it a few minutes: it runs the whole suite and prints nothing until the run ends.
A strict run can also report one `ERROR` for a test that is only just over a plain 2 s default budget (for example 2.08 s); re-run once, and see `AUDIT.md` section 3d for why that happens. Expected end: `No timed test is above 75% of its budget.` (it printed that, `suite exit code 0`, in about 80 s on the reference laptop.) The
script never edits a file; it prints a table. `--verify` can fail on a slow laptop: that means the budgets (sized on
another machine) do not fit this host. Compare against `scripts\budget_baseline.json`, use `--allow-loaded`, and if the
slowdown is consistent on a quiet machine, re-derive with `--runs` and update the budgets by hand (CLAUDE.md D17).

## viii. The audit-accuracy test fails

```powershell
.venv\Scripts\python -m pytest tests\test_audit_accuracy.py tests\test_audit_completeness.py -q
```

- `AUDIT.md PROVEN claim C## ... names <test id> which no longer exists`: someone renamed or deleted a test that `AUDIT.md`
  cites. Find the new name (`.venv\Scripts\python -m pytest --collect-only -q | Select-String <part of the name>`) and fix the id in
  `AUDIT.md` section 2 (both the Tests and Command cells), or, if the test was deleted on purpose, move the claim to
  section 3 with the reason. Never delete the claim row to make the test pass.
- `UN-AUDITED: servers\x_server.py`: a server, spec, or `learning/`, `ingest/`, `scripts/` module has no row. Add it to
  `Files covered` in section 2 (with the test that proves it) or to `Files with NO passing test` in section 3a. Never both.
  (If you did step i on your practice branch, expect exactly this failure for `servers/mycat_server.py` and `specs/mycat.yaml`.
  That is the test working.)
- Run every cited test for real: `.venv\Scripts\python -m pytest tests\test_audit_accuracy.py --runslow -q` (several minutes).

## ix. A run says `INVALID RUN (clock event):`

The closing line starts `INVALID RUN (clock event): N environmental interruption(s), run_id=...` instead of the pass count.
The process or the machine was paused (laptop sleep, VM pause) during a test, so durations are meaningless. The runner turned
the affected test into a SKIP naming the reason, did not record the run in `.perf\history.json`, and exited non-zero on purpose
so it can never look green. Running two test suites at the same time can cause one (it did, in the cold-start check of this page). A long silence (several minutes with no output, or a shell that moved your command to the background) is the usual sign; one cold-start check saw a 424 s pause this way. Do nothing clever: stop other work, and re-run. If the same test fails again on a
settled machine, it is a real failure. A run like this is excluded by `scripts\measure_budgets.py`.

## Finish: throw the practice away

Remove only what you created, then leave the branch. (Do not use `git checkout -- .` or `git reset --hard`: they would also
discard any unrelated change you have made.)

```powershell
Remove-Item specs\mycat.yaml, servers\mycat_server.py, tests\test_mycat.py -ErrorAction SilentlyContinue
git switch main
git branch -D onboarding-practice
git status --short
```

`git status --short` should show no `mycat` files. Step iv's `learning.collect` writes `learning\failures.jsonl`; it is git-ignored and harmless, and is rewritten on the next run.
