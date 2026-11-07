"""Generate an MCP server + tests from a system spec.

    python -m generators.from_spec specs/weather.yaml            # write servers/ and tests/ files
    python -m generators.from_spec specs/weather.yaml --check    # exit 1 if committed files are stale

Deterministic templating only (no LLM). The emitted server uses servers/_base.py and
servers/_http.py, so every tool raises ToolFailure on every failure, exactly like hand-written ones.

Fail closed: any operation that is not read-only must state ``idempotent`` and carry
``review: {safety_fields: reviewed}``; otherwise generation is refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pprint
import re
import sys
import textwrap
from pathlib import Path
from typing import Any

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parent.parent
PY_TYPES = {"string": "str", "number": "float", "integer": "int", "boolean": "bool"}


class SpecError(ValueError):
    """The spec is invalid or unsafe to generate from."""


# -- loading + validation ----------------------------------------------------------------------

def load_spec(path: Path) -> tuple[dict[str, Any], str]:
    """Return (validated spec, short sha256 of the normalized spec text)."""
    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    spec = yaml.safe_load(raw)
    schema = json.loads((ROOT / "specs" / "schema.json").read_text(encoding="utf-8"))
    errors = sorted(jsonschema.Draft7Validator(schema).iter_errors(spec), key=lambda e: list(e.path))
    if errors:
        raise SpecError("\n".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors))
    check_semantics(spec)
    return spec, hashlib.sha256(raw.encode()).hexdigest()[:12]


def check_semantics(spec: dict[str, Any]) -> None:
    names = [o["name"] for o in spec["operations"]]
    if len(set(names)) != len(names):
        raise SpecError(f"duplicate operation names: {sorted({n for n in names if names.count(n) > 1})}")
    for op in spec["operations"]:
        n = op["name"]
        path_vars = set(re.findall(r"\{([a-z_]+)\}", op["request"]["path"]))
        declared = {p["name"] for p in op["params"] if p["in"] == "path"}
        if path_vars != declared:
            raise SpecError(f"{n}: path variables {sorted(path_vars)} != path params {sorted(declared)}")
        if any(p["in"] == "path" and not p.get("required") for p in op["params"]):
            raise SpecError(f"{n}: path params must be required")
        for r in op.get("requires", []):
            if r not in names:
                raise SpecError(f"{n}: requires unknown operation '{r}'")
        if not any(e["expect"] == "ok" for e in op["examples"]):
            raise SpecError(f"{n}: needs at least one example with expect: ok")
        if op["side_effect"] != "read":
            if "idempotent" not in op:
                raise SpecError(f"{n}: side_effect={op['side_effect']} must state idempotent: true|false")
            if (op.get("review") or {}).get("safety_fields") != "reviewed":
                raise SpecError(
                    f"{n}: REFUSED. side_effect={op['side_effect']} operations need "
                    "review.safety_fields: reviewed (side_effect, idempotent, retryable flags, requires) "
                    "signed off by a human before a server is generated.")


# -- docstring ---------------------------------------------------------------------------------

def _is_idempotent(op: dict[str, Any]) -> bool:
    return bool(op.get("idempotent", op["side_effect"] == "read"))


def _error_summary(spec: dict[str, Any], op: dict[str, Any]) -> str:
    seen: dict[str, bool] = {"INVALID_ARGUMENT": False}
    for rule in list(op.get("errors", [])) + list(spec.get("error_map", [])):
        seen.setdefault(rule["code"], rule["retryable"])
    seen.setdefault("AUTH_FAILED", False)
    if any(p["in"] == "path" for p in op["params"]):
        seen.setdefault("NOT_FOUND", False)
    if _is_idempotent(op):
        seen.update({"TIMEOUT": True, "NETWORK_ERROR": True})
    else:
        seen["OUTCOME_UNKNOWN"] = False
    seen.setdefault("UNEXPECTED_RESPONSE", False)
    seen.setdefault("SERVICE_UNAVAILABLE", _is_idempotent(op))
    parts = [f"{c} (retryable={'true' if r else 'false'})" for c, r in seen.items()]
    return "\n".join(textwrap.wrap("Errors: " + ", ".join(parts) + ".", 96, subsequent_indent="    "))


def build_docstring(spec: dict[str, Any], op: dict[str, Any]) -> str:
    lines = [op["summary"].rstrip(), "", f"When to call: {op['when_to_call'].strip()}"]
    if op.get("requires"):
        lines.append("Call first: " + ", ".join(op["requires"]) + ".")
    if op["side_effect"] != "read":
        what = "moves money" if op["side_effect"] == "money" else "changes state"
        idem = "idempotent" if _is_idempotent(op) else "NOT idempotent: do not retry after OUTCOME_UNKNOWN"
        lines.append(f"Side effect: {what}; {idem}.")
    lines += ["", "Args:"]
    for p in sorted(op["params"], key=lambda p: not p.get("required")):
        bits = [PY_TYPES[p["type"]], "required" if p.get("required") else f"optional, default {p.get('default')!r}"]
        for k, v in (p.get("constraints") or {}).items():
            bits.append(f"{k}={v}")
        text = f"    {p['name']}: {p['description'].strip()} ({'; '.join(bits)})"
        lines.append(textwrap.fill(text, 96, subsequent_indent="        "))
    lines += ["", "Returns:", textwrap.fill("    " + op["response"]["description"].strip(), 96,
                                             subsequent_indent="    "), "", _error_summary(spec, op)]
    body = "\n".join(lines).replace("\\", "\\\\").replace('"""', "'''")
    return body


# -- server emission ---------------------------------------------------------------------------

def _runtime_ops(spec: dict[str, Any]) -> dict[str, Any]:
    ops: dict[str, Any] = {}
    for op in spec["operations"]:
        params = []
        for p in op["params"]:
            entry = {k: p[k] for k in ("name", "wire_name", "type", "in", "default", "constraints") if k in p}
            entry["required"] = bool(p.get("required"))
            params.append(entry)
        ops[op["name"]] = {
            "method": op["request"]["method"], "path": op["request"]["path"],
            "query_const": op["request"].get("query_const", {}), "side_effect": op["side_effect"],
            "idempotent": _is_idempotent(op), "params": params,
            "response": {"select": op["response"].get("select")}, "errors": op.get("errors", []),
        }
    return ops


def _signature(op: dict[str, Any]) -> str:
    parts = []
    for p in sorted(op["params"], key=lambda p: not p.get("required")):
        t = PY_TYPES[p["type"]]
        if p.get("required"):
            parts.append(f"{p['name']}: {t}")
        elif "default" in p:
            parts.append(f"{p['name']}: {t} = {p['default']!r}")
        else:
            parts.append(f"{p['name']}: {t} | None = None")
    ret = "dict" if op["response"]["type"] == "object" else "list[dict]"
    return f"({', '.join(parts)}) -> {ret}"


def render_server(spec: dict[str, Any], spec_file: str, digest: str) -> str:
    name = spec["system_name"]
    system = {k: spec[k] for k in ("system_name", "base_url", "base_url_env", "timeout_s", "auth", "error_map")
              if k in spec}
    out = [
        f"# GENERATED by generators/from_spec.py from {spec_file} (sha256 {digest}).",
        "# DO NOT EDIT: change the spec and regenerate (python -m generators.from_spec ...).",
        f'"""{name} MCP server: {spec.get("description", name)}\n\nRun standalone: python servers/{name}_server.py (stdio)."""',
        "from __future__ import annotations",
        "",
        "from mcp.server.mcpserver import MCPServer",
        "",
        "from _base import make_tool",
        "from _http import HttpClient",
        "",
        f'mcp = MCPServer("{name}")',
        "tool = make_tool(mcp)",
        "",
        "SYSTEM = " + pprint.pformat(system, width=100, sort_dicts=False),
        "OPERATIONS = " + pprint.pformat(_runtime_ops(spec), width=100, sort_dicts=False),
        "_client = HttpClient(SYSTEM, OPERATIONS)",
        "",
    ]
    for op in spec["operations"]:
        args = ", ".join(f'"{p["name"]}": {p["name"]}' for p in op["params"])
        idem = f", idempotent={_is_idempotent(op)}" if op["side_effect"] != "read" else ""
        out += [
            "", "", f'@tool(side_effect="{op["side_effect"]}"{idem})',
            f"def {op['name']}{_signature(op)}:",
            f'    """{textwrap.indent(build_docstring(spec, op), "    ").lstrip()}',
            '    """',
            f'    return _client.call("{op["name"]}", {{{args}}})',
        ]
    out += ["", "", 'if __name__ == "__main__":', "    mcp.run()", ""]
    return "\n".join(out)


# -- test emission -----------------------------------------------------------------------------

def render_tests(spec: dict[str, Any], spec_file: str, digest: str) -> str:
    name = spec["system_name"]
    examples = {op["name"]: op["examples"] for op in spec["operations"]}
    out = [
        f"# GENERATED by generators/from_spec.py from {spec_file} (sha256 {digest}). DO NOT EDIT.",
        f'"""Offline tests for the generated {name} server: in-memory MCP client + mock HTTP transport.',
        'Set LIVE_TESTS=1 to also call the real API (first ok example of each operation)."""',
        "from __future__ import annotations",
        "",
        "import json",
        "import os",
        "import re",
        "",
        "import httpx",
        "import pytest",
        "from mcp import Client",
        "",
        f"import {name}_server as server",
        "",
        "EXAMPLES = " + pprint.pformat(examples, width=100, sort_dicts=False),
        f"OPERATIONS = {[o['name'] for o in spec['operations']]!r}",
        "CODE = re.compile(r\"\\[([A-Z][A-Z0-9_]*)\\] retryable=(true|false) \")",
        "",
        "",
        "def _transport(mock):",
        "    def handler(request: httpx.Request) -> httpx.Response:",
        "        if mock is None:",
        '            raise AssertionError("example has no mock but the request reached the transport")',
        '        if "raises" in mock:',
        '            raise getattr(httpx, mock["raises"])("simulated", request=request)',
        '        return httpx.Response(mock.get("status", 200), json=mock.get("body"))',
        "",
        "    return httpx.MockTransport(handler)",
        "",
        "",
        "def _text(result) -> str:",
        '    return "\\n".join(getattr(c, "text", "") for c in result.content)',
        "",
        "",
        "async def _check(op: str, example: dict) -> None:",
        "    async with Client(server.mcp) as client:",
        "        result = await client.call_tool(op, example[\"args\"])",
        "    expect = example[\"expect\"]",
        "    if expect == \"ok\":",
        "        assert not result.is_error, _text(result)",
        "        payload = result.structured_content if result.structured_content is not None else json.loads(_text(result))",
        "        if isinstance(payload, dict) and set(payload) == {\"result\"}:",
        "            payload = payload[\"result\"]",
        "        for key in example.get(\"expect_keys\", []):",
        "            assert key in payload, f\"missing {key} in {payload}\"",
        "    else:",
        "        assert result.is_error, f\"expected failure {expect}, got success: {_text(result)}\"",
        "        m = CODE.search(_text(result))",
        "        assert m, f\"error does not follow '[CODE] retryable=<bool> message': {_text(result)!r}\"",
        "        assert m.group(1) == expect[\"code\"], _text(result)",
        "        assert (m.group(2) == \"true\") is expect[\"retryable\"], _text(result)",
        "",
        "",
        "async def test_tools_discovered():",
        "    async with Client(server.mcp) as client:",
        "        names = [t.name for t in (await client.list_tools()).tools]",
        "    assert sorted(names) == sorted(OPERATIONS)",
        "",
    ]
    for op in spec["operations"]:
        n = op["name"]
        out += [
            "", f'@pytest.mark.parametrize("example", EXAMPLES["{n}"], ids=lambda e: e.get("name", "example"))',
            f"async def test_{n}(example, monkeypatch):",
            '    monkeypatch.setattr(server._client, "transport", _transport(example.get("mock")))',
            f'    await _check("{n}", example)',
            "",
        ]
    out += [
        "",
        '@pytest.mark.skipif(os.environ.get("LIVE_TESTS") != "1", reason="set LIVE_TESTS=1 to call the real API")',
        '@pytest.mark.parametrize("op", OPERATIONS)',
        "async def test_live(op, monkeypatch):",
        '    example = next(e for e in EXAMPLES[op] if e["expect"] == "ok")',
        '    monkeypatch.setattr(server._client, "transport", None)',
        "    await _check(op, {**example, \"expect_keys\": example.get(\"expect_keys\", [])})",
        "",
    ]
    return "\n".join(out)


# -- cli ---------------------------------------------------------------------------------------

def generate(spec_path: Path, servers_dir: Path, tests_dir: Path, check: bool = False) -> list[Path]:
    spec, digest = load_spec(spec_path)
    try:
        spec_file = spec_path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        spec_file = spec_path.as_posix()
    files = {servers_dir / f"{spec['system_name']}_server.py": render_server(spec, spec_file, digest),
             tests_dir / f"test_{spec['system_name']}.py": render_tests(spec, spec_file, digest)}
    stale = []
    for path, text in files.items():
        compile(text, str(path), "exec")  # never write code that does not parse
        current = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else None
        if current != text:
            stale.append(path)
            if not check:
                path.write_text(text, encoding="utf-8", newline="\n")
    return stale


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("spec", type=Path)
    ap.add_argument("--servers-dir", type=Path, default=ROOT / "servers")
    ap.add_argument("--tests-dir", type=Path, default=ROOT / "tests")
    ap.add_argument("--check", action="store_true", help="do not write; exit 1 if files are stale")
    args = ap.parse_args(argv)
    try:
        changed = generate(args.spec, args.servers_dir, args.tests_dir, args.check)
    except SpecError as exc:
        print(f"SPEC ERROR:\n{exc}", file=sys.stderr)
        return 2
    verb = "STALE" if args.check else "wrote"
    for p in changed:
        print(f"{verb}: {p}")
    if not changed:
        print("up to date")
    return 1 if (args.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
