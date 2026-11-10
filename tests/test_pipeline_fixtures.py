"""The pipeline on real fixtures of every input type, in a throwaway layout (the real inbox/ is untouched).

Generated: OpenAPI (zippopotam), HAR trace (catfact), pasted docs (dog.ceo; the model is a STUB, so this proves
the wiring and the gates, not real extraction quality). Blocked for review: the full Swagger Petstore (writes,
oauth2, array/object params) and a doc whose model output contains a low-confidence op and a hallucinated one.
Also: the committed specs/<name>.yaml are exactly what the ingestors produce from these fixtures.
"""
from __future__ import annotations

import json
import shutil

import pytest
import yaml

from generators.from_spec import ROOT
from ingest.testing import StubAnthropic
from pipeline import process
from pipeline_helpers import layout_in

FIX = ROOT / "tests" / "ingest" / "fixtures"
GENERATED = [
    ("zippopotam.openapi.json", "openapi", "zippopotamus",
     ["zippopotamus__get_places_by_postal_code", "zippopotamus__get_postal_codes_by_place"], None),
    ("catfact.har", "trace", "catfact", ["catfact__get_fact", "catfact__list_facts", "catfact__list_breeds"], None),
    ("dogceo.doc.md", "doc", "dogceo",
     ["dogceo__list_all_breeds", "dogceo__get_random_dog_image", "dogceo__get_random_image_by_breed"],
     "dogceo.stub_response.json"),
]


@pytest.mark.parametrize("fixture,kind,name,tools,stub", GENERATED, ids=[g[1] for g in GENERATED])
# BUDGET (D17, measured over 15 full-suite runs, quiet host, 2026-11-09): max(2*p95, median+2) = 11.9 s; median 4.82, p95 5.93, max 6.56 (worst case: doc)
@pytest.mark.budget(11.9)
def test_fixture_is_ingested_generated_verified_and_matches_the_committed_spec(tmp_path, fixture, kind, name, tools, stub):
    layout = layout_in(tmp_path)
    shutil.copy2(FIX / fixture, layout.inbox / fixture)
    client = StubAnthropic(str(FIX / stub)) if stub else None

    outcome = process(layout.inbox / fixture, layout, doc_client=client)

    assert outcome.status == "generated", outcome.detail
    assert outcome.kind == kind and outcome.name == name and outcome.tools == tools
    assert (layout.servers / f"{name}_server.py").exists() and (layout.tests / f"test_{name}.py").exists()
    committed = ROOT / "specs" / f"{name}.yaml"
    assert yaml.safe_load((layout.specs / f"{name}.yaml").read_text(encoding="utf-8")) == \
        yaml.safe_load(committed.read_text(encoding="utf-8")), "committed spec is not what the pipeline produces"
    assert client is None or client.calls, "the doc path must have called the (stub) model"


def test_openapi_with_only_a_relative_server_url_fails_loudly_instead_of_guessing(tmp_path):
    layout = layout_in(tmp_path)
    shutil.copy2(FIX / "petstore.openapi.json", layout.inbox / "petstore.openapi.json")  # servers: [{url: /api/v3}]
    outcome = process(layout.inbox / "petstore.openapi.json", layout)
    assert outcome.status == "failed" and "no usable base_url" in outcome.detail and "/api/v3" in outcome.detail
    assert (layout.inbox / "failed" / "petstore.reason.txt").exists() or (layout.inbox / "failed" / "petstore.openapi.reason.txt").exists()


def test_full_petstore_is_blocked_for_review_and_nothing_is_generated(tmp_path):
    layout = layout_in(tmp_path)
    doc = json.loads((FIX / "petstore.openapi.json").read_text(encoding="utf-8"))
    doc["servers"] = [{"url": "https://petstore3.swagger.io/api/v3"}]  # what a person does for a relative URL
    source = layout.inbox / "petstore.openapi.json"
    source.write_text(json.dumps(doc), encoding="utf-8")
    outcome = process(source, layout)

    assert outcome.status == "review" and outcome.kind == "openapi"
    assert len(outcome.findings) == 19  # every one of the 19 operations needs a person
    assert any("oauth2" in f for f in outcome.findings) and any("write" in f for f in outcome.findings)
    assert not any(layout.servers.iterdir()) and not any(layout.specs.iterdir()) and not any(layout.tests.iterdir())
    assert outcome.review_file.exists()


def test_doc_with_a_doubtful_and_a_hallucinated_operation_is_blocked(tmp_path):
    layout = layout_in(tmp_path)
    shutil.copy2(FIX / "lowconf.doc.md", layout.inbox / "lowconf.doc.md")
    outcome = process(layout.inbox / "lowconf.doc.md", layout,
                      doc_client=StubAnthropic(str(FIX / "lowconf.stub_response.json")))

    assert outcome.status == "review" and outcome.kind == "doc"
    flagged = {f.split(" ")[0] for f in outcome.findings}
    assert flagged == {"search_authors", "list_daily_recommendations", "create_review"}  # get_book is fine, still held
    assert not any(layout.servers.iterdir()) and not any(layout.specs.iterdir())
