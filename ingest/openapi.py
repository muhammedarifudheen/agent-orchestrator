"""Deterministic OpenAPI 3.x / Swagger 2.0 -> draft system spec (no LLM, no network).

``ingest(document)`` returns a spec for ``specs/schema.json`` built with ``ingest.common``. Anything the
spec format cannot represent is OMITTED from the operation and recorded as a ``review_reasons`` entry
(which makes the operation ``needs_review``); nothing is dropped silently.

Deliberately not handled: remote/file ``$ref``; ``oneOf``/``anyOf`` bodies; arrays/objects as params;
non-JSON and multipart bodies; cookie params; per-operation ``security`` overrides (the document-level
schemes are used for every operation); OAuth2/OpenID/mutual TLS (auth becomes ``none`` + a review reason
on every operation); ``HEAD`` operations (the spec format has no HEAD request method; skipped with a
``warnings.warn``); OpenAPI 3.1 numeric ``exclusiveMinimum/Maximum``; callbacks, links, webhooks.
"""
from __future__ import annotations

import json
import re
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from ingest.common import (invalid_argument_example, make_operation, make_param, make_spec, normalize_path,
                           ok_example, snake, system_name_from)

METHODS = ("get", "put", "post", "delete", "patch", "head")
SCALARS = ("string", "number", "integer", "boolean")
BASE_CONFIDENCE = 0.95
PENALTY_NO_DESCRIPTION = 0.10
PENALTY_SYNTHESIZED = 0.15
PENALTY_UNSUPPORTED = 0.10
MAX_DEPTH = 5


class IngestError(ValueError):
    """The document cannot be turned into a spec at all (e.g. no base URL)."""


def load(path: str | Path) -> dict[str, Any]:
    """Read a .json / .yaml / .yml OpenAPI document."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(text)
    elif path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        raise IngestError(f"unsupported file type {path.suffix!r}: use .json, .yaml or .yml")
    if not isinstance(data, dict):
        raise IngestError(f"{path} does not contain an OpenAPI object")
    return data


# -- $ref / schema helpers ---------------------------------------------------------------------

def _follow(node: Any, root: dict, stack: tuple = ()) -> tuple[dict, tuple]:
    """Follow local ``$ref`` chains. A cycle or an unresolvable ref yields ``{}``."""
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in stack:
            return {}, stack
        stack = stack + (ref,)
        target: Any = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, dict) else None
        node = target
    return (node if isinstance(node, dict) else {}), stack


def _req(s: dict) -> list[str]:
    r = s.get("required")  # Swagger 2.0 parameters use a boolean here
    return r if isinstance(r, list) else []


def _flat(schema: Any, root: dict, stack: tuple = ()) -> tuple[dict, tuple]:
    """Resolve ``$ref`` and merge ``allOf`` into one schema dict (properties/required unioned)."""
    s, stack = _follow(schema, root, stack)
    out = {k: v for k, v in s.items() if k != "allOf"}
    props: dict[str, Any] = {}
    required: list[str] = []
    for sub in s.get("allOf") or []:
        f, _ = _flat(sub, root, stack)
        props.update(f.get("properties") or {})
        required += [r for r in _req(f) if r not in required]
        for k, v in f.items():
            if k not in ("properties", "required"):
                out.setdefault(k, v)
    props.update(s.get("properties") or {})
    required += [r for r in _req(s) if r not in required]
    if props:
        out["properties"] = props
    if required:
        out["required"] = required
    elif isinstance(out.get("required"), list):
        del out["required"]
    return out, stack


def _type(f: dict) -> str | None:
    t = f.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t is None:
        if f.get("properties"):
            return "object"
        if "items" in f:
            return "array"
    return t


def _synth(schema: Any, root: dict, stack: tuple = (), depth: int = 0) -> Any:
    f, st = _flat(schema, root, stack)
    for key in ("example", "default"):
        if f.get(key) is not None:
            return f[key]
    if f.get("enum"):
        return f["enum"][0]
    t = _type(f)
    if t is None:
        for key in ("oneOf", "anyOf"):
            if f.get(key):
                return _synth(f[key][0], root, st, depth + 1)
        return {}
    if t == "object":
        if depth >= MAX_DEPTH:
            return {}
        return {name: _synth(child, root, st, depth + 1) for name, child in (f.get("properties") or {}).items()}
    if t == "array":
        return [] if depth >= MAX_DEPTH else [_synth(f.get("items") or {}, root, st, depth + 1)]
    if t == "integer":
        return int(f["minimum"]) if isinstance(f.get("minimum"), (int, float)) else 1
    if t == "number":
        return f["minimum"] if isinstance(f.get("minimum"), (int, float)) else 1.5
    if t == "boolean":
        return True
    return "string"


def _constraints(f: dict) -> dict[str, Any]:
    c: dict[str, Any] = {}
    for src, dst in (("enum", "enum"), ("pattern", "pattern"), ("minLength", "min_length"),
                     ("maxLength", "max_length"), ("minimum", "minimum"), ("maximum", "maximum")):
        v = f.get(src)
        if v is None or isinstance(v, bool):
            continue
        if dst in ("minimum", "maximum") and not isinstance(v, (int, float)):
            continue
        c[dst] = v
    return c


def _sentence(text: str | None, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


# -- document level ----------------------------------------------------------------------------

def _base_url(doc: dict, source_name: str | None) -> str:
    url: str | None = None
    if "swagger" in doc:
        host = doc.get("host")
        if host:
            schemes = doc.get("schemes") or ["https"]
            url = f"{schemes[0]}://{host}{doc.get('basePath') or ''}"
    else:
        servers = doc.get("servers")
        if isinstance(servers, list) and servers and isinstance(servers[0], dict) and servers[0].get("url"):
            url = str(servers[0]["url"])
            for var, meta in (servers[0].get("variables") or {}).items():
                default = meta.get("default") if isinstance(meta, dict) else None
                if default is None and isinstance(meta, dict) and meta.get("enum"):
                    default = meta["enum"][0]
                url = url.replace("{" + var + "}", str(default if default is not None else ""))
    if url and not re.match(r"^https?://", url) and source_name and re.match(r"^https?://", source_name):
        url = urljoin(source_name, url)
    if not url or not re.match(r"^https?://", url):
        raise IngestError("no usable base_url: the document has no absolute servers[0].url (OpenAPI 3) or "
                          f"host/basePath (Swagger 2); found {url!r}")
    return url.rstrip("/")


def _auth(doc: dict, system: str) -> tuple[dict[str, Any], list[str]]:
    """Return (spec auth, per-operation review reasons). Env var NAMES only, never values."""
    schemes = (doc.get("components") or {}).get("securitySchemes") or doc.get("securityDefinitions") or {}
    prefix = system.upper()
    chosen: dict[str, Any] | None = None
    reasons: list[str] = []
    ordered = list(schemes.items())
    # honour the order of the document-level `security` requirement when there is one
    wanted = [k for req in doc.get("security") or [] if isinstance(req, dict) for k in req]
    ordered.sort(key=lambda kv: wanted.index(kv[0]) if kv[0] in wanted else len(wanted))
    for name, raw in ordered:
        s, _ = _follow(raw, doc)
        stype = str(s.get("type", "")).lower()
        auth: dict[str, Any] | None = None
        if stype == "apikey" and s.get("in") in ("header", "query") and s.get("name"):
            auth = {"kind": "api_key", "env": f"{prefix}_API_KEY", s["in"]: s["name"]}
        elif stype == "http" and str(s.get("scheme", "")).lower() == "bearer":
            auth = {"kind": "bearer", "env": f"{prefix}_TOKEN"}
        elif stype == "http" and str(s.get("scheme", "")).lower() == "basic":
            auth = {"kind": "basic", "user_env": f"{prefix}_USER", "password_env": f"{prefix}_PASSWORD"}
        elif stype == "basic":  # Swagger 2.0
            auth = {"kind": "basic", "user_env": f"{prefix}_USER", "password_env": f"{prefix}_PASSWORD"}
        if auth is None:
            reasons.append(f"auth scheme {name} is not supported (type {s.get('type', 'unknown')}"
                           f"{', ' + str(s.get('scheme')) if s.get('scheme') else ''}); calls may be rejected")
        elif chosen is None:
            chosen = auth
    return (chosen or {"kind": "none"}), reasons


# -- operations --------------------------------------------------------------------------------

def _scalar_param(f: dict) -> str | None:
    t = _type(f)
    if t is None and f.get("enum"):
        t = "string"
    return t if t in SCALARS else None


def _describe_kind(f: dict) -> str:
    t = _type(f)
    if t in ("array", "object"):
        return f"an {t}"
    if t is None:
        return "an untyped or composite (oneOf/anyOf) value"
    return f"of unsupported type {t}"


def _example_value(p: dict, f: dict) -> Any:
    for src in (p, f):
        if src.get("example") is not None:
            return src["example"]
        if src.get("x-example") is not None:
            return src["x-example"]
        ex = src.get("examples")
        if isinstance(ex, dict) and ex:
            first = next(iter(ex.values()))
            if isinstance(first, dict) and "value" in first:
                return first["value"]
    return None


def _arg_value(p: dict[str, Any], hint: Any) -> Any:
    if hint is not None:
        return hint
    c = p.get("constraints") or {}
    if p.get("default") is not None:
        return p["default"]
    if c.get("enum"):
        return c["enum"][0]
    if p["type"] == "integer":
        v: Any = 1
    elif p["type"] == "number":
        v = 1
    elif p["type"] == "boolean":
        v = True
    else:
        v = "example"
        if c.get("min_length") and len(v) < c["min_length"]:
            v = v + "x" * (c["min_length"] - len(v))
        if c.get("max_length") is not None and len(v) > c["max_length"]:
            v = v[:c["max_length"]]
        return v
    if "minimum" in c and v < c["minimum"]:
        v = c["minimum"]
    if "maximum" in c and v > c["maximum"]:
        v = c["maximum"]
    return v


def _json_media(content: Any) -> dict | None:
    if not isinstance(content, dict):
        return None
    for mime, media in content.items():
        if str(mime).lower().split(";")[0].strip() == "application/json" or str(mime).lower().endswith("+json"):
            return media if isinstance(media, dict) else {}
    return None


def _op_name(method: str, path: str, operation_id: str | None) -> str:
    if operation_id:
        return operation_id
    words = [method]
    for seg in path.strip("/").split("/"):
        m = re.fullmatch(r"\{(.+)\}", seg)
        words.append("by_" + snake(m.group(1)) if m else snake(seg))
    return "_".join(w for w in words if w)


def _collect_params(doc: dict, path_item: dict, op: dict, reasons: list[str]) -> list[dict[str, Any]]:
    """Path/query/header parameters -> spec params. Returns params with hidden `_hint` example values."""
    merged: dict[tuple[str, str], dict] = {}
    for raw in list(path_item.get("parameters") or []) + list(op.get("parameters") or []):
        p, _ = _follow(raw, doc)
        if p.get("name") and p.get("in"):
            merged[(p["in"], p["name"])] = p
    out: list[dict[str, Any]] = []
    for (where, name), p in merged.items():
        if where == "body":
            continue
        if where == "formData":
            reasons.append(f"form-data parameter '{name}' omitted: only JSON requests are supported")
            continue
        if where not in ("path", "query", "header"):
            reasons.append(f"parameter '{name}' ({where}) omitted: {where} parameters are not supported")
            continue
        f, _ = _flat(p.get("schema") if "schema" in p else p, doc)
        t = _scalar_param(f)
        if t is None:
            reasons.append(f"parameter '{name}' ({where}) is {_describe_kind(f)} and cannot be represented; "
                           "omitted from params")
            continue
        required = bool(p.get("required")) or where == "path"
        param = make_param(name, t, where, required, p.get("description") or f.get("description"),
                           constraints=_constraints(f) or None, default=f.get("default"))
        param["_hint"] = _example_value(p, f)
        out.append(param)
    return out


def _collect_body(doc: dict, op: dict, reasons: list[str]) -> list[dict[str, Any]]:
    schema: Any = None
    if "requestBody" in op:  # OpenAPI 3
        body, _ = _follow(op["requestBody"], doc)
        content = body.get("content") or {}
        media = _json_media(content)
        if media is None:
            if content:
                reasons.append("request body is not JSON (" + ", ".join(sorted(map(str, content))) +
                               "); body omitted from params")
            return []
        schema = media.get("schema")
    else:  # Swagger 2.0: `in: body` parameter
        bodies = [_follow(p, doc)[0] for p in op.get("parameters") or []]
        bodies = [p for p in bodies if p.get("in") == "body"]
        if not bodies:
            return []
        consumes = op.get("consumes") or doc.get("consumes")
        if consumes and not any("json" in str(c).lower() for c in consumes):
            reasons.append("request body is not JSON (" + ", ".join(map(str, consumes)) + "); body omitted from params")
            return []
        schema = bodies[0].get("schema")
    f, st = _flat(schema, doc)
    if _type(f) != "object" or not f.get("properties"):
        reasons.append(f"request body is {_describe_kind(f) if _type(f) != 'object' else 'an object without properties'}"
                       " and cannot be represented; body omitted from params")
        return []
    required = set(f.get("required") or [])
    out: list[dict[str, Any]] = []
    for pname, child in f["properties"].items():
        cf, _ = _flat(child, doc, st)
        t = _scalar_param(cf)
        if t is None:
            reasons.append(f"body property '{pname}' is {_describe_kind(cf)} and cannot be represented; "
                           "omitted from params")
            continue
        param = make_param(pname, t, "body", pname in required, cf.get("description"),
                           constraints=_constraints(cf) or None, default=cf.get("default"))
        param["_hint"] = _example_value({}, cf)
        out.append(param)
    return out


def _response(doc: dict, op: dict, reasons: list[str]) -> tuple[str, str, Any, int, bool, list[str]]:
    """-> (type, description, mock body, status, synthesized, expect_keys)."""
    responses = op.get("responses") or {}
    codes = sorted(str(c) for c in responses if re.fullmatch(r"2\d\d|2XX|2xx", str(c)))
    chosen: tuple[str, dict, dict | None] | None = None
    for code in codes:
        r, _ = _follow(responses.get(code, responses.get(int(code)) if code.isdigit() else None), doc)
        if "content" in r:
            media = _json_media(r["content"])
            if media is not None and media.get("schema") is not None:
                chosen = (code, r, media)
                break
        elif r.get("schema") is not None:  # Swagger 2.0
            chosen = (code, r, {"schema": r["schema"], "example": (r.get("examples") or {}).get("application/json"),
                                "examples": None})
            break
    if chosen is None:
        code = codes[0] if codes else "200"
        r, _ = _follow(responses.get(code, {}), doc) if codes else ({}, ())
        status = int(code) if code.isdigit() else 200
        return "object", _sentence(r.get("description")) or "The response body.", {}, status, True, []
    code, r, media = chosen
    status = int(code) if code.isdigit() else 200
    f, st = _flat(media["schema"], doc)
    t = _type(f)
    if t not in ("object", "array"):
        reasons.append(f"response body is {('a ' + t) if t else 'untyped or composite'}, not an object or array; "
                       "it is declared as an object and must be checked by a person")
        t = "object"
    item = f
    if t == "array":
        item, _ = _flat(f.get("items") or {}, doc, st)
    names = list((item.get("properties") or {}).keys())
    base = _sentence(r.get("description"), 160).rstrip(".")
    desc = (base + ". " if base else "") + ("List of items; each has: " if t == "array" and names else
                                             "List of items." if t == "array" else "Fields: " if names else "")
    desc += ", ".join(names[:15]) + ("." if names else "")
    # mock body: document example wins, else synthesized
    example = media.get("example")
    if example is None and isinstance(media.get("examples"), dict) and media["examples"]:
        first, _ = _follow(next(iter(media["examples"].values())), doc)
        example = first.get("value")
    synthesized = not (example is not None and isinstance(example, (list if t == "array" else dict)))
    mock = _synth(media["schema"], doc) if synthesized else example
    keys: list[str] = []
    if t == "object" and isinstance(mock, dict):
        keys = [k for k in item.get("required") or [] if k in mock] or [k for k in names if k in mock][:3]
    return t, desc.strip(), mock, status, synthesized, keys


def _operation(doc: dict, path: str, method: str, path_item: dict, op: dict, auth_reasons: list[str],
               declared_path_names: set[str]) -> dict[str, Any]:
    reasons: list[str] = list(auth_reasons)
    unsupported_before = len(reasons)
    params = _collect_params(doc, path_item, op, reasons)
    params += _collect_body(doc, op, reasons)
    # unique argument names
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for p in params:
        if p["name"] in seen:
            reasons.append(f"parameter '{p.get('wire_name', p['name'])}' ({p['in']}) collides with another "
                           f"parameter named {p['name']}; omitted from params")
            continue
        seen.add(p["name"])
        unique.append(p)
    params = unique
    # path placeholders must have a required path param, and only those may exist
    placeholders = re.findall(r"\{([^}]+)\}", path)
    ph_names = {snake(x) for x in placeholders}
    for p in list(params):
        if p["in"] == "path" and p["name"] not in ph_names:
            params.remove(p)
            reasons.append(f"path parameter '{p['name']}' is not a placeholder of {path}; omitted from params")
    for x in placeholders:
        if not any(p["in"] == "path" and p["name"] == snake(x) for p in params):
            if snake(x) in {p["name"] for p in params}:
                reasons.append(f"path placeholder '{x}' collides with another parameter name")
            params.insert(0, make_param(x, "string", "path", True, f"The {x} path segment."))
    unsupported_count = len(reasons) - unsupported_before
    rtype, rdesc, mock, status, synthesized, expect_keys = _response(doc, op, reasons)
    unsupported_count = len(reasons) - unsupported_before  # response problems count too

    confidence = BASE_CONFIDENCE - PENALTY_UNSUPPORTED * unsupported_count
    summary = _sentence(op.get("summary") or (op.get("description") or "").split(". ")[0], 200)
    description = _sentence(op.get("description"))
    if not summary:
        summary = f"{method.upper()} {path}"
    if description:
        when = description
    else:
        confidence -= PENALTY_NO_DESCRIPTION
        base = summary.rstrip(".")
        when = f"Call when you need to {base[0].lower() + base[1:]}." if base else f"Call when you need to {method} {path}."
    if synthesized:
        confidence -= PENALTY_SYNTHESIZED

    hints = {p["name"]: p.pop("_hint", None) for p in params}
    args = {p["name"]: _arg_value(p, hints[p["name"]]) for p in params if p.get("required") or hints[p["name"]] is not None}
    examples = [ok_example(args, mock, expect_keys, name="synthesized_from_schema" if synthesized else "example",
                           status=status)]
    for p in params:
        c = p.get("constraints") or {}
        bad: Any = None
        if p["type"] in ("integer", "number") and ("minimum" in c or "maximum" in c):
            bad = c["minimum"] - 1 if "minimum" in c else c["maximum"] + 1
        elif c.get("enum"):
            nums = [v for v in c["enum"] if isinstance(v, (int, float)) and not isinstance(v, bool)]
            bad = (max(nums) + 1) if nums and len(nums) == len(c["enum"]) else "not_a_valid_value"
        if bad is not None:
            examples.append(invalid_argument_example({**args, p["name"]: bad}))
            break
    return make_operation(
        name=_op_name(method, path, op.get("operationId")), summary=summary, when_to_call=when, method=method,
        path=normalize_path(path), params=params, response_type=rtype, response_description=rdesc,
        examples=examples, source="openapi", confidence=confidence, reasons=reasons)


def ingest(document: dict, *, source_name: str | None = None) -> dict:
    """Turn a parsed OpenAPI 3.x / Swagger 2.0 document into a draft spec (see module docstring)."""
    if not isinstance(document, dict) or not ("openapi" in document or "swagger" in document):
        raise IngestError("not an OpenAPI/Swagger document: missing `openapi` or `swagger` key")
    info = document.get("info") or {}
    title = info.get("title") or source_name or "api"
    system = system_name_from(title)
    base_url = _base_url(document, source_name)
    auth, auth_reasons = _auth(document, system)
    ops: list[dict[str, Any]] = []
    for path, raw_item in (document.get("paths") or {}).items():
        item, _ = _follow(raw_item, document)
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            if method == "head":
                warnings.warn(f"HEAD {path} skipped: the spec format has no HEAD request method")
                continue
            ops.append(_operation(document, path, method, item, op, auth_reasons, set()))
    if not ops:
        raise IngestError("the document has no get/put/post/delete/patch operations")
    description = _sentence(info.get("description") or info.get("summary") or "", 500)
    return make_spec(system_name=title, base_url=base_url, description=description or f"{title} API.",
                     operations=ops, auth=auth)
