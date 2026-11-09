"""Doc-ingestion contract: EVERY DocExtractionClient must satisfy the same assertions on the same fixture document.

StubDocExtractionClient always runs (it serves a fixture report, so it exercises everything downstream of the model:
grounding checks, confidence caps, the review rules, schema validity). HttpDocExtractionClient runs only when opted in
(ANTHROPIC_API_KEY set and LIVE_TESTS=1) and is otherwise SKIPPED with the reason, never passed. The assertions are one
function used for both: there is no looser version for either client.

The contract is checked per client, not by comparing outputs: a real model will not reproduce the stub's report, so
"the same needs_review flags" means the same RULE (D12/D15) applied to whatever each client returned. That the real HTTP
client path yields byte-identical operations to the stub when it is served the same report is a separate, unconditional
wiring test (test_doc_http_wiring.py).

Not a gate module: the opt-in half needs network.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from generators.from_spec import validate_schema
from ingest.common import CONFIDENCE_THRESHOLD
from ingest.doc import UNVERIFIED_SOURCE, DocExtractionClient, HttpDocExtractionClient, ingest
from ingest.testing import StubDocExtractionClient

FIX = Path(__file__).parent / "fixtures"


def doc_text(name: str) -> str:
    return (FIX / f"{name}.doc.md").read_text(encoding="utf-8")


# -- the contract (shared, identical for every client) --------------------------------------------

def check_doc_ingestion_contract(spec: dict, text: str) -> None:
    validate_schema(spec)                                            # operations validate against specs/schema.json
    assert spec["source"] == UNVERIFIED_SOURCE, "a doc-derived spec is never marked verified (D16)"
    assert spec["operations"], "at least one usable operation"
    for op in spec["operations"]:
        method = op["request"]["method"]
        must_review = (method not in ("GET", "HEAD")                  # D15: ingestion never decides safety
                       or op.get("confidence", 0) < CONFIDENCE_THRESHOLD   # D12
                       or bool(op.get("review_reasons")))
        assert op.get("needs_review", False) is must_review, (
            f"{op['name']}: needs_review={op.get('needs_review')} but the D12/D15 rule says {must_review}")
        if op.get("needs_review"):
            assert op.get("review_reasons"), f"{op['name']}: a flagged operation must say why"
        else:
            segments = [s for s in op["request"]["path"].split("/") if s and not s.startswith("{")]
            assert all(s.lower() in text.lower() for s in segments), (
                f"{op['name']}: unflagged but its path {op['request']['path']} is not grounded in the document")
        assert op["review"]["source"] == "llm"


def live_skip_reason() -> str | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY absent"
    if os.environ.get("LIVE_TESTS") != "1":
        return "LIVE_TESTS=1 not set"
    return None


def stub(name: str) -> DocExtractionClient:
    return StubDocExtractionClient(FIX / f"{name}.stub_response.json")


CLIENTS = [
    pytest.param("stub", "dogceo", id="stub-dogceo"),
    pytest.param("stub", "lowconf", id="stub-lowconf"),
    pytest.param("http", "dogceo", id="http-dogceo",
                 marks=pytest.mark.skipif(live_skip_reason() is not None, reason=live_skip_reason() or "opted in")),
]


@pytest.mark.parametrize("which,fixture", CLIENTS)
def test_every_client_yields_schema_valid_operations_with_the_review_rule_applied(which, fixture):
    client = stub(fixture) if which == "stub" else HttpDocExtractionClient()
    spec = ingest(doc_text(fixture), client=client)
    check_doc_ingestion_contract(spec, doc_text(fixture))


# -- the checks themselves must be able to fail ---------------------------------------------------

def test_the_contract_checks_can_fail():
    text = doc_text("lowconf")
    good = ingest(text, client=stub("lowconf"))
    check_doc_ingestion_contract(good, text)

    import copy
    unflagged_low_confidence = copy.deepcopy(good)
    next(o for o in unflagged_low_confidence["operations"] if o["name"] == "search_authors")["needs_review"] = False
    with pytest.raises(AssertionError, match="D12/D15 rule"):
        check_doc_ingestion_contract(unflagged_low_confidence, text)

    unflagged_write = copy.deepcopy(good)
    next(o for o in unflagged_write["operations"] if o["name"] == "create_review").update(needs_review=False,
                                                                                         review_reasons=[], confidence=0.9)
    with pytest.raises(AssertionError, match="D12/D15 rule"):
        check_doc_ingestion_contract(unflagged_write, text)

    verified = copy.deepcopy(good)
    verified["source"] = "verified"
    with pytest.raises(AssertionError, match="never marked verified"):
        check_doc_ingestion_contract(verified, text)

    invented = copy.deepcopy(good)
    invented["operations"][0].update(request={**invented["operations"][0]["request"], "path": "/made/up/path"})
    with pytest.raises(AssertionError, match="not grounded"):
        check_doc_ingestion_contract(invented, text)

    with pytest.raises(Exception):                                        # schema validity is really enforced
        check_doc_ingestion_contract({**good, "operations": [{"name": "x"}]}, text)


def test_the_live_skip_reason_names_what_is_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert live_skip_reason() == "ANTHROPIC_API_KEY absent"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    assert live_skip_reason() == "LIVE_TESTS=1 not set"
    monkeypatch.setenv("LIVE_TESTS", "1")
    assert live_skip_reason() is None
