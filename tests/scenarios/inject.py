"""Inject a REAL failure into a REAL registered tool without changing what the contract test sees.

The older `card_declined` fixture in conftest.py re-registers the tool WITHOUT its safety annotations, which would
fail tests/test_contract.py (`test_every_tool_declares_safety_annotations`) if a test using it ran first. This helper
swaps only the function body and puts the tool back exactly as it was (description and annotations included).
"""
from __future__ import annotations

import functools
from contextlib import asynccontextmanager
from typing import Any

from mcp import Client


@asynccontextmanager
async def failing_tool(module: Any, tool_name: str, failure: Exception):
    async with Client(module.mcp) as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
    original_def = listed[tool_name]
    original_fn = getattr(module, tool_name)

    def raiser(*args: Any, **kwargs: Any):
        raise failure

    functools.update_wrapper(raiser, original_fn)  # same signature, so the same input schema is derived
    module.mcp.remove_tool(tool_name)
    module.mcp.add_tool(raiser, name=tool_name, description=original_def.description, annotations=original_def.annotations)
    try:
        yield
    finally:
        module.mcp.remove_tool(tool_name)
        module.mcp.add_tool(original_fn, name=tool_name, description=original_def.description,
                            annotations=original_def.annotations)
