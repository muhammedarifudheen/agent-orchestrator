"""Phase A of the learning loop: aggregate handoffs/*.json into learning/failures.jsonl.

One record per (system, operation, error_code) per 24h window:
  {system, operation, error_code, recovery_attempted, outcome, ts, recurrence, sources}
`recurrence` counts the handoffs folded into the record; `sources` names them, so re-running over the same
handoffs never double-counts. Reads handoffs, writes only the jsonl. Never touches specs/.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WINDOW = timedelta(hours=24)


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def failure_from_handoff(path: Path) -> dict[str, Any] | None:
    """The unrecoverable failure a handoff records, or None if it has no tool failure (e.g. step limit)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    snap = data.get("error_snapshot") or {}
    tool, code = snap.get("tool"), snap.get("error_code")
    if not tool or not code or "__" not in tool:
        return None
    system, _, operation = tool.partition("__")
    earlier_same_tool = [s for s in data.get("steps", [])
                         if s.get("tool") == tool and not s.get("ok") and s.get("step", 0) < snap.get("step", 0)]
    status, deprecated = _status_of(snap)
    rec = {
        "system": system, "operation": operation, "error_code": code,
        "recovery_attempted": bool(earlier_same_tool),
        "outcome": "handed_off_human" if snap.get("recovery") == "human" else "handed_off_exhausted",
        "retryable": snap.get("retryable"), "recovery": snap.get("recovery"),
        "status": status,
        "message": str(snap.get("error_message") or "")[:200],
        # parameter NAME -> JSON type of the value the agent sent. Never the values: they may be personal data.
        "arg_types": {k: _json_type(v) for k, v in (snap.get("arguments") or {}).items()},
        "ts": data.get("created_at", ""), "recurrence": 1, "sources": [path.name],
    }
    if deprecated:
        rec["deprecated_parse"] = True  # status came from the message prefix: an old handoff without http_status
    return rec


def _status_of(snap: dict[str, Any]) -> tuple[int | None, bool]:
    """(status, used_deprecated_parse). The orchestrator stores `http_status` (D18); that is the source of truth."""
    if snap.get("http_status") is not None:
        return int(snap["http_status"]), False
    if "http_status" in snap:  # key present and null: the failure had no HTTP status. Do NOT guess from text.
        return None, False
    status = _http_status_from_prefix(snap.get("error_message"))
    return status, status is not None


def _http_status_from_prefix(message: Any) -> int | None:
    """DEPRECATED fallback for handoffs written before `http_status` existed (it breaks whenever a tool rewords
    its message). Only reached when the key is ABSENT from the snapshot; callers mark the record deprecated_parse."""
    text = str(message or "")
    if text.startswith("HTTP ") and text[5:8].isdigit():
        return int(text[5:8])
    return None


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object" if isinstance(value, dict) else "null"


def load_records(out: Path) -> list[dict[str, Any]]:
    if not out.exists():
        return []
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]


def collect(handoffs_dir: Path, out: Path) -> list[dict[str, Any]]:
    """Fold every not-yet-seen handoff into `out`; returns the full record list."""
    records = load_records(out)
    seen = {s for r in records for s in r["sources"]}
    new = []
    for path in sorted(handoffs_dir.glob("*.json")):
        if path.name in seen:
            continue
        rec = failure_from_handoff(path)
        if rec and rec["ts"]:
            new.append(rec)
    for rec in sorted(new, key=lambda r: _ts(r["ts"])):
        key = (rec["system"], rec["operation"], rec["error_code"])
        match = next((r for r in records if (r["system"], r["operation"], r["error_code"]) == key
                      and abs(_ts(rec["ts"]) - _ts(r["ts"])) < WINDOW), None)
        if match:
            match["recurrence"] += 1
            match["sources"].append(rec["sources"][0])
            match["recovery_attempted"] = match["recovery_attempted"] or rec["recovery_attempted"]
            if rec.get("deprecated_parse"):
                match["deprecated_parse"] = True
        else:
            records.append(rec)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8")
    return records


def recurrence_totals(records: list[dict[str, Any]]) -> dict[tuple[str, str, str], int]:
    """Total recurrence per (system, operation, error_code) across all windows."""
    totals: dict[tuple[str, str, str], int] = {}
    for r in records:
        key = (r["system"], r["operation"], r["error_code"])
        totals[key] = totals.get(key, 0) + r["recurrence"]
    return totals


def specless_systems(servers_dir: Path, specs_dir: Path) -> list[str]:
    """Systems that have a server under servers/ but no specs/<system>.yaml (sorted). The learning loop only
    proposes corrections to specs, so for these it is blind (D18 coverage gap)."""
    systems = {p.name[:-len("_server.py")] for p in Path(servers_dir).glob("*_server.py") if not p.name.startswith("_")}
    specced = {p.stem for p in Path(specs_dir).glob("*.yaml")}
    return sorted(systems - specced)


def coverage_line(servers_dir: Path, specs_dir: Path) -> str:
    names = specless_systems(servers_dir, specs_dir)
    return f"systems without specs (no proposals possible): {', '.join(names) if names else 'none'}."


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--handoffs", type=Path, default=ROOT / "handoffs")
    ap.add_argument("--out", type=Path, default=ROOT / "learning" / "failures.jsonl")
    ap.add_argument("--servers", type=Path, default=ROOT / "servers")
    ap.add_argument("--specs", type=Path, default=ROOT / "specs")
    args = ap.parse_args(argv)
    print(coverage_line(args.servers, args.specs))  # once, at startup, before any work
    recs = collect(args.handoffs, args.out)
    print(f"{len(recs)} failure record(s) in {args.out}")


if __name__ == "__main__":
    main()
