"""Offline tests for ingest/trace.py (recorded HTTP traffic -> draft spec)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from generators.from_spec import check_semantics, validate_schema
from ingest.common import CONFIDENCE_THRESHOLD
from ingest.trace import IngestError, ingest, ingest_with_report, load

FIX = Path(__file__).parent / "fixtures"


def pair(method, url, status=200, body=None, req_headers=None, req_body=None, resp_headers=None):
    request = {"method": method, "url": url}
    if req_headers:
        request["headers"] = req_headers
    if req_body is not None:
        request["body"] = req_body
    response = {"status": status, "body": body if body is not None else {}}
    if resp_headers:
        response["headers"] = resp_headers
    return {"request": request, "response": response}


def ops(spec):
    return {o["name"]: o for o in spec["operations"]}


def params(op):
    return {p["name"]: p for p in op["params"]}


@pytest.fixture(scope="module")
def har_spec():
    return ingest(load(FIX / "catfact.har"))


@pytest.fixture(scope="module")
def pairs_spec():
    return ingest(load(FIX / "catfact.pairs.json"))


# -- catfact (real recorded traffic) -----------------------------------------------------------------------

def test_catfact_validates_and_is_generatable(har_spec, pairs_spec):
    for spec in (har_spec, pairs_spec):
        validate_schema(spec)
        check_semantics(spec)  # raises if any op needs_review, so all ops are reads with enough confidence
        assert all(o["side_effect"] == "read" and not o["needs_review"] for o in spec["operations"])
        assert spec["auth"] == {"kind": "none"}
        assert spec["system_name"] == "catfact"
        assert spec["base_url"] == "https://catfact.ninja"


def test_har_and_pairs_give_the_same_operations(har_spec, pairs_spec):
    def shape(spec):
        return [(o["name"], o["request"], [(p["name"], p["type"], p["in"], p["required"], p.get("default"))
                                           for p in o["params"]], o["confidence"]) for o in spec["operations"]]
    assert shape(har_spec) == shape(pairs_spec)


def test_catfact_names_paths_and_confidence(har_spec):
    got = {o["name"]: (o["request"]["method"], o["request"]["path"], o["confidence"]) for o in har_spec["operations"]}
    # /fact is singular -> get_fact; /facts and /breeds are plural collections -> list_*; /nope was only ever a 404
    assert got == {
        "get_fact": ("GET", "/fact", 0.9),
        "list_facts": ("GET", "/facts", 0.9),
        "list_breeds": ("GET", "/breeds", 0.78),
    }


def test_catfact_params_required_and_defaults(har_spec):
    o = ops(har_spec)
    # max_length only in 1 of 2 samples -> optional integer; one observation is not evidence for a default
    ml = params(o["get_fact"])["max_length"]
    assert (ml["type"], ml["in"], ml["required"]) == ("integer", "query", False)
    assert "default" not in ml
    # limit is missing from the bare /facts sample -> optional; values 2 and 3 disagree -> no default
    limit = params(o["list_facts"])["limit"]
    assert (limit["type"], limit["required"]) == ("integer", False) and "default" not in limit
    page = params(o["list_facts"])["page"]
    assert (page["type"], page["required"]) == ("integer", False)
    # /breeds has one sample, so everything seen in it is "present in every sample" -> required
    assert params(o["list_breeds"])["limit"]["required"] is True


def test_catfact_response_shape(har_spec):
    facts = ops(har_spec)["list_facts"]
    assert facts["response"]["type"] == "object"
    assert "data (array)" in facts["response"]["description"]
    ex = facts["examples"][0]
    assert ex["expect"] == "ok" and "data" in ex["expect_keys"] and "current_page" in ex["expect_keys"]
    assert len(ex["mock"]["body"]["data"]) <= 2
    assert ops(har_spec)["get_fact"]["examples"][0]["expect_keys"] == ["fact", "length"]


def test_catfact_orphan_404_is_reported_not_an_operation():
    spec, report = ingest_with_report(load(FIX / "catfact.har"))
    assert "nope" not in json.dumps(spec["operations"])
    assert any("/nope" in line and "no 2xx" in line for line in report)


# -- inference rules ---------------------------------------------------------------------------------------

def test_path_template_from_numeric_ids():
    spec = ingest([pair("GET", "https://api.example.com/posts/1", body={"id": 1, "title": "a"}),
                   pair("GET", "https://api.example.com/posts/2", body={"id": 2, "title": "b"})])
    assert spec["system_name"] == "example"
    op = ops(spec)["get_post"]
    assert op["request"]["path"] == "/posts/{post_id}"
    p = params(op)["post_id"]
    assert (p["in"], p["type"], p["required"], p["constraints"]) == ("path", "integer", True, {"minimum": 1})
    assert op["confidence"] == 0.9
    assert any(e["name"] == "post_id_zero" and e["args"]["post_id"] == 0 for e in op["examples"])
    check_semantics(spec)


def test_text_segments_that_differ_become_a_parameter_but_unrelated_paths_do_not():
    spec = ingest([pair("GET", "https://x.test/users/alice", body={"n": 1}),
                   pair("GET", "https://x.test/users/bob", body={"n": 2}),
                   pair("GET", "https://x.test/health", body={"ok": True}),
                   pair("GET", "https://x.test/status", body={"ok": True})])
    paths = sorted(o["request"]["path"] for o in spec["operations"])
    assert paths == ["/health", "/status", "/users/{user}"]


def test_optional_param_default_only_when_samples_agree():
    base = "https://api.example.com/items"
    spec = ingest([pair("GET", base + "?size=10&sort=a", body={"a": 1}), pair("GET", base + "?size=10&sort=b", body={"a": 1}),
                   pair("GET", base, body={"a": 1})])
    p = params(ops(spec)["list_items"])
    assert p["size"]["required"] is False and p["size"]["default"] == 10
    assert p["sort"]["required"] is False and "default" not in p["sort"]


def test_404_becomes_failing_example_with_runtime_mapping():
    spec = ingest([pair("GET", "https://a.test/orders/1", body={"id": 1}),
                   pair("GET", "https://a.test/orders/999", status=404, body={"error": "nope"}),
                   pair("GET", "https://a.test/orders/5", status=429, body={})])
    op = ops(spec)["get_order"]
    by = {e["name"]: e for e in op["examples"]}
    assert by["not_found"]["mock"] == {"status": 404, "body": {"error": "nope"}}
    assert by["not_found"]["args"] == {"order_id": 999}
    assert by["not_found"]["expect"] == {"code": "NOT_FOUND", "retryable": False, "recovery": "agent"}
    assert by["rate_limited"]["expect"] == {"code": "RATE_LIMITED", "retryable": True, "recovery": "agent"}
    validate_schema(spec)


def test_post_needs_review_and_body_params():
    spec = ingest([pair("POST", "https://a.test/posts", status=201, req_body={"title": "t", "tags": ["x"], "n": 3},
                        body={"id": 101}),
                   pair("POST", "https://a.test/posts", status=201, req_body='{"title": "u"}', body={"id": 102})])
    op = ops(spec)["create_post"]
    assert op["needs_review"] is True and op["side_effect"] == "write"
    p = params(op)
    assert p["title"]["in"] == "body" and p["title"]["required"] is True
    assert p["n"]["required"] is False
    assert "tags" not in p and any("tags" in r for r in op["review_reasons"])
    validate_schema(spec)


def test_options_and_non_json_are_ignored_and_reported():
    spec, report = ingest_with_report([
        pair("OPTIONS", "https://a.test/x"), pair("GET", "https://a.test/page", body="<html>hi</html>"),
        pair("GET", "https://a.test/x", body={"a": 1})])
    assert [o["request"]["path"] for o in spec["operations"]] == ["/x"]
    assert any("OPTIONS" in r for r in report) and any("not JSON" in r for r in report)


def test_mixed_origins_raise_and_base_url_selects():
    trace = [pair("GET", "https://a.test/x", body={"a": 1}), pair("GET", "https://b.test/y", body={"b": 1})]
    with pytest.raises(IngestError) as exc:
        ingest(trace)
    assert "https://a.test" in str(exc.value) and "https://b.test" in str(exc.value)
    spec = ingest(trace, base_url="https://b.test")
    assert [o["request"]["path"] for o in spec["operations"]] == ["/y"]
    assert spec["base_url"] == "https://b.test"


def test_differing_key_sets_lower_confidence_and_flag():
    spec = ingest([pair("GET", "https://a.test/thing", body={"a": 1, "b": 2}),
                   pair("GET", "https://a.test/thing", body={"a": 1})])
    op = ops(spec)["get_thing"]
    assert op["confidence"] == 0.7 < CONFIDENCE_THRESHOLD
    assert op["needs_review"] is True
    assert any("key sets" in r for r in op["review_reasons"]) and any("below the threshold" in r for r in op["review_reasons"])


def test_truncation_of_mock_bodies():
    body = {"items": [{"s": "x" * 500}] * 5, "deep": {"a": {"b": {"c": {"d": 1}}}}}
    spec = ingest([pair("GET", "https://a.test/big", body=body)])
    mock = ops(spec)["get_big"]["examples"][0]["mock"]["body"]
    assert len(mock["items"]) == 2 and len(mock["items"][0]["s"]) == 200
    assert mock["deep"]["a"]["b"] == {}


def test_missing_input_errors():
    with pytest.raises(IngestError):
        ingest({"nope": 1})
    with pytest.raises(IngestError):
        ingest([pair("GET", "https://a.test/x", body="not json")])


# -- secrets -----------------------------------------------------------------------------------------------

def _secret_trace():
    h = {"Authorization": "Bearer SECRET-TOKEN-123", "Cookie": "sid=SECRET-COOKIE-789"}
    return [
        pair("GET", "https://api.shop.test/items/1?api_key=SECRET-QUERY-000", req_headers=h,
             body={"id": 1, "name": "n", "access_token": "SECRET-BODY-456", "note": "echo SECRET-TOKEN-123"},
             resp_headers={"Set-Cookie": "sid=SECRET-COOKIE-789; Path=/"}),
        pair("GET", "https://api.shop.test/items/2", req_headers=h, body={"id": 2, "name": "m", "access_token": "SECRET-BODY-456"}),
        pair("POST", "https://api.shop.test/items", status=201, req_headers=h,
             req_body={"name": "z", "password": "SECRET-PW-111"}, body={"id": 3}),
    ]


def test_secrets_never_reach_the_spec():
    spec = ingest(_secret_trace())
    text = json.dumps(spec)
    for secret in ("SECRET-TOKEN-123", "SECRET-COOKIE-789", "SECRET-BODY-456", "SECRET-QUERY-000", "SECRET-PW-111"):
        assert secret not in text, secret
    assert spec["auth"] == {"kind": "bearer", "env": "SHOP_TOKEN"}
    assert "<redacted>" in text
    assert all("auth inferred from traffic" in o["review_reasons"] for o in spec["operations"])
    assert all(o["needs_review"] for o in spec["operations"])
    assert not any(p["name"] in ("password", "api_key") for o in spec["operations"] for p in o["params"])
    validate_schema(spec)


def test_basic_and_api_key_detection():
    basic = ingest([pair("GET", "https://a.test/x", body={"a": 1}, req_headers={"authorization": "Basic dTpw"})])
    assert basic["auth"] == {"kind": "basic", "user_env": "A_USERNAME", "password_env": "A_PASSWORD"} or \
        basic["auth"]["kind"] == "basic"
    assert "dTpw" not in json.dumps(basic)
    hdr = ingest([pair("GET", "https://a.test/x", body={"a": 1}, req_headers={"X-API-Key": "k-12345"})])
    assert hdr["auth"]["kind"] == "api_key" and hdr["auth"]["header"] == "X-API-Key" and hdr["auth"]["env"].endswith("_API_KEY")
    assert "k-12345" not in json.dumps(hdr)
    qry = ingest([pair("GET", "https://a.test/x?apikey=k-98765", body={"a": 1})])
    assert qry["auth"]["kind"] == "api_key" and qry["auth"]["query"] == "apikey"
    assert "k-98765" not in json.dumps(qry)
