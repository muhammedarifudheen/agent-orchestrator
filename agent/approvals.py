"""Pending-approval records, and PARTIAL approval of the writes in one (D24).

When the write policy refuses a write because nobody approved it, the run stops with a record of what it wanted to do:
the tool, its arguments, what the server says about the blast radius (reversible, scope, whether it moves money), the
steps that already ran, and the reason. A reviewer then approves SOME of the writes in the record: only those execute,
the rest stay pending. Industry-agnostic: nothing here knows a tool.

Safety properties, each tested: a write executes at most once (an `executing` write whose outcome is unknown is never
re-run); the arguments executed are exactly the stored ones; a reviewer must be named; a tool that has lost its
metadata since the record was made is refused at execution, not run.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.tool_registry import ToolRegistry
from agent.write_policy import METADATA_MISSING, classify

KIND = "pending_write_approval"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create(directory: Path, *, user: str, goal: str, writes: list[dict[str, Any]], prior_steps: list[dict[str, Any]],
           duty: dict[str, Any] | None, write_boundary: dict[str, Any] | None) -> Path:
    """Write one pending-approval record and return its path. Several writes proposed in one run share one record."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now():%Y%m%d_%H%M%S}_{user}"
    path, n = directory / f"{stem}.json", 1
    while path.exists():  # two records in the same second must not overwrite each other
        n += 1
        path = directory / f"{stem}_{n}.json"
    record = {
        "kind": KIND, "id": path.stem, "created_at": _now(), "user": user, "goal": goal, "status": "pending",
        "duty": duty, "write_boundary": write_boundary, "prior_steps": prior_steps,
        "writes": [{"id": i, "tool": w["tool"], "arguments": w["arguments"], "cost_summary": w["cost_summary"],
                    "reason": w["reason"], "status": "pending",
                    "decided_by": None, "decided_at": None, "result": None, "error": None}
                   for i, w in enumerate(writes, 1)],
    }
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return path


def load(path: Path) -> dict[str, Any]:
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    if record.get("kind") != KIND:
        raise ValueError(f"{path} is not a pending-approval record")
    return record


def _save(path: Path, record: dict[str, Any]) -> None:
    pending = [w for w in record["writes"] if w["status"] == "pending"]
    record["status"] = ("pending" if len(pending) == len(record["writes"])
                        else "partial" if pending else "complete")
    Path(path).write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")


def _select(record: dict[str, Any], ids: list[int]) -> list[dict[str, Any]]:
    by_id = {w["id"]: w for w in record["writes"]}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"no write with id {unknown} in {record['id']} (it has {sorted(by_id)})")
    if len(set(ids)) != len(ids):
        raise ValueError(f"write ids repeated: {ids}")
    chosen = [by_id[i] for i in sorted(ids)]
    not_pending = [(w["id"], w["status"]) for w in chosen if w["status"] != "pending"]
    if not_pending:
        raise ValueError(f"only a pending write can be decided; these are not: {not_pending}")
    return chosen


async def approve(path: Path, ids: list[int], reviewer: str, registry: ToolRegistry) -> dict[str, Any]:
    """Approve the writes `ids` and execute exactly those, in id sequence. The others stay pending."""
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("approving a write needs a named reviewer")
    path = Path(path)
    record = load(path)
    chosen = _select(record, ids)
    for write in chosen:
        write.update(status="approved", decided_by=reviewer.strip(), decided_at=_now())
    _save(path, record)
    for write in chosen:
        safety = classify(registry, write["tool"])
        if safety.missing and not safety.is_read:  # the server no longer says what this tool is: do not run it
            write.update(status="refused", error=f"{METADATA_MISSING}: {write['tool']} lacks {', '.join(safety.missing)}")
            _save(path, record)
            continue
        write["status"] = "executing"  # saved BEFORE the call: a crash leaves it here and it is never run twice
        _save(path, record)
        outcome = await registry.call(write["tool"], write["arguments"])
        if outcome.ok:
            write.update(status="executed", result=outcome.data)
        else:
            write.update(status="failed", error={"code": outcome.error_code, "message": outcome.error_message,
                                                 "retryable": outcome.retryable, "recovery": outcome.recovery})
        _save(path, record)
    return summary(record)


def reject(path: Path, ids: list[int], reviewer: str, reason: str) -> dict[str, Any]:
    """Reject writes: they will never run. The others stay pending."""
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("rejecting a write needs a named reviewer")
    path = Path(path)
    record = load(path)
    for write in _select(record, ids):
        write.update(status="rejected", decided_by=reviewer.strip(), decided_at=_now(), error=reason)
    _save(path, record)
    return summary(record)


def summary(record: dict[str, Any]) -> dict[str, Any]:
    by = lambda status: [w["id"] for w in record["writes"] if w["status"] == status]  # noqa: E731
    return {"id": record["id"], "status": record["status"], "executed": by("executed"), "failed": by("failed"),
            "pending": by("pending"), "rejected": by("rejected"), "refused": by("refused"),
            "executing": by("executing")}
