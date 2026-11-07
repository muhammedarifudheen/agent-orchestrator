"""Small test helpers: (code, retryable[, recovery]) of a failed tool call result."""
from __future__ import annotations

from agent.tool_registry import parse_error


def _parsed(result):
    return parse_error("\n".join(getattr(c, "text", "") for c in result.content))


def parse(result) -> tuple[str, bool | None]:
    e = _parsed(result)
    return e.code, e.retryable


def parse_full(result) -> tuple[str, bool | None, str | None]:
    e = _parsed(result)
    return e.code, e.retryable, e.recovery
