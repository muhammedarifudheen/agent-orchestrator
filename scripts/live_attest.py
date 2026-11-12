"""Attestation: what did a live-judgment run REALLY talk to? (CLAUDE.md D26)

`real: true` in .perf/live_judgment.jsonl may not be something a script merely asserts: a fake key against the real
endpoint, a mock transport, or a loopback server must never be able to produce it. So the harness does not decide
`real` from its own flags; it reads this attestation. Every HTTP request any Python process of the run makes (through
`httpx.Client.send`, which is what AnthropicHTTP and servers/_http.py use) is appended to the file named by the
environment variable LIVE_ATTEST_FILE as {host, transport, status}, where `transport` is the class that actually carried
the request. The hook reaches child processes (pytest, the generated MCP servers) through a sitecustomize on PYTHONPATH
(scripts/live_attest_site/). A request counts as REAL only if it went over httpx's own network transport
(`httpx.HTTPTransport`) AND received a 2xx answer (any answer for the apply upstream); any request to the host through another
transport (MockTransport, FakeAnthropicTransport, any injected one) makes the host's attestation not real, whatever else
happened.

This is evidence, not a security boundary: someone who edits this file or forges a log line can fake it. What it removes
is the accidental and the lazy way: a mock, a loopback server or a wrong key cannot produce `real: true` by themselves.
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ENV_FILE = "LIVE_ATTEST_FILE"
REAL_TRANSPORT = "httpx.HTTPTransport"  # how httpx names its own network transport (it re-exports its classes as `httpx.*`)
ANTHROPIC_HOST = "api.anthropic.com"

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"path": None, "original": None}


def _transport_name(client: Any) -> str:
    transport = getattr(client, "_transport", None)
    return f"{type(transport).__module__}.{type(transport).__qualname__}"


def _write(entry: dict[str, Any]) -> None:
    path = _STATE["path"]
    if path is None:
        return
    with _LOCK, Path(path).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def install(path: Path | str) -> None:
    """Start attesting into `path`. Idempotent: a second call only changes the file. Not restored here (child processes
    end with the run); use `attesting()` in a long-lived process."""
    import httpx

    _STATE["path"] = Path(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if getattr(httpx.Client.send, "_live_attest", False):
        return
    original = httpx.Client.send

    def send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        host, transport = request.url.host, _transport_name(self)
        try:
            response = original(self, request, *args, **kwargs)
        except Exception as exc:  # the attempt is evidence too; the caller still sees the error
            _write({"host": host, "transport": transport, "status": None, "error": type(exc).__name__})
            raise
        _write({"host": host, "transport": transport, "status": response.status_code})
        return response

    send._live_attest = True  # type: ignore[attr-defined]
    _STATE["original"] = original
    httpx.Client.send = send  # type: ignore[method-assign]


def uninstall() -> None:
    import httpx

    if _STATE["original"] is not None and getattr(httpx.Client.send, "_live_attest", False):
        httpx.Client.send = _STATE["original"]  # type: ignore[method-assign]
    _STATE["original"], _STATE["path"] = None, None


@contextmanager
def attesting(path: Path | str) -> Iterator[Path]:
    """Attest everything this process sends inside the block, and restore httpx afterwards (tests call the harness in-process)."""
    install(path)
    try:
        yield Path(path)
    finally:
        uninstall()


def read(path: Path | str | None) -> list[dict[str, Any]]:
    if not path or not Path(path).is_file():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a torn line from a killed child is not evidence
    return out


def summarize(path: Path | str | None, host: str, require_success: bool = True) -> dict[str, Any]:
    """What the run's requests to `host` went over. `real` needs at least one answered request over the real transport and
    NO request to that host over any other transport. With `require_success` (the default, for the Anthropic API) the answer
    must be a 2xx: a wrong key is answered with a 401 and therefore is NOT real. `--apply` passes False: its upstream is
    SUPPOSED to answer with an error status, so any answer over the real transport is the evidence."""
    to_host = [r for r in read(path) if r.get("host") == host]
    real = [r for r in to_host if r.get("transport") == REAL_TRANSPORT and r.get("status") is not None
            and (200 <= r["status"] < 300 or not require_success)]
    other = [r for r in to_host if r.get("transport") != REAL_TRANSPORT]
    return {"host": host, "requests": len(to_host), "real_requests": len(real), "non_real_requests": len(other),
            "transports": sorted({str(r.get("transport")) for r in to_host}),
            "statuses": sorted({r["status"] for r in to_host if r.get("status") is not None}),
            "real": bool(real) and not other}


def child_environment(path: Path | str, root: Path, existing: str = "") -> dict[str, str]:
    """The variables that make every Python child of a run attest: the file, and a PYTHONPATH that puts the sitecustomize
    first (plus scripts/ and tests/ so the plugin modules import)."""
    dirs = [str(root / "scripts" / "live_attest_site"), str(root / "scripts"), str(root / "tests")]
    if existing:
        dirs.append(existing)
    return {ENV_FILE: str(path), "PYTHONPATH": os.pathsep.join(dirs)}
