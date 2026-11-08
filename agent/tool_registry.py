"""Discovers and connects all MCP servers; exposes their tools to the LLM.

Industry-agnostic: any ``servers/*_server.py`` is spawned over stdio, its tools are listed and
namespaced ``<server>__<tool>``. Every call is normalized into a ``ToolOutcome`` — a failure is
returned as data (never raised, never swallowed) so the orchestrator can route it.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SEP = "__"
# Server error contract (servers/errors.py): "[CODE] retryable=<true|false> recovery=<agent|human> message"
_CODE = re.compile(
    r"^\s*(?:Error executing tool \S+:\s*)?\[([A-Z][A-Z0-9_]*)\]\s+retryable=(true|false)"
    r"\s+recovery=(agent|human)\s*(?:http_status=(\d{3})\s+)?(.*)", re.S
)
# The SDK itself rejects bad/missing arguments before the tool runs, with pydantic text that does
# not follow the server contract. Normalize it here so the agent always sees [CODE] retryable=...
_ARG_VALIDATION = re.compile(r"\d+ validation errors? for \w+Arguments")


class ParsedError(NamedTuple):
    code: str
    retryable: bool | None  # None = text did not follow the contract
    recovery: str | None  # "agent" | "human" | None (unknown: callers must treat as human)
    message: str
    http_status: int | None = None  # structured upstream HTTP status (D18); None when the failure had none


def parse_error(text: str) -> ParsedError:
    """Parse an is_error text. Anything not following the contract keeps retryable/recovery None."""
    m = _CODE.match(text)
    if m:
        return ParsedError(m.group(1), m.group(2) == "true", m.group(3), m.group(5),
                           int(m.group(4)) if m.group(4) else None)
    if _ARG_VALIDATION.search(text):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and "errors.pydantic.dev" not in ln]
        return ParsedError("INVALID_ARGUMENTS", False, "agent", "; ".join(lines[:6])[:400])
    return ParsedError("TOOL_ERROR", None, None, text or "unknown error")


@dataclass
class ToolOutcome:
    """Normalized result of one tool call."""

    ok: bool
    tool: str
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None  # None = unknown (error did not follow the contract)
    recovery: str | None = None  # "agent" | "human"; None = unknown, treated as human
    duration_ms: int = 0
    http_status: int | None = None  # upstream HTTP status when the failure carried one (D18)

    def to_llm_text(self) -> str:
        """Render for Claude as the tool_result content."""
        if self.ok:
            return json.dumps(self.data, ensure_ascii=False, default=str)
        flag = "unknown" if self.retryable is None else str(self.retryable).lower()
        return f"[{self.error_code}] retryable={flag} recovery={self.recovery or 'unknown'} {self.error_message}"


@dataclass
class ToolRegistry:
    """Owns the lifecycle of all MCP server connections. Use as ``async with``."""

    servers_dir: Path
    pattern: str = "*_server.py"
    _stack: AsyncExitStack = field(default_factory=AsyncExitStack, init=False)
    _sessions: dict[str, ClientSession] = field(default_factory=dict, init=False)
    _tools: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    connect_errors: dict[str, str] = field(default_factory=dict, init=False)

    async def __aenter__(self) -> "ToolRegistry":
        await self._stack.__aenter__()
        for script in sorted(self.servers_dir.glob(self.pattern)):
            name = script.stem.removesuffix("_server")
            try:
                await self._connect(name, script)
            except Exception as exc:  # one broken server must not take the others down
                self.connect_errors[name] = f"{type(exc).__name__}: {exc}"
        return self

    async def __aexit__(self, *exc_info: Any) -> bool | None:
        return await self._stack.__aexit__(*exc_info)

    async def _connect(self, name: str, script: Path) -> None:
        params = StdioServerParameters(
            command=sys.executable, args=[str(script)], env=dict(os.environ)
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[name] = session
        for tool in (await session.list_tools()).tools:
            self._tools[f"{name}{SEP}{tool.name}"] = {
                "name": f"{name}{SEP}{tool.name}",
                "description": tool.description or "",
                "input_schema": tool.input_schema,
            }

    def anthropic_tools(self) -> list[dict[str, Any]]:
        """Tool definitions in Claude API format."""
        return list(self._tools.values())

    async def call(self, qualified_name: str, args: dict[str, Any]) -> ToolOutcome:
        """Call a tool. Never raises: all failures come back as ``ToolOutcome(ok=False)``."""
        start = time.monotonic()

        def fail(code: str, msg: str, retryable: bool | None = None, recovery: str | None = None,
                 http_status: int | None = None) -> ToolOutcome:
            return ToolOutcome(False, qualified_name, error_code=code, error_message=msg, retryable=retryable,
                               recovery=recovery, duration_ms=int((time.monotonic() - start) * 1000),
                               http_status=http_status)

        if qualified_name not in self._tools:
            return fail("UNKNOWN_TOOL", f"No tool named '{qualified_name}'.", retryable=False, recovery="agent")
        server, _, tool = qualified_name.partition(SEP)
        try:
            result = await self._sessions[server].call_tool(tool, args)
        except Exception as exc:  # transport/protocol failure
            return fail("TRANSPORT_ERROR", f"{type(exc).__name__}: {exc}", retryable=False,
                        recovery="human")  # a call may have been delivered: outcome unknown

        text = "\n".join(getattr(c, "text", "") for c in result.content).strip()
        if result.is_error:
            e = parse_error(text)
            return fail(e.code, e.message, retryable=e.retryable, recovery=e.recovery, http_status=e.http_status)
        data: Any = result.structured_content
        if isinstance(data, dict) and set(data) == {"result"}:  # SDK wraps non-dict returns
            data = data["result"]
        if data is None:  # untyped returns (e.g. bare dict) arrive only as JSON text
            try:
                data = json.loads(text)
            except ValueError:
                data = text
        return ToolOutcome(True, qualified_name, data=data,
                           duration_ms=int((time.monotonic() - start) * 1000))
