"""Offline tests for ingest/openapi.py (fixtures under tests/ingest/fixtures)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from generators.from_spec import SpecError, check_semantics, validate_schema
from ingest.openapi import IngestError, ingest, load

FIX = Path(__file__).parent / "fixtures"
PETSTORE_URL = "https://petstore3.swagger.io/api/v3/openapi.json"


@pytest.fixture(scope="module")
def zip_spec():
    return ingest(load(FIX / "zippopotam.openapi.json"))


@pytest.fixture(scope="module")
def pet_spec():
    return ingest(load(FIX / "petstore.openapi.json"), source_name=PETSTORE_URL)


def ops(spec):
    return {o["name"]: o for o in spec["operations"]}


# -- zippopotam: clean, read-only, generatable ---------------------------------------------------

def test_zippopotam_valid_and_generatable(zip_spec):
    validate_schema(zip_spec)
    check_semantics(zip_spec)
    assert zip_spec["base_url"] == "https://api.zippopotam.us"
    assert zip_spec["auth"] == {"kind": "none"}
    assert zip_spec["system_name"] == "zippopotamus"


def test_zippopotam_operations(zip_spec):
    assert len(zip_spec["operations"]) == 2
    for op in zip_spec["operations"]:
        assert op["side_effect"] == "read" and op["idempotent"] is True
        assert op["needs_review"] is False and op["review_reasons"] == []
        assert op["confidence"] >= 0.75
        assert op["request"]["method"] == "GET"
        placeholders = set(re.findall(r"\{([^}]+)\}", op["request"]["path"]))
        assert placeholders == {p["name"] for p in op["params"] if p["in"] == "path"}
        assert all(re.fullmatch(r"[a-z][a-z0-9_]*", x) for x in placeholders)
        assert all("wire_name" not in p for p in op["params"] if p["in"] == "path")
        ok = [e for e in op["examples"] if e["expect"] == "ok"]
        assert ok and ok[0]["expect_keys"] and ok[0]["name"] == "example"
        assert all(k in ok[0]["mock"]["body"] for k in ok[0]["expect_keys"])
    by_name = ops(zip_spec)
    first = by_name["get_places_by_postal_code"]
    assert first["request"]["path"] == "/{country_code}/{postal_code}"
    assert first["examples"][0]["args"] == {"country_code": "us", "postal_code": "90210"}
    assert "post code" in first["response"]["description"]


# -- petstore: flags what it cannot handle ---------------------------------------------------------

def test_petstore_valid_but_refused(pet_spec):
    validate_schema(pet_spec)
    assert pet_spec["base_url"] == "https://petstore3.swagger.io/api/v3"
    with pytest.raises(SpecError, match="needs_review"):
        check_semantics(pet_spec)
    names = [o["name"] for o in pet_spec["operations"]]
    assert len(names) == len(set(names)) == 19


def test_petstore_relative_server_without_source_raises():
    with pytest.raises(IngestError):
        ingest(load(FIX / "petstore.openapi.json"))


def test_petstore_writes_flagged(pet_spec):
    writes = [o for o in pet_spec["operations"] if o["request"]["method"] in ("POST", "PUT", "DELETE")]
    assert len(writes) >= 10
    for op in writes:
        assert op["needs_review"] is True and op["side_effect"] == "write"
        assert any("is a write" in r for r in op["review_reasons"]), op["name"]


def test_petstore_unrepresentable_params_named_not_in_params(pet_spec):
    by = ops(pet_spec)
    tags = by["find_pets_by_tags"]
    assert all(p["name"] != "tags" for p in tags["params"])
    assert any("'tags'" in r and "array" in r for r in tags["review_reasons"])
    add = by["add_pet"]
    assert {p["name"] for p in add["params"]} == {"id", "name", "status"}
    for prop, kind in (("category", "object"), ("photoUrls", "array"), ("tags", "array")):
        assert any(f"'{prop}'" in r and kind in r for r in add["review_reasons"])
    upload = by["upload_file"]
    assert any("not JSON" in r and "application/octet-stream" in r for r in upload["review_reasons"])
    assert any("array" in r for r in by["create_users_with_list_input"]["review_reasons"])


def test_petstore_oauth_and_api_key(pet_spec):
    # petstore declares oauth2 AND an api_key header: apiKey is mapped, oauth2 is flagged on every op
    assert pet_spec["auth"] == {"kind": "api_key", "env": "SWAGGERPETSTOREOPENAPI30_API_KEY", "header": "api_key"}
    for op in pet_spec["operations"]:
        assert any("auth scheme petstore_auth is not supported" in r for r in op["review_reasons"])
        assert op["needs_review"] is True


def test_synthesized_mock_penalised_and_named(pet_spec):
    op = ops(pet_spec)["get_pet_by_id"]
    assert op["examples"][0]["name"] == "synthesized_from_schema"
    assert op["confidence"] == 0.8  # 0.95 - 0.15 synthesized
    assert op["examples"][0]["args"] == {"pet_id": 1}


def test_petstore_invalid_argument_example(pet_spec):
    op = ops(pet_spec)["find_pets_by_status"]
    bad = [e for e in op["examples"] if e["name"] == "invalid_argument"]
    assert bad and bad[0]["args"]["status"] not in op["params"][0]["constraints"]["enum"]


# -- small inline documents ------------------------------------------------------------------------

def mini(**over):
    doc = {"openapi": "3.0.0", "info": {"title": "Mini API", "description": "A tiny API."},
           "servers": [{"url": "https://{region}.example.com/v1", "variables": {"region": {"default": "eu"}}}],
           "paths": {"/items/{itemId}": {"get": {
               "operationId": "getItem", "summary": "Get one item",
               "parameters": [{"name": "itemId", "in": "path", "required": True,
                               "schema": {"type": "integer", "minimum": 1}},
                              {"name": "X-Trace", "in": "header", "schema": {"type": "string"}}],
               "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                   "type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}}}}}}}}}
    doc.update(over)
    return doc


def test_server_variables_missing_description_and_invalid_example():
    spec = ingest(mini())
    validate_schema(spec)
    assert spec["base_url"] == "https://eu.example.com/v1"
    op = spec["operations"][0]
    assert op["when_to_call"] == "Call when you need to get one item."
    assert op["confidence"] == 0.7  # 0.95 - 0.1 no description - 0.15 synthesized
    assert op["needs_review"] is True
    hdr = [p for p in op["params"] if p["in"] == "header"][0]
    assert hdr["wire_name"] == "X-Trace" and hdr["name"] == "x_trace"
    bad = [e for e in op["examples"] if e["expect"] != "ok"][0]
    assert bad["args"]["item_id"] == 0


def test_missing_base_url_raises():
    doc = mini()
    del doc["servers"]
    with pytest.raises(IngestError):
        ingest(doc)
    with pytest.raises(IngestError):
        ingest({"swagger": "2.0", "info": {"title": "x"}, "paths": {}})


def test_swagger2_document():
    doc = {"swagger": "2.0", "info": {"title": "Old Shop", "description": "Legacy shop API."},
           "host": "api.old.example", "basePath": "/v2", "schemes": ["https"],
           "securityDefinitions": {"k": {"type": "apiKey", "in": "query", "name": "key"}},
           "paths": {"/orders/{orderId}": {"get": {
               "summary": "Fetch an order", "description": "Call when you know an order id.",
               "parameters": [{"name": "orderId", "in": "path", "required": True, "type": "string", "x-example": "A1"},
                              {"name": "expand", "in": "query", "type": "array", "items": {"type": "string"}}],
               "responses": {"200": {"description": "The order", "schema": {"$ref": "#/definitions/Order"}}}}}},
           "definitions": {"Order": {"type": "object", "required": ["orderId"],
                                     "properties": {"orderId": {"type": "string", "example": "A1"},
                                                    "total": {"type": "number"}}}}}
    spec = ingest(doc)
    validate_schema(spec)
    assert spec["base_url"] == "https://api.old.example/v2"
    assert spec["auth"] == {"kind": "api_key", "env": "OLDSHOP_API_KEY", "query": "key"}
    op = spec["operations"][0]
    assert op["request"]["path"] == "/orders/{order_id}"
    assert [p["name"] for p in op["params"]] == ["order_id"]
    assert any("'expand'" in r and "array" in r for r in op["review_reasons"])
    assert op["examples"][0]["args"] == {"order_id": "A1"}
    assert op["examples"][0]["expect_keys"] == ["orderId"]
    assert op["examples"][0]["name"] == "synthesized_from_schema"


def test_ref_cycle_does_not_hang():
    doc = mini()
    doc["components"] = {"schemas": {
        "Node": {"type": "object", "properties": {
            "name": {"type": "string"}, "next": {"$ref": "#/components/schemas/Node"},
            "alias": {"$ref": "#/components/schemas/Alias"}}},
        "Alias": {"$ref": "#/components/schemas/Alias"}}}
    doc["paths"]["/items/{itemId}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/Node"}
    spec = ingest(doc)
    validate_schema(spec)
    assert "name" in spec["operations"][0]["examples"][0]["mock"]["body"]


def test_bearer_basic_and_undeclared_path_param():
    doc = mini(components={"securitySchemes": {"b": {"type": "http", "scheme": "bearer"}}})
    doc["paths"] = {"/a/{thingId}": {"get": {"summary": "Get a thing", "description": "Call to get a thing.",
                                              "responses": {"200": {"description": "x"}}}}}
    spec = ingest(doc)
    validate_schema(spec)
    assert spec["auth"] == {"kind": "bearer", "env": "MINIAPI_TOKEN"}
    assert spec["operations"][0]["params"][0]["name"] == "thing_id"
    doc["components"]["securitySchemes"] = {"b": {"type": "http", "scheme": "basic"}}
    assert ingest(doc)["auth"] == {"kind": "basic", "user_env": "MINIAPI_USER", "password_env": "MINIAPI_PASSWORD"}


def test_oauth_only_gives_none_and_reason_everywhere():
    doc = mini(components={"securitySchemes": {"o": {"type": "oauth2", "flows": {}}}})
    spec = ingest(doc)
    assert spec["auth"] == {"kind": "none"}
    assert all(any("auth scheme o is not supported" in r for r in op["review_reasons"])
               for op in spec["operations"])


def test_no_secret_values_anywhere(pet_spec):
    text = json.dumps([pet_spec, ingest(mini())]).lower()
    assert not re.search(r"(sk-[a-z0-9]{8,}|bearer [a-z0-9._-]{12,}|eyj[a-z0-9]{10,})", text)
    for k in ("env", "user_env", "password_env"):
        if k in pet_spec["auth"]:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", pet_spec["auth"][k])
