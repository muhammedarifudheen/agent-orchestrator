"""Shared httpx client pool in servers/_http.py (D17): a client is built once per (base URL, timeout, auth kind,
transport) and reused, instead of one per request (each construction built an SSL context, ~0.3 s)."""
from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

import _http
from _http import HttpClient

OP = {"method": "GET", "path": "/x/{id}", "side_effect": "read", "idempotent": True,
      "params": [{"name": "id", "type": "string", "in": "path", "required": True}],
      "response": {"select": None}, "errors": []}


class Fake(httpx.BaseTransport):
    """Counts and records the requests it serves."""

    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self._handler = handler or (lambda r: httpx.Response(200, json={"ok": True}))

    def handle_request(self, request):
        self.requests.append(request)
        return self._handler(request)


@pytest.fixture
def created(monkeypatch):
    """Counts httpx.Client instantiations made by the runtime."""
    made = []
    real = httpx.Client

    class Counting(real):
        def __init__(self, *a, **kw):
            made.append(kw)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "Client", Counting)
    return made


def runtime(transport, base="https://h.test", timeout=10, auth=None, **system):
    return HttpClient({"base_url": base, "timeout_s": timeout, "auth": auth or {"kind": "none"}, **system},
                      {"op": OP}, transport)


def test_two_sequential_calls_to_the_same_upstream_reuse_one_client(created):
    fake = Fake()
    c = runtime(fake)
    c.call("op", {"id": "a"})
    c.call("op", {"id": "b"})
    assert len(created) == 1, "the second call must reuse the client built by the first"
    assert [r.url.path for r in fake.requests] == ["/x/a", "/x/b"]


def test_a_call_to_a_different_base_url_creates_a_new_client_and_the_first_is_still_reused(created):
    fake = Fake()
    runtime(fake, base="https://one.test").call("op", {"id": "a"})
    runtime(fake, base="https://two.test").call("op", {"id": "a"})
    assert len(created) == 2
    runtime(fake, base="https://one.test").call("op", {"id": "b"})  # a NEW HttpClient object, same upstream
    assert len(created) == 2, "a second HttpClient for an already-seen upstream shares the pooled client"


def test_a_different_timeout_or_auth_kind_or_transport_is_a_different_client(created, monkeypatch):
    monkeypatch.setenv("K", "v")
    t1, t2 = Fake(), Fake()
    runtime(t1).call("op", {"id": "a"})
    runtime(t1, timeout=3).call("op", {"id": "a"})
    runtime(t1, auth={"kind": "api_key", "env": "K", "header": "X-Key"}).call("op", {"id": "a"})
    runtime(t2).call("op", {"id": "a"})
    assert len(created) == 4


def test_the_base_url_env_override_is_part_of_the_key(created, monkeypatch):
    fake = Fake()
    c = runtime(fake, base_url_env="H_URL")
    c.call("op", {"id": "a"})
    monkeypatch.setenv("H_URL", "https://other.test")
    c.call("op", {"id": "a"})
    assert len(created) == 2 and fake.requests[1].url.host == "other.test"


def test_the_timeout_is_applied_per_request_not_baked_into_a_shared_client():
    seen = []
    fake = Fake(lambda r: (seen.append(r.extensions["timeout"]), httpx.Response(200, json={}))[1])
    runtime(fake, timeout=7).call("op", {"id": "a"})
    assert seen[0]["read"] == 7 and seen[0]["connect"] == 7


def test_credentials_are_per_request_so_a_reused_client_never_carries_an_old_one(monkeypatch):
    seen = []
    fake = Fake(lambda r: (seen.append(r.headers.get("x-key")), httpx.Response(200, json={}))[1])
    c = runtime(fake, auth={"kind": "api_key", "env": "K", "header": "X-Key"})
    monkeypatch.setenv("K", "first")
    c.call("op", {"id": "a"})
    monkeypatch.setenv("K", "second")
    c.call("op", {"id": "a"})
    monkeypatch.delenv("K")
    with pytest.raises(Exception):
        c.call("op", {"id": "a"})  # no credential configured: must fail, NOT silently reuse "second"
    assert seen == ["first", "second"]


def test_cookies_set_by_one_response_are_never_sent_on_the_next_request():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(200, json={}, headers={"Set-Cookie": "session=abc123; Path=/"} if state["n"] == 1 else {})

    fake = Fake(handler)
    c = runtime(fake)
    c.call("op", {"id": "a"})
    c.call("op", {"id": "b"})
    assert "cookie" not in fake.requests[1].headers, "an upstream session cookie must not leak into the next call"


def test_a_failed_request_does_not_poison_the_pooled_client(created):
    flaky = {"fail": True}

    def handler(request):
        if flaky["fail"]:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    c = runtime(Fake(handler))
    with pytest.raises(Exception, match="NETWORK_ERROR"):
        c.call("op", {"id": "a"})
    flaky["fail"] = False
    assert c.call("op", {"id": "a"}) == {"ok": True}
    assert len(created) == 1


def test_close_pool_forgets_every_client(created):
    c = runtime(Fake())
    c.call("op", {"id": "a"})
    _http.close_pool()
    assert _http._POOL == {}
    c.call("op", {"id": "a"})
    assert len(created) == 2


def test_no_per_request_client_construction_is_left_in_the_runtime():
    """grep for httpx.Client( in servers/_http.py: exactly one construction site, the pool's factory."""
    tree = ast.parse((Path(__file__).parent.parent / "servers" / "_http.py").read_text(encoding="utf-8"))
    sites = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Client"
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "httpx"):
                    sites.append(fn.name)
    assert sites == ["_new_client"], sites
