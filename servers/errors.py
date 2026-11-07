"""Shared error contract for MCP servers (not itself a server; not auto-discovered).

Every tool must be wrapped with ``@guarded`` and report failure ONLY by raising ``ToolFailure``.
It reaches the agent as ``is_error=True`` with the text ``[CODE] retryable=<true|false> message``.
"""
from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

F = TypeVar("F", bound=Callable[..., Any])


class ToolFailure(ToolError):
    """A tool failed. Subclasses the SDK's ToolError because v2 hides the text of any other
    exception from the client."""

    def __init__(self, code: str, message: str, retryable: bool) -> None:
        super().__init__(f"[{code}] retryable={str(retryable).lower()} {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


def guarded(fn: F) -> F:
    """Decorator for tools: any exception other than ToolFailure becomes ToolFailure.

    Place it directly under ``@mcp.tool()``. Signature and annotations are preserved so the SDK
    still derives the input schema from the type hints.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolFailure:
            raise
        except TimeoutError as exc:
            raise ToolFailure("TIMEOUT", f"{fn.__name__} timed out: {exc}", retryable=True) from exc
        except Exception as exc:  # contract: nothing raw escapes to the agent
            raise ToolFailure("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", retryable=False) from exc

    return wrapper  # type: ignore[return-value]
