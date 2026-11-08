"""Offline stand-in for ``anthropic.Anthropic`` shared by ingestor tests and the pipeline test.

``StubAnthropic(canned)`` takes a dict or a path to a stub_response JSON file and answers every
``client.messages.create(**kwargs)`` with a Messages-shaped response holding ONE ``tool_use`` block
(``name="report_api"``, ``input=<canned>``). Every call's kwargs are recorded in ``.calls``.
It proves the plumbing and the deterministic checks; it says nothing about real extraction quality.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

TOOL_NAME = "report_api"


class _Messages:
    def __init__(self, owner: "StubAnthropic") -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self._owner.calls.append(kwargs)
        block = SimpleNamespace(type="tool_use", id="toolu_stub_1", name=TOOL_NAME,
                                input=copy.deepcopy(self._owner.canned))
        return SimpleNamespace(id="msg_stub_1", type="message", role="assistant", model=kwargs.get("model"),
                               content=[block], stop_reason="tool_use", stop_details=None,
                               usage=SimpleNamespace(input_tokens=0, output_tokens=0))


class StubAnthropic:
    def __init__(self, canned: dict[str, Any] | str | Path) -> None:
        if isinstance(canned, (str, Path)):
            canned = json.loads(Path(canned).read_text(encoding="utf-8"))
        self.canned: dict[str, Any] = canned
        self.calls: list[dict[str, Any]] = []
        self.messages = _Messages(self)
