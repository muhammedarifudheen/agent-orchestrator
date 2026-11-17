"""Offline Gradio demo of the flows AUDIT.md section 2 marks as proven.

No model call, no upstream call, no network. Every flow runs the repo's real library code in-process and writes only to
temporary directories. Works as `python demo/app.py` in the repo and from the root of a Space that holds agent/,
servers/, learning/, generators/, duties/ and specs/ next to this file.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE if (HERE / "agent").is_dir() else HERE.parent
for _p in (str(ROOT / "servers"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

BANNER = ("This demo shows the flows AUDIT.md §2 marks as proven. It does not show live-model planner judgment (C24), "
          "real doc extraction (C25), or real upstream apply (C27) — all open in §3a.")
APP_TITLE = "Agent orchestrator: offline demo of proven flows. " + BANNER
CLAIMS = {"duty": ["C33", "C34"], "write": ["C44", "C12"], "learning": ["C05"]}
WRITE_TOOLS = ("search_flights", "book_flight", "transfer_funds")

MAX_INTENT = 500
MAX_HANDOFF_CHARS = 20000
_QUALIFIED = {"search_flights": ("travel", "travel__search_flights", "travel_server.py"),
              "book_flight": ("travel", "travel__book_flight", "travel_server.py"),
              "transfer_funds": ("banking", "banking__transfer_funds", "banking_server.py")}
_ILLUSTRATIVE_ARGS = {
    "search_flights": {"origin": "DEL", "destination": "BOM", "date": "2027-01-15"},
    "book_flight": {"flight_id": "DEMO-FLIGHT-1", "passenger_name": "Asha Verma", "passenger_email": "asha@example.com"},
    "transfer_funds": {"from_acct": "ACCT-DEMO-1", "to_acct": "ACCT-DEMO-2", "amount": "10.00"},
}
_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]*__[a-z][a-z0-9_]*$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

# ---- safety: offline-only by design ---------------------------------------------------------------------------------

_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PROVIDER_API_KEY")  # any of these could switch a live planner on
PROTECTED_DIRS = ("specs", "servers", "handoffs", "learning", "proposals")  # the demo never writes under these


def assert_offline_only(env=None) -> None:
    """Refuse to run if a model key is present, so a deployer cannot accidentally turn the live planner on. An empty value
    is not a key. Runs before gradio is imported, so it also refuses on a machine that cannot render the app."""
    env = os.environ if env is None else env
    for name in _KEY_VARS:
        if (env.get(name) or "").strip():
            raise SystemExit(f"This demo is offline-only; {name} must be unset.")


def _assert_not_protected(path) -> None:
    resolved = Path(path).resolve()
    for name in PROTECTED_DIRS:
        root = (ROOT / name).resolve()
        if resolved == root or root in resolved.parents:
            raise PermissionError(f"the demo never writes under {name}/ (asked for {resolved})")


@contextlib.contextmanager
def _scratch_dir():
    """A throwaway directory, created only after checking that the temp location is not one of the protected directories."""
    _assert_not_protected(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix="offline_demo_") as tmp:
        _assert_not_protected(tmp)
        yield Path(tmp)


# ---- Tab 1: duty matching -------------------------------------------------------------------------------------------

def match_duty(intent: str) -> dict:
    """Mirror agent/orchestrator.py `_try_duty` on the real duty library. No model, no tool call."""
    from agent.duties import DUTY_CONFIDENCE_THRESHOLD, DutyLibrary

    text = (intent or "").strip()[:MAX_INTENT]
    out = {"intent": text, "matched": False, "duty": None, "confidence": None, "kind": None,
           "threshold": DUTY_CONFIDENCE_THRESHOLD, "ambiguous": False, "outcome": "no_match", "captures": {},
           "write_boundary": None, "steps": [], "decision": "", "model_consulted": False}
    planner = ("In a real run a model-based planner would take over; this demo does not show that path.")
    if not text:
        out["decision"] = "Empty request, so no duty fits. " + planner
        return out
    match, ambiguous = DutyLibrary(ROOT / "duties").best_match(text)
    if match is None:
        out["decision"] = "No duty pattern fits this request. " + planner
        return out
    duty = match.duty
    out.update(duty=duty.name, confidence=match.confidence, kind=match.kind, ambiguous=bool(ambiguous),
               captures=dict(match.captures), steps=[{"id": s.id, "tool": s.tool} for s in duty.steps])
    boundary = next((s for s in duty.steps if s.id == duty.boundary_step), None)
    if boundary is not None:
        out["write_boundary"] = {"step": boundary.id, "tool": boundary.tool}
    if ambiguous:
        out["outcome"] = "ambiguous"
        out["decision"] = (f"'{duty.name}' ({match.confidence:.2f}) is not clearly the best fit, so no duty runs. " + planner)
    elif match.confidence < DUTY_CONFIDENCE_THRESHOLD:
        out["outcome"] = "below_threshold"
        out["decision"] = (f"Closest duty is '{duty.name}' at {match.confidence:.2f}, below the "
                           f"{DUTY_CONFIDENCE_THRESHOLD} threshold, so no duty runs. " + planner)
    else:
        out["matched"], out["outcome"] = True, "matched"
        out["decision"] = (f"A duty would run: '{duty.name}' ({match.kind} match, confidence {match.confidence:.2f} >= "
                           f"{DUTY_CONFIDENCE_THRESHOLD}), no model consulted. Nothing was executed here; its first write "
                           "step is the boundary shown.")
    return out


# ---- Tab 2: write-safety refusal ------------------------------------------------------------------------------------

def _registry_class():
    from agent.tool_registry import ToolRegistry

    class _InProcessRegistry(ToolRegistry):
        """Runs the real MCP servers in-process (no subprocess, no network) just to read their published metadata."""

        async def _connect(self, name: str, script: Path) -> None:
            from mcp import Client

            module = importlib.import_module(script.stem)
            client = await self._stack.enter_async_context(Client(module.mcp))
            self._sessions[name] = client
            for tool in (await client.list_tools()).tools:
                self._tools[f"{name}__{tool.name}"] = {
                    "name": f"{name}__{tool.name}", "description": tool.description or "",
                    "input_schema": tool.input_schema}
                self._record_annotations(f"{name}__{tool.name}", tool)

    return _InProcessRegistry


async def _write_safety(tool: str) -> dict:
    from agent import approvals
    from agent.write_policy import WritePolicy, WriteProposal, classify

    server, qualified, pattern = _QUALIFIED[tool]
    args = dict(_ILLUSTRATIVE_ARGS[tool])
    async with _registry_class()(servers_dir=ROOT / "servers", pattern=pattern) as registry:
        if qualified not in registry._tools:
            raise RuntimeError(f"{qualified} was not published by its server: {registry.connect_errors}")
        safety = classify(registry, qualified)  # metadata only; registry.call is never used
    goal = f"Illustrative request that would call {qualified}"
    proposal = WriteProposal(qualified, args, safety, goal, [], None)
    decision = await WritePolicy().decide(proposal)
    allowed = await WritePolicy(allow_writes=True).decide(proposal)
    pending = None
    if not decision.allowed and decision.mode == "pending":
        with _scratch_dir() as scratch:
            path = approvals.create(scratch, user="demo", goal=goal, prior_steps=[], duty=None, write_boundary=None,
                                    writes=[{"tool": qualified, "arguments": args, "cost_summary": proposal.cost_summary(),
                                             "reason": decision.reason}])
            pending = json.loads(path.read_text(encoding="utf-8"))
    return {"tool": qualified, "server": server, "safety": safety.summary(), "is_write": safety.writes is not False,
            "allow_writes_required": not decision.allowed,
            "decision": {"allowed": decision.allowed, "mode": decision.mode, "code": decision.code,
                         "reason": decision.reason},
            "if_allow_writes_true": {"allowed": allowed.allowed, "mode": allowed.mode},
            "pending_approval": pending, "executed": False, "illustrative_arguments": args,
            "note": "Nothing was executed; arguments are illustrative."}


def write_safety(tool: str) -> dict:
    if tool not in WRITE_TOOLS:
        raise ValueError(f"tool must be one of {list(WRITE_TOOLS)}, got {tool!r}")
    return asyncio.run(_write_safety(tool))


# ---- Tab 3: learning-loop proposal ----------------------------------------------------------------------------------

_NOTE3 = ("Not applied; human approval required.  This is a proposal from the real learning/propose.py run on a "
          "synthetic handoff.")


def _bad(error: str, times: int | None = None) -> dict:
    return {"ok": False, "error": error, "times_observed": times, "failure_record": None, "proposals": [],
            "actions": [], "report": [], "applied": False, "specs_written": False, "note": _NOTE3}


def learning_proposal(handoff_json: str, times_observed: int = 2) -> dict:
    from learning import collect as collect_mod
    from learning import patchfmt
    from learning import propose as propose_mod

    try:
        times = int(times_observed)
    except (TypeError, ValueError):
        return _bad("times_observed must be an integer from 1 to 5")
    if not 1 <= times <= 5:
        return _bad("times_observed must be from 1 to 5", times)
    if not isinstance(handoff_json, str) or len(handoff_json) > MAX_HANDOFF_CHARS:
        return _bad(f"handoff text must be a string of at most {MAX_HANDOFF_CHARS} characters", times)
    try:
        data = json.loads(handoff_json)
    except ValueError as exc:
        return _bad(f"not valid JSON: {exc}", times)
    if not isinstance(data, dict):
        return _bad("the handoff must be a JSON object", times)
    snap = data.get("error_snapshot")
    if not isinstance(snap, dict):
        return _bad("error_snapshot must be an object", times)
    tool, code = snap.get("tool"), snap.get("error_code")
    if not isinstance(tool, str) or not _TOOL_RE.fullmatch(tool):
        return _bad("error_snapshot.tool must look like system__operation (lowercase letters, digits, underscores)", times)
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        return _bad("error_snapshot.error_code must be UPPER_SNAKE_CASE (2 to 64 characters)", times)
    base = None
    created = data.get("created_at")
    if created is None:
        base = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        try:
            base = datetime.fromisoformat(str(created))
        except ValueError:
            return _bad("created_at must be an ISO timestamp", times)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)

    with _scratch_dir() as tmp_path:
        handoffs, specs, out_dir = tmp_path / "handoffs", tmp_path / "specs", tmp_path / "proposals"
        handoffs.mkdir()
        for i in range(times):
            copy = dict(data)
            copy["created_at"] = (base + timedelta(minutes=i)).isoformat(timespec="seconds")
            (handoffs / f"demo_handoff_{i}.json").write_text(json.dumps(copy), encoding="utf-8")
        shutil.copytree(ROOT / "specs", specs)
        failures = tmp_path / "failures.jsonl"
        records = collect_mod.collect(handoffs, failures)
        report: list[str] = []
        written = propose_mod.propose(failures, specs, out_dir, report)
        report.append(collect_mod.coverage_line(ROOT / "servers", ROOT / "specs"))
        proposals, actions = [], []
        for path in sorted(out_dir.glob("*.patch")) if out_dir.is_dir() else []:
            text = path.read_text(encoding="utf-8")
            header, _ = patchfmt.parse_proposal(text)
            proposals.append({"file": path.name, "kind": header["proposal"], "content": text})
        for path in sorted(out_dir.glob("*.action.md")) if out_dir.is_dir() else []:
            actions.append({"file": path.name, "content": path.read_text(encoding="utf-8")})
    # tmp paths in the report lines mean nothing outside this run: keep only the file names
    report = [re.sub(r"(?:[A-Za-z]:)?[^\s>]*[\\/](?=[^\\/\s]+\.(?:patch|action\.md))", "", ln) for ln in report]
    del written
    return {"ok": True, "error": None, "times_observed": times, "failure_record": records[0] if records else None,
            "proposals": proposals, "actions": actions, "report": report, "applied": False, "specs_written": False,
            "note": _NOTE3}


def _tab3(handoff_json: str, times_observed: int = 2):
    result = learning_proposal(handoff_json, times_observed)
    summary = dict(result)
    summary["proposals"] = [{"file": p["file"], "kind": p["kind"]} for p in result["proposals"]]
    summary["actions"] = [{"file": a["file"]} for a in result["actions"]]
    patch = result["proposals"][0]["content"] if result["proposals"] else ""
    action = result["actions"][0]["content"] if result["actions"] else ""
    return summary, patch, action


# ---- fixtures -------------------------------------------------------------------------------------------------------

def fixture_names() -> list[str]:
    return sorted(p.stem for p in (HERE / "fixtures").glob("*.json"))


def load_fixture(name: str) -> str:
    if name not in fixture_names():
        raise ValueError(f"unknown fixture {name!r}")
    return (HERE / "fixtures" / f"{name}.json").read_text(encoding="utf-8")


# ---- UI -------------------------------------------------------------------------------------------------------------

def _claims(key: str) -> str:
    return f"Rests on AUDIT.md §2 claims {', '.join(CLAIMS[key])}."


def build_app():
    assert_offline_only()
    import gradio as gr

    names = fixture_names()
    first = names[0] if names else None
    with gr.Blocks(title=APP_TITLE) as demo:
        gr.Markdown(f"**{BANNER}**")
        with gr.Tab("Duty matching"):
            gr.Markdown(_claims("duty") + " Real: the repo's duty library and its confidence threshold, applied the way "
                        "the orchestrator applies them. Not shown: any model, any tool call, the planner path.")
            t1 = gr.Textbox(label="Request", value="refund order ORD-ABCD1234")
            b1 = gr.Button("Match")
            o1 = gr.JSON(label="Result")
            b1.click(fn=match_duty, inputs=[t1], outputs=[o1])
        with gr.Tab("Write-safety refusal"):
            gr.Markdown(_claims("write") + " Real: the write policy and the safety metadata the servers publish, read "
                        "in-process. Not shown: execution; nothing is ever called and the arguments are illustrative.")
            d2 = gr.Dropdown(choices=list(WRITE_TOOLS), value="book_flight", label="Tool")
            b2 = gr.Button("Check write policy")
            o2 = gr.JSON(label="Result")
            b2.click(fn=write_safety, inputs=[d2], outputs=[o2])
        with gr.Tab("Learning-loop proposal"):
            gr.Markdown(_claims("learning") + " Real: learning/collect.py and learning/propose.py run on a synthetic "
                        "handoff in a temporary directory. Not shown: applying a proposal; nothing is written to specs.")
            d3 = gr.Dropdown(choices=names, value=first, label="Fixture handoff")
            t3 = gr.Textbox(lines=14, label="Handoff JSON", value=load_fixture(first) if first else "")
            n3 = gr.Number(value=2, precision=0, minimum=1, maximum=5, label="Times observed")
            b3 = gr.Button("Propose")
            o3 = gr.JSON(label="Summary")
            c3 = gr.Code(label="Proposal patch")
            a3 = gr.Code(label="Action file")
            d3.change(fn=load_fixture, inputs=[d3], outputs=[t3])
            b3.click(fn=_tab3, inputs=[t3, n3], outputs=[o3, c3, a3])
    return demo


def main() -> None:
    assert_offline_only()
    build_app().launch()


if __name__ == "__main__":
    main()
