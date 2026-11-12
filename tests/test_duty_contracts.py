"""Every duty in duties/ keeps its contract: it replays its own example, alone, through the real orchestrator (D22).
Discovered from the directory, nothing hardcoded. A duty induced later gets the same check as a generated test (D23)."""
from __future__ import annotations

import pytest

import duty_contract
from duty_helpers import DUTIES
from agent.duties import DutyLibrary

NAMES = sorted(p.stem for p in DUTIES.glob("*.yaml"))


@pytest.mark.parametrize("name", NAMES)
async def test_the_duty_replays_its_example_alone_and_keeps_its_contract(name):
    await duty_contract.check_duty_contract(name)


def test_the_parametrization_really_found_the_library():
    assert {"book_flight", "process_refund", "kyc_onboarding"} <= set(NAMES)


async def test_the_check_fails_when_the_duty_cannot_complete_alone(tmp_path):
    """The check must be able to fail: a duty whose success criterion cannot hold is reported, by name."""
    import yaml
    from duty_helpers import make_library

    def break_success(name, data):
        data["success"] = {"name": "impossible", "check": data["success"]["check"], "equals": "never-true"}

    make_library(tmp_path, break_success, only=("process_refund",))
    with pytest.raises(AssertionError, match="process_refund: the example did not complete as this duty"):
        await duty_contract.check_duty_contract("process_refund", tmp_path / "duties")
    assert yaml and DutyLibrary
