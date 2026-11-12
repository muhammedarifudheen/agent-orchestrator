"""Three paths through the duty layer (D22), each on the real Orchestrator, the real registry and the real servers:

  1. DUTY PATH      a request that fits a duty runs the duty (the model is never asked) and the write boundary fires
                    BEFORE the first write; a refusing gate stops the run with nothing written.
  2. PLANNER PATH   a request that fits no duty (or fits too weakly) goes to the planner.
  3. FALLBACK PATH  a duty that matches but whose contract fails while running falls back to the planner AND the failure
                    is recorded: on the run result, in the log and in the handoff. Never silently.

The scripted model is not a model; it proves which code path ran."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import banking_server
import travel_server
from agent.duties import BoundaryEvent
from agent.orchestrator import Orchestrator
from duty_helpers import SERVERS, ScriptedLLM, book_flight_goal, future, make_library, reset_worlds
from inmem import SpyRegistry

pytestmark = pytest.mark.usefixtures("fresh_worlds")


@pytest.fixture
def fresh_worlds(monkeypatch):
    reset_worlds(monkeypatch)
    yield
    reset_worlds()


def handoff(tmp_path: Path) -> dict:
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


class RecordingGate:
    """A write gate that records what the registry had been asked to run at the moment it was consulted."""

    def __init__(self, registry: SpyRegistry, verdict: bool = True) -> None:
        self.registry, self.verdict = registry, verdict
        self.events: list[BoundaryEvent] = []
        self.calls_when_consulted: list[list[str]] = []

    def __call__(self, event: BoundaryEvent) -> bool:
        self.events.append(event)
        self.calls_when_consulted.append(list(self.registry.calls))
        return self.verdict


# -- 1. duty path ------------------------------------------------------------------------------------------------------

async def test_a_matching_request_runs_the_duty_not_the_planner_and_the_write_gate_fires_before_the_first_write(tmp_path):
    llm = ScriptedLLM()
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        gate = RecordingGate(reg)
        orch = Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", write_gate=gate)
        result = await orch.run(book_flight_goal())

        assert result.success and result.duty_used == "book_flight", result.final_message
        assert llm.calls == 0, "the model was consulted although a duty matched"
        assert reg.calls == ["travel__search_flights", "travel__book_flight", "travel__send_confirmation"]
        # the gate was consulted exactly once, after the read and BEFORE any write reached a server
        assert len(gate.events) == 1
        assert gate.calls_when_consulted == [["travel__search_flights"]]
        event = gate.events[0]
        assert (event.duty, event.step, event.tool) == ("book_flight", "book", "travel__book_flight")
        assert event.write_tools == ["travel__book_flight", "travel__send_confirmation"]
        assert result.state.write_boundary["approved"] is True
        # and the world really changed: one confirmed booking for the passenger
        assert [b["passenger_email"] for b in travel_server._BOOKINGS.values() if b["status"] == "CONFIRMED"] == ["asha@example.com"]
        assert any("DUTY: using 'book_flight'" in line for line in result.state.log)
        assert not list(tmp_path.glob("*.json")), "a successful duty run must not write a handoff"


async def test_a_refusing_write_gate_stops_the_run_with_nothing_written_and_says_why(tmp_path):
    llm = ScriptedLLM()
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        gate = RecordingGate(reg, verdict=False)
        orch = Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", write_gate=gate)
        result = await orch.run(book_flight_goal())

        assert not result.success and result.handoff is not None
        assert reg.calls == ["travel__search_flights"], "a write reached a server after the gate said no"
        assert not travel_server._BOOKINGS
        assert llm.calls == 0
    data = handoff(tmp_path)
    assert "Write boundary of duty 'book_flight' was not approved" in data["reason"]
    assert data["write_boundary"]["approved"] is False and data["write_boundary"]["tool"] == "travel__book_flight"
    assert data["duty"]["outcome"] == "declined"


async def test_an_async_write_gate_is_awaited(tmp_path):
    seen = []

    async def gate(event: BoundaryEvent) -> bool:
        seen.append(event.tool)
        return True

    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=ScriptedLLM(), handoff_dir=tmp_path, user_id="tester", write_gate=gate)
        result = await orch.run(book_flight_goal())
    assert result.success and seen == ["travel__book_flight"]


async def test_a_pre_boundary_step_that_is_no_longer_read_only_is_refused_before_it_runs(tmp_path, monkeypatch):
    """Runtime enforcement, independent of review: if a server now reports a pre-boundary tool as a write, the duty
    does not run it (nothing reaches a server), the failure is recorded, and the planner takes over."""
    llm = ScriptedLLM([("text", "I will not do that.")])
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        real = reg.is_read_only
        monkeypatch.setattr(reg, "is_read_only", lambda name: False if name == "travel__search_flights" else real(name))
        orch = Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester")
        result = await orch.run(book_flight_goal())
        assert reg.calls == []
        assert result.duty_contract_failed == "book_flight" and result.duty_used is None
        assert result.state.duty["failure"]["kind"] == "write_before_boundary"
        assert llm.calls == 1


async def test_a_duty_failure_with_recovery_human_hands_off_by_the_same_policy_as_the_planner(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVEL_FAIL_MODE", "all_down")  # SERVICE_UNAVAILABLE retryable=true, recovery=agent
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=ScriptedLLM(), handoff_dir=tmp_path, user_id="tester")
        result = await orch.run(book_flight_goal())
        assert not result.success
        assert reg.calls == ["travel__search_flights", "travel__search_flights"], "one retry, then a handoff"
    data = handoff(tmp_path)
    assert data["error_snapshot"]["error_code"] == "SERVICE_UNAVAILABLE"
    assert data["duty"]["name"] == "book_flight" and data["duty"]["outcome"] == "handed_off"
    assert "duty_contract_failed" not in data  # a tool failure is not a contract failure


# -- 2. planner path ---------------------------------------------------------------------------------------------------

async def test_an_unmatched_request_goes_to_the_planner(tmp_path):
    llm = ScriptedLLM(
        [("text", "Searching."), ("call", "travel__search_flights", {"origin": "DEL", "destination": "BOM", "date": future()})],
        [("text", "Found flights.")])
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester")
        result = await orch.run("what flights go from DEL to BOM next month?")
        assert result.success and result.duty_used is None
        assert llm.calls == 2
        assert reg.calls == ["travel__search_flights"]
        assert result.state.duty is None
        assert any("no duty fits" in line for line in result.state.log)


async def test_a_request_that_fits_a_duty_too_weakly_goes_to_the_planner_and_says_so(tmp_path):
    # one pattern word dropped ("a"): 7/8 words = 0.875, below the 0.9 threshold
    goal = f"book flight from DEL to BOM on {future()} for Asha Verma at asha@example.com"
    llm = ScriptedLLM([("text", "That is not something I will book without more detail.")])
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester")
        result = await orch.run(goal)
        assert result.duty_used is None and llm.calls == 1 and reg.calls == []
        assert result.state.duty["outcome"] == "below_threshold" and result.state.duty["name"] == "book_flight"
        assert 0.8 < result.state.duty["confidence"] < 0.9


async def test_with_duties_switched_off_the_orchestrator_behaves_as_before(tmp_path):
    llm = ScriptedLLM([("text", "No tools needed.")])
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", duties=None)
        result = await orch.run(book_flight_goal())
        assert result.duty_used is None and llm.calls == 1 and reg.calls == [] and result.state.duty is None


# -- 3. matched but its contract fails: fallback, recorded ------------------------------------------------------------

async def test_a_duty_whose_contract_fails_before_any_write_falls_back_to_the_planner_and_records_it(tmp_path):
    """The reverse route has no flights in the mock, so `$search.0.flight_id` cannot resolve: a REAL contract failure."""
    llm = ScriptedLLM([("text", "No flights operate on that route.")])
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester")
        result = await orch.run(book_flight_goal(origin="BOM", destination="DEL"))
        assert reg.calls == ["travel__search_flights"]  # the read ran, then nothing was written
        assert not travel_server._BOOKINGS
        assert llm.calls == 1, "the planner must take over"
        assert result.success and result.duty_used is None
        assert result.duty_contract_failed == "book_flight"
        assert result.state.duty["outcome"] == "contract_failed"
        assert result.state.duty["failure"]["kind"] == "unresolved_reference"
        assert any(line.startswith("DUTY CONTRACT FAILED: book_flight") for line in result.state.log)
        assert "A reviewed procedure ('book_flight')" in llm.first_user_message()


async def test_the_contract_failure_reaches_the_handoff_when_the_planner_then_fails_too(tmp_path):
    llm = ScriptedLLM(
        [("call", "banking__get_account_balance", {"account_id": "ACCT-NOPE-1"})],
        [("call", "banking__get_account_balance", {"account_id": "ACCT-NOPE-2"})])
    async with SpyRegistry(SERVERS, pattern="*_server.py") as reg:
        orch = Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester")
        result = await orch.run(book_flight_goal(origin="BOM", destination="DEL"))
        assert not result.success and result.handoff is not None
    data = handoff(tmp_path)
    assert data["duty_contract_failed"] == "book_flight"
    assert data["duty"]["outcome"] == "contract_failed" and data["duty"]["failure"]["kind"] == "unresolved_reference"
    assert data["industries_touched"][0] == "travel"  # the duty's own read is in the record


async def test_a_contract_failure_after_the_writes_tells_the_planner_what_was_already_done(tmp_path, tmp_path_factory):
    """A success criterion that is not met AFTER the writes: the planner is told, in the request itself, which actions are
    complete and must not be repeated."""
    def impossible_success(name, data):
        if name == "book_flight":
            data["success"] = {"name": "confirmation_email_sent", "check": "$confirm", "equals": "never"}

    library = make_library(tmp_path_factory.mktemp("lib"), impossible_success, only=("book_flight",))
    llm = ScriptedLLM([("text", "The booking and the email are already done; nothing to repeat.")])
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", duties=library)
        result = await orch.run(book_flight_goal())
        assert reg.calls == ["travel__search_flights", "travel__book_flight", "travel__send_confirmation"]
        assert result.duty_contract_failed == "book_flight"
        assert result.state.duty["failure"]["kind"] == "success_criterion"
        first = llm.first_user_message()
        assert "must NOT repeat" in first and "travel__book_flight" in first and "travel__send_confirmation" in first
        assert len([b for b in travel_server._BOOKINGS.values()]) == 1, "the planner must not book again"


# -- the library is discovered, nothing is named in the orchestrator --------------------------------------------------

async def test_a_new_duty_dropped_into_the_directory_is_used_with_no_code_change(tmp_path, tmp_path_factory):
    def rename(name, data):
        if name == "book_flight":
            data["name"] = "reserve_seat"
            data["intent_patterns"] = [p.replace("book a flight", "reserve a seat") for p in data["intent_patterns"]]
            data["example"]["intent"] = data["example"]["intent"].replace("book a flight", "reserve a seat")

    lib_dir = tmp_path_factory.mktemp("lib")
    library = make_library(lib_dir, rename, only=("book_flight",))
    (lib_dir / "duties" / "book_flight.yaml").rename(lib_dir / "duties" / "reserve_seat.yaml")
    library = type(library)(lib_dir / "duties")
    assert sorted(library.duties) == ["reserve_seat"], library.errors
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=ScriptedLLM(), handoff_dir=tmp_path, user_id="tester", duties=library)
        result = await orch.run(book_flight_goal().replace("book a flight", "reserve a seat"))
        assert result.success and result.duty_used == "reserve_seat"
