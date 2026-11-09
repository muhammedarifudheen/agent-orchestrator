"""FakeAnthropicTransport: an httpx transport that plays the Anthropic Messages API.

It exercises the WIRE FORMAT (request body, headers, response shape, error shape) end to end through the real client
code. It does NOT imitate the model's judgment: the plans it returns are whatever the test queues. The response shapes
below follow the published Messages API (a message object with typed content blocks and a stop_reason); they are
modelled on that documentation, not captured from a live call, because there is no key on this machine.
"""
from __future__ import annotations

import json
from typing import Any

import httpx


def tool_use_response(calls: Any, *, tool: str = "submit_plan", stop_reason: str = "tool_use") -> dict[str, Any]:
    """A realistic assistant message whose content is one tool_use block."""
    return {
        "id": "msg_01XFDUDYJgAACzvnptvVoYEL", "type": "message", "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": "I'll submit the plan."},
                    {"type": "tool_use", "id": "toolu_01A09q90qw90lq917835lq9", "name": tool,
                     "input": {"calls": calls}}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1103, "output_tokens": 187},
    }


def text_response(text: str) -> dict[str, Any]:
    """A reply that never calls a tool (what the planner must treat as a miss)."""
    return {
        "id": "msg_01Aq9w938a90dw8q", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1103, "output_tokens": 41},
    }


def error_body(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


class FakeAnthropicTransport(httpx.BaseTransport):
    """Serves queued responses in order and records every request (parsed) for assertions."""

    def __init__(self, *responses: tuple[int, dict[str, Any]]) -> None:
        self._queue = list(responses)
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    def queue(self, status: int, body: dict[str, Any]) -> None:
        self._queue.append((status, body))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content.decode("utf-8")))
        if not self._queue:
            return httpx.Response(500, json=error_body("api_error", "FakeAnthropicTransport: no response queued"))
        status, body = self._queue.pop(0)
        return httpx.Response(status, json=body)
