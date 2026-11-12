"""The orchestrator has no industry knowledge (D19). Two proofs, both discovered from the registry, not hard-coded:

1. For EVERY registered server and EVERY tool it exposes, a stub goal makes the real Orchestrator call that tool and the
   result comes back through the one contract (ok, or a failure with retryable/recovery) with no per-server branch.
2. The orchestrator package contains no server name, no tool name, and none of the industry words from the brief.

The stub "plan" is a stand-in for the model (it only names a tool); what this proves is that the orchestrator and
registry treat every industry identically, not that a real model plans well.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

from agent.orchestrator import Orchestrator
from agent.tool_registry import SEP
from inmem import InMemoryRegistry

ROOT = Path(__file__).resolve().parent.parent
SERVERS = ROOT / "servers"
SERVER_NAMES = sorted(p.name[: -len("_server.py")] for p in SERVERS.glob("*_server.py"))
# Words from the brief that must not appear in orchestrator logic. Every allowed hit needs a justification here.
BRIEF_WORDS = ["travel", "retail", "banking", "weather", "flight", "kyc", "order"]
ALLOWED_HITS: dict[tuple[str, str], str] = {}  # (file, word) -> why it is not industry logic. Empty on purpose.


class PlanLLM:
    """Stub model: turn 1 calls one named tool with no arguments, turn 2 says it is done."""

    def __init__(self, tool: str) -> None:
        self.messages = self
        self.tool, self.turns = tool, 0

    async def create(self, **_):
        self.turns += 1
        if self.turns == 1:
            return NS(content=[NS(type="text", text="Doing the step."),
                               NS(type="tool_use", id="t1", name=self.tool, input={})])
        return NS(content=[NS(type="text", text="Stopping.")])


@pytest.fixture(autouse=True)
def stub_upstream(monkeypatch):
    """HTTP-backed (generated) servers get a stub upstream that answers 503, instead of a real connection attempt.
    The real tool code still runs (validation, error mapping, contract text); only the socket is replaced. Without
    this every no-argument tool paid ~0.3 s to build an SSL context just to be refused by the offline guard, which put
    the 2 s gate budget at risk on a loaded host (D17)."""
    real = httpx.Client
    handler = lambda request: httpx.Response(503, json={"error": "stub upstream"})
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}))


def test_every_server_in_the_directory_is_registered_and_the_params_cover_them():
    assert len(SERVER_NAMES) >= 8, SERVER_NAMES  # travel, retail, banking + the five generated ones


@pytest.mark.parametrize("server", SERVER_NAMES)
async def test_orchestrator_drives_every_tool_of_this_server_through_the_same_contract(server, tmp_path):
    # scoped to this server: the cost must not grow with the number of OTHER servers (D17)
    async with InMemoryRegistry(SERVERS, pattern=f"{server}_server.py") as reg:
        tools = [t["name"] for t in reg.anthropic_tools() if t["name"].partition(SEP)[0] == server]
        assert tools, f"registry discovered no tools for {server}"
        for tool in tools:
            result = await Orchestrator(reg, allow_writes=True, client=PlanLLM(tool), handoff_dir=tmp_path / tool,
                                        user_id="tester").run("a stub goal")
            calls = [o for o in result.state.operations if o.tool == tool]
            assert len(calls) >= 1, f"{tool}: the orchestrator never reached it"
            first = calls[0]
            if not first.ok:  # every failure, from every industry, arrives in the one contract shape
                assert first.error_code not in {"TOOL_ERROR", "TRANSPORT_ERROR", "UNKNOWN_TOOL"}, (tool, first.error_code)
                assert isinstance(first.retryable, bool) and first.recovery in {"agent", "human"}, tool
            if result.handoff is not None:  # and every handoff names the industry generically
                assert result.handoff.failure_industry == server or result.handoff.failure_industry is None
                assert set(result.handoff.industries_touched) == {server}


def test_orchestrator_source_names_no_industry_no_server_and_no_tool():
    """Forbidden = the brief's words + every server name + every tool name the registry exposes (discovered)."""
    async def discover() -> set[str]:
        async with InMemoryRegistry(SERVERS) as reg:
            return {t["name"].partition(SEP)[2] for t in reg.anthropic_tools()}

    import asyncio
    tool_names = asyncio.run(discover())
    forbidden = set(BRIEF_WORDS) | set(SERVER_NAMES) | tool_names
    assert {"travel", "retail", "banking", "search_flights", "transfer_funds"} <= forbidden  # discovery really worked
    hits = []
    for path in sorted((ROOT / "agent").glob("*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for word in forbidden:
                if re.search(rf"\b{re.escape(word)}\b", line, re.IGNORECASE) and (path.name, word) not in ALLOWED_HITS:
                    hits.append(f"{path.name}:{no}: {word!r}: {line.strip()[:80]}")
    assert hits == [], "industry-specific text in orchestrator logic:\n" + "\n".join(hits)


UNSEEN_SERVER = '''
from mcp.server.mcpserver import MCPServer
from _base import make_tool
from errors import ToolFailure

mcp = MCPServer("aquarium")
tool = make_tool(mcp)


@tool(side_effect="write", idempotent=False, reversible=False, scope="single")
def feed_fish(tank_id: str) -> dict:
    """Feed the fish in a tank. Call when a tank is hungry. Errors: TANK_SEALED.

    Args:
        tank_id: the tank id.
    """
    if tank_id == "T-SEALED":
        raise ToolFailure("TANK_SEALED", "The tank is sealed for maintenance.", retryable=False, recovery="human")
    return {"tank_id": tank_id, "fed": True}
'''


class FeedLLM:
    def __init__(self, tank: str) -> None:
        self.messages, self.tank, self.turns = self, tank, 0

    async def create(self, **_):
        self.turns += 1
        if self.turns == 1:
            return NS(content=[NS(type="tool_use", id="f1", name="aquarium__feed_fish", input={"tank_id": self.tank})])
        return NS(content=[NS(type="text", text="Done.")])


async def test_a_brand_new_industry_works_with_zero_orchestrator_changes(tmp_path, monkeypatch):
    """An industry that did not exist when the orchestrator was written: success and failure both route correctly."""
    servers = tmp_path / "srv"
    servers.mkdir()
    (servers / "aquarium_server.py").write_text(UNSEEN_SERVER, encoding="utf-8")
    monkeypatch.syspath_prepend(str(servers))
    async with InMemoryRegistry(servers, pattern="aquarium_server.py") as reg:
        ok = await Orchestrator(reg, allow_writes=True, client=FeedLLM("T-1"), handoff_dir=tmp_path / "ok", user_id="tester").run("feed")
        assert ok.success and ok.handoff is None
        bad = await Orchestrator(reg, allow_writes=True, client=FeedLLM("T-SEALED"), handoff_dir=tmp_path / "bad", user_id="tester").run("feed")
    assert not bad.success and bad.handoff.failure_industry == "aquarium"
    data = json.loads(next((tmp_path / "bad").glob("*.json")).read_text(encoding="utf-8"))
    assert data["industries_touched"] == ["aquarium"] and data["error_snapshot"]["error_code"] == "TANK_SEALED"
