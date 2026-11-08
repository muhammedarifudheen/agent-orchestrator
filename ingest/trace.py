"""Recorded HTTP traffic (HAR or request/response pairs) -> draft system spec. Deterministic: no LLM, no network.

    spec = ingest(load("capture.har"))                 # or a list of {"request": .., "response": ..} pairs
    spec, report = ingest_with_report(data)            # report = what was ignored/skipped and why

What a trace can show: endpoints that were exercised, the parameter names/types that were sent, the top-level
shape of what came back, and the auth scheme. It cannot show: constraints, optional parameters that were never
sent, errors that never happened, nested schemas, purpose, or safety. Summaries are therefore mechanical and
every write comes out ``needs_review`` (see ingest/common.py).

SECRETS: values of Authorization/Cookie/Set-Cookie/api-key headers, secret-looking query names and body keys are
never copied. Secret-looking keys in mock bodies become "<redacted>", and every secret value seen in the trace is
scrubbed from every string in the finished spec as a last line of defence.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ingest.common import (infer_json_type, invalid_argument_example, make_operation, make_param, make_spec,
                           normalize_path, ok_example, snake, system_name_from)

REDACTED = "<redacted>"
AUTH_REASON = "auth inferred from traffic"
MAX_ARRAY, MAX_STRING, MAX_DEPTH = 2, 200, 3

# status -> (code, retryable, recovery): the mapping servers/_base.py:standard_http_error applies at runtime.
STATUS_EXPECT: dict[int, tuple[str, str, bool, str]] = {
    400: ("invalid_request", "INVALID_REQUEST", False, "agent"),
    401: ("auth_failed", "AUTH_FAILED", False, "human"),
    403: ("forbidden", "FORBIDDEN", False, "human"),
    404: ("not_found", "NOT_FOUND", False, "agent"),
    408: ("timeout", "TIMEOUT", True, "agent"),
    409: ("conflict", "CONFLICT", False, "human"),
    422: ("invalid_request_422", "INVALID_REQUEST", False, "agent"),
    429: ("rate_limited", "RATE_LIMITED", True, "agent"),
}
_SECRET_PARTS = ("authorization", "cookie", "apikey", "token", "password", "passwd", "secret")
_API_KEY_HEADERS = {"x-api-key", "api-key", "apikey", "x-apikey"}
_API_KEY_QUERY = {"api_key", "apikey", "token", "access_token", "key"}
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX = re.compile(r"^(?=.*\d)[0-9a-fA-F]{16,}$")
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE", "GET", "HEAD"}


class IngestError(ValueError):
    """The trace cannot be turned into a spec (mixed origins, nothing usable, malformed input)."""


# -- small helpers -----------------------------------------------------------------------------------------

def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _is_secret_key(key: str) -> bool:
    k = _norm_key(key)
    return any(part in k for part in _SECRET_PARTS)


def _singular(word: str) -> str:
    word = snake(word)
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def _plural_like(word: str) -> bool:
    return word.endswith("s") and not word.endswith("ss")


def _coerce(text: Any) -> Any:
    """Numeric strings count as numbers (query/path values arrive as text)."""
    if isinstance(text, str):
        if re.fullmatch(r"-?\d+", text) and len(text) < 16:
            return int(text)
        if re.fullmatch(r"-?\d+\.\d+", text):
            return float(text)
    return text


def _clean(value: Any, depth: int = 0) -> Any:
    """Redact secret-looking keys, truncate arrays (2), strings (200) and depth (3)."""
    if isinstance(value, dict):
        if depth >= MAX_DEPTH:
            return {}
        return {k: (REDACTED if _is_secret_key(k) else _clean(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        if depth >= MAX_DEPTH:
            return []
        return [_clean(v, depth + 1) for v in value[:MAX_ARRAY]]
    if isinstance(value, str):
        return value[:MAX_STRING]
    return value


def _jtype(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return "array" if isinstance(value, list) else "object"


def _merge_type(types: list[str]) -> str:
    kinds = set(types)
    if len(kinds) == 1:
        return kinds.pop()
    if kinds <= {"integer", "number"}:
        return "number"
    return "string"


# -- loading and normalising -------------------------------------------------------------------------------

def load(path: str | Path) -> Any:
    """Read a .har / .json file."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise IngestError(f"cannot read trace {path}: {exc}") from exc


class _Sample:
    def __init__(self, method: str, url: str, req_headers: dict[str, tuple[str, str]], req_body: Any,
                 status: int, resp_body: Any, resp_json: bool, resp_headers: dict[str, tuple[str, str]]):
        self.method = method.upper()
        parts = urlsplit(url)
        self.origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        self.path = parts.path or "/"
        self.query = parse_qsl(parts.query, keep_blank_values=True)
        self.req_headers, self.req_body = req_headers, req_body
        self.status, self.resp_body, self.resp_json, self.resp_headers = status, resp_body, resp_json, resp_headers


def _headers(raw: Any) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    items = raw.items() if isinstance(raw, dict) else [(h.get("name", ""), h.get("value", "")) for h in (raw or [])
                                                        if isinstance(h, dict)]
    for name, value in items:
        out.setdefault(str(name).lower(), (str(name), str(value)))
    return out


def _parse_json(value: Any) -> tuple[Any, bool]:
    """(parsed, is_json). A str is parsed as JSON text; any other value is already a JSON value."""
    if isinstance(value, str):
        if not value.strip():
            return None, False
        try:
            return json.loads(value), True
        except ValueError:
            return value, False
    if value is None:
        return None, False
    return value, True


def _from_entry(entry: dict[str, Any]) -> _Sample:
    req, resp = entry["request"], entry["response"]
    post = req.get("postData") or {}
    body: Any = None
    if post.get("text"):
        body, ok = _parse_json(post["text"])
        if not ok:
            body = "<non-json>"
    elif post.get("params"):
        body = "<non-json>"
    content = resp.get("content") or {}
    text = content.get("text")
    if text and content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            text = None
    rbody, rjson = _parse_json(text)
    return _Sample(req["method"], req["url"], _headers(req.get("headers")), body, int(resp.get("status", 0)),
                   rbody, rjson, _headers(resp.get("headers")))


def _from_pair(pair: dict[str, Any]) -> _Sample:
    req, resp = pair["request"], pair["response"]
    body = req.get("body")
    if isinstance(body, str):
        body, ok = _parse_json(body)
        body = body if ok else ("<non-json>" if body else None)
    rbody, rjson = _parse_json(resp.get("body"))
    return _Sample(req["method"], req["url"], _headers(req.get("headers")), body, int(resp.get("status", 0)),
                   rbody, rjson, _headers(resp.get("headers")))


def _samples(data: Any) -> list[_Sample]:
    try:
        if isinstance(data, dict) and isinstance(data.get("log"), dict):
            return [_from_entry(e) for e in data["log"].get("entries", [])]
        if isinstance(data, list):
            return [_from_pair(p) for p in data]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise IngestError(f"malformed trace entry: {exc!r}") from exc
    raise IngestError("trace must be a HAR document ({'log': {'entries': [...]}}) or a list of "
                      "{'request': .., 'response': ..} pairs")


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _system_from_host(netloc: str) -> str:
    host = netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    if re.fullmatch(r"[\d.]+", host) or host == "localhost":
        return system_name_from(host)
    parts = [p for p in host.split(".") if p]
    if parts and parts[0] == "www":
        parts = parts[1:]
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"} and len(parts[-1]) == 2:
        return system_name_from(parts[-3])
    return system_name_from(parts[-2] if len(parts) >= 2 else (parts[0] if parts else "api"))


# -- path templates ----------------------------------------------------------------------------------------

def _id_like(seg: str) -> bool:
    return bool(re.fullmatch(r"\d+", seg) or _UUID.match(seg) or _HEX.match(seg))


def _tokens(path: str) -> list[tuple[str, str]]:
    """('s', text) static or ('p', name) parameter; id-like segments are named from the previous static one."""
    toks: list[tuple[str, str]] = []
    used: set[str] = set()
    for seg in [s for s in path.split("/") if s]:
        if _id_like(seg):
            prev = next((t[1] for t in reversed(toks) if t[0] == "s"), None)
            toks.append(("p", _unique((_singular(prev) + "_id") if prev else "id", used)))
        else:
            toks.append(("s", seg))
    return toks


def _unique(name: str, used: set[str]) -> str:
    base, n = name, 1
    while name in used:
        n += 1
        name = f"{base}_{n}"
    used.add(name)
    return name


def _generalise(templates: dict[str, list[tuple[str, str]]], methods: dict[str, set[str]]) -> None:
    """Positions whose static values differ across same-method same-length paths that agree elsewhere become
    parameters. Mutates ``templates`` (raw path -> tokens). Needs >= 1 agreeing static segment."""
    changed = True
    while changed:
        changed = False
        groups: dict[tuple[Any, ...], list[str]] = {}
        for raw, toks in templates.items():
            for meth in methods[raw]:
                for i in range(len(toks)):
                    if toks[i][0] != "s":
                        continue
                    key = (meth, len(toks), i, tuple(t if j != i else None for j, t in enumerate(toks)))
                    groups.setdefault(key, []).append(raw)
        for key, raws in sorted(groups.items(), key=lambda kv: str(kv[0])):
            i, masked = key[2], key[3]
            if not any(t is not None and t[0] == "s" for t in masked):
                continue
            if len({templates[r][i][1] for r in raws}) < 2:
                continue
            used = {t[1] for t in masked if t is not None and t[0] == "p"}
            prev = next((t[1] for t in reversed(masked[:i]) if t is not None and t[0] == "s"), None)
            name = _unique(_singular(prev) if prev else "item", used)
            for r in set(raws):
                templates[r][i] = ("p", name)
            changed = True
            break


def _render(toks: list[tuple[str, str]]) -> str:
    return "/" + "/".join(t[1] if t[0] == "s" else "{" + t[1] + "}" for t in toks)


# -- the ingestor ------------------------------------------------------------------------------------------

def ingest(data: Any, *, system_name: str | None = None, base_url: str | None = None) -> dict[str, Any]:
    return ingest_with_report(data, system_name=system_name, base_url=base_url)[0]


def ingest_with_report(data: Any, *, system_name: str | None = None,
                       base_url: str | None = None) -> tuple[dict[str, Any], list[str]]:
    report: list[str] = []
    samples = _samples(data)
    if not samples:
        raise IngestError("trace contains no entries")

    prefix = ""
    if base_url:
        origin = _origin_of(base_url)
        prefix = urlsplit(base_url).path.rstrip("/")
        kept = [s for s in samples if s.origin == origin]
        if len(kept) != len(samples):
            report.append(f"{len(samples) - len(kept)} entries ignored: origin differs from base_url {origin}")
        samples = kept
        if not samples:
            raise IngestError(f"no entries match base_url origin {origin}")
    else:
        origins = sorted({s.origin for s in samples})
        if len(origins) > 1:
            raise IngestError("trace mixes several origins; pass base_url to pick one: " + ", ".join(origins))
        origin = origins[0]
    base = origin + prefix
    if prefix:
        for s in samples:
            if s.path == prefix or s.path.startswith(prefix + "/"):
                s.path = s.path[len(prefix):] or "/"

    usable: list[_Sample] = []
    skipped = {"options": 0, "nonjson": 0, "scalar": 0, "method": 0}
    for s in samples:
        if s.method == "OPTIONS":
            skipped["options"] += 1
        elif s.method not in _WRITE_METHODS:
            skipped["method"] += 1
        elif not s.resp_json:
            skipped["nonjson"] += 1
        elif not isinstance(s.resp_body, (dict, list)):
            skipped["scalar"] += 1
        else:
            usable.append(s)
    for key, why in (("options", "OPTIONS request(s) ignored (CORS preflight, not an API operation)"),
                     ("nonjson", "entr(y/ies) ignored: response is empty or not JSON"),
                     ("scalar", "entr(y/ies) ignored: JSON response is a scalar, not an object or array"),
                     ("method", "entr(y/ies) ignored: HTTP method not supported by the spec schema")):
        if skipped[key]:
            report.append(f"{skipped[key]} {why}")
    if not usable:
        raise IngestError("no usable JSON request/response entries in the trace; " + "; ".join(report))

    # templates
    templates = {s.path: _tokens(s.path) for s in usable}
    methods: dict[str, set[str]] = {}
    for s in usable:
        methods.setdefault(s.path, set()).add(s.method)
    _generalise(templates, methods)

    groups: dict[tuple[str, str], list[tuple[_Sample, list[tuple[str, str]]]]] = {}
    for s in usable:
        toks = templates[s.path]
        groups.setdefault((s.method, _render(toks)), []).append((s, toks))

    secrets = _collect_secrets(usable)
    auth, auth_reasons = _detect_auth(usable, system_name or _system_from_host(urlsplit(origin).netloc))
    sysname = system_name_from(system_name or _system_from_host(urlsplit(origin).netloc))
    if auth["kind"] != "none" or auth_reasons:
        auth = _finalise_auth(auth, sysname)

    operations = []
    for (method, template), members in groups.items():
        op = _build_operation(method, template, members, bool(auth_reasons))
        if op is None:
            report.append(f"{method} {template} skipped: no 2xx JSON response was observed "
                          f"(statuses seen: {sorted({m[0].status for m in members})}); nothing to mock")
            continue
        for r in auth_reasons:
            op["review_reasons"].append(r)
        op["needs_review"] = bool(op["review_reasons"])
        operations.append(op)
    if not operations:
        raise IngestError("no operation with a successful (2xx) JSON response in the trace; " + "; ".join(report))

    spec = make_spec(system_name=sysname, base_url=base,
                     description=f"{sysname} API inferred from {len(usable)} recorded HTTP request(s); "
                                 "draft, no documentation was available.",
                     operations=operations, auth=auth)
    spec = _scrub(spec, secrets)
    return spec, report


# -- secrets -----------------------------------------------------------------------------------------------

def _collect_secrets(samples: list[_Sample]) -> set[str]:
    found: set[str] = set()

    def add(v: Any) -> None:
        if isinstance(v, str) and len(v) >= 4:
            found.add(v)
            parts = v.split(None, 1)
            if len(parts) == 2 and len(parts[1]) >= 4:
                found.add(parts[1])

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if _is_secret_key(k) and not isinstance(x, (dict, list)):
                    add(x)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for s in samples:
        for hdrs in (s.req_headers, s.resp_headers):
            for key, (_, value) in hdrs.items():
                if _is_secret_key(key) or key in _API_KEY_HEADERS:
                    add(value)
                    if key in ("cookie", "set-cookie"):
                        for piece in re.split(r"[;,]", value):
                            if "=" in piece:
                                add(piece.split("=", 1)[1].strip())
        for k, v in s.query:
            if _is_secret_key(k) or k.lower() in _API_KEY_QUERY:
                add(v)
        walk(s.req_body)
        walk(s.resp_body)
    return found


def _scrub(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, REDACTED)
    return value


def _detect_auth(samples: list[_Sample], system: str) -> tuple[dict[str, Any], list[str]]:
    """({'kind': ...} with placeholder names, reasons). Never reads a credential VALUE beyond its scheme word."""
    schemes: dict[str, Any] = {}
    cookie = False
    for s in samples:
        h = s.req_headers
        if "authorization" in h:
            word = h["authorization"][1].split(None, 1)[0].lower() if h["authorization"][1].strip() else ""
            schemes.setdefault("bearer" if word == "bearer" else "basic" if word == "basic" else "api_key_auth",
                               {"header": "Authorization"})
        for key in _API_KEY_HEADERS:
            if key in h:
                schemes.setdefault("api_key_header", {"header": h[key][0]})
        for k, _ in s.query:
            if k.lower() in _API_KEY_QUERY:
                schemes.setdefault("api_key_query", {"query": k})
        if "cookie" in h or "set-cookie" in s.resp_headers:
            cookie = True
    reasons: list[str] = []
    order = ["bearer", "basic", "api_key_header", "api_key_auth", "api_key_query"]
    chosen = next((k for k in order if k in schemes), None)
    auth: dict[str, Any] = {"kind": "none"}
    if chosen == "bearer":
        auth = {"kind": "bearer"}
    elif chosen == "basic":
        auth = {"kind": "basic"}
    elif chosen == "api_key_query":
        auth = {"kind": "api_key", "query": schemes[chosen]["query"]}
    elif chosen:
        auth = {"kind": "api_key", "header": schemes[chosen]["header"]}
    if chosen:
        reasons.append(AUTH_REASON)
        if len(schemes) > 1:
            reasons.append(f"several auth schemes observed ({', '.join(sorted(schemes))}); only {chosen} is modelled")
    if cookie:
        reasons.append("cookie credentials observed; cookie/session auth is not modelled by the spec schema")
    return auth, reasons


def _finalise_auth(auth: dict[str, Any], sysname: str) -> dict[str, Any]:
    up = sysname.upper()
    if auth["kind"] == "bearer":
        auth["env"] = f"{up}_TOKEN"
    elif auth["kind"] == "basic":
        auth.update(user_env=f"{up}_USERNAME", password_env=f"{up}_PASSWORD")
    elif auth["kind"] == "api_key":
        auth["env"] = f"{up}_API_KEY"
    return auth


# -- operations --------------------------------------------------------------------------------------------

def _op_name(method: str, template: str, response_type: str) -> str:
    statics = [seg for seg in template.split("/") if seg and not seg.startswith("{")]
    segs = [seg for seg in template.split("/") if seg]
    if not statics:
        return "get_root" if method == "GET" else f"{method.lower()}_root"
    last = statics[-1]
    ends_with_param = segs[-1].startswith("{")
    noun = _singular(last)
    if method == "GET":
        verb = "get" if (ends_with_param or not _plural_like(snake(last))) else "list"
        name = f"{verb}_{noun if verb == 'get' else snake(last)}"
        if verb == "list" and len(segs) >= 3 and segs[-2].startswith("{") and len(statics) >= 2:
            name += "_for_" + _singular(statics[-2])
        return name
    verb = {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}.get(method, method.lower())
    return f"{verb}_{noun}"


def _build_operation(method: str, template: str, members: list[tuple[_Sample, list[tuple[str, str]]]],
                     has_auth: bool) -> dict[str, Any] | None:
    samples = [m[0] for m in members]
    ok = [s for s in samples if 200 <= s.status < 300]
    if not ok:
        return None
    reasons: list[str] = []
    n = len(samples)

    def args_of(sample: _Sample, toks: list[tuple[str, str]]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        segs = [x for x in sample.path.split("/") if x]
        for seg, tok in zip(segs, toks):
            if tok[0] == "p":
                args[tok[1]] = _coerce(seg)
        return args

    # path params
    params: list[dict[str, Any]] = []
    path_names = [t[1] for t in members[0][1] if t[0] == "p"]
    path_vals: dict[str, list[Any]] = {nm: [] for nm in path_names}
    for s, toks in members:
        for k, v in args_of(s, toks).items():
            path_vals[k].append(v)
    for nm in path_names:
        vals = path_vals[nm]
        typ = _merge_type([infer_json_type(v) for v in vals])
        idlike = typ == "integer" and all(isinstance(v, int) and v >= 1 for v in vals)
        seen = ", ".join(dict.fromkeys(str(v) for v in vals[:3]))
        params.append(make_param(nm, typ, "path", True, f"The {nm.replace('_', ' ')} taken from the path "
                                 f"(observed: {seen}).", constraints={"minimum": 1} if idlike else None))

    # query params
    qvals: dict[str, list[Any]] = {}
    qcount: dict[str, int] = {}
    for s in samples:
        first: dict[str, Any] = {}
        for k, v in s.query:
            if _is_secret_key(k) or k.lower() in _API_KEY_QUERY:
                continue
            first.setdefault(k, _coerce(v))
        for k, v in first.items():
            qvals.setdefault(k, []).append(v)
            qcount[k] = qcount.get(k, 0) + 1
    for k, vals in qvals.items():
        required = qcount[k] == n
        typ = _merge_type([infer_json_type(v) for v in vals])
        default = vals[0] if (not required and len(vals) >= 2 and all(v == vals[0] for v in vals)) else None
        params.append(make_param(k, typ, "query", required, f"Query parameter '{k}' observed in recorded traffic "
                                 f"(type inferred from {len(vals)} sample(s)).", default=default))

    # body params
    bvals: dict[str, list[Any]] = {}
    nonscalar: set[str] = set()
    secret_fields: set[str] = set()
    body_samples = 0
    for s in samples:
        if s.req_body is None:
            continue
        if isinstance(s.req_body, dict):
            body_samples += 1
            for k, v in s.req_body.items():
                if _is_secret_key(k):
                    secret_fields.add(k)
                elif isinstance(v, (str, int, float, bool)):
                    bvals.setdefault(k, []).append(v)
                else:
                    nonscalar.add(k)
        else:
            reasons.append("request body is not a JSON object (array, text or form data); not modelled")
    for k, vals in bvals.items():
        if k in nonscalar:
            continue
        required = len(vals) == n
        typ = _merge_type([infer_json_type(v) for v in vals])
        default = vals[0] if (not required and len(vals) >= 2 and all(v == vals[0] for v in vals)) else None
        params.append(make_param(k, typ, "body", required, f"Body field '{k}' observed in recorded traffic.",
                                 default=default))
    if nonscalar:
        reasons.append("request body properties omitted (non-scalar, not modelled): " + ", ".join(sorted(nonscalar)))
    if secret_fields:
        reasons.append("secret-looking request body fields omitted (values never copied): "
                       + ", ".join(sorted(secret_fields)))

    # de-duplicate names across locations (path wins)
    names: set[str] = set()
    uniq = []
    for p in params:
        if p["name"] in names:
            reasons.append(f"parameter name '{p['name']}' appears in several locations; later one dropped")
            continue
        names.add(p["name"])
        uniq.append(p)
    params = uniq

    # responses
    first_ok = ok[0]
    resp = first_ok.resp_body
    rtype = "array" if isinstance(resp, list) else "object"
    keysets: list[frozenset[str]] = []
    for s in ok:
        b = s.resp_body
        if isinstance(b, dict):
            keysets.append(frozenset(b))
        elif isinstance(b, list):
            first_item = next((i for i in b if isinstance(i, dict)), None)
            if first_item is not None:
                keysets.append(frozenset(first_item))
    consistent = len(set(keysets)) <= 1
    if len(ok) == 1:
        confidence = 0.78
    elif consistent:
        confidence = 0.9
    else:
        confidence = 0.7
        reasons.append("2xx responses have differing top-level key sets: " +
                       " | ".join(sorted(",".join(sorted(k)) or "<empty>" for k in set(keysets))))
    if len({"array" if isinstance(s.resp_body, list) else "object" for s in ok}) > 1:
        reasons.append("2xx responses mix object and array bodies")
        confidence = min(confidence, 0.7)

    if isinstance(resp, dict):
        desc = "Object with top-level keys: " + ", ".join(f"{k} ({_jtype(v)})" for k, v in resp.items()) + "."
        expect_keys = [k for k in resp if all(isinstance(s.resp_body, dict) and k in s.resp_body for s in ok)]
        expect_keys = [k for k in expect_keys if not _is_secret_key(k)]
    else:
        item = next((i for i in resp if isinstance(i, dict)), None)
        desc = (f"Array of {_jtype(resp[0]) if resp else 'items'} items"
                + (", each with keys: " + ", ".join(f"{k} ({_jtype(v)})" for k, v in item.items()) if item else "")
                + ".")
        expect_keys = []

    ok_args = args_of(first_ok, members[[m[0] for m in members].index(first_ok)][1])
    for p in params:
        if p["in"] == "query":
            raw = dict(first_ok.query).get(p.get("wire_name", p["name"]))
            if raw is not None:
                ok_args[p["name"]] = _coerce(raw)
        elif p["in"] == "body" and isinstance(first_ok.req_body, dict):
            wire = p.get("wire_name", p["name"])
            if wire in first_ok.req_body:
                ok_args[p["name"]] = first_ok.req_body[wire]
    # an ok sample that lacks a required param cannot happen (required = in every sample), but be safe
    ok_args = {k: v for k, v in ok_args.items() if k in names}

    examples = [ok_example(ok_args, _clean(resp), expect_keys, name="observed_response")]
    # failing examples
    for status, (ename, code, retry, recovery) in STATUS_EXPECT.items():
        bad = next((m for m in members if m[0].status == status), None)
        if bad is None:
            continue
        s, toks = bad
        eargs = args_of(s, toks)
        for p in params:
            if p["in"] == "query":
                raw = dict(s.query).get(p.get("wire_name", p["name"]))
                if raw is not None:
                    eargs[p["name"]] = _coerce(raw)
            elif p["in"] == "body" and isinstance(s.req_body, dict):
                wire = p.get("wire_name", p["name"])
                if wire in s.req_body:
                    eargs[p["name"]] = s.req_body[wire]
        if any(p["required"] and p["name"] not in eargs for p in params):
            continue
        body = _clean(s.resp_body) if isinstance(s.resp_body, (dict, list)) else {}
        examples.append({"name": ename, "args": {k: v for k, v in eargs.items() if k in names},
                         "mock": {"status": status, "body": body},
                         "expect": {"code": code, "retryable": retry, "recovery": recovery}})

    for p in params:
        if p["in"] == "path" and p["type"] == "integer" and p.get("constraints", {}).get("minimum") == 1 \
                and p["name"] in ok_args:
            bad_args = dict(ok_args)
            bad_args[p["name"]] = 0
            examples.append(invalid_argument_example(bad_args, name=f"{p['name']}_zero"))
            break

    path = normalize_path(template)
    summary = (f"Fetch {path}." if method == "GET" else f"Send {method} to {path}.") + \
        " Inferred from recorded HTTP traffic; no documentation was available."
    when = (f"Call when you need what {method} {path} returns; purpose inferred from {n} recorded request(s), "
            "no documentation was available.")
    return make_operation(name=_op_name(method, template, rtype), summary=summary, when_to_call=when, method=method,
                          path=path, params=params, response_type=rtype, response_description=desc,
                          examples=examples, source="har", confidence=confidence, reasons=reasons)
