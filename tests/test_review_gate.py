"""The honesty gate: nothing a person has not reviewed may be generated.

Each test below fails if the corresponding rule stops being enforced. The threshold itself is a recorded
decision (CLAUDE.md D12); `test_threshold_matches_the_recorded_decision` fails if code and doc disagree.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from generators.from_spec import ROOT, SpecError, generate, load_spec
from ingest.common import CONFIDENCE_THRESHOLD
import pipeline
from pipeline import process, review_findings
from pipeline_helpers import drop, layout_in, read_op, spec_with


def nothing_generated(layout, name):
    return not any([(layout.servers / f"{name}_server.py").exists(), (layout.specs / f"{name}.yaml").exists(),
                    (layout.tests / f"test_{name}.py").exists()])


def test_threshold_matches_the_recorded_decision():
    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    d12 = re.search(r"- D12\..*?(?=\n- D\d+\.|\n## )", claude_md, re.S)
    assert d12, "CLAUDE.md must record decision D12"
    assert f"`CONFIDENCE_THRESHOLD = {CONFIDENCE_THRESHOLD}`" in d12.group(0), \
        "ingest.common.CONFIDENCE_THRESHOLD and CLAUDE.md D12 disagree: change both in one commit, with a reason"


def test_low_confidence_operation_blocks_generation(tmp_path):
    layout = layout_in(tmp_path)
    source = drop(layout.inbox, spec_with("gatelow", read_op("get_item", confidence=CONFIDENCE_THRESHOLD - 0.25)))
    outcome = process(source, layout)

    assert outcome.status == "review"
    assert nothing_generated(layout, "gatelow")
    assert outcome.review_file and outcome.review_file.exists()
    assert (layout.inbox / "review" / "gatelow.spec.yaml").exists()
    assert not source.exists() and (layout.inbox / "review" / source.name).exists()  # inbox is clean
    assert any("confidence 0.5" in f and "threshold" in f for f in outcome.findings), outcome.findings
    draft = yaml.safe_load((layout.inbox / "review" / "gatelow.spec.yaml").read_text(encoding="utf-8"))
    assert draft["operations"][0]["needs_review"] is True


def test_threshold_boundary_is_exact():
    at = spec_with("gateedge", read_op(confidence=CONFIDENCE_THRESHOLD))
    below = spec_with("gateedge", read_op(confidence=round(CONFIDENCE_THRESHOLD - 0.01, 2)))
    assert review_findings(at) == []
    assert review_findings(below) != []


def test_a_write_operation_always_blocks(tmp_path):
    layout = layout_in(tmp_path)
    write = read_op("create_item", confidence=0.99, method="POST", path="/items")
    outcome = process(drop(layout.inbox, spec_with("gatewrite", write)), layout)
    assert outcome.status == "review" and nothing_generated(layout, "gatewrite")
    assert any("write" in f for f in outcome.findings)


def test_one_flagged_operation_blocks_the_whole_spec(tmp_path):
    layout = layout_in(tmp_path)
    spec = spec_with("gatemixed", read_op("get_good", confidence=0.95), read_op("get_doubtful", confidence=0.4,
                                                                                path="/doubtful/{item_id}"))
    outcome = process(drop(layout.inbox, spec), layout)
    assert outcome.status == "review" and nothing_generated(layout, "gatemixed")  # the good op is held too
    assert len(outcome.findings) == 1 and "get_doubtful" in outcome.findings[0]


def test_clearing_the_flag_without_naming_a_reviewer_does_not_unblock(tmp_path):
    layout = layout_in(tmp_path)
    spec = spec_with("gatenoname", read_op(confidence=0.4))
    spec["operations"][0]["needs_review"] = False  # someone just flipped the flag
    outcome = process(drop(layout.inbox, spec), layout)
    assert outcome.status == "review" and nothing_generated(layout, "gatenoname")
    assert "no reviewer is named" in outcome.findings[0]


def test_the_generator_itself_refuses_a_needs_review_spec(tmp_path):
    """Defence in depth: bypassing the pipeline and calling the generator directly is also refused."""
    path = tmp_path / "gatedirect.yaml"
    path.write_text(yaml.safe_dump(spec_with("gatedirect", read_op(confidence=0.3)), sort_keys=False), encoding="utf-8")
    with pytest.raises(SpecError, match="needs_review"):
        load_spec(path)


def _review_and_approve(layout, name="gateok"):
    """A person reviews the blocked draft, fixes nothing, clears the flag and signs it; returns the approved file."""
    blocked = process(drop(layout.inbox, spec_with(name, read_op(confidence=0.5))), layout)
    assert blocked.status == "review" and nothing_generated(layout, name)
    draft_path = layout.inbox / "review" / f"{name}.spec.yaml"
    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    op = draft["operations"][0]
    op["needs_review"] = False
    op["review"] = {"source": op["review"]["source"], "safety_fields": "reviewed", "reviewer": "A. Reviewer"}
    approved = layout.inbox / f"{name}.yaml"
    approved.write_text(yaml.safe_dump(draft, sort_keys=False), encoding="utf-8")
    return approved


def test_reviewed_and_approved_spec_flows_through_the_gate_and_is_generated(tmp_path, monkeypatch):
    """The review-gate FLOW (blocked -> reviewed -> approved -> generated). The pipeline's two verification gates are
    replaced by a recorder here: they run for real in test_pipeline.py and in the contract test below (split from one
    ~6 s test that did all of it, D17)."""
    calls = []

    def fake_verify(layout, name, servers_dir, tests_dir, spec_dir):
        staged_spec = Path(spec_dir) / f"{name}.yaml"
        calls.append({"name": name, "servers": Path(servers_dir), "specs": Path(spec_dir),
                      "spec_text": staged_spec.read_text(encoding="utf-8") if staged_spec.exists() else None})
        return True, "stub"

    monkeypatch.setattr(pipeline, "_verify", fake_verify)
    layout = layout_in(tmp_path)
    outcome = process(_review_and_approve(layout), layout)
    assert outcome.status == "generated", outcome.detail
    assert outcome.tools == ["gateok__get_item"]
    assert not nothing_generated(layout, "gateok")
    # verified twice, STAGED first (the approved spec, reviewer named) and then on the promoted files
    assert [c["name"] for c in calls] == ["gateok", "gateok"]
    assert layout.inbox / ".staging" in calls[0]["servers"].parents and "A. Reviewer" in calls[0]["spec_text"]
    assert calls[1]["servers"] == layout.servers and calls[1]["specs"] == layout.specs


# BUDGET (D17): max(2*p95, median+2) = 3.2 s on QUIET-STATE estimates: 15 runs on 2026-11-10 (median 1.72, p95 1.83,
# max 1.85) on a host running 1.44x slower than the 2026-11-09 quiet baseline, divided by 1.44 -> median 1.19, p95 1.27.
# One real pytest subprocess. Not today's loaded numbers: an estimate of the quiet state.
@pytest.mark.budget(3.2)
def test_the_identical_contract_test_passes_on_an_approved_generated_server(tmp_path):
    layout = layout_in(tmp_path)
    spec = spec_with("gateok", read_op(confidence=0.5))
    op = spec["operations"][0]
    op["needs_review"] = False
    op["review"] = {"source": op["review"]["source"], "safety_fields": "reviewed", "reviewer": "A. Reviewer"}
    spec_path = layout.specs / "gateok.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    generate(spec_path, layout.servers, layout.tests)

    # independent evidence: the identical tests/test_contract.py, run again by this test on the promoted files
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(layout.servers), str(ROOT / "servers"), str(ROOT)]),
               CONTRACT_SPEC_DIRS=str(layout.specs))
    env.pop("LIVE_TESTS", None)
    done = subprocess.run([sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider", "-c", str(ROOT / "pytest.ini"),
                           str(ROOT / "tests" / "test_contract.py"), "-k", "gateok"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-1500:]
    results = {m.group(1): m.group(2) for m in re.finditer(r"::(test_\w+)\[[^\]]*generated:gateok[^\]]*\] (PASSED|SKIPPED|FAILED)", done.stdout)}
    assert "FAILED" not in done.stdout.split("short test summary")[0]
    # every contract test that applies to a one-operation server ran and passed
    for name in ("test_exposes_tools_with_descriptions", "test_descriptions_state_when_to_call_args_and_errors",
                 "test_every_tool_declares_safety_annotations", "test_every_tool_is_wrapped_by_guarded",
                 "test_probe_outcome_follows_the_contract", "test_missing_arguments_are_a_failure_the_registry_normalizes",
                 "test_unexpected_exception_becomes_internal_error"):
        assert results.get(name) == "PASSED", (name, results)
    # ingested read operations carry a standard 503 example, so the outage probe runs too: nothing is skipped
    assert results.get("test_outage_is_a_retryable_failure") == "PASSED", results
    assert "SKIPPED" not in results.values(), results


def test_naming_a_reviewer_is_not_approval_the_flag_itself_must_be_cleared():
    """A reviewer name alone must not unblock an operation whose needs_review flag is still set."""
    spec = spec_with("gatehalf", read_op(confidence=0.4))
    op = spec["operations"][0]
    op["review"]["reviewer"] = "A. Reviewer"
    assert op["needs_review"] is True
    findings = review_findings(spec)
    assert len(findings) == 1 and "needs_review is true" in findings[0]
