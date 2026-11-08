"""Offline tests for ingest/doc.py: StubAnthropic only, no network, no API key.

They prove the plumbing and the deterministic hallucination guard. They do NOT prove real extraction quality.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from generators.from_spec import SpecError, check_semantics, validate_schema
from ingest.common import CONFIDENCE_THRESHOLD
from ingest.doc import IngestError, TOOL_NAME, ingest, ingest_with_report
from ingest.testing import StubAnthropic

FIX = Path(__file__).parent / "fixtures"


def _doc(name):
    return (FIX / f"{name}.doc.md").read_text(encoding="utf-8")


def _stub(name):
    return StubAnthropic(FIX / f"{name}.stub_response.json")


def _canned(name):
    return json.loads((FIX / f"{name}.stub_response.json").read_text(encoding="utf-8"))


def _ops(spec):
    return {o["name"]: o for o in spec["operations"]}


@pytest.fixture(scope="module")
def dog():
    return ingest(_doc("dogceo"), client=_stub("dogceo"))


@pytest.fixture(scope="module")
def low():
    return ingest(_doc("lowconf"), client=_stub("lowconf"))


def test_dogceo_is_valid_and_generatable(dog):
    validate_schema(dog)
    check_semantics(dog)  # no needs_review anywhere, path vars match params
    assert dog["base_url"] == "https://dog.ceo/api"
    assert dog["auth"] == {"kind": "none"}


def test_dogceo_read_ops_clean(dog):
    assert len(dog["operations"]) == 3
    for op in dog["operations"]:
        assert op["side_effect"] == "read"
        assert op["needs_review"] is False and op["review_reasons"] == []
        assert op["confidence"] >= CONFIDENCE_THRESHOLD
        assert op["review"]["source"] == "llm"


def test_dogceo_placeholder_and_mock_from_document(dog):
    op = _ops(dog)["get_random_image_by_breed"]
    assert op["request"]["path"] == "/breed/{breed}/images/random"
    assert [p["name"] for p in op["params"]] == ["breed"]
    doc = _doc("dogceo")
    for o in dog["operations"]:
        body = o["examples"][0]["mock"]["body"]
        assert body["status"] == "success"
        msg = body["message"]
        # the mock's content is literally in the document
        assert all(k in doc for k in msg) if isinstance(msg, dict) else msg in doc


def test_placeholder_is_snake_cased():
    canned = _canned("dogceo")
    canned["operations"][2]["path"] = "/breed/{subBreed}/images/random"
    canned["operations"][2]["params"][0]["name"] = "subBreed"
    canned["operations"][2]["example_args"] = {"subBreed": "hound"}
    spec = ingest(_doc("dogceo") + "\nsubBreed", client=StubAnthropic(canned))
    op = _ops(spec)["get_random_image_by_breed"]
    assert op["request"]["path"] == "/breed/{sub_breed}/images/random"
    assert op["params"][0]["name"] == "sub_breed"
    validate_schema(spec)


def test_lowconf_schema_valid(low):
    validate_schema(low)


def test_threshold_reason_for_half_confidence(low):
    op = _ops(low)["search_authors"]
    assert op["confidence"] == 0.5
    assert op["needs_review"] is True
    assert any("threshold" in r for r in op["review_reasons"])
    assert any("no sample response" in r for r in op["review_reasons"])
    assert op["examples"][0]["mock"]["body"] == []


def test_honest_op_is_clean(low):
    op = _ops(low)["get_book"]
    assert op["confidence"] == 0.9 and op["needs_review"] is False


def test_hallucinated_op_is_capped_despite_model_confidence(low):
    assert _canned("lowconf")["operations"][2]["confidence"] == 0.95
    op = _ops(low)["list_daily_recommendations"]
    assert op["confidence"] <= 0.4
    assert op["needs_review"] is True
    text = " ".join(op["review_reasons"])
    assert "evidence quote does not appear" in text
    assert "/discover/daily does not appear" in text
    assert "parameter mood does not appear" in text


def test_post_op_needs_review(low):
    op = _ops(low)["create_review"]
    assert op["side_effect"] == "write"
    assert op["needs_review"] is True
    assert any("POST is a write" in r for r in op["review_reasons"])


def test_check_semantics_refuses_lowconf(low):
    with pytest.raises(SpecError, match="REFUSED"):
        check_semantics(low)


def test_confidence_never_above_model_value(dog, low):
    for name, spec in (("dogceo", dog), ("lowconf", low)):
        said = {o["name"]: o["confidence"] for o in _canned(name)["operations"]}
        for op in spec["operations"]:
            assert op["confidence"] <= said[op["name"]]


def test_base_url_not_in_document_flags_every_op():
    canned = _canned("dogceo")
    canned["base_url"] = "https://other.example/api"
    spec = ingest(_doc("dogceo"), client=StubAnthropic(canned))
    for op in spec["operations"]:
        assert op["needs_review"] is True
        assert any("base_url" in r for r in op["review_reasons"])


def test_forced_tool_choice_and_document_sent():
    stub = _stub("dogceo")
    text = _doc("dogceo")
    ingest(text, client=stub, model="claude-test-model")
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "report_api"}
    assert call["model"] == "claude-test-model"
    assert [t["name"] for t in call["tools"]] == [TOOL_NAME]
    assert text in call["messages"][0]["content"]
    assert "never invent" in call["system"].lower()


def test_missing_api_key_raises_ingest_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(IngestError, match="ANTHROPIC_API_KEY"):
        ingest("some docs", client=None)


def test_response_without_operations_raises():
    canned = _canned("dogceo")
    del canned["operations"]
    with pytest.raises(IngestError, match="operations"):
        ingest(_doc("dogceo"), client=StubAnthropic(canned))


def test_unsupported_and_pathless_ops_reported_not_silent():
    canned = _canned("dogceo")
    canned["operations"].append(copy.deepcopy(canned["operations"][0]) | {"name": "trace_it", "method": "TRACE"})
    canned["operations"].append(copy.deepcopy(canned["operations"][0]) | {"name": "nowhere", "path": ""})
    spec, dropped = ingest_with_report(_doc("dogceo"), client=StubAnthropic(canned))
    assert len(spec["operations"]) == 3
    assert len(dropped) == 2
    assert "trace_it" in dropped[0] and "nowhere" in dropped[1]
    validate_schema(spec)
