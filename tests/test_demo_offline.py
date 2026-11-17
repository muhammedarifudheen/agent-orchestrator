"""Offline test for demo/app.py (the Gradio demo of the flows AUDIT.md section 2 marks as proven).

Proves: the demo's functions return the expected results using the real matcher, tool safety,
approval records and proposer; any network attempt fails the test; no tool is ever executed;
build_app wires the banner, three tabs and three click handlers (checked with a recording
stand-in for gradio, so gradio itself is NOT needed); the claim ids the demo cites are proven
rows of AUDIT.md section 2 and C24, C25, C27 are open rows of 3a.

Does NOT prove: that real gradio renders the app, any live-model behaviour, or any real upstream
call. It only reads the real AUDIT.md; it never writes into the repo.
"""
from __future__ import annotations

import importlib.util
import json
import re
import socket
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
BANNER_TEXT = (
    "This demo shows the flows AUDIT.md §2 marks as proven. It does not show live-model planner "
    "judgment (C24), real doc extraction (C25), or real upstream apply (C27) — all open in §3a."
)
PROD_HOST = "api" + "." + "anthropic" + "." + "com"


@pytest.fixture(scope="module")
def app():
    spec = importlib.util.spec_from_file_location("offline_demo_app", ROOT / "demo" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["offline_demo_app"] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop("offline_demo_app", None)
        raise
    yield mod
    sys.modules.pop("offline_demo_app", None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts: list[str] = []

    loopback = {"127.0.0.1", "::1", "localhost", None, ""}
    real_connect, real_gai = socket.socket.connect, socket.getaddrinfo

    def boom(name):
        def _raise(*a, **k):
            attempts.append(name)
            raise AssertionError("network call attempted")
        return _raise

    # asyncio's Windows event loop builds its self-pipe from a loopback socketpair (connect to
    # 127.0.0.1), which is in-process plumbing, not network: loopback is allowed, all else raises.
    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host in loopback:
            return real_connect(self, address)
        return boom("socket.connect")()

    def guarded_gai(host, *a, **k):
        if host in loopback:
            return real_gai(host, *a, **k)
        return boom("socket.getaddrinfo")()

    monkeypatch.setattr(httpx.Client, "send", boom("httpx.Client.send"))
    monkeypatch.setattr(httpx.AsyncClient, "send", boom("httpx.AsyncClient.send"))
    try:  # mcp 2.x imports `httpx2` (its own HTTP client library) when write_safety builds an in-process mcp.Client
        import httpx2
    except ImportError:
        httpx2 = None
    if httpx2 is not None:
        monkeypatch.setattr(httpx2.Client, "send", boom("httpx2.Client.send"))
        monkeypatch.setattr(httpx2.AsyncClient, "send", boom("httpx2.AsyncClient.send"))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PROVIDER_API_KEY"):  # a developer's shell must not change these tests
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_gai)
    yield attempts
    assert attempts == [], f"network attempts: {attempts}"


# (c) duty matching
def test_duty_refund(app):
    r = app.match_duty("refund order ORD-ABCD1234")
    assert r["matched"] is True
    assert r["duty"] == "process_refund"
    assert r["confidence"] == 1.0
    assert r["kind"] == "exact"
    wb = r["write_boundary"]
    assert wb and wb["step"] and wb["tool"].startswith("retail__")
    assert r["model_consulted"] is False


def test_duty_book_flight(app):
    r = app.match_duty("book a flight from DEL to BOM on 2027-01-15 for Asha Verma at asha@example.com")
    assert r["matched"] is True
    assert r["duty"] == "book_flight"


def test_duty_no_match(app):
    r = app.match_duty("what is the weather in Pune")
    assert r["matched"] is False
    assert r["outcome"] == "no_match"
    assert r["model_consulted"] is False


# (d) write safety
def test_write_safety_read(app):
    r = app.write_safety("search_flights")
    assert r["is_write"] is False
    assert r["allow_writes_required"] is False
    assert r["pending_approval"] is None
    assert r["executed"] is False


@pytest.mark.parametrize("tool", ["book_flight", "transfer_funds"])
def test_write_safety_writes(app, tool):
    r = app.write_safety(tool)
    assert r["is_write"] is True
    assert r["allow_writes_required"] is True
    assert r["decision"]["allowed"] is False
    assert r["decision"]["code"] == "WRITE_APPROVAL_REQUIRED"
    assert r["if_allow_writes_true"]["allowed"] is True
    assert r["executed"] is False
    pa = r["pending_approval"]
    assert pa["kind"] == "pending_write_approval"
    assert pa["writes"][0]["tool"] == r["tool"]
    assert pa["writes"][0]["status"] == "pending"


def test_transfer_funds_moves_money(app):
    r = app.write_safety("transfer_funds")
    assert "money" in json.dumps(r["safety"]).lower()


def test_unknown_tool_raises(app):
    with pytest.raises(ValueError):
        app.write_safety("no_such_tool")


def test_demo_never_executes_a_tool(app, monkeypatch):
    from agent.tool_registry import ToolRegistry

    calls = []

    def spy(*a, **k):
        calls.append(a)
        raise AssertionError("tool executed by demo")

    monkeypatch.setattr(ToolRegistry, "call", spy)
    for t in app.WRITE_TOOLS:
        app.write_safety(t)
    assert calls == []


# (e) learning proposal
def _check_applies(proposal):
    import yaml
    from generators.from_spec import validate_schema
    from learning import patchfmt

    headers, diff = patchfmt.parse_proposal(proposal["content"])
    name = Path(str(headers.get("file") or proposal.get("file") or "weather.yaml")).name
    path = ROOT / "specs" / name
    if not path.exists():
        path = ROOT / "specs" / "weather.yaml"
    new = patchfmt.apply_diff(path.read_text(encoding="utf-8"), diff)
    validate_schema(yaml.safe_load(new))


@pytest.mark.parametrize(
    "fixture,kind",
    [("handoff_undeclared_status", "add_error_code"), ("handoff_wrong_param_type", "correct_param_type"),
     ("handoff_declared_code", "mark_needs_review")],
)
def test_learning_real_proposer(app, monkeypatch, fixture, kind):
    import learning.propose as lp

    seen = []
    real = lp.propose

    def spy(*a, **k):
        seen.append(1)
        return real(*a, **k)

    monkeypatch.setattr(lp, "propose", spy)
    if hasattr(app, "propose"):
        monkeypatch.setattr(app, "propose", spy)
    r = app.learning_proposal(app.load_fixture(fixture))
    assert r["ok"] is True, r
    assert len(r["proposals"]) == 1
    assert r["proposals"][0]["kind"] == kind
    assert r["applied"] is False and r["specs_written"] is False
    assert "human approval required" in r["note"]
    _check_applies(r["proposals"][0])
    assert seen, "spy never saw learning.propose.propose called"


def test_a_code_the_spec_declares_gives_a_needs_review_patch_and_its_action_file(app):
    fixture = app.load_fixture("handoff_declared_code")
    r = app.learning_proposal(fixture)
    assert r["ok"] is True and [p["kind"] for p in r["proposals"]] == ["mark_needs_review"]
    [action] = r["actions"]
    assert action["file"].endswith(".action.md") and "INVALID_COORDINATES" in action["content"]
    summary, patch, shown = app._tab3(fixture)  # what the third tab displays: the patch AND the action file
    assert patch.startswith("# proposal: mark_needs_review") and shown == action["content"]
    assert r["applied"] is False and "human approval required" in r["note"]


def test_learning_no_spec_system(app):
    r = app.learning_proposal(app.load_fixture("handoff_no_spec_system"))
    assert r["ok"] is True
    assert r["proposals"] == []
    assert r["report"]
    assert r["applied"] is False and r["specs_written"] is False
    assert "human approval required" in r["note"]


def test_learning_below_recurrence(app):
    r = app.learning_proposal(app.load_fixture("handoff_undeclared_status"), times_observed=1)
    assert r["ok"] is True
    assert r["proposals"] == []


def _set_key(obj, key, value):
    hit = False
    if isinstance(obj, dict):
        for k in list(obj):
            if k == key:
                obj[k] = value
                hit = True
            else:
                hit = _set_key(obj[k], key, value) or hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _set_key(v, key, value) or hit
    return hit


def test_learning_bad_inputs(app):
    good = app.load_fixture("handoff_undeclared_status")
    evil = json.loads(good)
    assert _set_key(evil, "error_code", "../../evil"), "fixture has no error_code key to tamper with"
    bad_tool = json.loads(good)
    assert _set_key(bad_tool, "tool", "a/b__c"), "fixture has no tool key to tamper with"
    cases = [
        ("not json", 2),
        ("[1, 2]", 2),
        (json.dumps(evil), 2),
        (json.dumps(bad_tool), 2),
        (good, 0),
        (good, 6),
        (" " * 20001 + good, 2),
    ]
    for text, n in cases:
        r = app.learning_proposal(text, n)
        assert r["ok"] is False, (text[:60], n, r)
        assert r["error"]
        assert r["applied"] is False and r["specs_written"] is False


# (f) banner
def test_banner(app):
    assert app.BANNER == BANNER_TEXT
    assert BANNER_TEXT in app.APP_TITLE
    for tok in ("C24", "C25", "C27", "§3a"):
        assert tok in app.BANNER


# (g) build_app wiring against a recording stand-in for gradio
def _fake_gradio():
    import types

    rec = {"comps": [], "tabs": [], "clicks": [], "blocks": [], "stack": []}

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Blocks(_Ctx):
        def __init__(self, *a, **k):
            rec["blocks"].append(k)

    class Tab(_Ctx):
        def __init__(self, label=None, *a, **k):
            self.label = label if label is not None else (a[0] if a else k.get("label"))
            rec["tabs"].append(self.label)

        def __enter__(self):
            rec["stack"].append(self.label)
            return self

        def __exit__(self, *a):
            rec["stack"].pop()
            return False

    def make(kind):
        class C:
            def __init__(self, *a, **k):
                self.kind, self.args, self.kwargs = kind, a, k
                self.tab = rec["stack"][-1] if rec["stack"] else None
                rec["comps"].append(self)

            def click(self, fn=None, inputs=None, outputs=None, *a, **k):
                rec["clicks"].append((self, fn, inputs, outputs))

            def change(self, fn=None, inputs=None, outputs=None, *a, **k):
                pass

        C.__name__ = kind
        return C

    mod = types.ModuleType("gradio")
    mod.Blocks, mod.Tab = Blocks, Tab
    for k in ("Markdown", "Textbox", "Dropdown", "Number", "Button", "JSON", "Code"):
        setattr(mod, k, make(k))
    return mod, rec


def _text_of(c):
    v = c.kwargs.get("value", c.args[0] if c.args else "")
    return v if isinstance(v, str) else str(v)


def test_build_app_wiring(app, monkeypatch):
    fake, rec = _fake_gradio()
    monkeypatch.setitem(sys.modules, "gradio", fake)
    app.build_app()

    assert rec["blocks"] and rec["blocks"][0].get("title") == app.APP_TITLE
    assert app.BANNER in app.APP_TITLE

    first = rec["comps"][0]
    assert first.kind == "Markdown" and app.BANNER in _text_of(first)
    assert first.tab is None, "banner must be outside every tab"

    labels = ["Duty matching", "Write-safety refusal", "Learning-loop proposal"]
    assert rec["tabs"] == labels

    for label, key in zip(labels, ["duty", "write", "learning"]):
        first_in_tab = next(c for c in rec["comps"] if c.tab == label)
        assert first_in_tab.kind == "Markdown", f"first component of {label} must be its caption"
        for cid in app.CLAIMS[key]:
            assert cid in _text_of(first_in_tab), (label, cid)

    assert len(rec["clicks"]) == 3
    assert [c[1] for c in rec["clicks"]] == [app.match_duty, app.write_safety, app._tab3]

    for _btn, fn, inputs, outputs in rec["clicks"]:
        assert inputs and outputs
        out = fn(*[i.kwargs.get("value") for i in inputs])
        if fn is app._tab3:
            assert isinstance(out, tuple) and len(out) == 3
        else:
            assert isinstance(out, dict)


# (h) claims against AUDIT.md (read only)
def _audit_ids():
    text = (ROOT / "AUDIT.md").read_text(encoding="utf-8")
    s2 = text.split("## 2. PROVEN", 1)[1].split("## 3. NOT PROVEN", 1)[0]
    s3a = text.split("### 3a.", 1)[1]
    s3a = re.split(r"^#{2,3} ", s3a, maxsplit=1, flags=re.M)[0]
    return (set(re.findall(r"^\| (C\d+) \|", s2, flags=re.M)),
            set(re.findall(r"^\| (C\d+) \|", s3a, flags=re.M)))


def test_claims_are_proven_and_banner_is_true(app):
    ids2, ids3a = _audit_ids()
    for key, ids in app.CLAIMS.items():
        for cid in ids:
            assert cid in ids2, f"{cid} ({key}) is not a proven row in AUDIT.md section 2"
    for cid in ("C24", "C25", "C27"):
        assert cid in ids3a, f"{cid} is not an open row of 3a"
        assert cid not in ids2


# (i) offline by construction
def test_demo_dir_is_offline():
    files = [p for p in (ROOT / "demo").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert files
    for p in files:
        if p.suffix in (".py", ".md"):
            assert PROD_HOST not in p.read_text(encoding="utf-8"), p
        if p.suffix == ".py":
            src = p.read_text(encoding="utf-8")
            assert not re.search(r"^\s*(import|from)\s+(anthropic|httpx)\b", src, flags=re.M), p


# the three directories the brief names must not change, before and after EACH demo function
def _hashes(*names):
    import hashlib

    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for n in names for p in sorted((ROOT / n).rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def test_specs_servers_and_handoffs_are_unchanged_before_and_after_each_demo_function(app):
    calls = [("match_duty", lambda: app.match_duty("refund order ORD-ABCD1234")),
             *[("write_safety " + t, (lambda t=t: app.write_safety(t))) for t in app.WRITE_TOOLS],
             *[("learning_proposal " + n, (lambda n=n: app.learning_proposal(app.load_fixture(n)))) for n in app.fixture_names()]]
    assert len(calls) == 1 + 3 + 4
    for label, call in calls:
        before = _hashes("specs", "servers", "handoffs")
        call()
        assert _hashes("specs", "servers", "handoffs") == before, f"{label} changed specs/, servers/ or handoffs/"
