"""Which LLM provider the planner and the doc-extraction client talk to (CLAUDE.md D29). Stdlib only.

Both clients speak the Anthropic Messages format (ingest/anthropic_http.py). This module is the ONE place that decides where
that request goes and how it is authenticated, so the same code runs against Anthropic, Ollama, freellmpool or OpenRouter with
environment variables alone:

  PROVIDER_NAME      anthropic (default) | ollama | freellmpool | openrouter
  PROVIDER_BASE_URL  default: Anthropic's production URL; Ollama http://localhost:11434; OpenRouter https://openrouter.ai/api;
                     freellmpool has no default (required)
  PROVIDER_API_KEY   default for anthropic ONLY: ANTHROPIC_API_KEY. A key is never borrowed across providers: ANTHROPIC_API_KEY
                     is not sent to Ollama, freellmpool or OpenRouter.
  PROVIDER_MODEL     default: anthropic AGENT_MODEL or claude-sonnet-5-5; ollama qwen3-coder; openrouter openrouter/free;
                     freellmpool has no default (required)
  PROVIDER_HEADERS   optional JSON object of extra headers, merged over the derived ones

Derived headers: anthropic `x-api-key: <key>`; ollama on a local URL `x-api-key: ollama` (no real key needed); ollama on any
other URL (Ollama cloud) `Authorization: Bearer <key>`; openrouter and freellmpool `Authorization: Bearer <key>`.

A required variable that is missing is a loud ProviderConfigError naming it, at the moment a client is built (never silently
at import). Nothing here is read at import time, so tests and callers can change the environment first.

Attestation (scripts/live_attest.py, D26) keeps naming ANTHROPIC_HOST as the production host: a run through any other provider
is, by construction, not `real` and can never close a gap that is about the real Claude.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlparse

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_HOST = urlparse(ANTHROPIC_BASE_URL).hostname or ""
PROVIDERS = ("anthropic", "ollama", "freellmpool", "openrouter")
DEFAULT_BASE_URLS: dict[str, str | None] = {
    "anthropic": ANTHROPIC_BASE_URL, "ollama": "http://localhost:11434", "openrouter": "https://openrouter.ai/api",
    "freellmpool": None}
DEFAULT_MODELS: dict[str, str | None] = {
    "anthropic": "claude-sonnet-5-5", "ollama": "qwen3-coder", "openrouter": "openrouter/free", "freellmpool": None}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class ProviderConfigError(RuntimeError):
    """The provider settings are unusable. `code` lets a caller map it onto its own error type."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)  # never printed
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    @property
    def is_default_anthropic(self) -> bool:
        return self.name == "anthropic" and self.host == ANTHROPIC_HOST


def is_local(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".localhost")


def _name(env: Mapping[str, str]) -> str:
    name = (env.get("PROVIDER_NAME") or "anthropic").strip().lower()
    if name not in PROVIDERS:
        raise ProviderConfigError("bad_provider", f"PROVIDER_NAME={name!r} is not one of {', '.join(PROVIDERS)}")
    return name


def _model(name: str, env: Mapping[str, str]) -> str:
    model = (env.get("PROVIDER_MODEL") or "").strip()
    if not model and name == "anthropic":
        model = (env.get("AGENT_MODEL") or "").strip()  # the variable the orchestrator has always honoured
    model = model or DEFAULT_MODELS[name] or ""
    if not model:
        raise ProviderConfigError("no_model", f"PROVIDER_NAME={name} needs PROVIDER_MODEL, and it is not set")
    return model


def resolve_model(env: Mapping[str, str] | None = None) -> str:
    """The model id for the configured provider. Needs no key: a stub client in a test has none."""
    env = os.environ if env is None else env
    return _model(_name(env), env)


def headers_for(name: str, base_url: str, key: str) -> dict[str, str]:
    if name == "anthropic":
        return {"x-api-key": key}
    if name == "ollama" and is_local(base_url):
        return {"x-api-key": "ollama"}
    return {"Authorization": f"Bearer {key}"} if key else {}


def resolve(env: Mapping[str, str] | None = None, *, api_key: str | None = None, base_url: str | None = None) -> ProviderConfig:
    """Read the four settings (and the headers) from `env`. An explicit `api_key` or `base_url` wins over the environment."""
    env = os.environ if env is None else env
    name = _name(env)
    url = (base_url or env.get("PROVIDER_BASE_URL") or DEFAULT_BASE_URLS[name] or "").strip()
    if not url:
        raise ProviderConfigError("no_base_url", f"PROVIDER_NAME={name} needs PROVIDER_BASE_URL, and it is not set")
    model = _model(name, env)
    key = (api_key or env.get("PROVIDER_API_KEY") or (env.get("ANTHROPIC_API_KEY") if name == "anthropic" else "") or "").strip()
    needs_key = name in ("anthropic", "openrouter") or (name == "ollama" and not is_local(url))
    if needs_key and not key:
        if name == "anthropic":
            if (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip():
                raise ProviderConfigError("unsupported_auth_token",
                                          "ANTHROPIC_AUTH_TOKEN is set but no longer supported; set ANTHROPIC_API_KEY.")
            raise ProviderConfigError(
                "no_api_key", "ANTHROPIC_API_KEY is not set (PROVIDER_NAME=anthropic needs PROVIDER_API_KEY or ANTHROPIC_API_KEY)")
        raise ProviderConfigError("no_api_key", f"PROVIDER_NAME={name} at {url} needs PROVIDER_API_KEY, and it is not set")
    headers = headers_for(name, url, key)
    raw = (env.get("PROVIDER_HEADERS") or "").strip()
    if raw:
        try:
            extra = json.loads(raw)
        except ValueError:
            extra = None
        if not isinstance(extra, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()):
            raise ProviderConfigError("bad_headers", "PROVIDER_HEADERS must be a JSON object of string names to string values")
        headers.update(extra)
    return ProviderConfig(name=name, base_url=url, model=model, api_key=key, headers=headers)
