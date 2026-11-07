"""Small test helper: (code, retryable) of a failed tool call result."""
from __future__ import annotations

from agent.tool_registry import parse_error


def parse(result) -> tuple[str, bool | None]:
    text = "\n".join(getattr(c, "text", "") for c in result.content)
    code, retryable, _ = parse_error(text)
    return code, retryable
