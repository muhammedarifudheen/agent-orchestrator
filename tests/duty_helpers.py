"""Shared helpers for the duty tests: a scripted stand-in for the planner's model, a way to build a throwaway duty
library, and a clean-world fixture body. The scripted client is NOT a model: it issues exactly the calls it was told to,
so a test can prove that a code path used it (or did not)."""
from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Callable

import yaml

import banking_server
import retail_server
import travel_server
from agent.duties import DutyLibrary

ROOT = Path(__file__).resolve().parent.parent
SERVERS = ROOT / "servers"
DUTIES = ROOT / "duties"


def future(days: int = 30) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def reset_worlds(monkeypatch=None) -> None:
    """Fresh mock worlds for every server a duty touches (the servers keep state for the life of the process)."""
    if monkeypatch is not None:
        for var in ("TRAVEL_FAIL_MODE", "BANKING_FAIL_MODE", "RETAIL_FAIL_MODE"):
            monkeypatch.delenv(var, raising=False)
    travel_server._BOOKINGS.clear()
    banking_server._seed()
    retail_server._reset_state()


class ScriptedLLM:
    """Quacks like the Anthropic client. `turns` is a list; each turn is a list of ("text", str) and
    ("call", tool, args) items; `args` may be a function of the list of earlier tool results. After the script runs out it answers with a final text and no calls."""

    def __init__(self, *turns: list[tuple]) -> None:
        self.messages = self
        self.turns = list(turns)
        self.requests: list[list[dict[str, Any]]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def create(self, *, messages, **_):
        self.requests.append(list(messages))
        turn = self.turns.pop(0) if self.turns else [("text", "Nothing more to do.")]
        content = []
        for i, item in enumerate(turn):
            if item[0] == "text":
                content.append(NS(type="text", text=item[1]))
            else:
                args = item[2](self.results(messages)) if callable(item[2]) else item[2]
                content.append(NS(type="tool_use", id=f"t{len(self.requests)}_{i}", name=item[1], input=dict(args)))
        return NS(content=content)

    @staticmethod
    def results(messages) -> list[Any]:
        """The parsed results of every tool call so far (JSON where it is JSON), oldest first: what a model would read."""
        out = []
        for message in messages:
            if message.get("role") == "user" and isinstance(message.get("content"), list):
                for block in message["content"]:
                    if block.get("type") == "tool_result":
                        try:
                            out.append(json.loads(block["content"]))
                        except ValueError:
                            out.append(block["content"])
        return out

    def first_user_message(self) -> str:
        first = self.requests[0][0]["content"]
        return first if isinstance(first, str) else str(first)


def make_library(tmp_path: Path, mutate: Callable[[str, dict], None] | None = None, only: tuple[str, ...] | None = None) -> DutyLibrary:
    """Copy the real duties (and the schema) into tmp_path/duties; `mutate(name, data)` may edit each one first."""
    target = tmp_path / "duties"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(DUTIES / "schema.json", target / "schema.json")
    for path in sorted(DUTIES.glob("*.yaml")):
        if only is not None and path.stem not in only:
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if mutate is not None:
            mutate(path.stem, data)
        (target / path.name).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return DutyLibrary(target)


def book_flight_goal(origin: str = "DEL", destination: str = "BOM", on: str | None = None,
                     name: str = "Asha Verma", email: str = "asha@example.com") -> str:
    return f"book a flight from {origin} to {destination} on {on or future()} for {name} at {email}"
