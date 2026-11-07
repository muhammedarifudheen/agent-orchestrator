"""Every server must import cleanly as ``servers.<name>_server`` (a fresh interpreter, project root as
cwd) AND start as a script over stdio (which is how the registry launches it)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
NAMES = sorted(p.stem for p in (ROOT / "servers").glob("*_server.py"))


@pytest.mark.parametrize("name", NAMES)
def test_imports_as_package_module(name):
    done = subprocess.run([sys.executable, "-c", f"import servers.{name}"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-600:]


@pytest.mark.parametrize("name", NAMES)
async def test_starts_as_script_over_stdio(name):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "servers" / f"{name}.py")])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert (await session.list_tools()).tools
