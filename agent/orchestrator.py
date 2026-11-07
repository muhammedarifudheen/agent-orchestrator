"""Agent loop: Claude decides tool calls, results are fed back, failures route to a human.

This module is industry-agnostic. Do not add domain-specific logic here (see CLAUDE.md).
"""
from __future__ import annotations

import getpass
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent.tool_registry import ToolRegistry

DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5-5")

SYSTEM_PROMPT = """You are an autonomous business-process agent. Achieve the user's goal using \
only the provided tools; you have no other way to act on external systems.
Today's date is {today}. Resolve relative dates ("tomorrow") against it.
Rules:
- Read each tool's description carefully, including argument formats and error codes.
- Plan, then call tools in a sensible order, using earlier results as inputs to later calls.
- A failed tool call says `recovery=agent` or `recovery=human`. With `recovery=agent` you get ONE \
chance to fix it (correct the arguments, look an id up again, or retry once if it says retryable=true); \
a second failure of the same tool ends the run. With `recovery=human` stop calling tools and say \
plainly what needs a person. Do not repeat an identical failing call. Never repeat an action that \
already succeeded (especially one with side effects).
- If information that only the user can provide is missing and cannot be inferred from the \
request, use sensible stated assumptions where harmless; otherwise say what is missing.
- If you cannot make progress, stop and say plainly what failed. Never claim success you did \
not observe in a tool result.
- When done, give a short summary of what was actually completed."""


@dataclass
class Operation:
    """One attempted tool call (kept for the decision log and the handoff snapshot)."""

    step: int
    tool: str
    arguments: dict[str, Any]
    ok: bool
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    recovery: str | None = None  # "agent" | "human" | None (unknown format: treated as human)
    duration_ms: int = 0
    thought: str = ""  # Claude's text in the same turn, before this call


@dataclass
class ExecutionState:
    """Everything known about the run so far. Source of truth for the handoff."""

    goal: str
    operations: list[Operation] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    consecutive_failures: int = 0  # failures in a row of any kind (reset by any success)
    consecutive_retryable: int = 0  # retryable=true failures in a row (reset by success or other outcome)
    agent_failures: dict[str, int] = field(default_factory=dict)  # tool -> recovery=agent failures since its last success
    steps: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)  # calls not run because of a handoff

    def note(self, line: str) -> None:
        self.log.append(line)

    @property
    def completed(self) -> list[Operation]:
        return [o for o in self.operations if o.ok]

    @property
    def failed(self) -> list[Operation]:
        return [o for o in self.operations if not o.ok]


@dataclass
class Handoff:
    """Complete context handed to a human when automation cannot finish the goal."""

    reason: str
    goal: str
    user: str
    created_at: str
    attempted_operations: list[dict[str, Any]]
    error_codes: list[str]
    error_snapshot: dict[str, Any] | None  # the failure that fired the handoff (None for non-tool causes)
    current_state: dict[str, Any]
    agent_last_message: str
    steps: list[dict[str, Any]]  # structured decision log: thought, tool, args, result per call
    decision_log: list[str]  # the same run as human-readable lines

    def to_text(self) -> str:
        lines = ["=" * 70, "HUMAN HANDOFF REQUIRED", "=" * 70,
                 f"Reason : {self.reason}", f"Goal   : {self.goal}", f"User   : {self.user}", f"Time   : {self.created_at}",
                 "", f"Attempted operations ({len(self.attempted_operations)}):"]
        for o in self.attempted_operations:
            status = "OK  " if o["ok"] else f"FAIL[{o['error_code']}, retryable={o['retryable']}, recovery={o['recovery']}]"
            lines.append(f"  #{o['step']} {status} {o['tool']} {json.dumps(o['arguments'], default=str)}")
            if not o["ok"]:
                lines.append(f"       -> {o['error_message']}")
        lines += ["", f"Error codes seen: {', '.join(self.error_codes) or '(none)'}",
                  "", "Current state (side effects already applied - do NOT redo these):"]
        done = self.current_state["completed_operations"]
        if done:
            for o in done:
                lines.append(f"  {o['tool']} -> {json.dumps(o['result'], default=str)}")
        else:
            lines.append("  (nothing completed)")
        lines += ["", f"Agent's last message: {self.agent_last_message or '(none)'}", "=" * 70]
        return "\n".join(lines)


@dataclass
class RunResult:
    success: bool
    final_message: str
    state: ExecutionState
    handoff: Handoff | None = None


class Orchestrator:
    """Runs one goal to completion or to a human handoff."""

    def __init__(
        self,
        registry: ToolRegistry,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        max_steps: int = 15,
        max_consecutive_failures: int = 2,
        handoff_dir: Path | None = None,
        user_id: str | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic  # reads ANTHROPIC_API_KEY

            client = AsyncAnthropic()
        self.registry, self.client, self.model = registry, client, model
        self.max_steps, self.max_consecutive_failures = max_steps, max_consecutive_failures
        self.handoff_dir = handoff_dir
        self.user_id = re.sub(r"[^A-Za-z0-9_.-]", "_", user_id or getpass.getuser()) or "unknown"
        self._on_log = on_log or (lambda _l: None)

    async def run(self, goal: str) -> RunResult:
        state = ExecutionState(goal=goal)
        self._log(state, f"GOAL: {goal}")
        tools = self.registry.anthropic_tools()
        self._tool_defs = {t["name"]: t for t in tools}
        self._log(state, f"TOOLS AVAILABLE: {', '.join(t['name'] for t in tools) or '(none)'}")
        if not tools:
            return self._escalate(state, "No tools available (all MCP servers failed to connect: "
                                  f"{self.registry.connect_errors}).", "")

        system = SYSTEM_PROMPT.format(today=date.today().isoformat())
        messages: list[dict[str, Any]] = [{"role": "user", "content": goal}]
        last_text = ""

        while state.steps < self.max_steps:
            state.steps += 1
            try:
                resp = await self.client.messages.create(
                    model=self.model, max_tokens=4096, system=system, tools=tools, messages=messages
                )
            except Exception as exc:  # LLM unreachable: cannot continue, hand off with context
                return self._escalate(state, f"LLM call failed: {type(exc).__name__}: {exc}", last_text)

            texts = [b.text for b in resp.content if b.type == "text" and b.text.strip()]
            thought = "\n".join(texts)
            if texts:
                last_text = thought
                self._log(state, f"[step {state.steps}] CLAUDE: {last_text}")
            calls = [b for b in resp.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True)
                                                               if hasattr(b, "model_dump") else b
                                                               for b in resp.content]})

            if not calls:
                if state.operations and not state.operations[-1].ok:
                    return self._escalate(state, "Agent stopped while its last tool call was "
                                          "still failing (unrecovered error).", last_text)
                self._log(state, "DONE")
                return RunResult(True, last_text, state)

            results = []
            for i, call in enumerate(calls):
                args = dict(call.input or {})
                self._log(state, f"[step {state.steps}] CALL {call.name}({json.dumps(args, default=str)})")
                out = await self.registry.call(call.name, args)
                op = Operation(state.steps, call.name, args, out.ok, out.data if out.ok else None,
                               out.error_code, out.error_message, out.retryable, out.recovery, out.duration_ms, thought)
                state.operations.append(op)
                state.consecutive_failures = 0 if out.ok else state.consecutive_failures + 1
                self._track(state, op)
                self._log(state, f"[step {state.steps}] RESULT {call.name}: "
                          + (out.to_llm_text()[:500] if out.ok else out.to_llm_text()))
                reason = self._handoff_reason(state, op)
                if reason:  # stop NOW: skip the rest of this turn's calls, never call the LLM again
                    state.skipped = [{"tool": c.name, "arguments": dict(c.input or {})} for c in calls[i + 1:]]
                    results.append({"type": "tool_result", "tool_use_id": call.id,
                                    "content": out.to_llm_text(), "is_error": True})
                    return self._escalate(state, reason, last_text, trigger=op)
                hint = "" if out.ok else self._self_correct_hint(op)
                if hint:
                    self._log(state, f"[step {state.steps}] RE-PROMPT (one self-correction allowed): {hint.strip()[:300]}")
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": out.to_llm_text() + hint, "is_error": not out.ok})
            messages.append({"role": "user", "content": results})

        return self._escalate(state, f"Step limit ({self.max_steps}) reached without completing "
                              "the goal.", last_text)

    # -- exception routing -------------------------------------------------------------------

    @staticmethod
    def _track(state: ExecutionState, op: Operation) -> None:
        """Update the failure counters after every tool call."""
        if op.ok:
            state.consecutive_retryable = 0
            state.agent_failures.pop(op.tool, None)
            return
        state.consecutive_retryable = state.consecutive_retryable + 1 if op.retryable is True else 0
        if op.recovery == "agent":
            state.agent_failures[op.tool] = state.agent_failures.get(op.tool, 0) + 1

    def _handoff_reason(self, state: ExecutionState, op: Operation) -> str | None:
        """Recovery-aware handoff policy, evaluated after every tool call. None = keep going.

        1. recovery=human (or an error that did not follow the contract): hand off at once.
        2. consecutive retryable=true failures >= max_consecutive_failures (default 2): hand off.
        3. recovery=agent: the agent gets ONE self-correction per tool; the second failure of the
           same tool (since that tool last succeeded) hands off.
        """
        if op.ok:
            return None
        where = f"[{op.error_code}] from {op.tool}: {op.error_message}"
        if op.recovery == "human":
            return f"Human required {where} (recovery=human)."
        if op.recovery != "agent":
            return f"Human required {where} (error did not state a recovery path; failing safe)."
        if state.consecutive_retryable >= self.max_consecutive_failures:
            return (f"{state.consecutive_retryable} consecutive retryable failures; "
                    f"automated recovery exhausted. Last: {where}")
        if state.agent_failures.get(op.tool, 0) >= 2:
            return f"Agent could not self-correct {op.tool}: second recovery=agent failure. Last: {where}"
        return None

    def _self_correct_hint(self, op: Operation) -> str:
        """Text appended to the FIRST recovery=agent failure of a tool: the one re-prompt."""
        if op.recovery != "agent":
            return ""
        how = ("This may be transient: you may repeat the same call once."
               if op.retryable else "Correct the arguments (or choose a different approach) and call again once.")
        return (f"\n\nSelf-correction (ONE attempt; a second failure of {op.tool} hands the task to a human). "
                f"{how}\nValid parameters for {op.tool}:\n{self._param_hints(op.tool)}")

    def _param_hints(self, tool: str) -> str:
        """Parameter help for a tool, taken from its own description (Args:) and JSON schema."""
        tool_def = getattr(self, "_tool_defs", {}).get(tool, {})
        schema = tool_def.get("input_schema") or {}
        required = set(schema.get("required") or [])
        summary = "; ".join(f"{n} ({p.get('type', 'any')}, {'required' if n in required else 'optional'})"
                            for n, p in (schema.get("properties") or {}).items()) or "(none)"
        m = re.search(r"(?ms)^\s*Args:\s*\n(.*?)(?:\n\s*\n|\Z)", tool_def.get("description") or "")
        return summary + ("\n" + m.group(1).rstrip() if m else "")

    def _escalate(self, state: ExecutionState, reason: str, last_text: str,
                  trigger: Operation | None = None) -> RunResult:
        """Build the full-context handoff. Never drops information; always surfaces the failure."""
        handoff = Handoff(
            reason=reason, goal=state.goal, user=self.user_id,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            attempted_operations=[asdict(o) for o in state.operations],
            error_codes=sorted({o.error_code for o in state.failed if o.error_code}),
            error_snapshot=None if trigger is None else {
                "step": trigger.step, "tool": trigger.tool, "arguments": trigger.arguments,
                "error_code": trigger.error_code, "error_message": trigger.error_message,
                "retryable": trigger.retryable, "recovery": trigger.recovery,
                "consecutive_failures": state.consecutive_failures,
                "consecutive_retryable": state.consecutive_retryable},
            current_state={
                "steps_taken": state.steps,
                "consecutive_failures": state.consecutive_failures,
                "consecutive_retryable": state.consecutive_retryable,
                "agent_failures": dict(state.agent_failures),
                "completed_operations": [{"tool": o.tool, "arguments": o.arguments, "result": o.result}
                                         for o in state.completed],
                "failed_operations": [{"tool": o.tool, "error_code": o.error_code,
                                       "retryable": o.retryable, "recovery": o.recovery,
                                       "error_message": o.error_message}
                                       for o in state.failed],
                "skipped_operations": state.skipped,
            },
            agent_last_message=last_text,
            steps=[{"step": o.step, "thought": o.thought, "tool": o.tool, "arguments": o.arguments,
                    "ok": o.ok,
                    "result": o.result if o.ok else {"error_code": o.error_code,
                                                     "error_message": o.error_message,
                                                     "retryable": o.retryable, "recovery": o.recovery}}
                   for o in state.operations],
            decision_log=list(state.log),
        )
        self._log(state, f"HANDOFF: {reason}")
        if self.handoff_dir is not None:
            self.handoff_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{datetime.now():%Y%m%d_%H%M%S}_{self.user_id}"
            path, n = self.handoff_dir / f"{stem}.json", 1
            while path.exists():  # two handoffs in the same second must not overwrite each other
                n += 1
                path = self.handoff_dir / f"{stem}_{n}.json"
            path.write_text(json.dumps(asdict(handoff), indent=2, default=str), encoding="utf-8")
            self._log(state, f"Handoff snapshot saved to {path}")
        return RunResult(False, last_text, state, handoff)

    def _log(self, state: ExecutionState, line: str) -> None:
        state.note(line)
        self._on_log(line)
