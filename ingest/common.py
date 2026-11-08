"""Shared building blocks for every ingestor, so all three emit the same shape and apply the same rules.

Use ``make_spec`` / ``make_operation`` / ``make_param``; do not hand-assemble spec dicts. The output
of ``make_spec`` must pass ``generators.from_spec.validate_schema`` (the existing validator).

Review rules (enforced HERE, once):
  * every non-read operation (anything but GET/HEAD) is ``needs_review: true``: an ingestor can infer
    the HTTP method, never whether the call moves money, is idempotent, or is safe to retry;
  * an operation whose ``confidence`` is below ``CONFIDENCE_THRESHOLD`` is ``needs_review: true``;
  * any operation given ``reasons`` is ``needs_review: true``.
"""
from __future__ import annotations

import re
from typing import Any

# DECISION D12 (CLAUDE.md): changing this number requires changing D12 in the same commit.
# tests/test_review_gate.py asserts the two agree. Never lower it to make a test or a gate pass.
CONFIDENCE_THRESHOLD = 0.75

READ_METHODS = ("GET", "HEAD")
IDEMPOTENT_WRITE_METHODS = ("PUT", "DELETE")
SOURCES = ("openapi", "har", "llm")  # values allowed in review.source by specs/schema.json

# System-level defaults every ingested spec gets (429 and 5xx are transient and the agent may retry once).
STANDARD_ERROR_MAP: list[dict[str, Any]] = [
    {"when": {"status": 429}, "code": "RATE_LIMITED", "retryable": True, "recovery": "agent",
     "meaning": "Rate limit reached; retry later"},
    {"when": {"status": "5xx"}, "code": "SERVICE_UNAVAILABLE", "retryable": True, "recovery": "agent",
     "meaning": "The service is unavailable"},
]


def snake(text: str) -> str:
    """lower_snake_case identifier that starts with a letter (``postCode`` -> ``post_code``)."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(text))
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text if text and text[0].isalpha() else "p_" + text


def system_name_from(text: str) -> str:
    """Spec ``system_name``: lowercase letters/digits only, starting with a letter."""
    name = re.sub(r"[^a-z0-9]", "", str(text).lower())
    return name if name and name[0].isalpha() else "api" + name


def normalize_path(path: str) -> str:
    """Rename ``{postalCode}`` placeholders to snake_case so they match the generated argument names."""
    return re.sub(r"\{([^}]+)\}", lambda m: "{" + snake(m.group(1)) + "}", path)


def infer_json_type(value: Any) -> str:
    """Spec param type for a sample JSON/query value (``string|number|integer|boolean``)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _text(value: str | None, fallback: str, minimum: int) -> str:
    value = " ".join((value or "").split())
    return value if len(value) >= minimum else fallback


def make_param(name: str, type: str, where: str, required: bool, description: str | None = None, *,
               constraints: dict[str, Any] | None = None, default: Any = None) -> dict[str, Any]:
    """One operation parameter. ``name`` is snake_cased; the original becomes ``wire_name`` when it differs
    (not for path params: their placeholder in the path is renamed instead, see ``normalize_path``)."""
    arg = snake(name)
    param: dict[str, Any] = {"name": arg, "type": type, "in": where, "required": bool(required),
                             "description": _text(description, f"The {name} parameter.", 3)}
    if where != "path" and arg != name:
        param["wire_name"] = name
    if constraints:
        param["constraints"] = constraints
    if default is not None and not required:
        param["default"] = default
    return param


def ok_example(args: dict[str, Any], body: Any, expect_keys: list[str] | None = None, *,
               name: str = "example", status: int = 200) -> dict[str, Any]:
    """A successful example: the offline mock the generated tests replay, and what must come back."""
    example: dict[str, Any] = {"name": name, "args": args, "mock": {"status": status, "body": body}, "expect": "ok"}
    if expect_keys:
        example["expect_keys"] = expect_keys
    return example


def invalid_argument_example(args: dict[str, Any], name: str = "invalid_argument") -> dict[str, Any]:
    """A client-side validation failure (no mock needed: it never reaches the network)."""
    return {"name": name, "args": args, "expect": {"code": "INVALID_ARGUMENT", "retryable": False,
                                                   "recovery": "agent"}}


def make_operation(*, name: str, summary: str, when_to_call: str, method: str, path: str,
                   params: list[dict[str, Any]], response_type: str, response_description: str,
                   examples: list[dict[str, Any]], source: str, confidence: float,
                   reasons: list[str] | None = None, select: str | None = None,
                   query_const: dict[str, Any] | None = None, errors: list[dict[str, Any]] | None = None,
                   idempotent: bool | None = None) -> dict[str, Any]:
    """Build one schema-valid operation and apply the review rules (module docstring)."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if not any(e.get("expect") == "ok" for e in examples):
        raise ValueError(f"{name}: an ingestor must supply at least one `expect: ok` example")
    method = method.upper()
    side_effect = "read" if method in READ_METHODS else "write"
    reasons = list(reasons or [])
    if side_effect != "read":
        reasons.append(f"{method} is a write: side_effect (could it move money?), idempotency and retry safety "
                       "cannot be inferred and must be set by a person (review.safety_fields: reviewed)")
    confidence = round(max(0.0, min(1.0, float(confidence))), 2)
    if confidence < CONFIDENCE_THRESHOLD:
        reasons.append(f"confidence {confidence} is below the threshold {CONFIDENCE_THRESHOLD}")
    if idempotent is None:
        idempotent = side_effect == "read" or method in IDEMPOTENT_WRITE_METHODS
    if side_effect == "read":
        # The standard error_map (make_spec) turns 5xx into a retryable, agent-recoverable SERVICE_UNAVAILABLE.
        # Exercising it here means every ingested server has an outage probe in the shared contract test.
        first_ok = next(e for e in examples if e.get("expect") == "ok")
        examples = list(examples) + [{
            "name": "service_unavailable", "args": dict(first_ok["args"]),
            "mock": {"status": 503, "body": {"error": "service unavailable"}},
            "expect": {"code": "SERVICE_UNAVAILABLE", "retryable": True, "recovery": "agent"}}]
    request: dict[str, Any] = {"method": method, "path": path}
    if query_const:
        request["query_const"] = query_const
    response: dict[str, Any] = {"type": response_type, "description": _text(response_description, "The response body.", 10)}
    if select:
        response["select"] = select
    op: dict[str, Any] = {
        "name": snake(name),
        "summary": _text(summary, f"{method} {path}", 10),
        "when_to_call": _text(when_to_call, f"Call when you need to {method} {path}.", 10),
        "side_effect": side_effect,
        "idempotent": bool(idempotent),
        "request": request,
        "params": params,
        "response": response,
        "examples": examples,
        "review": {"source": source, "safety_fields": "unreviewed"},
        "confidence": confidence,
        "needs_review": bool(reasons),
        "review_reasons": reasons,
    }
    if errors:
        op["errors"] = errors
    return op


def make_spec(*, system_name: str, base_url: str, description: str, operations: list[dict[str, Any]],
              auth: dict[str, Any] | None = None, timeout_s: float = 10) -> dict[str, Any]:
    """A complete draft spec. Operation names are de-duplicated (``_2`` suffix) and the spec carries
    the standard error map. Always run ``generators.from_spec.validate_schema`` on the result."""
    seen: dict[str, int] = {}
    for op in operations:
        base = op["name"]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            op["name"] = f"{base}_{seen[base]}"
    name = system_name_from(system_name)
    return {
        "system_name": name,
        "description": _text(description, f"{name} API.", 3),
        "base_url": base_url.rstrip("/"),
        "base_url_env": f"{name.upper()}_BASE_URL",
        "timeout_s": timeout_s,
        "auth": auth or {"kind": "none"},
        "error_map": [dict(rule, when=dict(rule["when"])) for rule in STANDARD_ERROR_MAP],
        "operations": operations,
    }
