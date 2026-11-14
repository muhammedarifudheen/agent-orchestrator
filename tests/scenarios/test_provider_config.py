"""The planner and doc clients are provider-agnostic (CLAUDE.md D29): the same code runs against Anthropic, Ollama, freellmpool
or OpenRouter with environment variables only. Nothing here reaches a network: an httpx MockTransport plays the provider and
records the request, so the tests see the URL, the headers and the body the clients really send. They prove the WIRING and the
configuration rules; they say nothing about whether any provider's model plans or extracts well, and none of these providers
other than Anthropic has been called by this repo."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agent import provider_config as pc
from fake_anthropic import tool_use_message, tool_use_response
from ingest.anthropic_http import AnthropicAPIError, AnthropicHTTP
from ingest.doc import TOOL_NAME, HttpDocExtractionClient, IngestError
from planners import ClaudePlanner, PlannedCall

ROOT = Path(__file__).resolve().parents[2]
REPORT = json.loads((ROOT / "tests" / "ingest" / "fixtures" / "dogceo.stub_response.json").read_text(encoding="utf-8"))
TEXT = (ROOT / "tests" / "ingest" / "fixtures" / "dogceo.doc.md").read_text(encoding="utf-8")
INTENTS = [{"id": "a", "intent": "look things up", "consumes": []}, {"id": "b", "intent": "act on it", "consumes": ["a"]}]
TOOLS = [{"name": "s1__find", "description": "Find things."}, {"name": "s2__act", "description": "Act."}]
GOOD = [{"intent_id": "a", "tool": "s1__find"}, {"intent_id": "b", "tool": "s2__act"}]
VARS = ("PROVIDER_NAME", "PROVIDER_BASE_URL", "PROVIDER_API_KEY", "PROVIDER_MODEL", "PROVIDER_HEADERS", "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN", "AGENT_MODEL")
ANTHROPIC_KEY = "sk-ant-the-anthropic-key-must-never-leave"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in VARS:
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def wire(monkeypatch):
    """Every AnthropicHTTP built WITHOUT a transport talks to a MockTransport that answers in the Anthropic Messages shape (a
    tool_use block), whatever base URL it was configured with. Returns the list of requests it saw."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        tools = {t["name"] for t in json.loads(request.content)["tools"]}
        if "submit_plan" in tools:
            return httpx.Response(200, json=tool_use_response(GOOD))
        return httpx.Response(200, json=tool_use_message(TOOL_NAME, REPORT))

    original = AnthropicHTTP.__init__

    def init(self, api_key=None, *, base_url=None, transport=None, timeout=120.0):
        original(self, api_key, base_url=base_url, transport=transport or httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(AnthropicHTTP, "__init__", init)
    return seen


def configure(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)


PROVIDER_CASES = [
    # id, env, expected host, expected auth header, expected model
    ("anthropic", {"PROVIDER_NAME": "anthropic", "PROVIDER_API_KEY": "k-anthropic"}, pc.ANTHROPIC_HOST,
     ("x-api-key", "k-anthropic"), "claude-sonnet-5-5"),
    ("ollama-local", {"PROVIDER_NAME": "ollama"}, "localhost", ("x-api-key", "ollama"), "qwen3-coder"),
    ("ollama-cloud", {"PROVIDER_NAME": "ollama", "PROVIDER_BASE_URL": "https://ollama.example.test", "PROVIDER_API_KEY": "k-cloud"},
     "ollama.example.test", ("authorization", "Bearer k-cloud"), "qwen3-coder"),
    ("openrouter", {"PROVIDER_NAME": "openrouter", "PROVIDER_API_KEY": "k-or"}, "openrouter.ai",
     ("authorization", "Bearer k-or"), "openrouter/free"),
    ("freellmpool", {"PROVIDER_NAME": "freellmpool", "PROVIDER_BASE_URL": "http://pool.test:8080", "PROVIDER_MODEL": "pool/any",
                     "PROVIDER_API_KEY": "k-pool"}, "pool.test", ("authorization", "Bearer k-pool"), "pool/any"),
]


@pytest.mark.parametrize("env,host,auth,model", [c[1:] for c in PROVIDER_CASES], ids=[c[0] for c in PROVIDER_CASES])
def test_both_clients_parse_an_anthropic_shaped_tool_use_reply_from_the_configured_provider(monkeypatch, wire, env, host, auth, model):
    configure(monkeypatch, ANTHROPIC_API_KEY=ANTHROPIC_KEY, **env)
    plan = ClaudePlanner().plan(INTENTS, TOOLS)
    report = HttpDocExtractionClient().extract(TEXT)
    assert plan == [PlannedCall("a", "s1__find"), PlannedCall("b", "s2__act")]
    assert report == REPORT
    assert len(wire) == 2
    for req in wire:
        assert req.url.host == host and req.url.path.endswith("/v1/messages"), "the request went to the configured provider"
        assert req.headers[auth[0]] == auth[1]
        assert json.loads(req.content)["model"] == model
        assert ANTHROPIC_KEY not in "".join(f"{k}={v}" for k, v in req.headers.items()) + req.content.decode(), \
            "ANTHROPIC_API_KEY is the Anthropic provider's fallback only; PROVIDER_API_KEY wins and no other provider borrows it"


def test_the_anthropic_key_is_never_sent_to_another_provider(monkeypatch, wire):
    configure(monkeypatch, ANTHROPIC_API_KEY=ANTHROPIC_KEY, PROVIDER_NAME="openrouter")
    with pytest.raises(AnthropicAPIError, match="PROVIDER_API_KEY"):  # no borrowed key: it refuses instead
        ClaudePlanner().plan(INTENTS, TOOLS)
    assert wire == [], "nothing was sent"
    configure(monkeypatch, PROVIDER_API_KEY="k-or")
    ClaudePlanner().plan(INTENTS, TOOLS)
    assert ANTHROPIC_KEY not in "".join(str(r.headers) for r in wire) + "".join(r.content.decode() for r in wire)


def test_a_missing_key_for_the_anthropic_provider_is_a_loud_error_and_nothing_is_sent(monkeypatch, wire):
    for name in ({}, {"PROVIDER_NAME": "anthropic"}):  # the default and the explicit form are the same rule
        for k in VARS:
            monkeypatch.delenv(k, raising=False)
        configure(monkeypatch, **name)
        with pytest.raises(pc.ProviderConfigError, match="ANTHROPIC_API_KEY is not set") as e:
            pc.resolve()
        assert e.value.code == "no_api_key" and "PROVIDER_API_KEY" in str(e.value)
        with pytest.raises(AnthropicAPIError, match="no_api_key"):
            ClaudePlanner().plan(INTENTS, TOOLS)
        with pytest.raises(IngestError, match="ANTHROPIC_API_KEY is not set"):
            HttpDocExtractionClient()
    assert wire == []
    configure(monkeypatch, PROVIDER_NAME="anthropic", PROVIDER_API_KEY="k")
    assert pc.resolve().api_key == "k"  # PROVIDER_API_KEY alone is enough; ANTHROPIC_API_KEY is the fallback


def test_ollama_with_a_local_base_url_needs_no_real_key(monkeypatch, wire):
    configure(monkeypatch, PROVIDER_NAME="ollama", PROVIDER_BASE_URL="http://127.0.0.1:11434")
    cfg = pc.resolve()
    assert cfg.api_key == "" and dict(cfg.headers) == {"x-api-key": "ollama"} and cfg.model == "qwen3-coder"
    assert ClaudePlanner().plan(INTENTS, TOOLS) == [PlannedCall("a", "s1__find"), PlannedCall("b", "s2__act")]
    assert HttpDocExtractionClient().extract(TEXT) == REPORT
    assert {r.headers["x-api-key"] for r in wire} == {"ollama"} and {r.url.host for r in wire} == {"127.0.0.1"}
    # the same provider on a non-local URL (Ollama cloud) DOES need one
    monkeypatch.setenv("PROVIDER_BASE_URL", "https://ollama.example.test")
    with pytest.raises(pc.ProviderConfigError, match="PROVIDER_NAME=ollama .* needs PROVIDER_API_KEY"):
        pc.resolve()


def test_every_provider_names_the_variable_it_needs_and_a_bad_setting_is_loud(monkeypatch):
    configure(monkeypatch, PROVIDER_NAME="openrouter")
    with pytest.raises(pc.ProviderConfigError, match="PROVIDER_API_KEY"):
        pc.resolve()
    monkeypatch.setenv("PROVIDER_NAME", "freellmpool")
    with pytest.raises(pc.ProviderConfigError, match="PROVIDER_BASE_URL"):
        pc.resolve()
    monkeypatch.setenv("PROVIDER_BASE_URL", "http://pool.test")
    with pytest.raises(pc.ProviderConfigError, match="PROVIDER_MODEL"):
        pc.resolve()
    monkeypatch.setenv("PROVIDER_NAME", "mystery")
    with pytest.raises(pc.ProviderConfigError, match="not one of anthropic, ollama, freellmpool, openrouter"):
        pc.resolve()
    monkeypatch.setenv("PROVIDER_NAME", "ollama")
    monkeypatch.setenv("PROVIDER_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("PROVIDER_HEADERS", "not json")
    with pytest.raises(pc.ProviderConfigError, match="PROVIDER_HEADERS"):
        pc.resolve()
    monkeypatch.setenv("PROVIDER_HEADERS", '{"x-team": "dev"}')
    assert pc.resolve().headers["x-team"] == "dev" and pc.resolve().headers["x-api-key"] == "ollama"


def test_with_nothing_set_the_behaviour_is_what_it_was_before_the_change(monkeypatch):
    configure(monkeypatch, ANTHROPIC_API_KEY="k")
    cfg = pc.resolve()
    assert (cfg.name, cfg.base_url, cfg.model, dict(cfg.headers)) == ("anthropic", pc.ANTHROPIC_BASE_URL, "claude-sonnet-5-5", {"x-api-key": "k"})
    assert cfg.is_default_anthropic and "k" not in repr(cfg), "the key is not in the repr"
    monkeypatch.setenv("AGENT_MODEL", "an-older-override")
    assert pc.resolve_model() == "an-older-override", "AGENT_MODEL is still honoured for Anthropic"
    monkeypatch.setenv("PROVIDER_MODEL", "wins")
    assert pc.resolve_model() == "wins"
    configure(monkeypatch, ANTHROPIC_API_KEY="", ANTHROPIC_AUTH_TOKEN="tok")
    with pytest.raises(pc.ProviderConfigError, match="ANTHROPIC_AUTH_TOKEN is set but no longer supported; set ANTHROPIC_API_KEY.") as e:
        pc.resolve()
    assert "tok" not in str(e.value).replace("ANTHROPIC_AUTH_TOKEN", "")


def test_a_run_through_another_provider_can_never_be_attested_real(monkeypatch):
    """D26: `real` is about the production host. Another provider's host is not it, so a pass over Ollama or OpenRouter cannot
    close a gap that is about the real Claude."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import live_attest
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    assert live_attest.ANTHROPIC_HOST == pc.ANTHROPIC_HOST
    configure(monkeypatch, PROVIDER_NAME="openrouter", PROVIDER_API_KEY="k")
    assert pc.resolve().host != live_attest.ANTHROPIC_HOST and not pc.resolve().is_default_anthropic


def test_the_production_host_is_written_in_exactly_one_python_file():
    needle = "api." + "anthropic" + ".com"
    hits = []
    for path in ROOT.rglob("*.py"):
        parts = set(path.relative_to(ROOT).parts)
        if parts & {".venv", ".git", ".perf", "__pycache__", "inbox", "runs", "proposals"}:
            continue
        if needle in path.read_text(encoding="utf-8", errors="replace"):
            hits.append(path.relative_to(ROOT).as_posix())
    assert hits == ["agent/provider_config.py"], f"{needle} must live only in agent/provider_config.py, found in {hits}"
    assert pc.ANTHROPIC_HOST == needle


@pytest.mark.parametrize("env,expect", [
    ({}, False), ({"ANTHROPIC_API_KEY": "k"}, True), ({"ANTHROPIC_AUTH_TOKEN": "tok"}, False),
    ({"PROVIDER_NAME": "ollama"}, True), ({"PROVIDER_NAME": "ollama", "PROVIDER_BASE_URL": "https://ollama.example.test"}, False),
    ({"PROVIDER_NAME": "ollama", "PROVIDER_BASE_URL": "https://ollama.example.test", "PROVIDER_API_KEY": "k"}, True),
    ({"PROVIDER_NAME": "openrouter"}, False), ({"PROVIDER_NAME": "openrouter", "PROVIDER_API_KEY": "k"}, True),
    ({"PROVIDER_NAME": "freellmpool", "PROVIDER_BASE_URL": "http://pool.test"}, False),
    ({"PROVIDER_NAME": "freellmpool", "PROVIDER_BASE_URL": "http://pool.test", "PROVIDER_MODEL": "m"}, True),
    ({"PROVIDER_NAME": "mystery", "ANTHROPIC_API_KEY": "k"}, False),
])
def test_credentials_present_is_the_gate_for_live_runs_and_follows_the_provider(env, expect):
    assert pc.credentials_present(env) is expect


def test_the_harness_context_sees_the_provider_and_never_hands_a_placeholder_to_the_client():
    import io
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import live_judgment as lj
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    mk = lambda env: lj._Ctx(env, io.StringIO(), io.StringIO())  # noqa: E731
    assert mk({}).key == "" and mk({"ANTHROPIC_API_KEY": "sk-a"}).key == "sk-a" and mk({"ANTHROPIC_API_KEY": "sk-a"}).client_key == "sk-a"
    local = mk({"PROVIDER_NAME": "ollama"})
    assert local.key and local.client_key is None, "a truthy gate value, and the client reads PROVIDER_* itself"
    assert mk({"PROVIDER_NAME": "openrouter"}).key == "" and mk({"PROVIDER_NAME": "openrouter", "PROVIDER_API_KEY": "k-or"}).client_key is None