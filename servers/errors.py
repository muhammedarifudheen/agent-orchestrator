"""Shared error contract for MCP servers (not itself a server; not auto-discovered).

Every tool must be wrapped with ``@guarded`` (normally via ``_base.make_tool``) and report failure
ONLY by raising ``ToolFailure``. It reaches the agent as ``is_error=True`` with the text
``[CODE] retryable=<true|false> recovery=<agent|human> message``.

Two independent facts, both mandatory on every failure:
  * ``retryable``: may the SAME call succeed if repeated later (transient fault)?
  * ``recovery``: WHO can resolve it?
      "agent": the agent can plausibly fix it itself: bad/missing/mistyped arguments, a wrong id it
               can look up again, wrong call order, a transient fault worth one retry.
      "human": it needs a decision or authority the agent does not have: a business decline
               (card declined, insufficient funds, frozen account, KYC), a state conflict that
               could cause a duplicate side effect, auth/permission problems, unknown outcomes,
               internal faults.
"""
from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

F = TypeVar("F", bound=Callable[..., Any])

RECOVERY = ("agent", "human")


class ToolFailure(ToolError):
    """A tool failed. Subclasses the SDK's ToolError because v2 hides the text of any other
    exception from the client."""

    def __init__(self, code: str, message: str, retryable: bool, recovery: str) -> None:
        if recovery not in RECOVERY:
            raise ValueError(f"recovery must be one of {RECOVERY}, got {recovery!r}")
        super().__init__(f"[{code}] retryable={str(retryable).lower()} recovery={recovery} {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.recovery = recovery


def unknown_outcome(what: str) -> ToolFailure:
    """A non-idempotent call timed out: it may or may not have taken effect. Needs a human."""
    return ToolFailure(
        "OUTCOME_UNKNOWN",
        f"{what} timed out and may or may not have completed. Do not retry; verify the current "
        "state first (or hand off to a human).",
        retryable=False,
        recovery="human",
    )


def guarded(fn: F | None = None, *, idempotent: bool = True) -> Any:
    """Decorator for tools: any exception other than ToolFailure becomes ToolFailure.

    Use as ``@guarded`` or ``@guarded(idempotent=False)``. A TimeoutError is ``TIMEOUT
    retryable=true recovery=agent`` for idempotent tools, but ``OUTCOME_UNKNOWN retryable=false
    recovery=human`` otherwise (a timed-out charge or transfer may have happened; retrying risks
    doing it twice). Any other exception is ``INTERNAL_ERROR`` for a human.

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
                raise ToolFailure("TIMEOUT", f"{func.__name__} timed out: {exc}",
                                  retryable=True, recovery="agent") from exc
            except Exception as exc:  # contract: nothing raw escapes to the agent
                raise ToolFailure("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}",
                                  retryable=False, recovery="human") from exc

        wrapper.__guarded__ = True  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorate if fn is None else decorate(fn)
