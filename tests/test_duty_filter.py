"""Reference filters (D22): `$search.0.flight_id where seats_available > 0` binds the first item that PASSES, not the first
item. Closes the "first result is sold out" gap in the first version of the grammar."""
from __future__ import annotations

import pytest

import travel_server
from agent.duties import DutyContractError, DutyError, DutyLibrary, parse_duty, resolve
from duty_helpers import SERVERS, ScriptedLLM, book_flight_goal, make_library, reset_worlds
from inmem import SpyRegistry
from agent.orchestrator import Orchestrator

ITEMS = [{"id": "A", "available": False, "qty": 0, "meta": {"tier": "gold"}},
         {"id": "B", "available": True, "qty": 3, "meta": {"tier": "silver"}},
         {"id": "C", "available": True, "qty": 9, "meta": {"tier": "gold"}}]


@pytest.fixture(autouse=True)
def worlds(monkeypatch):
    reset_worlds(monkeypatch)
    yield
    reset_worlds()


@pytest.mark.parametrize("expr,expected", [
    ("$items.0.id where available == true", "B"),
    ("$items.1.id where available == true", "C"),
    ("$items.0.id where qty > 5", "C"),
    ("$items.0.id where qty >= 3", "B"),
    ("$items.0.id where qty < 1", "A"),
    ("$items.0.id where qty != 0", "B"),
    ("$items.0.id where meta.tier == silver", "B"),
    ("$items.0.id where meta.tier == 'gold'", "A"),
    ("$items.0 where available == true", ITEMS[1]),
    ("$items.0.id", "A"),  # no filter: the old behaviour is unchanged
])
def test_a_filter_picks_from_the_items_that_pass_it(expr, expected):
    assert resolve(expr, {}, {"items": ITEMS}) == expected


@pytest.mark.parametrize("expr,kind", [
    ("$items.0.id where qty > 100", "unresolved_reference"),       # nothing passes: there is no item 0 of the filtered list
    ("$items.5.id where available == true", "unresolved_reference"),
    ("$items.id where available == true", "bad_filter"),           # a filter picks from a list: it needs an index
    ("$items.0.id where available", "bad_filter"),                  # not `<field> <op> <value>`
    ("$items.0.id where available =~ true", "bad_filter"),
    ("$ghost.0.id where available == true", "unresolved_reference"),
])
def test_a_filter_that_cannot_bind_is_a_contract_error_not_a_guess(expr, kind):
    with pytest.raises(DutyContractError) as err:
        resolve(expr, {}, {"items": ITEMS})
    assert err.value.kind == kind


def test_a_filter_on_something_that_is_not_a_list_is_refused():
    with pytest.raises(DutyContractError, match="applies to a list"):
        resolve("$one.0.id where available == true", {}, {"one": {"id": "A", "available": True}})


def test_items_missing_the_field_do_not_satisfy_the_filter_and_incomparable_values_do_not_crash():
    items = [{"id": "A"}, {"id": "B", "qty": "many"}, {"id": "C", "qty": 4}]
    assert resolve("$items.0.id where qty > 1", {}, {"items": items}) == "C"


def test_filters_are_checked_when_the_duty_loads():
    import yaml
    from duty_helpers import DUTIES
    data = yaml.safe_load((DUTIES / "book_flight.yaml").read_text(encoding="utf-8"))
    assert "where seats_available > 0" in data["steps"][1]["args"]["flight_id"]  # the shipped duty uses it
    parse_duty(data)
    data["steps"][1]["args"]["flight_id"] = "$search.0.flight_id where seats_available"
    with pytest.raises(DutyError, match="not `<field>"):
        parse_duty(data)


# -- the scenario that motivated it: the first search result is sold out -----------------------------------------------

async def test_a_sold_out_first_result_binds_to_the_next_available_one(monkeypatch, tmp_path):
    routes = travel_server._MOCK_ROUTES[("DEL", "BOM")]
    monkeypatch.setitem(routes[0], "seats_available", 0)
    assert routes[1]["seats_available"] > 0
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        result = await Orchestrator(reg, client=ScriptedLLM(), user_id="tester", allow_writes=True).run(book_flight_goal())
        assert reg.calls == ["travel__search_flights", "travel__book_flight", "travel__send_confirmation"]
    assert result.success and result.duty_used == "book_flight"
    [booking] = travel_server._BOOKINGS.values()
    assert booking["flight_id"].startswith(routes[1]["flight_number"]), booking["flight_id"]


async def test_without_the_filter_the_same_world_fails_at_the_booking(monkeypatch, tmp_path):
    """The contrast: the old `$search.0` binding picks the sold-out flight and the booking fails (SOLD_OUT)."""
    routes = travel_server._MOCK_ROUTES[("DEL", "BOM")]
    monkeypatch.setitem(routes[0], "seats_available", 0)

    def drop_filter(name, data):
        data["steps"][1]["args"]["flight_id"] = "$search.0.flight_id"

    library = make_library(tmp_path / "lib", drop_filter, only=("book_flight",))
    llm = ScriptedLLM([("text", "That flight is sold out.")])
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        result = await Orchestrator(reg, client=llm, user_id="tester", duties=library, allow_writes=True).run(book_flight_goal())
    assert result.state.duty["outcome"] == "step_failed" and result.state.duty["failure"]["code"] == "SOLD_OUT"
    assert travel_server._BOOKINGS == {} and llm.calls == 1


async def test_when_every_flight_is_sold_out_the_duty_stops_before_writing_and_records_it(monkeypatch):
    for flight in travel_server._MOCK_ROUTES[("DEL", "BOM")]:
        monkeypatch.setitem(flight, "seats_available", 0)
    llm = ScriptedLLM([("text", "Everything is sold out on that day.")])
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        result = await Orchestrator(reg, client=llm, user_id="tester", allow_writes=True).run(book_flight_goal())
        assert reg.calls == ["travel__search_flights"]
    assert result.duty_contract_failed == "book_flight" and result.state.duty["failure"]["kind"] == "unresolved_reference"
    assert travel_server._BOOKINGS == {} and llm.calls == 1 and DutyLibrary
