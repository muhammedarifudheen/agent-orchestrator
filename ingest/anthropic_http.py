"""A minimal client for the Anthropic Messages API over plain httpx. No SDK.

Why it exists: on the machine this project was built on, `import anthropic` fails (its `jiter` DLL is blocked by a
Windows Application Control policy), so an SDK-based client could not run here even with a key. Speaking the documented
HTTP API directly removes that dependency AND puts the injectable seam at the wire format: a fake httpx transport sees
the exact request body and answers with a real-shaped response (tests/fake_anthropic.py).

Surface used by callers: `client.messages.create(model=, max_tokens=, system=, tools=, tool_choice=, messages=)`
returning an object with `.content` (blocks with `.type`, `.name`, `.input`, `.text`) and `.stop_reason`; the same
surface the SDK offers, so SDK-shaped stubs and this client are interchangeable.
Not verified against the live API in this repo (no key): the request/response shapes follow the published Messages API.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import httpx

API_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"


class AnthropicAPIError(RuntimeError):
    """The API answered with a non-2xx status, or with something that is not a Messages response."""

    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(f"HTTP {status} {kind}: {message}")
        self.status, self.kind = status, kind


def _obj(value: Any, keep_dict: bool = False) -> Any:
    """JSON -> attribute access (`block.type`), except tool `input` stays a plain dict."""
    if isinstance(value, dict) and not keep_dict:
        return SimpleNamespace(**{k: _obj(v, keep_dict=(k == "input")) for k, v in value.items()})
    if isinstance(value, list):
        return [_obj(v) for v in value]
    return value


class _Messages:
    def __init__(self, owner: "AnthropicHTTP") -> None:
        self._owner = owner

    def create(self, **body: Any) -> Any:
        owner = self._owner
        resp = owner._http.post("/v1/messages", json=body, headers={
            "x-api-key": owner._api_key, "anthropic-version": API_VERSION, "content-type": "application/json"})
        try:
            data = resp.json()
        except ValueError:
            raise AnthropicAPIError(resp.status_code, "invalid_response", "body is not JSON") from None
        if resp.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise AnthropicAPIError(resp.status_code, err.get("type", "error"), str(err.get("message", ""))[:200])
        if not isinstance(data, dict) or data.get("type") != "message" or not isinstance(data.get("content"), list):
            raise AnthropicAPIError(resp.status_code, "invalid_response", "not a Messages API response")
        return _obj(data)


class AnthropicHTTP:
    def __init__(self, api_key: str | None = None, *, base_url: str = DEFAULT_BASE_URL,
                 transport: httpx.BaseTransport | None = None, timeout: float = 120.0) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise AnthropicAPIError(0, "no_api_key", "ANTHROPIC_API_KEY is not set")
        self._api_key = key  # never logged, never put in an error message
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=timeout)
        self.messages = _Messages(self)
