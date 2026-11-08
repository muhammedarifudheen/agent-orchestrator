"""A relative-only OpenAPI server URL must be refused LOUDLY: the reason names what is wrong and what to do,
and nothing at all is written to servers/, specs/ or tests/ (nor held in inbox/review/)."""
from __future__ import annotations

import json
import shutil

import pytest

from generators.from_spec import ROOT
from pipeline import process
from pipeline_helpers import layout_in

PETSTORE = ROOT / "tests" / "ingest" / "fixtures" / "petstore.openapi.json"


def swagger2_without_host() -> dict:
    return {"swagger": "2.0", "info": {"title": "No Host API", "version": "1"}, "basePath": "/v1",
            "paths": {"/things": {"get": {"summary": "List things", "responses": {"200": {"description": "ok", "schema": {"type": "object"}}}}}}}


def assert_loud_refusal(layout, outcome, stem):
    assert outcome.status == "failed"
    assert "absolute URL required" in outcome.detail, outcome.detail
    assert "servers[0].url" in outcome.detail, "the reason must say which field to fix"
    reason_file = layout.inbox / "failed" / f"{stem}.reason.txt"
    assert reason_file.exists() and "absolute URL required" in reason_file.read_text(encoding="utf-8")
    # nothing generated, promoted, staged or held for review
    assert not any(layout.servers.iterdir()) and not any(layout.specs.iterdir()) and not any(layout.tests.iterdir())
    assert not (layout.inbox / "review").exists() and not (layout.inbox / ".staging").exists()


def test_openapi3_with_relative_server_url_is_refused_with_an_actionable_reason(tmp_path):
    layout = layout_in(tmp_path)
    shutil.copy2(PETSTORE, layout.inbox / "petstore.openapi.json")  # servers: [{"url": "/api/v3"}]
    assert json.loads(PETSTORE.read_text(encoding="utf-8"))["servers"] == [{"url": "/api/v3"}]
    outcome = process(layout.inbox / "petstore.openapi.json", layout)
    assert_loud_refusal(layout, outcome, "petstore.openapi")
    assert "'/api/v3'" in outcome.detail  # echoes the offending value


def test_swagger2_without_a_host_is_refused_the_same_way(tmp_path):
    layout = layout_in(tmp_path)
    path = layout.inbox / "nohost.json"
    path.write_text(json.dumps(swagger2_without_host()), encoding="utf-8")
    assert_loud_refusal(layout, process(path, layout), "nohost")


def test_the_same_document_with_an_absolute_url_is_not_refused_for_that_reason(tmp_path):
    layout = layout_in(tmp_path)
    doc = swagger2_without_host()
    doc["host"], doc["schemes"] = "api.example.test", ["https"]
    path = layout.inbox / "withhost.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    outcome = process(path, layout)
    assert "absolute URL required" not in outcome.detail  # whatever happens next, it got past the URL check
    assert outcome.status in ("generated", "review"), outcome.detail
