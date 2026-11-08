"""http_status is structured data (D18): ToolFailure -> wire -> registry -> handoff -> collect -> propose.
Prefix-parsing the message is a deprecated fallback for old handoffs and must not be load-bearing."""
from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

from agent.orchestrator import Orchestrator
from agent.tool_registry import parse_error
from errors import ToolFailure
from inmem import InMemoryRegistry
from learning import collect, propose
from learning.patchfmt import apply_diff, parse_proposal

ROOT = Path(__file__).resolve().parents[2]


def weather_handoff(dir_: Path, name: str, ts: str, **snap_extra) -> None:
    snap = {"step": 1, "tool": "weather__get_current_weather", "arguments": {"latitude": 1.0, "longitude": 2.0},
            "error_code": "NOT_FOUND", "error_message": "no such grid cell",  # NB: wording has no status in it
            "retryable": False, "recovery": "agent", **snap_extra}
    (dir_ / name).write_text(json.dumps({"created_at": ts, "steps": [], "error_snapshot": snap}), encoding="utf-8")


def test_toolfailure_status_survives_the_wire_and_is_not_part_of_the_message():
    text = str(ToolFailure("NOT_FOUND", "gone", retryable=False, recovery="agent", http_status=404))
    parsed = parse_error(text)
    assert (parsed.code, parsed.retryable, parsed.recovery, parsed.message, parsed.http_status) == \
        ("NOT_FOUND", False, "agent", "gone", 404)
    plain = str(ToolFailure("NOT_FOUND", "gone", retryable=False, recovery="agent"))
    assert "http_status" not in plain and parse_error(plain).http_status is None
    assert plain == "[NOT_FOUND] retryable=false recovery=agent gone", "no-status wire format must be unchanged"


FAKE_SERVER = """
from mcp.server.mcpserver import MCPServer
from _base import make_tool
from errors import ToolFailure

mcp = MCPServer("httpstatus")
tool = make_tool(mcp)


@tool(side_effect="read", idempotent=True)
def lookup_record(record_id: str) -> dict:
    \"\"\"Look up a record. Call when you know the record id. Errors: NOT_FOUND.

    Args:
        record_id: the record id.
    \"\"\"
    raise ToolFailure("NOT_FOUND", "upstream has no such record", retryable=False, recovery="human", http_status=404)
"""


class OneCallLLM:
    def __init__(self) -> None:
        self.messages = self

    async def create(self, **_):
        return NS(content=[NS(type="tool_use", id="a", name="httpstatus__lookup_record", input={"record_id": "R1"})])


async def test_handoff_stores_http_status_as_its_own_field(tmp_path, monkeypatch):
    servers = tmp_path / "srv"
    servers.mkdir()
    (servers / "httpstatus_server.py").write_text(FAKE_SERVER, encoding="utf-8")
    monkeypatch.syspath_prepend(str(servers))
    async with InMemoryRegistry(servers, pattern="httpstatus_server.py") as reg:
        await Orchestrator(reg, client=OneCallLLM(), handoff_dir=tmp_path, user_id="tester").run("find R1")
    data = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert data["error_snapshot"]["http_status"] == 404
    assert data["error_snapshot"]["error_message"] == "upstream has no such record"
    assert data["steps"][-1]["result"]["http_status"] == 404
    assert data["attempted_operations"][-1]["http_status"] == 404


def test_http_status_alone_drives_an_add_error_code_proposal_when_the_message_has_no_404(tmp_path):
    h = tmp_path / "handoffs"
    h.mkdir()
    weather_handoff(h, "a.json", "2026-11-08T09:00:00+00:00", http_status=404)
    weather_handoff(h, "b.json", "2026-11-08T10:00:00+00:00", http_status=404)
    recs = collect.collect(h, tmp_path / "f.jsonl")
    assert recs[0]["status"] == 404 and "404" not in recs[0]["message"]
    assert "deprecated_parse" not in recs[0], "the structured field was used, not the prefix fallback"

    specs = tmp_path / "specs"
    specs.mkdir()
    shutil.copy(ROOT / "specs" / "weather.yaml", specs / "weather.yaml")
    written = propose.propose(tmp_path / "f.jsonl", specs, tmp_path / "proposals")
    assert [p.name for p in written] == ["weather__get_current_weather__NOT_FOUND__add_error_code.patch"]
    header, diff = parse_proposal(written[0].read_text(encoding="utf-8"))
    assert header["proposal"] == "add_error_code" and header["error_code"] == "NOT_FOUND"
    patched = yaml.safe_load(apply_diff((specs / "weather.yaml").read_text(encoding="utf-8").replace("\r\n", "\n"), diff))
    op = next(o for o in patched["operations"] if o["name"] == "get_current_weather")
    rules = [r for r in op["errors"] if r["code"] == "NOT_FOUND"]
    assert len(rules) == 1 and rules[0]["when"] == {"status": 404}


def test_old_handoff_without_the_field_uses_the_deprecated_prefix_fallback_and_says_so(tmp_path):
    h = tmp_path / "handoffs"
    h.mkdir()
    weather_handoff(h, "old.json", "2026-11-08T09:00:00+00:00", error_message="HTTP 404: no such grid cell")
    rec = collect.collect(h, tmp_path / "f.jsonl")[0]
    assert rec["status"] == 404 and rec["deprecated_parse"] is True


def test_an_explicit_null_http_status_is_never_guessed_from_the_message(tmp_path):
    h = tmp_path / "handoffs"
    h.mkdir()
    weather_handoff(h, "n.json", "2026-11-08T09:00:00+00:00", error_message="HTTP 404: looks like one", http_status=None)
    rec = collect.collect(h, tmp_path / "f.jsonl")[0]
    assert rec["status"] is None and "deprecated_parse" not in rec


def test_prefix_parsing_exists_only_inside_the_deprecated_fallback_function():
    """grep for startswith("HTTP in learning/: it may appear only in _http_status_from_prefix."""
    found = []
    for path in (ROOT / "learning").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "startswith" and node.args
                            and isinstance(node.args[0], ast.Constant) and str(node.args[0].value).startswith("HTTP")):
                        found.append((path.name, fn.name))
    assert found == [("collect.py", "_http_status_from_prefix")], found
