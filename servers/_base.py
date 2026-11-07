"""Shared core for every MCP server, hand-written or generated. Not itself a server.

    from _base import ToolFailure, make_tool, check_outage

    mcp = MCPServer("shop")
    tool = make_tool(mcp)

    @tool(side_effect="money", idempotent=False)
    def charge(...): ...

Contents: the ToolFailure contract, the tool-registration decorator (MCP registration + error
guard + safety annotations in one place), the auth-from-env helper, outage simulation, and the
standard HTTP-status -> (error code, retryable, recovery) mapping.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from errors import ToolFailure, guarded, unknown_outcome

__all__ = ["ToolFailure", "guarded", "unknown_outcome", "make_tool", "check_outage", "outage_active",
           "auth_from_env", "AuthMaterial", "standard_http_error", "SIDE_EFFECTS"]

SIDE_EFFECTS = ("read", "write", "money")


# -- tool registration -------------------------------------------------------------------------

def make_tool(mcp: MCPServer) -> Callable[..., Callable[[Callable], Callable]]:
    """Return a ``tool(side_effect=..., idempotent=...)`` decorator bound to ``mcp``.

    It registers the function as an MCP tool, wraps it in ``@guarded`` with the right timeout
    policy, and publishes the safety facts as MCP tool annotations (``read_only_hint``,
    ``idempotent_hint``) so clients and the shared contract test can read them.

    ``side_effect``: "read" (no effect), "write" (changes state), "money" (moves money).
    ``idempotent`` defaults to True for reads and MUST be stated for write/money tools.
    """

    def tool(*, side_effect: str = "read", idempotent: bool | None = None) -> Callable[[Callable], Callable]:
        if side_effect not in SIDE_EFFECTS:
            raise ValueError(f"side_effect must be one of {SIDE_EFFECTS}, got {side_effect!r}")
        if idempotent is None:
            if side_effect != "read":
                raise ValueError("write/money tools must state idempotent=True|False explicitly")
            idempotent = True
        annotations = ToolAnnotations(read_only_hint=side_effect == "read", idempotent_hint=idempotent)

        def decorate(fn: Callable) -> Callable:
            return mcp.tool(annotations=annotations)(guarded(fn, idempotent=idempotent))

        return decorate

    return tool


# -- outage simulation (mock backends) ---------------------------------------------------------

def outage_active(env_var: str, tool: str, modes: dict[str, set[str] | None]) -> bool:
    """True if ``env_var`` selects a mode that covers ``tool``.

    ``modes`` maps mode name -> set of affected tool names (None = every tool). The mode
    ``all_down`` always covers every tool.
    """
    mode = os.environ.get(env_var, "")
    if mode == "all_down":
        return True
    if mode not in modes:
        return False
    affected = modes[mode]
    return affected is None or tool in affected


def check_outage(env_var: str, tool: str, modes: dict[str, set[str] | None], *, code: str,
                 message: str, retryable: bool = True, recovery: str = "agent") -> None:
    """Raise ToolFailure(code, ...) when a simulated outage covers ``tool`` (see outage_active)."""
    if outage_active(env_var, tool, modes):
        raise ToolFailure(code, f"{message} (simulated: {os.environ.get(env_var)}).",
                          retryable=retryable, recovery=recovery)


# -- auth from environment ---------------------------------------------------------------------

@dataclass
class AuthMaterial:
    """Credentials resolved from the environment, ready to attach to an HTTP request."""

    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ToolFailure("AUTH_NOT_CONFIGURED", f"Environment variable {name} is not set.", retryable=False,
                          recovery="human")
    return value


def auth_from_env(auth: dict[str, Any] | None) -> AuthMaterial:
    """Resolve an auth spec block to request credentials. Secrets only ever come from env vars.

    kinds: ``none``; ``api_key`` (``env``, and ``header`` or ``query`` name); ``bearer`` (``env``);
    ``basic`` (``user_env``, ``password_env``). OAuth/MFA/signing flows are not supported here.
    """
    kind = (auth or {}).get("kind", "none")
    if kind == "none":
        return AuthMaterial()
    assert auth is not None
    if kind == "api_key":
        value = _env(auth["env"])
        if auth.get("query"):
            return AuthMaterial(query={auth["query"]: value})
        return AuthMaterial(headers={auth.get("header", "X-API-Key"): value})
    if kind == "bearer":
        return AuthMaterial(headers={"Authorization": f"Bearer {_env(auth['env'])}"})
    if kind == "basic":
        raw = f"{_env(auth['user_env'])}:{_env(auth['password_env'])}".encode()
        return AuthMaterial(headers={"Authorization": "Basic " + base64.b64encode(raw).decode()})
    raise ToolFailure("AUTH_NOT_CONFIGURED", f"Unsupported auth kind '{kind}'.", retryable=False,
                      recovery="human")


# -- standard error code -> recovery mapping ---------------------------------------------------

# status -> (code, retryable, recovery)
_STANDARD_4XX: dict[int, tuple[str, bool, str]] = {
    400: ("INVALID_REQUEST", False, "agent"),   # the agent can fix its arguments
    401: ("AUTH_FAILED", False, "human"),       # credentials are not the agent's to fix
    403: ("FORBIDDEN", False, "human"),
    404: ("NOT_FOUND", False, "agent"),         # likely a wrong id; the agent can look it up again
    408: ("TIMEOUT", True, "agent"),
    409: ("CONFLICT", False, "human"),          # state conflict: retrying could duplicate a side effect
    422: ("INVALID_REQUEST", False, "agent"),
    429: ("RATE_LIMITED", True, "agent"),       # rejected before processing: safe to retry later
}


def standard_http_error(status: int, idempotent: bool) -> tuple[str, bool, str]:
    """Default (code, retryable, recovery) for an HTTP error status without an explicit mapping.

    4xx are the caller's fault (not retryable) except 408/429. 5xx are retryable, and left to the
    agent, only for idempotent operations: for a write/money call a 5xx leaves the outcome
    uncertain, so it goes to a human.
    """
    if status in _STANDARD_4XX:
        return _STANDARD_4XX[status]
    if 500 <= status <= 599:
        return "SERVICE_UNAVAILABLE", idempotent, "agent" if idempotent else "human"
    if 400 <= status <= 499:
        return "REQUEST_REJECTED", False, "human"
    return "UNEXPECTED_RESPONSE", False, "human"
