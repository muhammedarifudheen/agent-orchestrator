"""A loopback stand-in for the five third-party APIs, for OFFLINE tests of `live_judgment.py --live-apis` (CLAUDE.md D28).

It answers each request from the specs' own examples (the first `expect: ok` example's mock body, matched by request path and
the operation's constant query). Pointed at with the specs' `<SYSTEM>_BASE_URL` overrides, the real 14 live-API tests pass
against it. Because the requests go to 127.0.0.1 and not to the five real hosts, the harness's attestation can never mark such
a run real: that is exactly what the tests that use this assert."""
from __future__ import annotations

import http.server
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
SYSTEMS = ("weather", "jsonplaceholder", "catfact", "dogceo", "zippopotamus")


def _routes() -> list[tuple[re.Pattern[str], dict, dict]]:
    out = []
    for system in SYSTEMS:
        spec = yaml.safe_load((ROOT / "specs" / f"{system}.yaml").read_text(encoding="utf-8"))
        base_path = urlsplit(spec["base_url"]).path.rstrip("/")
        for op in spec["operations"]:
            ex = next((e for e in op["examples"] if e["expect"] == "ok"), None)
            if not ex or "mock" not in ex:
                continue
            template = base_path + op["request"]["path"]
            pattern = "^" + "[^/?]+".join(re.escape(part) for part in re.split(r"\{[^}]+\}", template)) + "$"
            out.append((re.compile(pattern), ex["mock"], op["request"].get("query_const", {}) or {}))
    return sorted(out, key=lambda r: -len(r[2]))  # an operation with a constant query is more specific than one without


class FakeLiveApis:
    """`with FakeLiveApis() as fake:` then `fake.env()` gives the base-URL overrides for the five systems."""

    def __init__(self, status: int | None = None) -> None:
        self.routes, self.hits, self.status = _routes(), 0, status
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                parts = urlsplit(self.path)
                for pattern, mock, query in outer.routes:
                    if pattern.match(parts.path) and all(f"{k}=" in parts.query for k in query):
                        body = json.dumps(mock["body"]).encode()
                        self.send_response(outer.status or mock.get("status", 200))
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "FakeLiveApis":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def env(self) -> dict[str, str]:
        return {"WEATHER_BASE_URL": self.base, "JSONPLACEHOLDER_BASE_URL": self.base, "CATFACT_BASE_URL": self.base,
                "DOGCEO_BASE_URL": self.base + "/api", "ZIPPOPOTAMUS_BASE_URL": self.base}
