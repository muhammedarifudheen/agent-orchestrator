"""Generic HTTP runtime for spec-generated MCP servers. Not itself a server.

``HttpClient(system, operations).call(op_name, args)`` turns an operation described in a system
spec into one HTTP request, and turns EVERY failure into ``ToolFailure``: argument constraints,
auth, network/timeouts, error statuses, malformed responses. Nothing raw escapes.

Timeout policy: if the request definitely never reached the server (connect failure/timeout) it is
retryable. If it may have reached the server (read timeout, dropped connection) it is retryable
only for idempotent operations; otherwise ``OUTCOME_UNKNOWN`` (not retryable).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from _base import ToolFailure, auth_from_env, standard_http_error, unknown_outcome

_MISSING = object()


def get_path(obj: Any, path: str) -> Any:
    """Dotted-path lookup ('a.b.0.c'); returns _MISSING if any step is absent."""
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return _MISSING
    return obj


def _status_matches(want: int | str, status: int) -> bool:
    if isinstance(want, int):
        return want == status
    return len(want) == 3 and want[1:] == "xx" and status // 100 == int(want[0])


def _rule_matches(when: dict[str, Any], status: int, body: Any) -> bool:
    if "status" in when and not _status_matches(when["status"], status):
        return False
    return all(get_path(body, p) == v for p, v in (when.get("body") or {}).items())


def _body_message(body: Any, text: str) -> str:
    """Human-readable detail from an error body; empty when the body carries no message ({} etc.)."""
    if isinstance(body, dict):
        for key in ("reason", "message", "error_description", "detail", "error"):
            if isinstance(body.get(key), str) and body[key]:
                return body[key][:300]
        return ""
    return "" if body is not None else text.strip()[:300]


class HttpClient:
    """Executes operations described by a system spec. ``transport`` is injectable for tests."""

    def __init__(self, system: dict[str, Any], operations: dict[str, dict[str, Any]],
                 transport: httpx.BaseTransport | None = None) -> None:
        self.system, self.operations, self.transport = system, operations, transport

    # -- request building ----------------------------------------------------------------------

    def _validate(self, op: dict[str, Any], args: dict[str, Any]) -> None:
        for p in op["params"]:
            name, value = p["name"], args.get(p["name"])
            if value is None:
                if p.get("required"):
                    raise ToolFailure("INVALID_ARGUMENT", f"'{name}' is required.", retryable=False,
                                      recovery="agent")
                continue
            c = p.get("constraints") or {}
            problem = None
            if "enum" in c and value not in c["enum"]:
                problem = f"must be one of {c['enum']}"
            elif "pattern" in c and not re.fullmatch(c["pattern"], str(value)):
                problem = f"must match pattern {c['pattern']}"
            elif "min_length" in c and len(str(value)) < c["min_length"]:
                problem = f"must have at least {c['min_length']} characters"
            elif "max_length" in c and len(str(value)) > c["max_length"]:
                problem = f"must have at most {c['max_length']} characters"
            elif "minimum" in c and value < c["minimum"]:
                problem = f"must be >= {c['minimum']}"
            elif "maximum" in c and value > c["maximum"]:
                problem = f"must be <= {c['maximum']}"
            if problem:
                raise ToolFailure("INVALID_ARGUMENT", f"'{name}' {problem} (got {value!r}).",
                                  retryable=False, recovery="agent")

    def _build(self, op: dict[str, Any], args: dict[str, Any]) -> tuple[str, dict, dict, Any]:
        base = (os.environ.get(self.system.get("base_url_env") or "") or self.system["base_url"]).rstrip("/")
        path, query, headers, body = op["path"], dict(op.get("query_const") or {}), {}, {}
        for p in op["params"]:
            value = args.get(p["name"])
            if value is None:
                continue
            wire = p.get("wire_name", p["name"])
            if p["in"] == "path":
                path = path.replace("{" + p["name"] + "}", quote(str(value), safe=""))
            elif p["in"] == "query":
                query[wire] = str(value).lower() if isinstance(value, bool) else value
            elif p["in"] == "header":
                headers[wire] = str(value)
            else:
                body[wire] = value
        return base + path, query, headers, (body or None)

    # -- execution -----------------------------------------------------------------------------

    def call(self, op_name: str, args: dict[str, Any]) -> Any:
        op = self.operations[op_name]
        if self.system.get("read_only") and (op["method"] not in ("GET", "HEAD") or op["side_effect"] != "read"):
            raise ToolFailure("READ_ONLY_VIOLATION",
                              f"{op_name} would write to a system declared read_only; nothing was sent.",
                              retryable=False, recovery="human")
        idempotent = bool(op.get("idempotent", op["side_effect"] == "read"))
        self._validate(op, args)
        url, query, headers, body = self._build(op, args)
        auth = auth_from_env(self.system.get("auth"))
        headers.update(auth.headers)
        query.update(auth.query)
        try:
            with httpx.Client(transport=self.transport, timeout=self.system.get("timeout_s", 10)) as client:
                resp = client.request(op["method"], url, params=query or None, headers=headers, json=body)
        except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:  # never left this machine
            raise ToolFailure("TIMEOUT", f"Could not connect in time ({type(exc).__name__}).",
                            retryable=True, recovery="agent") from exc
        except httpx.ConnectError as exc:
            raise ToolFailure("NETWORK_ERROR", f"Could not connect: {exc}", retryable=True,
                            recovery="agent") from exc
        except httpx.TimeoutException as exc:  # request may have been processed
            if idempotent:
                raise ToolFailure("TIMEOUT", f"{op_name} timed out ({type(exc).__name__}).",
                                retryable=True, recovery="agent") from exc
            raise unknown_outcome(op_name) from exc
        except httpx.TransportError as exc:
            if idempotent:
                raise ToolFailure("NETWORK_ERROR", f"Network error: {exc}", retryable=True,
                                recovery="agent") from exc
            raise unknown_outcome(op_name) from exc

        try:
            data: Any = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            raise self._map_error(op, idempotent, resp.status_code, data, resp.text)
        if data is None:
            raise ToolFailure("UNEXPECTED_RESPONSE", f"HTTP {resp.status_code} body is not JSON.",
                            retryable=False, recovery="human")
        select = (op.get("response") or {}).get("select")
        if select:
            data = get_path(data, select)
            if data is _MISSING:
                raise ToolFailure("UNEXPECTED_RESPONSE", f"Response has no '{select}' field.",
                                retryable=False, recovery="human")
        return data

    def _map_error(self, op: dict[str, Any], idempotent: bool, status: int, body: Any, text: str) -> ToolFailure:
        detail = _body_message(body, text)
        for rule in list(op.get("errors") or []) + list(self.system.get("error_map") or []):
            if _rule_matches(rule["when"], status, body):
                meaning = rule.get("meaning")
                msg = f"{meaning} ({detail})" if meaning and detail else (meaning or detail or "no details in response")
                return ToolFailure(rule["code"], f"HTTP {status}: {msg}", retryable=rule["retryable"],
                                   recovery=rule["recovery"], http_status=status)
        code, retryable, recovery = standard_http_error(status, idempotent)
        return ToolFailure(code, f"HTTP {status}: {detail or 'no details in response'}",
                           retryable=retryable, recovery=recovery, http_status=status)
