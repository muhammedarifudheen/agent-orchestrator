from __future__ import annotations

import pytest

from learning.patchfmt import PatchError, apply_diff, make_proposal, parse_proposal

HEAD = {"proposal": "mark_needs_review", "system": "s", "operation": "o", "error_code": "E", "recurrence": 2,
        "spec": "specs/s.yaml", "base_sha256": "0" * 64, "rationale": "r"}
OLD = "a: 1\nb:\n  - x\n  - y\nc: 3\nd: 4\ne: 5\nf: 6\ng: 7\nh: 8\n"
NEW = "a: 1\nb:\n  - x\n  - y\n  - z\nc: 3\nd: 4\ne: 5\nf: 6\ng: 7\nh: 9\n"


def test_round_trip_and_header():
    header, diff = parse_proposal(make_proposal(HEAD, OLD, NEW))
    assert header["proposal"] == "mark_needs_review" and header["recurrence"] == "2"
    assert apply_diff(OLD, diff) == NEW


def test_stale_file_is_refused_not_guessed_at():
    _, diff = parse_proposal(make_proposal(HEAD, OLD, NEW))
    with pytest.raises(PatchError, match="mismatch"):
        apply_diff(OLD.replace("x", "q"), diff)


def test_bad_header_and_no_change_are_refused():
    with pytest.raises(PatchError):
        make_proposal({**HEAD, "proposal": "delete_everything"}, OLD, NEW)
    with pytest.raises(PatchError, match="identical"):
        make_proposal(HEAD, OLD, OLD)
    with pytest.raises(PatchError, match="missing"):
        parse_proposal("# proposal: add_error_code\n--- a/x\n+++ b/x\n")


def test_file_without_trailing_newline():
    _, diff = parse_proposal(make_proposal(HEAD, "a: 1\nb: 2", "a: 1\nb: 3\n"))
    assert apply_diff("a: 1\nb: 2", diff).rstrip("\n") == "a: 1\nb: 3"
