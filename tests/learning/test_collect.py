"""Phase A gate: handoffs -> failures.jsonl, deduplicated within 24h, recurrence counted."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from learning.collect import collect, load_records, recurrence_totals

T0 = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


def handoff(dir_, name, tool, code, at, recovery="agent", prior_failure=False, args=None):
    steps = []
    if prior_failure:
        steps.append({"step": 1, "tool": tool, "ok": False, "error_code": code})
    steps.append({"step": 2, "tool": tool, "ok": False, "error_code": code})
    data = {"reason": "x", "goal": "g", "user": "u", "created_at": at.isoformat(timespec="seconds"),
            "steps": steps, "error_snapshot": {"step": 2, "tool": tool, "error_code": code, "error_message": "m",
                                               "retryable": False, "recovery": recovery, "arguments": args or {"latitude": 1.5}}}
    (dir_ / name).write_text(json.dumps(data), encoding="utf-8")


def make3(tmp_path):
    h = tmp_path / "handoffs"
    h.mkdir()
    handoff(h, "a.json", "weather__get_current_weather", "BAD_COORDS", T0, prior_failure=True)
    handoff(h, "b.json", "weather__get_current_weather", "BAD_COORDS", T0 + timedelta(hours=3))
    handoff(h, "c.json", "retail__charge_payment", "PAYMENT_DECLINED", T0 + timedelta(hours=1), recovery="human")
    return h


def test_recurring_counts_two_unique_counts_one_and_no_dupes(tmp_path):
    out = tmp_path / "failures.jsonl"
    recs = collect(make3(tmp_path), out)
    by = {(r["system"], r["operation"], r["error_code"]): r for r in recs}
    assert len(recs) == 2, "identical (system, op, code) within 24h must be ONE record"
    assert by[("weather", "get_current_weather", "BAD_COORDS")]["recurrence"] == 2
    assert by[("retail", "charge_payment", "PAYMENT_DECLINED")]["recurrence"] == 1
    assert by[("weather", "get_current_weather", "BAD_COORDS")]["recovery_attempted"] is True
    assert by[("retail", "charge_payment", "PAYMENT_DECLINED")]["outcome"] == "handed_off_human"


def test_output_is_valid_jsonl_with_the_required_fields(tmp_path):
    out = tmp_path / "failures.jsonl"
    collect(make3(tmp_path), out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)
        assert {"system", "operation", "error_code", "recovery_attempted", "outcome", "ts"} <= set(rec)


def test_rerunning_does_not_double_count(tmp_path):
    out = tmp_path / "failures.jsonl"
    h = make3(tmp_path)
    collect(h, out)
    collect(h, out)
    assert sorted(r["recurrence"] for r in load_records(out)) == [1, 2]


def test_same_failure_more_than_24h_apart_is_a_new_record_but_totals_still_add_up(tmp_path):
    h = tmp_path / "handoffs"
    h.mkdir()
    handoff(h, "a.json", "x__op", "E1", T0)
    handoff(h, "b.json", "x__op", "E1", T0 + timedelta(hours=25))
    recs = collect(h, tmp_path / "f.jsonl")
    assert len(recs) == 2
    assert recurrence_totals(recs) == {("x", "op", "E1"): 2}


def test_handoff_without_a_tool_failure_is_ignored(tmp_path):
    h = tmp_path / "handoffs"
    h.mkdir()
    (h / "s.json").write_text(json.dumps({"created_at": T0.isoformat(), "error_snapshot": {}, "steps": []}), encoding="utf-8")
    assert collect(h, tmp_path / "f.jsonl") == []


def test_incremental_collect_keeps_old_records_when_handoffs_are_deleted(tmp_path):
    out = tmp_path / "f.jsonl"
    h = make3(tmp_path)
    collect(h, out)
    for p in h.glob("*.json"):
        p.unlink()
    assert len(collect(h, out)) == 2


def test_record_carries_status_and_argument_types_but_never_argument_values(tmp_path):
    h = tmp_path / "handoffs"
    h.mkdir()
    handoff(h, "a.json", "weather__get_current_weather", "NOT_FOUND", T0, args={"latitude": 12.5, "city": "Secret Town"})
    data = json.loads((h / "a.json").read_text(encoding="utf-8"))
    data["error_snapshot"]["error_message"] = "HTTP 404: no such place"
    (h / "a.json").write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "f.jsonl"
    rec = collect(h, out)[0]
    assert rec["status"] == 404 and rec["arg_types"] == {"latitude": "number", "city": "string"}
    assert "Secret Town" not in out.read_text(encoding="utf-8")


# -- coverage gap: systems the loop cannot learn about (D18) --------------------------------------------

from learning.collect import coverage_line, main, specless_systems  # noqa: E402

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


def test_summary_line_names_exactly_the_systems_that_have_a_server_but_no_spec(tmp_path):
    servers, specs = tmp_path / "servers", tmp_path / "specs"
    servers.mkdir()
    specs.mkdir()
    for name in ("travel_server.py", "retail_server.py", "weather_server.py", "_base.py", "_http.py", "errors.py"):
        (servers / name).write_text("", encoding="utf-8")
    (specs / "weather.yaml").write_text("x: 1\n", encoding="utf-8")
    (specs / "orphan.yaml").write_text("x: 1\n", encoding="utf-8")  # a spec with no server is not a gap
    assert specless_systems(servers, specs) == ["retail", "travel"]
    assert coverage_line(servers, specs) == "systems without specs (no proposals possible): retail, travel."


def test_summary_line_for_this_repo_matches_what_is_actually_on_disk(tmp_path):
    servers = {p.name[:-len("_server.py")] for p in (ROOT / "servers").glob("*_server.py")}
    specced = {p.stem for p in (ROOT / "specs").glob("*.yaml")}
    expected = sorted(servers - specced)
    assert expected, "if every system has a spec now, delete the coverage-gap note in D18 and this test"
    assert {"travel", "retail", "banking"} <= set(expected)
    assert coverage_line(ROOT / "servers", ROOT / "specs") == \
        f"systems without specs (no proposals possible): {', '.join(expected)}."
    assert not (set(expected) & {"weather", "catfact", "dogceo", "jsonplaceholder", "zippopotamus"})


def test_cli_prints_the_summary_exactly_once_before_the_count(tmp_path, capsys):
    h = tmp_path / "handoffs"
    h.mkdir()
    main(["--handoffs", str(h), "--out", str(tmp_path / "f.jsonl")])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("systems without specs (no proposals possible): ")
    assert sum(ln.startswith("systems without specs") for ln in lines) == 1
    assert lines[1] == f"0 failure record(s) in {tmp_path / 'f.jsonl'}"
