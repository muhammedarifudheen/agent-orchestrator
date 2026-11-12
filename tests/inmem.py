"""Registries that run the real MCP servers in-process (no subprocess, no network).

Lets tests drive the real Orchestrator + ToolRegistry code against real servers, and monkeypatch tools.
"""
from __future__ import annotations

import importlib
from pathlib import Path

from mcp import Client

from agent.tool_registry import ToolRegistry


class InMemoryRegistry(ToolRegistry):
    """Same registry, but servers run in-process so tests can monkeypatch their tools."""

    async def _connect(self, name: str, script: Path) -> None:
        import importlib

        module = importlib.import_module(script.stem)
        client = await self._stack.enter_async_context(Client(module.mcp))
        self._sessions[name] = client  # Client exposes list_tools/call_tool like ClientSession
        for tool in (await client.list_tools()).tools:
            self._tools[f"{name}__{tool.name}"] = {
                "name": f"{name}__{tool.name}", "description": tool.description or "",
                "input_schema": tool.input_schema}
            self._record_annotations(f"{name}__{tool.name}", tool)


class SpyRegistry(InMemoryRegistry):
    """Records every tool call that actually reaches a server."""

    calls: list[str]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = []  # present (and empty) before the first call, so "nothing reached a server" can be asserted

    async def call(self, qualified_name, args):
        self.calls.append(qualified_name)
        return await super().call(qualified_name, args)
