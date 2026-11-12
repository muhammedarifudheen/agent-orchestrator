"""The write policy (D24): no tool that changes the world runs unless the run was allowed to write or a person approved it.

Driven through the real Orchestrator, registry and servers. The scripted model is not a model: it issues exactly the calls
it is told to, so a test can prove what reached a server and what did not."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import retail_server
import travel_server
from agent import approvals
from agent.orchestrator import Orchestrator
from agent.write_policy import APPROVAL_REQUIRED, METADATA_MISSING, WriteProposal, classify
from duty_helpers import SERVERS, ScriptedLLM, book_flight_goal, future, reset_worlds
from inmem import InMemoryRegistry, SpyRegistry

ROOT = Path(__file__).resolve().parent.parent
ORDER = {"sku": "SKU-1001", "qty": 1, "customer_id": "C-1001"}


@pytest.fixture(autouse=True)
def worlds(monkeypatch):
    reset_worlds(monkeypatch)
    yield
    reset_worlds()


def handoff(tmp_path: Path, sub: str | None = None) -> dict:
    files = list((tmp_path / sub if sub else tmp_path).glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


def approval_file(tmp_path: Path) -> Path:
    [path] = list((tmp_path / "approvals").glob("*.json"))
    return path


# -- (i) the planner path: no duty, no boundary, and still no unapproved write ----------------------------------------

async def test_a_planner_run_cannot_call_a_write_tool_without_allow_writes_or_an_approver(tmp_path):
    llm = ScriptedLLM([("call", "retail__create_order", ORDER)])
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        result = await Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", duties=None).run("place an order")
        assert not result.success and result.handoff is not None
        assert reg.calls == [], "a write reached a server although nobody approved it"
    assert retail_server._ORDERS == {} and retail_server._INVENTORY["SKU-1001"]["stock"] == 120
    assert result.state.pending_writes[0]["tool"] == "retail__create_order"
    data = handoff(tmp_path)
    assert data["error_snapshot"]["error_code"] == APPROVAL_REQUIRED and data["error_snapshot"]["recovery"] == "human"
    assert "1 write(s) await approval: retail__create_order" in data["reason"]
    assert data["write_decisions"][0]["allowed"] is False and data["write_decisions"][0]["mode"] == "pending"


async def test_the_refusal_is_a_pending_approval_record_with_the_blast_radius_and_the_prior_steps(tmp_path):
    llm = ScriptedLLM([("call", "retail__check_inventory", {"sku": "SKU-1001", "qty": 1}),
                       ("call", "retail__create_order", ORDER)])
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        result = await Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", duties=None).run("place an order")
        assert reg.calls == ["retail__check_inventory"], "the read ran; the write did not"
    record = approvals.load(approval_file(tmp_path))
    [write] = record["writes"]
    assert record["status"] == "pending" and record["goal"] == "place an order" and record["user"] == "tester"
    assert (write["id"], write["tool"], write["arguments"], write["status"]) == (1, "retail__create_order", ORDER, "pending")
    assert write["cost_summary"] == {"writes": True, "side_effect": "write", "idempotent": False, "reversible": False,
                                     "scope": "single", "moves_money": False}
    assert write["reason"] and [s["tool"] for s in record["prior_steps"]] == ["retail__check_inventory"]
    assert result.pending_approval["artifact"] == str(approval_file(tmp_path))
    assert handoff(tmp_path)["pending_approval"]["writes"][0]["tool"] == "retail__create_order"


async def test_allow_writes_lets_the_same_write_run_and_records_why(tmp_path):
    llm = ScriptedLLM([("call", "retail__create_order", ORDER)], [("text", "Ordered.")])
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        result = await Orchestrator(reg, client=llm, user_id="tester", duties=None, allow_writes=True).run("place an order")
        assert reg.calls == ["retail__create_order"]
    assert result.success and len(retail_server._ORDERS) == 1
    assert result.state.write_decisions[0]["mode"] == "allow_writes" and result.state.write_decisions[0]["allowed"] is True


async def test_an_approver_decides_each_write_and_an_unapproved_one_is_pending_not_run(tmp_path):
    seen: list[WriteProposal] = []

    def approver(proposal: WriteProposal) -> bool:
        seen.append(proposal)
        return proposal.arguments["qty"] == 1  # approve the small one only

    llm = ScriptedLLM([("call", "retail__create_order", ORDER),
                       ("call", "retail__create_order", {**ORDER, "qty": 50})])
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        result = await Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", duties=None,
                                    approver=approver).run("place orders")
        assert reg.calls == ["retail__create_order"], "only the approved write ran"
    assert [p.arguments["qty"] for p in seen] == [1, 50] and seen[0].safety.scope == "single"
    assert [o["qty"] for o in retail_server._ORDERS.values()] == [1]
    assert not result.success and [w["arguments"]["qty"] for w in result.pending_approval["writes"]] == [50]


async def test_an_approver_that_raises_or_answers_loosely_is_not_an_approval(tmp_path):
    def boom(proposal):
        raise RuntimeError("approver down")

    for approver in (boom, lambda p: "yes", lambda p: 1, lambda p: None):
        reset_worlds()
        async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
            result = await Orchestrator(reg, client=ScriptedLLM([("call", "retail__create_order", ORDER)]), user_id="tester",
                                        duties=None, approver=approver).run("place an order")
            assert reg.calls == [] and not result.success and retail_server._ORDERS == {}


async def test_an_async_approver_is_awaited():
    async def approver(proposal):
        return True

    llm = ScriptedLLM([("call", "retail__create_order", ORDER)], [("text", "Ordered.")])
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        result = await Orchestrator(reg, client=llm, user_id="tester", duties=None, approver=approver).run("place an order")
    assert result.success and len(retail_server._ORDERS) == 1


async def test_reads_never_need_approval(tmp_path):
    llm = ScriptedLLM([("call", "retail__check_inventory", {"sku": "SKU-1001", "qty": 1})], [("text", "In stock.")])
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        result = await Orchestrator(reg, client=llm, user_id="tester", duties=None).run("is it in stock?")
        assert reg.calls == ["retail__check_inventory"]
    assert result.success and result.state.write_decisions == []


# -- (ii) the duty path: its boundary is a convenience, not a substitute ---------------------------------------------

async def test_a_duty_run_cannot_write_without_allow_writes_or_an_approver_even_if_its_gate_says_yes(tmp_path):
    gate_calls = []

    def gate(event):
        gate_calls.append(event.tool)
        return True  # the boundary hook agrees; that must not be enough

    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        result = await Orchestrator(reg, client=ScriptedLLM(), handoff_dir=tmp_path, user_id="tester",
                                    write_gate=gate).run(book_flight_goal())
        assert reg.calls == ["travel__search_flights"], "the read ran, the write was stopped at the boundary"
    assert not result.success and gate_calls == ["travel__book_flight"]
    assert travel_server._BOOKINGS == {}
    assert result.state.duty["name"] == "book_flight" and result.state.duty["outcome"] == "pending_approval"
    record = approvals.load(approval_file(tmp_path))
    assert [w["tool"] for w in record["writes"]] == ["travel__book_flight"]
    assert record["writes"][0]["arguments"]["flight_id"].startswith("6E-2041") and record["duty"]["name"] == "book_flight"
    assert [s["tool"] for s in record["prior_steps"]] == ["travel__search_flights"]
    assert record["write_boundary"]["tool"] == "travel__book_flight"


async def test_allow_writes_lets_a_duty_run_to_completion():
    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        result = await Orchestrator(reg, client=ScriptedLLM(), user_id="tester", allow_writes=True).run(book_flight_goal())
    assert result.success and result.duty_used == "book_flight" and len(travel_server._BOOKINGS) == 1
    assert [d["mode"] for d in result.state.write_decisions] == ["allow_writes", "allow_writes"]


async def test_an_approver_can_approve_the_duty_writes_one_by_one():
    asked = []

    def approver(proposal):
        asked.append((proposal.tool, proposal.duty))
        return True

    async with SpyRegistry(SERVERS, pattern="travel_server.py") as reg:
        result = await Orchestrator(reg, client=ScriptedLLM(), user_id="tester", approver=approver).run(book_flight_goal())
    assert result.success and asked == [("travel__book_flight", "book_flight"), ("travel__send_confirmation", "book_flight")]


# -- (iii) partial approval --------------------------------------------------------------------------------------------

async def three_pending(tmp_path, reg):
    llm = ScriptedLLM([("call", "retail__create_order", ORDER),
                       ("call", "retail__create_order", {**ORDER, "qty": 2}),
                       ("call", "retail__notify_customer", {"customer_id": "C-1001", "message": "hello"})])
    await Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", duties=None).run("do three things")
    return approval_file(tmp_path)


async def test_partial_approval_executes_only_the_approved_subset_and_the_rest_stay_pending(tmp_path):
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        path = await three_pending(tmp_path, reg)
        assert reg.calls == [] and [w["status"] for w in approvals.load(path)["writes"]] == ["pending"] * 3

        summary = await approvals.approve(path, [1, 3], "Dana Reviewer", reg)

        assert reg.calls == ["retail__create_order", "retail__notify_customer"], "exactly the approved writes ran"
        assert summary["executed"] == [1, 3] and summary["pending"] == [2] and summary["status"] == "partial"
        record = approvals.load(path)
        assert [w["status"] for w in record["writes"]] == ["executed", "pending", "executed"]
        assert record["writes"][0]["decided_by"] == "Dana Reviewer" and record["writes"][0]["result"]["status"] == "PENDING_PAYMENT"
        assert [o["qty"] for o in retail_server._ORDERS.values()] == [1], "the pending write (qty 2) did not run"

        # an executed write can never run again, and an unknown id is refused
        with pytest.raises(ValueError, match="only a pending write"):
            await approvals.approve(path, [1], "Dana Reviewer", reg)
        with pytest.raises(ValueError, match="no write with id"):
            await approvals.approve(path, [9], "Dana Reviewer", reg)
        assert len(reg.calls) == 2

        # the rest can be approved later, once
        done = await approvals.approve(path, [2], "Dana Reviewer", reg)
        assert done["status"] == "complete" and done["pending"] == [] and len(retail_server._ORDERS) == 2


async def test_approving_needs_a_named_reviewer_and_runs_nothing_without_one(tmp_path):
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        path = await three_pending(tmp_path, reg)
        for blank in ("", "   "):
            with pytest.raises(ValueError, match="named reviewer"):
                await approvals.approve(path, [1], blank, reg)
        assert reg.calls == [] and retail_server._ORDERS == {}


async def test_rejected_writes_never_run_and_the_others_stay_pending(tmp_path):
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        path = await three_pending(tmp_path, reg)
        summary = approvals.reject(path, [2], "Dana Reviewer", "quantity is wrong")
        assert summary["rejected"] == [2] and summary["pending"] == [1, 3]
        with pytest.raises(ValueError, match="only a pending write"):
            await approvals.approve(path, [2], "Dana Reviewer", reg)
        assert reg.calls == []


async def test_a_write_whose_outcome_is_unknown_is_never_run_twice(tmp_path):
    """A crash between 'executing' and the result leaves the write `executing`: approving it again must not re-run it."""
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        path = await three_pending(tmp_path, reg)
        record = approvals.load(path)
        record["writes"][0]["status"] = "executing"
        path.write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(ValueError, match="only a pending write"):
            await approvals.approve(path, [1], "Dana Reviewer", reg)
        assert reg.calls == []


async def test_a_failing_approved_write_is_recorded_as_failed_and_does_not_stop_the_rest(tmp_path, monkeypatch):
    async with SpyRegistry(SERVERS, pattern="retail_server.py") as reg:
        path = await three_pending(tmp_path, reg)
        summary = await approvals.approve(path, [1, 2, 3], "Dana Reviewer", reg)
        assert summary["executed"] == [1, 2, 3]
        # order 3 writes to an unknown customer: a failure of the TOOL, recorded, not a crash
        record = approvals.load(path)
        record["writes"].append({**record["writes"][2], "id": 4, "status": "pending",
                                 "arguments": {"customer_id": "C-NOPE", "message": "x"}})
        path.write_text(json.dumps(record), encoding="utf-8")
        summary = await approvals.approve(path, [4], "Dana Reviewer", reg)
        assert summary["failed"] == [4]
        assert approvals.load(path)["writes"][3]["error"]["code"] == "CUSTOMER_NOT_FOUND"


# -- (iv) a write tool that does not describe itself is refused, loudly, by name ---------------------------------------

BARE_SERVER = '''
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("bare")
RAN = []


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False))
def mystery_write(x: str) -> dict:
    """Changes something. Args: x: anything. Errors: none."""
    RAN.append(("mystery_write", x))
    return {"ok": True}


@mcp.tool()
def silent_tool(x: str) -> dict:
    """Says nothing about whether it writes. Args: x: anything. Errors: none."""
    RAN.append(("silent_tool", x))
    return {"ok": True}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False), meta={"writes": True, "reversible": True})
def half_declared(x: str) -> dict:
    """Declares reversible but not scope. Args: x: anything. Errors: none."""
    RAN.append(("half_declared", x))
    return {"ok": True}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False),
          meta={"writes": True, "side_effect": "write", "idempotent": False, "reversible": True, "scope": "batch"})
def fully_declared(x: str) -> dict:
    """Declares everything. Args: x: anything. Errors: none."""
    RAN.append(("fully_declared", x))
    return {"ok": True}
'''


@pytest.fixture
def bare(tmp_path, monkeypatch):
    srv = tmp_path / "srv"
    srv.mkdir()
    (srv / "bare_server.py").write_text(BARE_SERVER, encoding="utf-8")
    monkeypatch.syspath_prepend(str(srv))
    import sys
    sys.modules.pop("bare_server", None)
    yield srv
    sys.modules.pop("bare_server", None)


@pytest.mark.parametrize("tool,missing", [("mystery_write", "reversible and scope"), ("silent_tool", "writes"),
                                          ("half_declared", "scope")])
async def test_a_write_tool_missing_its_metadata_is_refused_by_name_even_with_allow_writes(bare, tmp_path, tool, missing):
    llm = ScriptedLLM([("call", f"bare__{tool}", {"x": "go"})])
    async with SpyRegistry(bare, pattern="bare_server.py") as reg:
        result = await Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", duties=None,
                                    allow_writes=True).run("do it")
        assert reg.calls == [], "the tool must not run, however the run was configured"
    import bare_server
    assert bare_server.RAN == []
    assert not result.success
    data = handoff(tmp_path)
    snapshot = data["error_snapshot"]
    assert snapshot["error_code"] == METADATA_MISSING and snapshot["recovery"] == "human"
    assert f"bare__{tool}" in snapshot["error_message"] and missing in snapshot["error_message"]
    assert not (tmp_path / "approvals").exists(), "an undescribable write is not approvable, so no pending record"


async def test_a_fully_declared_write_tool_runs_and_its_scope_is_surfaced(bare, tmp_path):
    llm = ScriptedLLM([("call", "bare__fully_declared", {"x": "go"})])
    async with SpyRegistry(bare, pattern="bare_server.py") as reg:
        result = await Orchestrator(reg, client=llm, handoff_dir=tmp_path, user_id="tester", duties=None).run("do it")
        assert reg.calls == []
    summary = result.pending_approval["writes"][0]["cost_summary"]
    assert summary["reversible"] is True and summary["scope"] == "batch" and summary["side_effect"] == "write"
    async with SpyRegistry(bare, pattern="bare_server.py") as reg:
        ok = await Orchestrator(reg, client=ScriptedLLM([("call", "bare__fully_declared", {"x": "go"})], [("text", "done")]),
                                user_id="tester", duties=None, allow_writes=True).run("do it")
    assert ok.success


def test_make_tool_refuses_to_define_a_write_tool_without_reversible_and_scope():
    from mcp.server.mcpserver import MCPServer

    from _base import make_tool
    tool = make_tool(MCPServer("t"))
    with pytest.raises(ValueError, match="must state reversible and scope"):
        tool(side_effect="write", idempotent=False)(lambda: None)
    with pytest.raises(ValueError, match="must state scope"):
        tool(side_effect="money", idempotent=False, reversible=False)
    with pytest.raises(ValueError, match="scope must be one of"):
        tool(side_effect="write", idempotent=False, reversible=False, scope="galaxy")
    with pytest.raises(ValueError, match="read tools take no reversible/scope"):
        tool(side_effect="read", scope="single")


def test_the_generator_refuses_a_write_operation_without_reversible_and_scope():
    from generators.from_spec import SpecError, check_semantics
    import copy

    from test_generator import PAYMENTS  # the money-operation fixture, with its metadata
    spec = copy.deepcopy(PAYMENTS)
    check_semantics(spec)  # complete: accepted
    for missing in ("reversible", "scope"):
        broken = copy.deepcopy(spec)
        del broken["operations"][0][missing]
        with pytest.raises(SpecError, match=f"create_payment: .*must state {missing}"):
            check_semantics(broken)


# -- name independence: the policy reads metadata, never a list of names ----------------------------------------------

async def test_classification_depends_on_the_published_metadata_never_on_the_tool_name(bare):
    async with InMemoryRegistry(bare, pattern="bare_server.py") as reg:
        assert classify(reg, "bare__fully_declared").writes is True and classify(reg, "bare__fully_declared").missing == ()
        silent = classify(reg, "bare__silent_tool")  # says nothing: fail closed, a write with nothing declared
        assert not silent.is_read and silent.missing == ("writes", "reversible", "scope")
    async with InMemoryRegistry(SERVERS, pattern="retail_server.py") as reg:
        # the same server, the same rule: a read by its own declaration, a write by its own declaration
        assert classify(reg, "retail__check_inventory").is_read and not classify(reg, "retail__create_order").is_read
        # rename the metadata, not the tool: the verdict follows the metadata
        reg._meta["retail__check_inventory"] = {"writes": True, "reversible": False, "scope": "single"}
        reg._read_only["retail__check_inventory"] = False
        assert not classify(reg, "retail__check_inventory").is_read


def test_the_policy_modules_name_no_tool_and_hold_no_list_of_write_tools():
    import asyncio

    async def names():
        async with InMemoryRegistry(SERVERS) as reg:
            return {t["name"].partition("__")[2] for t in reg.anthropic_tools()}

    tool_names = asyncio.run(names())
    assert {"create_order", "transfer_funds", "book_flight"} <= tool_names  # discovery really worked
    for module in ("write_policy.py", "approvals.py"):
        text = (ROOT / "agent" / module).read_text(encoding="utf-8")
        hits = sorted(n for n in tool_names if re.search(rf"\b{re.escape(n)}\b", text))
        assert hits == [], f"agent/{module} names tools: {hits}"
        assert not re.search(r"(?i)(write_tools|WRITE_TOOLS|WRITE_LIST|\bin \(\"|\bin \{\")", text), f"agent/{module} looks like it holds a list"
