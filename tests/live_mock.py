"""OFFLINE PROOF ONLY: a mock of the Anthropic Messages API that returns the exact replies the production ClaudePlanner and
HttpDocExtractionClient expect, so the live-judgment harness and the ceremony can be exercised end to end with no key (D26).

It can NEVER close a gap, and that is by construction, not by a flag: it works by injecting a transport object into
AnthropicHTTP, and the harness decides `real` from the attestation of what carried each request (scripts/live_attest.py),
where this transport is not `httpx._transports.default.HTTPTransport`. A run that used it therefore attests as not real
whatever the harness, the key or the environment say. It also marks every record `mock: true`, and
scripts/move_audit_row.py refuses a mock record before it looks at anything else.

The replies model the WIRE FORMAT only (tests/fake_anthropic.py): the planner reply is computed by the keyword planner
from the request, the doc reply is the committed stub report. Neither says anything about a real model's judgment.

Activated in a pytest subprocess with `-p live_mock` and LIVE_JUDGMENT_MOCK=1, or in-process with `mocked()`.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV = "LIVE_JUDGMENT_MOCK"
MARKER = "OfflineAnthropicMock"


def _paths() -> None:
    for p in (ROOT, ROOT / "servers", ROOT / "tests", ROOT / "tests" / "scenarios"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


class OfflineAnthropicMock(httpx.BaseTransport):
    """Answers the two requests the live harness makes: the planner's `submit_plan` and the doc client's `report_api`."""

    def __init__(self) -> None:
        _paths()
        self.requests: list[dict[str, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        from fake_anthropic import error_body, tool_use_message, tool_use_response
        from ingest.doc import TOOL_NAME

        body = json.loads(request.content.decode("utf-8"))
        self.requests.append(body)
        tool_names = [t.get("name") for t in body.get("tools", [])]
        if "submit_plan" in tool_names:
            from planners import KeywordPlanner

            payload = json.loads(body["messages"][0]["content"])
            plan = KeywordPlanner().plan(payload["intents"], payload["tools"])
            return httpx.Response(200, json=tool_use_response([{"intent_id": c.intent_id, "tool": c.tool} for c in plan]))
        if TOOL_NAME in tool_names:
            report = json.loads((ROOT / "tests" / "ingest" / "fixtures" / "dogceo.stub_response.json").read_text(encoding="utf-8"))
            return httpx.Response(200, json=tool_use_message(TOOL_NAME, report))
        return httpx.Response(400, json=error_body("invalid_request_error", f"{MARKER}: no canned reply for tools {tool_names}"))


def install() -> None:
    """Make every AnthropicHTTP() built without an explicit transport use the mock. Process-wide; see `mocked()` to undo."""
    _paths()
    from ingest import anthropic_http as ah

    if getattr(ah.AnthropicHTTP.__init__, "_live_mock", False):
        return
    original = ah.AnthropicHTTP.__init__

    def init(self: Any, api_key: str | None = None, *, base_url: str = ah.DEFAULT_BASE_URL,
             transport: httpx.BaseTransport | None = None, timeout: float = 120.0) -> None:
        original(self, api_key, base_url=base_url, transport=transport or OfflineAnthropicMock(), timeout=timeout)

    init._live_mock = True  # type: ignore[attr-defined]
    init._original = original  # type: ignore[attr-defined]
    ah.AnthropicHTTP.__init__ = init  # type: ignore[method-assign]


def uninstall() -> None:
    from ingest import anthropic_http as ah

    init = ah.AnthropicHTTP.__init__
    if getattr(init, "_live_mock", False):
        ah.AnthropicHTTP.__init__ = init._original  # type: ignore[method-assign]


@contextmanager
def mocked() -> Iterator[None]:
    install()
    try:
        yield
    finally:
        uninstall()


def pytest_configure(config: Any) -> None:  # the plugin hook, for `-p live_mock`
    if os.environ.get(ENV) == "1":
        install()
