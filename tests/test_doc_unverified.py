"""Doc-extracted specs carry `source: doc-unverified` IN THE SPEC FILE, and the pipeline prints a warning at the
point of use. The doc path has only ever run against a stub model (no API key), and the product says so."""
from __future__ import annotations

import yaml

from generators.from_spec import ROOT, validate_schema
from ingest.doc import UNVERIFIED_SOURCE, ingest
from ingest.testing import StubAnthropic
from pipeline import watch
from pipeline_helpers import layout_in, read_op, spec_with, drop

FIX = ROOT / "tests" / "ingest" / "fixtures"


def test_doc_ingestor_marks_the_spec_and_the_marker_validates():
    spec = ingest((FIX / "dogceo.doc.md").read_text(encoding="utf-8"),
                  client=StubAnthropic(str(FIX / "dogceo.stub_response.json")))
    assert spec["source"] == UNVERIFIED_SOURCE == "doc-unverified"
    validate_schema(spec)  # the schema allows it (specs/schema.json, top-level `source`)


def test_other_ingestors_do_not_claim_doc_provenance():
    assert "source" not in spec_with("x", read_op())  # hand/synthetic specs carry no marker


def test_committed_doc_derived_spec_carries_the_marker():
    committed = yaml.safe_load((ROOT / "specs" / "dogceo.yaml").read_text(encoding="utf-8"))
    assert committed["source"] == "doc-unverified"


def test_pipeline_prints_a_one_line_warning_when_it_sees_such_a_spec(tmp_path, capsys):
    layout = layout_in(tmp_path)
    (layout.inbox / "lowconf.doc.md").write_text((FIX / "lowconf.doc.md").read_text(encoding="utf-8"), encoding="utf-8")
    outcomes = watch(layout, once=True, doc_client=StubAnthropic(str(FIX / "lowconf.stub_response.json")))
    out = capsys.readouterr().out
    warning_lines = [ln for ln in out.splitlines() if "UNVERIFIED" in ln]
    assert len(warning_lines) == 1, out
    assert warning_lines[0].lstrip().startswith("WARNING:") and "doc-unverified" in warning_lines[0]
    assert outcomes[0].warnings and "UNVERIFIED" in outcomes[0].warnings[0]


def test_the_warning_survives_review_a_hand_approved_draft_still_carries_the_marker(tmp_path):
    layout = layout_in(tmp_path)
    (layout.inbox / "lowconf.doc.md").write_text((FIX / "lowconf.doc.md").read_text(encoding="utf-8"), encoding="utf-8")
    watch(layout, once=True, doc_client=StubAnthropic(str(FIX / "lowconf.stub_response.json")))
    draft = yaml.safe_load(next((layout.inbox / "review").glob("*.spec.yaml")).read_text(encoding="utf-8"))
    assert draft["source"] == "doc-unverified"  # a person editing the draft does not launder the provenance
