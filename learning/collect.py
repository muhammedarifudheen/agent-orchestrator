"""Phase A of the learning loop: aggregate handoffs/*.json into learning/failures.jsonl.

One record per (system, operation, error_code) per 24h window:
  {system, operation, error_code, recovery_attempted, outcome, ts, recurrence, sources}
`recurrence` counts the handoffs folded into the record; `sources` names them, so re-running over the same
handoffs never double-counts. Reads handoffs, writes only the jsonl. Never touches specs/.
"""
from __future__ import annotations

import argparse
import json
import re
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
    return {
        "system": system, "operation": operation, "error_code": code,
        "recovery_attempted": bool(earlier_same_tool),
        "outcome": "handed_off_human" if snap.get("recovery") == "human" else "handed_off_exhausted",
        "retryable": snap.get("retryable"), "recovery": snap.get("recovery"),
        "status": _http_status(snap.get("error_message")),
        "message": str(snap.get("error_message") or "")[:200],
        # parameter NAME -> JSON type of the value the agent sent. Never the values: they may be personal data.
        "arg_types": {k: _json_type(v) for k, v in (snap.get("arguments") or {}).items()},
        "ts": data.get("created_at", ""), "recurrence": 1, "sources": [path.name],
    }


def _http_status(message: Any) -> int | None:
    """The generated HTTP runtime words upstream errors as 'HTTP <status>: ...'."""
    m = re.match(r"HTTP (\d{3})\b", str(message or ""))
    return int(m.group(1)) if m else None


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--handoffs", type=Path, default=ROOT / "handoffs")
    ap.add_argument("--out", type=Path, default=ROOT / "learning" / "failures.jsonl")
    args = ap.parse_args()
    recs = collect(args.handoffs, args.out)
    print(f"{len(recs)} failure record(s) in {args.out}")


if __name__ == "__main__":
    main()
