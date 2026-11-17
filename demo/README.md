---
title: Agent orchestrator offline demo
emoji: 🧭
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: 6.32.0
app_file: app.py
pinned: false
---

> This demo shows the flows AUDIT.md §2 marks as proven. It does not show live-model planner judgment (C24), real doc extraction (C25), or real upstream apply (C27) — all open in §3a.

# Agent orchestrator: offline demo of proven flows

## What it is

A small Gradio app that runs three flows of the agent orchestrator locally, using the real
repository code and no model. Every claim it rests on is a row of AUDIT.md section 2 (PROVEN).

## The three tabs

| Tab | Rests on (AUDIT.md §2) | Real in the demo | Not shown |
|---|---|---|---|
| Duty matching | C33, C34 | The real duty matcher decides whether an intent fits a duty (exact first, then scored, against the recorded threshold; near-equal duties are ambiguous) and lists the steps and the write boundary. | Any planner or model run. An unmatched intent is only reported as going to the planner; the planner is not called (C24 is open). |
| Write-safety refusal | C44, C12 | The real tool safety metadata and the real approval record: a write tool is refused without approval and a pending approval record is produced. | No tool is ever executed. Nothing is booked or transferred. |
| Learning-loop proposal | C05 | The real proposer turns a sample failed-call handoff into a candidate spec patch (or none when the system has no spec, or the failure has not recurred), and for a failure the spec already declares a needs-review patch with its action file. | Nothing is applied and no spec is written; a human must approve and apply. No real upstream failure is involved (C27 is open). |

## What it does NOT do

- No model is called and no network is used.
- It never writes to `specs/`, `servers/` or `handoffs/` (or `learning/`, `proposals/`).
- It refuses to start if `ANTHROPIC_API_KEY`, `PROVIDER_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is set.
- It does not demonstrate live-model planner judgment, real doc extraction or a real upstream apply.

## Run locally

From the repository root:

```
pip install -r demo/requirements.txt
python demo/app.py
```

## Publishing (a human step)

Nothing in this repository pushes anything. To publish:

1. Create a new Space on Hugging Face with SDK **Gradio**.
2. The Space root must contain `app.py` (the contents of `demo/`) AND the folders the app imports, copied beside it: `agent/`, `servers/`, `learning/`, `generators/`, `duties/`, `specs/`.
3. Never add an `ANTHROPIC_API_KEY` secret (or any provider key) to the Space: the app refuses to start with one.
4. The banner refers to `AUDIT.md`, which is not part of the Space. Add a link to the source repository that holds it to the Space description, so a visitor can check the claims.
5. `sdk_version: 6.32.0` (and the pin in `requirements.txt`) is the Gradio version the app was smoke-tested with. If the Space does not offer that version, change both together and re-test; do not leave them different.

Copy commands, run from the repository root, with the Space checkout path set first. Start from an EMPTY Space checkout (the commands copy folders whole, so a second run would nest them), and do not copy any `__pycache__` folders.

PowerShell:

```
$SPACE = "C:\path\to\your-space"
Copy-Item demo\* $SPACE -Recurse -Force
foreach ($d in "agent","servers","learning","generators","duties","specs") { Copy-Item $d (Join-Path $SPACE $d) -Recurse -Force }
```

bash:

```
SPACE=/path/to/your-space
cp -r demo/. "$SPACE"/
for d in agent servers learning generators duties specs; do cp -r "$d" "$SPACE/$d"; done
```

Then, as a documented example for the human to run in the Space checkout:

```
cd "$SPACE"
git add .
git commit -m "Offline demo of proven flows"
git push
```
