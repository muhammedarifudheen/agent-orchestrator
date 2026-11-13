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

from types import SimpleNamespace
from typing import Any

import httpx

from agent import provider_config

API_VERSION = "2023-06-01"


class AnthropicAPIError(RuntimeError):
    """The API answered with a non-2xx status, or with something that is not a Messages response."""

    def __init__(self, status: int, kind: str, message: str) -> None:
        # status 0 = never reached the API (a configuration error): no "HTTP 0" noise in the message
        super().__init__(f"HTTP {status} {kind}: {message}" if status else f"{kind}: {message}")
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
            **owner.config.headers, "anthropic-version": API_VERSION, "content-type": "application/json"})
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
    def __init__(self, api_key: str | None = None, *, base_url: str | None = None,
                 transport: httpx.BaseTransport | None = None, timeout: float = 120.0) -> None:
        # Where the request goes and how it authenticates comes from agent/provider_config.py (PROVIDER_* variables;
        # Anthropic by default). An explicit api_key or base_url wins. A missing key is refused HERE, before any request:
        # the SDK accepted ANTHROPIC_AUTH_TOKEN as a bearer token, this client does not, and failing to authenticate
        # silently would be worse than refusing to start, so the error says exactly what to do.
        try:
            self.config = provider_config.resolve(api_key=api_key, base_url=base_url)
        except provider_config.ProviderConfigError as exc:
            raise AnthropicAPIError(0, exc.code, str(exc)) from None
        self._http = httpx.Client(base_url=self.config.base_url, transport=transport, timeout=timeout)
        self.messages = _Messages(self)
