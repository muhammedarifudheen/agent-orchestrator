"""The run record (D23): what duty induction learns from. One file per SUCCESSFUL run, only when `run_log_dir` is set.

It must carry exactly what induction needs (goal, each operation with its arguments, result and read-only classification,
and whether a duty did the work) and must never be written for a run that ended in a handoff."""
from __future__ import annotations

import json

import pytest

from agent.orchestrator import Orchestrator
from duty_helpers import SERVERS, ScriptedLLM, book_flight_goal, future, reset_worlds
from inmem import SpyRegistry


@pytest.fixture(autouse=True)
def worlds(monkeypatch):
    reset_worlds(monkeypatch)
    yield
    reset_worlds()


def planner_script():
    return ScriptedLLM(
        [("call", "travel__search_flights", {"origin": "DEL", "destination": "BOM", "date": future()})],
        [("text", "Found flights.")])


async def test_a_successful_planner_run_is_recorded_with_what_induction_needs(tmp_path):
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=planner_script(), user_id="tester", run_log_dir=tmp_path, duties=None)
        result = await orch.run("find flights DEL to BOM")
    assert result.success
    [path] = list(tmp_path.glob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["goal"] == "find flights DEL to BOM" and record["success"] is True
    assert record["duty_used"] is None and record["duty_contract_failed"] is None and record["user"] == "tester"
    [op] = record["operations"]
    assert op["tool"] == "travel__search_flights" and op["ok"] is True and op["read_only"] is True
    assert op["arguments"]["origin"] == "DEL" and isinstance(op["result"], list) and op["result"]


async def test_a_duty_run_is_recorded_as_a_duty_run_with_its_write_classifications(tmp_path):
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=ScriptedLLM(), user_id="tester", run_log_dir=tmp_path)
        result = await orch.run(book_flight_goal())
    assert result.duty_used == "book_flight"
    record = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert record["duty_used"] == "book_flight"
    assert [(o["tool"], o["read_only"]) for o in record["operations"]] == [
        ("travel__search_flights", True), ("travel__book_flight", False), ("travel__send_confirmation", False)]


async def test_a_run_that_ends_in_a_handoff_is_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVEL_FAIL_MODE", "all_down")
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=ScriptedLLM(), user_id="tester", run_log_dir=tmp_path / "runs",
                            handoff_dir=tmp_path / "handoffs")
        result = await orch.run(book_flight_goal())
    assert not result.success
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").glob("*.json"))
    assert len(list((tmp_path / "handoffs").glob("*.json"))) == 1


async def test_nothing_is_written_unless_a_directory_is_given(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        await Orchestrator(reg, client=planner_script(), user_id="tester", duties=None).run("find flights DEL to BOM")
    assert list(tmp_path.iterdir()) == []


async def test_two_runs_in_the_same_second_do_not_overwrite_each_other(tmp_path):
    for _ in range(2):
        async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
            await Orchestrator(reg, client=planner_script(), user_id="tester", run_log_dir=tmp_path, duties=None).run("find flights")
    assert len(list(tmp_path.glob("*.json"))) == 2
