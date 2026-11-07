"""`read_only: true` is ENFORCED: in the spec, in the generator, in the generated tool list and in the
HTTP runtime. Each test below fails if the corresponding layer stops enforcing it."""
from __future__ import annotations

import copy
import re
from pathlib import Path

import httpx
import pytest
import yaml
from mcp import Client

import jsonplaceholder_server as server
from _http import HttpClient
from errors import ToolFailure
from generators.from_spec import ROOT, SpecError, generate, load_spec
from test_generator import PAYMENTS, write_spec

SPEC = ROOT / "specs" / "jsonplaceholder.yaml"
WRITE_VERBS = re.compile(r"^(create|add|update|edit|delete|remove|post|put|patch|set|send|charge|transfer|book)_")


def test_spec_carries_an_explicit_read_only_marker():
    raw = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    assert raw.get("read_only") is True, "specs/jsonplaceholder.yaml must declare `read_only: true`"
    assert all(op["side_effect"] == "read" for op in raw["operations"])


async def test_generated_tool_list_contains_no_write_operation():
    async with Client(server.mcp) as c:
        tools = (await c.list_tools()).tools
    assert sorted(t.name for t in tools) == ["get_post", "get_user", "list_comments_for_post", "list_posts_by_user"]
    for t in tools:
        assert t.annotations.read_only_hint is True, f"{t.name} is not annotated read-only"
        assert not WRITE_VERBS.match(t.name), f"{t.name} looks like a write operation"
    assert all(op["method"] in ("GET", "HEAD") and op["side_effect"] == "read" for op in server.OPERATIONS.values())
    assert server.SYSTEM["read_only"] is True  # the marker reached the generated server


def test_generator_refuses_a_write_operation_in_a_read_only_spec(tmp_path):
    spec = copy.deepcopy(PAYMENTS)  # a fully REVIEWED money operation: passes every other gate
    spec["read_only"] = True
    with pytest.raises(SpecError, match="read_only: true"):
        load_spec(write_spec(tmp_path, spec))
    del spec["read_only"]
    assert load_spec(write_spec(tmp_path, spec))  # same spec is fine without the marker: the marker is what blocks


def test_marker_is_emitted_into_the_generated_server(tmp_path):
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    servers, tests = tmp_path / "s", tmp_path / "t"
    servers.mkdir(), tests.mkdir()
    spec_path = tmp_path / "jsonplaceholder.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    generate(spec_path, servers, tests)
    assert "'read_only': True" in (servers / "jsonplaceholder_server.py").read_text(encoding="utf-8")


def test_runtime_refuses_to_send_a_write_even_if_the_operations_table_is_tampered_with():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    op = {"method": "POST", "path": "/posts", "side_effect": "write", "idempotent": False,
          "params": [], "response": {"select": None}, "errors": []}
    client = HttpClient({"base_url": "https://h.test", "read_only": True}, {"create_post": op},
                        httpx.MockTransport(handler))
    with pytest.raises(ToolFailure) as e:
        client.call("create_post", {})
    assert (e.value.code, e.value.retryable, e.value.recovery) == ("READ_ONLY_VIOLATION", False, "human")
    assert sent == [], "a request was sent to a read_only system"
    # a GET mislabelled as read is still fine; a mislabelled write (POST + side_effect=read) is also refused
    op2 = {**op, "side_effect": "read"}
    with pytest.raises(ToolFailure):
        HttpClient({"base_url": "https://h.test", "read_only": True}, {"x": op2}, httpx.MockTransport(handler)).call("x", {})
    assert sent == []
