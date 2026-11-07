"""Shared error contract for MCP servers (not itself a server; not auto-discovered).

Every tool must be wrapped with ``@guarded`` (normally via ``_base.make_tool``) and report failure
ONLY by raising ``ToolFailure``. It reaches the agent as ``is_error=True`` with the text
``[CODE] retryable=<true|false> message``.
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


def unknown_outcome(what: str) -> ToolFailure:
    """A non-idempotent call timed out: it may or may not have taken effect. NOT retryable."""
    return ToolFailure(
        "OUTCOME_UNKNOWN",
        f"{what} timed out and may or may not have completed. Do not retry; verify the current "
        "state first (or hand off to a human).",
        retryable=False,
    )


def guarded(fn: F | None = None, *, idempotent: bool = True) -> Any:
    """Decorator for tools: any exception other than ToolFailure becomes ToolFailure.

    Use as ``@guarded`` or ``@guarded(idempotent=False)``. A TimeoutError is ``TIMEOUT
    retryable=true`` for idempotent tools, but ``OUTCOME_UNKNOWN retryable=false`` otherwise (a
    timed-out charge or transfer may have happened; retrying risks doing it twice).

    Signature and annotations are preserved so the SDK still derives the input schema from the
    type hints.
    """

    def decorate(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except ToolFailure:
                raise
            except TimeoutError as exc:
                if not idempotent:
                    raise unknown_outcome(func.__name__) from exc
                raise ToolFailure("TIMEOUT", f"{func.__name__} timed out: {exc}", retryable=True) from exc
            except Exception as exc:  # contract: nothing raw escapes to the agent
                raise ToolFailure("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", retryable=False) from exc

        wrapper.__guarded__ = True  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorate if fn is None else decorate(fn)
