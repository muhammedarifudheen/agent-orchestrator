"""D22/D24: the duty boundary is WHERE to stop, not WHETHER; `unattended: true` + a rationale is the only bypass; and the
boundary must be the first write-classified step, checked when the duty is loaded (not only when it runs)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import travel_server
from agent.duties import DutyError, DutyLibrary, boundary_error, validate_duty_data
from agent.orchestrator import Orchestrator
from agent.write_policy import classify
from duty_helpers import DUTIES, SERVERS, ScriptedLLM, book_flight_goal, make_library, reset_worlds
from inmem import InMemoryRegistry, SpyRegistry

ROOT = Path(__file__).resolve().parent.parent
RATIONALE = "Bookings under the reviewed fare cap are refundable by the airline within 24h; the passenger is the requester."


@pytest.fixture(autouse=True)
def worlds(monkeypatch):
    reset_worlds(monkeypatch)
    yield
    reset_worlds()


def kind_from_servers() -> "callable":
    """tool -> "write" | "read" | "absent", from the real servers' own published metadata."""
    async def table():
        out = {}
        async with InMemoryRegistry(SERVERS) as reg:
            for t in reg.anthropic_tools():
                out[t["name"]] = "read" if classify(reg, t["name"]).is_read else "write"
        return out

    known = asyncio.run(table())
    return lambda tool: known.get(tool, "absent")


def make_unattended(rationale):
    def mutate(name, data):
        if name == "book_flight":
            data["unattended"] = True
            if rationale is not None:
                data["unattended_rationale"] = rationale
    return mutate


# -- Q2: refuse by default; unattended + rationale is the only bypass -------------------------------------------------

async def test_a_duty_without_unattended_refuses_the_write_without_approval(tmp_path):
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        result = await Orchestrator(reg, client=ScriptedLLM(), handoff_dir=tmp_path, user_id="tester").run(book_flight_goal())
        assert reg.calls == ["travel__search_flights"]
    assert not result.success and travel_server._BOOKINGS == {}
    assert "unattended_rationale" not in (result.state.write_boundary or {})
    assert result.state.write_decisions[0]["mode"] == "pending"


async def test_a_duty_with_unattended_and_a_rationale_proceeds_and_records_the_rationale(tmp_path):
    library = make_library(tmp_path / "lib", make_unattended(RATIONALE), only=("book_flight",))
    assert library.errors == {} and library.duties["book_flight"].unattended_rationale == RATIONALE
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        result = await Orchestrator(reg, client=ScriptedLLM(), user_id="tester", duties=library,
                                    run_log_dir=tmp_path / "runs").run(book_flight_goal())
        assert reg.calls == ["travel__search_flights", "travel__book_flight", "travel__send_confirmation"]
    assert result.success and result.duty_used == "book_flight" and len(travel_server._BOOKINGS) == 1
    assert result.state.write_boundary["unattended_rationale"] == RATIONALE
    assert [(d["mode"], d["unattended_rationale"]) for d in result.state.write_decisions] == [("unattended", RATIONALE)] * 2
    record = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert record["write_decisions"][0]["unattended_rationale"] == RATIONALE


async def test_the_rationale_is_in_the_handoff_when_an_unattended_duty_later_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVEL_FAIL_MODE", "booking_down")  # the write itself fails: retried once, then a handoff
    library = make_library(tmp_path / "lib", make_unattended(RATIONALE), only=("book_flight",))
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        result = await Orchestrator(reg, client=ScriptedLLM(), handoff_dir=tmp_path / "h", user_id="tester",
                                    duties=library).run(book_flight_goal())
    assert not result.success
    [file] = list((tmp_path / "h").glob("*.json"))
    data = json.loads(file.read_text(encoding="utf-8"))
    assert data["duty"]["unattended_rationale"] == RATIONALE
    assert data["write_boundary"]["unattended_rationale"] == RATIONALE
    assert all(d["mode"] == "unattended" and d["unattended_rationale"] == RATIONALE for d in data["write_decisions"])


@pytest.mark.parametrize("rationale", [None, "", "   "])
def test_unattended_true_without_a_real_rationale_is_refused_at_load_time(tmp_path, rationale):
    library = make_library(tmp_path, make_unattended(rationale), only=("book_flight",))
    assert "book_flight" not in library.duties
    assert "unattended" in library.errors["book_flight.yaml"] and "rationale" in library.errors["book_flight.yaml"]


def test_a_rationale_without_unattended_is_refused_too(tmp_path):
    def mutate(name, data):
        data["unattended_rationale"] = "because"

    library = make_library(tmp_path, mutate, only=("book_flight",))
    assert "book_flight" not in library.duties and "only meaningful with unattended: true" in library.errors["book_flight.yaml"]


def test_the_schema_itself_requires_the_rationale_when_unattended():
    import yaml
    data = yaml.safe_load((DUTIES / "book_flight.yaml").read_text(encoding="utf-8"))
    validate_duty_data(data)  # the shipped duty is valid and is NOT unattended
    assert "unattended" not in data
    for bad in ({"unattended": True}, {"unattended": True, "unattended_rationale": ""}, {"unattended": "yes", "unattended_rationale": "x"}):
        with pytest.raises(DutyError):
            validate_duty_data({**data, **bad})
    validate_duty_data({**data, "unattended": True, "unattended_rationale": RATIONALE})


def test_no_shipped_duty_is_unattended():
    assert all(not d.unattended for d in DutyLibrary().duties.values())


async def test_unattended_covers_only_its_own_duty_never_another_duty_or_the_planner(tmp_path):
    library = make_library(tmp_path / "lib", make_unattended(RATIONALE))  # book_flight unattended; the others are not
    assert library.duties["book_flight"].unattended and not library.duties["process_refund"].unattended
    # a planner-path write in the same orchestrator is still refused
    llm = ScriptedLLM([("call", "retail__create_order", {"sku": "SKU-1001", "qty": 1, "customer_id": "C-1001"})])
    async with SpyRegistry(SERVERS, pattern="*_server.py") as reg:
        result = await Orchestrator(reg, client=llm, handoff_dir=tmp_path / "h", user_id="tester", duties=library).run("place an order")
        assert "retail__create_order" not in reg.calls and not result.success
    # and so is another duty's write: process_refund is not unattended, so its refund is stopped at the boundary
    import retail_server
    reset_worlds()
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        order = (await reg.call("retail__create_order", {"sku": "SKU-1001", "qty": 1, "customer_id": "C-1001"})).data
        await reg.call("retail__charge_payment", {"order_id": order["order_id"], "amount": order["total_amount"]})
        reg.calls.clear()
        refund = await Orchestrator(reg, client=ScriptedLLM(), handoff_dir=tmp_path / "h2", user_id="tester",
                                    duties=library).run(f"refund order {order['order_id']}")
        assert reg.calls == ["retail__verify_order", "retail__check_refund_eligibility"], reg.calls
    assert not refund.success and refund.state.duty["name"] == "process_refund"
    assert retail_server._ORDERS[order["order_id"]]["status"] == "PAID" and retail_server._REFUNDS == {}


# -- Q3: the boundary is the FIRST write-classified step, checked at load time -------------------------------------------

def test_a_boundary_after_a_write_step_fails_at_load_time_naming_both_steps(tmp_path):
    def late_boundary(name, data):
        data["write_boundary"] = {"before": "confirm"}  # `book` writes before it

    library = make_library(tmp_path, late_boundary, only=("book_flight",))
    assert "book_flight" in DutyLibrary(tmp_path / "duties").duties, "without a classification the file alone is not judged"
    library = DutyLibrary(tmp_path / "duties", classify=kind_from_servers())
    assert "book_flight" not in library.duties
    message = library.errors["book_flight.yaml"]
    assert "'book'" in message and "travel__book_flight" in message, message     # the write step
    assert "'confirm'" in message, message                                          # the declared boundary
    assert "FIRST write-classified step" in message and "before the declared write boundary" in message


def test_a_boundary_before_a_read_step_is_refused_too(tmp_path):
    def early_boundary(name, data):
        data["write_boundary"] = {"before": "search"}  # `search` only reads: the first write is `book`

    make_library(tmp_path, early_boundary, only=("book_flight",))
    library = DutyLibrary(tmp_path / "duties", classify=kind_from_servers())
    message = library.errors["book_flight.yaml"]
    assert "'search'" in message and "'book'" in message and "not write-classified" in message


def test_a_duty_with_no_write_step_has_nothing_to_put_a_boundary_before():
    def kind(tool):
        return "read"

    duty = next(iter(DutyLibrary().duties.values()))
    assert "no step writes" in boundary_error(duty, kind)


def test_the_shipped_duties_all_pass_the_load_time_check():
    library = DutyLibrary(classify=kind_from_servers())
    assert library.errors == {} and sorted(library.duties) == ["book_flight", "kyc_onboarding", "process_refund"]


def test_a_tool_that_is_not_registered_is_not_judged_by_the_load_time_check():
    library = DutyLibrary()
    duty = library.duties["book_flight"]
    assert boundary_error(duty, lambda tool: "absent") is None  # the run-time check covers an absent tool


async def test_the_orchestrator_will_not_use_a_duty_whose_boundary_is_misplaced(tmp_path):
    def late_boundary(name, data):
        data["write_boundary"] = {"before": "confirm"}

    library = make_library(tmp_path / "lib", late_boundary, only=("book_flight",))
    llm = ScriptedLLM([("text", "I could not use a procedure for that.")])
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        orch = Orchestrator(reg, client=llm, user_id="tester", duties=library, allow_writes=True)
        result = await orch.run(book_flight_goal())
        assert reg.calls == [], "the misplaced duty must not run a single step"
    assert result.duty_used is None and llm.calls == 1
    assert "book_flight" in orch.duty_errors and "'confirm'" in orch.duty_errors["book_flight"]
    assert any(line.startswith("DUTY UNUSABLE: duty 'book_flight'") for line in result.state.log)
    assert travel_server._BOOKINGS == {}


def test_the_rule_is_recorded_in_claude_md():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "write_boundary is the FIRST write-classified step, regardless of what the brief names" in text
    assert "duty boundary is where, not whether" in text


@pytest.mark.parametrize("rationale", [None, "", "   ", 5])
def test_parse_duty_itself_refuses_unattended_without_a_rationale_even_if_the_schema_were_skipped(rationale):
    """Two layers: the schema and parse_duty. This calls the second one directly (no schema), so removing it is visible."""
    import yaml
    from agent.duties import parse_duty
    data = yaml.safe_load((DUTIES / "book_flight.yaml").read_text(encoding="utf-8"))
    data["unattended"] = True
    if rationale is not None:
        data["unattended_rationale"] = rationale
    with pytest.raises(DutyError, match="unattended: true needs a non-empty unattended_rationale"):
        parse_duty(data)
    data["unattended_rationale"] = RATIONALE
    assert parse_duty(data).unattended is True
