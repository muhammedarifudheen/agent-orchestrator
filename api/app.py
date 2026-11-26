"""A minimal API serving ONLY the three AUDIT.md section 2 flows that the offline demo (demo/app.py) already proves (CLAUDE.md D38):

    POST /v1/duty-match   {intent}                -> which duty would run, its confidence, the write it stops before
    POST /v1/write-check  {tool, allow_writes}    -> is it a write, would the write policy allow it, why not
    POST /v1/propose      {handoff}               -> the proposal the real learning loop makes for a synthetic failure; never applied

No planner, no document extraction, no live upstream, no model: nothing here is credit-metered and nothing leaves the process. Every
endpoint calls the demo's own functions (`match_duty`, `write_safety`, `learning_proposal`), loaded from demo/app.py: they are not
reimplemented. The service refuses to start when a model key is set, so a deployer cannot switch a live path on by accident.

There is NO authentication and NO rate limiting here (the flows are deterministic and public by design); put a proxy that rate-limits in
front of it before exposing it. The three functions are synchronous and one of them runs an event loop of its own, so the endpoints are
plain `def` (FastAPI runs them in a worker thread)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent  # api/ sits next to demo/, agent/, learning/, servers/, specs/ and duties/
MAX_BODY_BYTES = 64 * 1024
_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PROVIDER_API_KEY")  # any of these could switch a live planner on
RECURRENCE = 2  # the demo's default: the handoff is treated as observed twice, the recurrence the proposer needs (learning.propose.MIN_RECURRENCE)


def assert_flows_only(env=None) -> None:
    """Refuse to start if a model key is present. An empty value is not a key."""
    env = os.environ if env is None else env
    for name in _KEY_VARS:
        if (env.get(name) or "").strip():
            raise SystemExit(f"this service serves AUDIT §2 flows only; {name} must be unset.")


def _demo():
    """demo/app.py by path (the repo root also holds a demo.py, so `import demo` would be the wrong module). Imports nothing network-capable."""
    name = "api_demo_flows"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "demo" / "app.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


class DutyMatchIn(BaseModel):
    intent: str = Field(max_length=500)


class DutyMatchOut(BaseModel):
    duty: str | None
    confidence: float
    write_boundary: str | None


class WriteCheckIn(BaseModel):
    tool: str = Field(max_length=100)
    allow_writes: bool = False


class WriteCheckOut(BaseModel):
    is_write: bool
    allowed: bool
    refusal_code: str | None
    pending_approval: dict[str, Any] | None


class ProposeIn(BaseModel):
    handoff: dict[str, Any]


class ProposeOut(BaseModel):
    proposal_type: str
    diff: str
    action_md: str | None
    applied: bool
    specs_written: bool


def create_app() -> FastAPI:
    assert_flows_only()  # first, before anything is built
    demo = _demo()
    # the three automatic documentation routes are off: the service has exactly three endpoints
    app = FastAPI(title="AUDIT §2 flows", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def cap_body(request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"detail": f"the request body is limited to {MAX_BODY_BYTES} bytes"}, status_code=413)
        return await call_next(request)

    @app.post("/v1/duty-match", response_model=DutyMatchOut)
    def duty_match(body: DutyMatchIn) -> DutyMatchOut:
        """`duty` is the duty that WOULD run (the real matcher at its real threshold), else null; `confidence` is the best candidate's, 0.0 if
        there is none; `write_boundary` is the tool of the write the duty stops before, null when no duty would run. Nothing is executed."""
        r = demo.match_duty(body.intent)
        matched = bool(r["matched"])
        return DutyMatchOut(duty=r["duty"] if matched else None, confidence=float(r["confidence"] or 0.0),
                            write_boundary=(r["write_boundary"] or {}).get("tool") if matched else None)

    @app.post("/v1/write-check", response_model=WriteCheckOut)
    def write_check(body: WriteCheckIn) -> WriteCheckOut:
        """The real write policy on the safety metadata the real servers publish. `allowed` is what the policy says for `allow_writes`; when it
        is false, `refusal_code` and the real pending-approval record say why. Nothing is executed."""
        try:
            r = demo.write_safety(body.tool)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"tool must be one of {list(demo.WRITE_TOOLS)}") from None
        allowed = bool(r["if_allow_writes_true"]["allowed"] if body.allow_writes else r["decision"]["allowed"])
        return WriteCheckOut(is_write=bool(r["is_write"]), allowed=allowed, refusal_code=None if allowed else r["decision"]["code"],
                             pending_approval=None if allowed else r["pending_approval"])

    @app.post("/v1/propose", response_model=ProposeOut)
    def propose(body: ProposeIn) -> ProposeOut:
        """The real learning/collect.py and learning/propose.py on the handoff, treated as observed twice, in a scratch directory. `proposal_type`
        is "none" when the proposer makes none (a system with no spec, say). The proposal is never applied and no spec is written."""
        r = demo.learning_proposal(json.dumps(body.handoff), RECURRENCE)
        if not r["ok"]:
            raise HTTPException(status_code=422, detail=r["error"])
        proposals = r["proposals"]
        if not proposals:
            return ProposeOut(proposal_type="none", diff="", action_md=None, applied=False, specs_written=False)
        return ProposeOut(proposal_type=proposals[0]["kind"], diff=proposals[0]["content"],
                          action_md=r["actions"][0]["content"] if r["actions"] else None, applied=False, specs_written=False)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
