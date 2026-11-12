"""The contract every duty must keep (D22), as one reusable check.

`check_duty_contract(name)` is what a duty's generated test calls, and what `tests/test_duty_contracts.py` runs for every
duty in duties/. It loads the duty, checks the write boundary against the servers' own classification, then REPLAYS the
duty's example on fresh mock worlds through the real Orchestrator with a model that fails the test if it is consulted:
the duty must complete the request alone, its success criterion must hold, and the write gate must have been consulted
once, after reads only, before the first write.

A duty induced from real runs may not replay on fresh mocks (it can depend on state those runs created): then this check
fails and the duty is not installed until its example carries a `setup` that builds the state (D23).

Problems are collected inside the registry's context and raised once outside it: an assertion raised inside an
`async with` over MCP sessions is wrapped in an ExceptionGroup, which would hide the message.
"""
from __future__ import annotations

import os
from pathlib import Path

from agent.duties import BoundaryEvent, DutyLibrary, resolve, substitute_setup
from agent.orchestrator import Orchestrator
from duty_helpers import DUTIES, SERVERS, reset_worlds
from inmem import SpyRegistry


class NoModel:
    """A planner that must never be asked: the duty has to complete the request on its own."""

    def __init__(self) -> None:
        self.messages = self
        self.asked = 0

    async def create(self, **_):
        self.asked += 1
        raise RuntimeError("the model was consulted: the duty did not complete the request by itself")


def duties_dir(explicit: Path | None = None) -> Path:
    return Path(explicit or os.environ.get("DUTIES_DIR") or DUTIES)


async def check_duty_contract(name: str, directory: Path | None = None) -> None:
    library = DutyLibrary(duties_dir(directory))
    assert name in library.duties, f"duty {name!r} does not load: {library.errors.get(name + '.yaml', 'no such file')}"
    duty = library.duties[name]
    alone = DutyLibrary.__new__(DutyLibrary)  # only this duty may match: no other duty can mask a failure of this one
    alone.dir, alone.duties, alone.errors = library.dir, {name: duty}, {}
    problems: list[str] = []
    reset_worlds()
    try:
        async with SpyRegistry(SERVERS) as registry:
            known = {t["name"] for t in registry.anthropic_tools()}
            missing = [s.tool for s in duty.steps if s.tool not in known]
            if missing:
                problems.append(f"{name}: tools that no server offers: {missing}")
            else:
                boundary = duty.boundary_index
                for i, step in enumerate(duty.steps[:boundary]):
                    if registry.is_read_only(step.tool) is not True:
                        problems.append(f"{name}: {step.tool} comes before the boundary but is not read-only")
                if registry.is_read_only(duty.steps[boundary].tool) is True:
                    problems.append(f"{name}: the boundary step {duty.steps[boundary].id} is not a write")

                scope: dict = {}
                for setup in duty.example.get("setup", []):
                    args = resolve(setup.get("args", {}), {}, {"setup": scope})
                    outcome = await registry.call(setup["tool"], args)
                    if not outcome.ok:
                        problems.append(f"{name}: example setup {setup['id']} failed: {outcome.error_code} {outcome.error_message}")
                        break
                    scope[setup["id"]] = outcome.data
                else:
                    intent = substitute_setup(duty.example["intent"], scope)
                    registry.calls.clear()  # only what the duty itself does is judged
                    events: list[tuple[BoundaryEvent, list[str]]] = []

                    def gate(event: BoundaryEvent) -> bool:
                        events.append((event, list(registry.calls)))
                        return True

                    model = NoModel()
                    result = await Orchestrator(registry, allow_writes=True, client=model, duties=alone, write_gate=gate, user_id="contract").run(intent)
                    if not (result.success and result.duty_used == name):
                        problems.append(
                            f"{name}: the example did not complete as this duty: success={result.success} "
                            f"duty_used={result.duty_used} duty={result.state.duty} message={result.final_message!r} "
                            f"handoff={result.handoff.reason if result.handoff else None!r}")
                    elif model.asked or result.duty_contract_failed:
                        problems.append(f"{name}: the model was asked or the contract failed during the replay")
                    elif len(events) != 1:
                        problems.append(f"{name}: the write gate was consulted {len(events)} times, expected once")
                    else:
                        event, calls_then = events[0]
                        if event.tool != duty.steps[boundary].tool:
                            problems.append(f"{name}: the gate was consulted before {event.tool}, not the boundary step")
                        if any(registry.is_read_only(tool) is not True for tool in calls_then):
                            problems.append(f"{name}: something that is not read-only ran before the write boundary: {calls_then}")
    finally:
        reset_worlds()
    assert not problems, "\n".join(problems)
