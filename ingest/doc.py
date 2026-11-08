"""Pasted API documentation text -> draft system spec (specs/schema.json): LLM extraction + deterministic checks.

The model may hallucinate, so it is only trusted for WHAT the document seems to say. Code then checks every
operation against the source text (grounding) and caps the confidence; safety fields (side_effect,
idempotent, needs_review) come only from ``ingest.common.make_operation`` and are never the model's call.

Grounding caps (final confidence = min(model confidence, all caps); never raised above the model's value):
  * evidence_quote not in the document ........ 0.3
  * path static segments not in the document .. 0.4
  * a param name not in the document .......... 0.6 (per missing name; the minimum applies)
  * base_url not in the document .............. review reason on every operation (no cap)
  * no sample response in the document ........ review reason, mock body is a minimal ``{}`` / ``[]``
"""
from __future__ import annotations

import os
import re
from typing import Any

from ingest.common import (invalid_argument_example, make_operation, make_param, make_spec,
                           normalize_path, ok_example, snake)

TOOL_NAME = "report_api"
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5-5")
SUPPORTED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")  # what specs/schema.json allows

CAP_EVIDENCE = 0.3
CAP_PATH = 0.4
CAP_PARAM = 0.6

SYSTEM_PROMPT = (
    "You extract a machine-readable description of an HTTP API from documentation text the user pastes.\n"
    "Rules:\n"
    "- Extract only what the document states. Never invent endpoints, parameters, base URLs or responses.\n"
    "- If you are unsure of anything, use null and a LOW confidence instead of guessing.\n"
    "- evidence_quote must be copied verbatim from the document (a short passage that documents that operation).\n"
    "- example_response must be a JSON value copied from the document if it shows one; otherwise null.\n"
    "- confidence is your honest certainty (0..1) that the operation is exactly as documented.\n"
    f"You must answer by calling the {TOOL_NAME} tool exactly once; do not write any other text."
)

_PARAM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"enum": ["string", "number", "integer", "boolean"]},
        "in": {"enum": ["path", "query", "header", "body"]},
        "required": {"type": "boolean"},
        "description": {"type": "string"},
    },
    "required": ["name", "type", "in", "required"],
}
_OPERATION = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "snake_case verb phrase, e.g. list_all_breeds"},
        "summary": {"type": "string"},
        "when_to_call": {"type": "string"},
        "method": {"type": "string", "description": "HTTP method as documented"},
        "path": {"type": "string", "description": "Path relative to base_url, placeholders as {name}"},
        "params": {"type": "array", "items": _PARAM},
        "response_type": {"enum": ["object", "array"]},
        "response_description": {"type": "string"},
        "example_args": {"type": "object"},
        "example_response": {"description": "JSON value copied from the document, or null if none shown"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_quote": {"type": "string", "description": "Verbatim quote from the document"},
    },
    "required": ["name", "method", "path", "confidence", "evidence_quote"],
}
TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": "Report the API described by the document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "system_title": {"type": "string"},
            "base_url": {"type": "string"},
            "description": {"type": "string"},
            "auth": {
                "type": "object",
                "properties": {
                    "kind": {"enum": ["none", "api_key", "bearer", "basic"]},
                    "env": {"type": "string", "description": "Suggested env var name for the secret"},
                    "header": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["kind"],
            },
            "operations": {"type": "array", "items": _OPERATION},
        },
        "required": ["system_title", "base_url", "auth", "operations"],
    },
}


class IngestError(ValueError):
    """The document could not be turned into a draft spec (no key, bad model output, ...)."""


class DocNoToolCall(IngestError):
    """The model replied without calling the extraction tool. Recoverable: the caller may retry ONCE with
    ``strict=True`` (a stricter prompt); it is never silently papered over. Carries the same fields a
    ToolFailure does (code / retryable / recovery) and the same text format."""

    code = "DOC_NO_TOOL_CALL"
    retryable = True
    recovery = "agent"

    def __init__(self, stop_reason: Any, said: str = "") -> None:
        super().__init__(f"[{self.code}] retryable=true recovery=agent the model did not call the {TOOL_NAME} "
                         f"tool (stop_reason={stop_reason}); it said: {said!r}")


# Provenance written into every spec this module produces. Never "verified": no real-model run has checked this path.
UNVERIFIED_SOURCE = "doc-unverified"


# -- LLM step ------------------------------------------------------------------------------------

def _get_client() -> Any:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise IngestError("ANTHROPIC_API_KEY is not set: doc ingestion calls the Claude API. Set the key, or "
                          "pass client=... (tests use ingest.testing.StubAnthropic).")
    try:
        import anthropic
        return anthropic.Anthropic()
    except Exception as exc:  # missing package or bad config: say so plainly
        raise IngestError(f"could not create the Anthropic client: {type(exc).__name__}: {exc}") from exc


STRICT_SUFFIX = (
    "\nIMPORTANT: your previous reply did not call the tool. Reply ONLY by calling the "
    f"{TOOL_NAME} tool. No prose, no apologies, no questions; if you are unsure, call the tool with low confidence."
)


def _call_model(client: Any, model: str, text: str, strict: bool = False) -> dict[str, Any]:
    """ONE call, tool_choice "auto". The happy path is the tested path: no forced choice, no fallback.
    If the model answers without calling the tool that is an explicit, recoverable failure (DocNoToolCall)."""
    try:
        resp = client.messages.create(
            model=model, max_tokens=16000, system=SYSTEM_PROMPT + (STRICT_SUFFIX if strict else ""),
            tools=[TOOL_SCHEMA], tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": "Documentation to analyse:\n\n" + text}])
    except Exception as exc:
        raise IngestError(f"Claude API call failed: {type(exc).__name__}: {exc}") from exc
    blocks = getattr(resp, "content", None) or []
    for block in blocks:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
            data = block.input
            if not isinstance(data, dict):
                raise IngestError("the model's tool input is not a JSON object")
            return data
    said = " ".join(str(getattr(b, "text", "")) for b in blocks if getattr(b, "type", None) == "text").strip()
    raise DocNoToolCall(getattr(resp, "stop_reason", None), said[:200])


# -- deterministic verification --------------------------------------------------------------------

def _norm(s: Any) -> str:
    return " ".join(str(s).split())


def _static_segments(path: str) -> list[str]:
    return [s for s in (_norm(p) for p in re.split(r"\{[^}]*\}", path)) if s.strip("/")]


def _relative_path(raw: Any, base_url: str) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = raw.strip().split("?")[0].split("#")[0]
    if re.match(r"https?://", path):
        base = base_url.rstrip("/")
        if base and path.startswith(base):
            path = path[len(base):] or "/"
        else:
            return None
    return path if path.startswith("/") else None


def _sample_is_grounded(sample: Any, doc: str) -> bool:
    """True when the sample's top-level keys (first element's, for lists) appear in the document."""
    if isinstance(sample, list):
        sample = next((i for i in sample if isinstance(i, dict)), None)
    return isinstance(sample, dict) and bool(sample) and any(k in doc for k in sample)


def _build_operation(raw: dict[str, Any], doc: str, base_reasons: list[str]) -> dict[str, Any]:
    path, method = raw["_path"], raw["_method"]
    reasons = list(base_reasons)
    conf = raw.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        reasons.append("the model reported no usable confidence; assumed 0.5")
        conf = 0.5
    conf = max(0.0, min(1.0, float(conf)))
    caps = [conf]

    quote = _norm(raw.get("evidence_quote") or "")
    if not quote or quote not in doc:
        reasons.append("evidence quote does not appear in the document")
        caps.append(CAP_EVIDENCE)
    if any(s not in doc for s in _static_segments(path)):
        reasons.append(f"path {path} does not appear in the document")
        caps.append(CAP_PATH)

    params: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in raw.get("params") or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        name = str(p["name"])
        arg = snake(name)
        if arg in seen:
            continue
        seen.add(arg)
        where = p.get("in") if p.get("in") in ("path", "query", "header", "body") else "query"
        if name not in doc:
            reasons.append(f"parameter {name} does not appear in the document")
            caps.append(CAP_PARAM)
        ptype = p.get("type") if p.get("type") in ("string", "number", "integer", "boolean") else "string"
        params.append(make_param(name, ptype, where, bool(p.get("required")) or where == "path",
                                 p.get("description")))

    placeholders = re.findall(r"\{([^}]+)\}", path)
    placeholder_names = {snake(x) for x in placeholders}
    for prm in params:  # a "path" param without a placeholder cannot be generated: demote it
        if prm["in"] == "path" and prm["name"] not in placeholder_names:
            prm["in"] = "query"
            reasons.append(f"parameter {prm['name']} was declared in the path but {path} has no such placeholder")
    declared = {prm["name"] for prm in params}
    for ph in placeholders:
        if snake(ph) not in declared:
            params.append(make_param(ph, "string", "path", True, f"The {ph} path parameter."))
            declared.add(snake(ph))
            reasons.append(f"path placeholder {{{ph}}} was not declared as a parameter by the model")

    args = raw.get("example_args") if isinstance(raw.get("example_args"), dict) else {}
    args = {snake(k): v for k, v in args.items()}
    for prm in params:
        if prm["required"] and prm["name"] not in args:
            args[prm["name"]] = {"integer": 1, "number": 1.0, "boolean": True}.get(prm["type"], "example")
            reasons.append(f"no example value for required parameter {prm['name']}; a placeholder was used")

    rtype = raw.get("response_type") if raw.get("response_type") in ("object", "array") else "object"
    sample = raw.get("example_response")
    if _sample_is_grounded(sample, doc):
        body = sample
        rtype = "array" if isinstance(sample, list) else "object"
        first = sample[0] if isinstance(sample, list) else sample
        expect_keys = list(first) if isinstance(sample, dict) else None
    else:
        reasons.append("no sample response in the document")
        body = [] if rtype == "array" else {}
        expect_keys = None

    examples = [ok_example(args, body, expect_keys)]
    num = next((p for p in params if p["required"] and p["type"] in ("integer", "number")), None)
    if num:
        examples.append(invalid_argument_example({**args, num["name"]: "not-a-number"}))

    return make_operation(
        name=str(raw.get("name") or f"{method}_{path}"), summary=raw.get("summary") or "",
        when_to_call=raw.get("when_to_call") or "", method=method, path=normalize_path(path), params=params,
        response_type=rtype, response_description=raw.get("response_description") or "",
        examples=examples, source="llm", confidence=min(caps), reasons=reasons)


def ingest_with_report(text: str, client: Any = None, model: str | None = None,
                       strict: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Return (draft spec, notes about operations that were left out of it)."""
    if not isinstance(text, str) or not text.strip():
        raise IngestError("the documentation text is empty")
    if client is None:
        client = _get_client()
    data = _call_model(client, model or DEFAULT_MODEL, text, strict)
    ops_raw = data.get("operations")
    if not isinstance(ops_raw, list) or not ops_raw:
        raise IngestError("the model response has no `operations` list")
    base_url = str(data.get("base_url") or "").strip()
    if not re.match(r"https?://", base_url):
        raise IngestError(f"the model reported no usable base_url ({base_url!r}); cannot build a spec")

    doc = _norm(text)
    base_reasons: list[str] = []
    if _norm(base_url).rstrip("/") not in doc:
        base_reasons.append(f"base_url {base_url} does not appear in the document")

    dropped: list[str] = []
    operations: list[dict[str, Any]] = []
    for i, raw in enumerate(ops_raw):
        if not isinstance(raw, dict):
            dropped.append(f"operation #{i + 1}: not an object")
            continue
        label = f"{raw.get('name') or f'operation #{i + 1}'} ({raw.get('method')} {raw.get('path')})"
        method = str(raw.get("method") or "").strip().upper()
        path = _relative_path(raw.get("path"), base_url)
        if method not in SUPPORTED_METHODS:
            dropped.append(f"{label}: unsupported or missing method {raw.get('method')!r}")
        elif path is None:
            dropped.append(f"{label}: no usable path")
        else:
            operations.append(_build_operation({**raw, "_path": path, "_method": method}, doc, base_reasons))
    if not operations:
        raise IngestError("no usable operations in the model response: " + "; ".join(dropped))

    auth_in = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    kind = auth_in.get("kind") if auth_in.get("kind") in ("none", "api_key", "bearer", "basic") else "none"
    auth: dict[str, Any] = {"kind": kind}
    if kind in ("api_key", "bearer"):
        auth["env"] = str(auth_in.get("env") or "API_KEY")
    if kind == "api_key":
        if auth_in.get("query"):
            auth["query"] = str(auth_in["query"])
        else:
            auth["header"] = str(auth_in.get("header") or "X-API-Key")
    if kind == "basic":
        auth["user_env"], auth["password_env"] = "API_USER", "API_PASSWORD"
    spec = make_spec(system_name=str(data.get("system_title") or "api"), base_url=base_url,
                     description=str(data.get("description") or ""), operations=operations, auth=auth)
    spec["source"] = UNVERIFIED_SOURCE  # visible in the spec file itself and printed by the pipeline (D16)
    return spec, dropped


def ingest(text: str, *, client: Any = None, model: str | None = None, strict: bool = False) -> dict[str, Any]:
    """Pasted documentation text -> draft spec dict. See the module docstring for the checks applied.
    Raises DocNoToolCall (recoverable) when the model does not call the tool; retry once with strict=True."""
    return ingest_with_report(text, client=client, model=model, strict=strict)[0]
