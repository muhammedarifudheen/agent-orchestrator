"""Stdio smoke test for any MCP server in servers/.

    python tests/stdio_smoke.py [servers/travel_server.py]

Launches the server over stdio, lists tools, calls each tool once with valid args, then calls one
tool with invalid args and asserts the contract: is_error=True and "[CODE] retryable=<bool> ...".
Per-server fixtures (valid args, one invalid call) live in CASES; add an entry per new server.
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).parent.parent
ERROR_TEXT = re.compile(r"\[([A-Z][A-Z0-9_]*)\]\s+retryable=(true|false)\s+(.*)", re.S)
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


def travel_valid_calls(state: dict) -> Iterator[tuple[str, dict]]:
    """Valid calls in dependency order; a generator so later args can use `state` set by earlier calls."""
    yield "search_flights", {"origin": "DEL", "destination": "BOM", "date": TOMORROW}
    yield "book_flight", {"flight_id": f"AI-865_DELBOM_{TOMORROW}", "passenger_name": "Aarav Sharma",
                          "passenger_email": "aarav@example.com"}
    yield "send_confirmation", {"email": "aarav@example.com", "booking_ref": state["booking_ref"],
                                "details": "AI-865 DEL->BOM"}


def weather_valid_calls(state: dict) -> Iterator[tuple[str, dict]]:
    """Calls the LIVE Open-Meteo API (no key needed)."""
    yield "get_current_weather", {"latitude": 28.61, "longitude": 77.21}
    yield "get_daily_forecast", {"latitude": 28.61, "longitude": 77.21, "days": 2}


CASES = {
    "weather": {
        "valid": weather_valid_calls,
        "invalid": ("get_current_weather", {"latitude": 999, "longitude": 77.21}, "INVALID_ARGUMENT", False),
    },
    "travel": {
        "valid": travel_valid_calls,
        "invalid": ("search_flights", {"origin": "DEL", "destination": "BOM", "date": "not-a-date"},
                    "INVALID_DATE", False),
    },
}


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


async def main(script: Path) -> int:
    name = script.stem.removesuffix("_server")
    case = CASES.get(name)
    if case is None:
        print(f"No smoke fixtures for '{name}': add an entry to CASES.", file=sys.stderr)
        return 2
    params = StdioServerParameters(command=sys.executable, args=[str(script)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"== {len(tools)} tools discovered")
            for t in tools:
                print(f"  - {t.name}: {(t.description or '').strip().splitlines()[0]}")
            assert tools, "server exposes no tools"
            assert all(t.description for t in tools), "every tool needs a docstring"

            print("== valid calls")
            state: dict = {}
            for tool, args in case["valid"](state):
                res = await session.call_tool(tool, args)
                body = text_of(res)
                assert not res.is_error, f"{tool} unexpectedly failed: {body}"
                print(f"  {tool}({args}) -> {body[:160]}")
                if tool == "book_flight":
                    state["booking_ref"] = res.structured_content["booking_ref"] \
                        if res.structured_content else re.search(r'"booking_ref":\s*"(\w+)"', body).group(1)

            tool, args, code, retryable = case["invalid"]
            print("== invalid call")
            res = await session.call_tool(tool, args)
            body = text_of(res)
            print(f"  {tool}({args}) -> is_error={res.is_error} {body}")
            assert res.is_error, "invalid call must come back as is_error=True"
            m = ERROR_TEXT.search(body)
            assert m, f"error text does not follow '[CODE] retryable=<bool> message': {body!r}"
            assert m.group(1) == code, f"expected {code}, got {m.group(1)}"
            assert (m.group(2) == "true") is retryable, f"retryable flag wrong: {m.group(2)}"
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "servers" / "travel_server.py"
    sys.exit(asyncio.run(main(target.resolve())))
