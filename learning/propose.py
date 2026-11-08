"""Phase B of the learning loop: turn recurring failures into candidate spec corrections (proposal files).

Reads learning/failures.jsonl (see collect.py) and specs/<system>.yaml; writes `<out>/<system>__<op>__<code>__<type>.patch`
(format: learning/patchfmt.py). It NEVER writes to the specs dir and never applies anything: a person reviews a
proposal and apply.py applies it. Only keys that recurred at least MIN_RECURRENCE times get a proposal (never lower it).

Edits are made at TEXT level so the diff is small and comments/formatting survive. Every edit is verified: the new text is
parsed and must equal the original spec with exactly the intended change, and must pass the schema. If a text edit cannot
be done safely the key is skipped ("cannot patch safely"); the file is never re-dumped.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python learning/propose.py` as well as `python -m learning.propose`
    sys.path.insert(0, str(ROOT))

from generators.from_spec import validate_schema  # noqa: E402
from learning import patchfmt  # noqa: E402
from learning.collect import load_records, recurrence_totals  # noqa: E402

MIN_RECURRENCE = 2
ARG_ERROR_CODES = {"INVALID_ARGUMENT", "INVALID_ARGUMENTS", "INVALID_REQUEST"}
SCALAR_TYPES = {"integer", "number", "string", "boolean"}
DONE_DIRS = ("", "approved", "rejected", "applied")


class _Unsafe(Exception):
    """The text edit cannot be done (or verified) safely."""


# ---------------------------------------------------------------- text helpers

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _last_content(lines: list[str], start: int, end: int) -> int:
    """Index of the last content line in [start, end)."""
    for i in range(end - 1, start - 1, -1):
        if _is_content(lines[i]):
            return i
    return start


def _find_op_block(lines: list[str], op: str) -> tuple[int, int, int]:
    """(start, end, key_indent) of the operation's block; `end` is exclusive and excludes trailing blanks/comments."""
    try:
        ops_at = next(i for i, ln in enumerate(lines) if re.match(r"operations:\s*(#.*)?$", ln))
    except StopIteration:
        raise _Unsafe("no operations: key") from None
    first = next((i for i in range(ops_at + 1, len(lines)) if _is_content(lines[i])), None)
    if first is None or not lines[first].lstrip().startswith("- "):
        raise _Unsafe("operations is not a block list")
    item = _indent(lines[first])
    pat = re.compile(r"^ {%d}- name:\s*[\"']?%s[\"']?\s*(#.*)?$" % (item, re.escape(op)))
    starts = [i for i in range(ops_at + 1, len(lines)) if pat.match(lines[i])]
    if len(starts) != 1:
        raise _Unsafe("operation line not found uniquely")
    start = starts[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _is_content(lines[i]) and _indent(lines[i]) <= item:
            end = i
            break
    return start, _last_content(lines, start, end) + 1, item + 2


def _sub_block(lines: list[str], head: int, end: int, key_indent: int) -> int:
    """Exclusive end (after the last content line) of the value block of the key at `head` (indent `key_indent`)."""
    stop = end
    for i in range(head + 1, end):
        ln = lines[i]
        if not _is_content(ln):
            continue
        if _indent(ln) > key_indent or (_indent(ln) == key_indent and ln.lstrip().startswith("- ")):
            continue
        stop = i
        break
    return _last_content(lines, head, stop) + 1


def _find_key(lines: list[str], start: int, end: int, key_indent: int, key: str) -> int | None:
    pat = re.compile(r"^ {%d}%s:(.*)$" % (key_indent, re.escape(key)))
    for i in range(start + 1, end):
        if pat.match(lines[i]):
            return i
    return None


def _seq_indent(lines: list[str], start: int, end: int, key_indent: int) -> int:
    """Indent for list items under a key: same as the key (yaml.safe_dump style) or two deeper."""
    for i in range(start + 1, end):
        if _indent(lines[i]) == key_indent and lines[i].lstrip().startswith("- "):
            return key_indent
    return key_indent + 2


def _is_flow_style(lines: list[str], start: int, end: int) -> bool:
    return any(re.match(r"^\s*- \{", lines[i]) for i in range(start, end))


# ---------------------------------------------------------------- the three edits

def _edit_param_type(lines: list[str], op: str, param: str, old: str, new: str) -> list[str]:
    start, end, k = _find_op_block(lines, op)
    head = _find_key(lines, start, end, k, "params")
    if head is None or lines[head].split(":", 1)[1].strip():
        raise _Unsafe("params not a block list")
    sub_end = _sub_block(lines, head, end, k)
    items = [i for i in range(head + 1, sub_end) if re.match(r"^\s*- ", lines[i])]
    if not items:
        raise _Unsafe("no param items")
    j = _indent(lines[items[0]])
    items = [i for i in items if _indent(lines[i]) == j]
    for n, i in enumerate(items):
        stop = items[n + 1] if n + 1 < len(items) else sub_end
        first = lines[i].lstrip()[2:]
        if first.startswith("{"):
            text = "\n".join(lines[i:stop])
            if not re.search(r"[{,]\s*name:\s*[\"']?%s[\"']?\s*[,}]" % re.escape(param), text):
                continue
            text2, count = re.subn(r"([{,]\s*)type:\s*[\"']?%s[\"']?(\s*[,}])" % re.escape(old),
                                   lambda m: f"{m.group(1)}type: {new}{m.group(2)}", text, count=1)
            if count != 1:
                raise _Unsafe("type not found in flow param")
            return lines[:i] + text2.split("\n") + lines[stop:]
        if re.match(r"name:\s*[\"']?%s[\"']?\s*(#.*)?$" % re.escape(param), first):
            pat = re.compile(r"^( {%d})type:\s*[\"']?%s[\"']?\s*$" % (j + 2, re.escape(old)))
            for t in range(i + 1, stop):
                if pat.match(lines[t]):
                    out = list(lines)
                    out[t] = f"{' ' * (j + 2)}type: {new}"
                    return out
            raise _Unsafe("type line not found in block param")
    raise _Unsafe("param entry not found")


def _flow_error(status: int, code: str, retryable: bool, recovery: str, meaning: str) -> str:
    return (f"{{when: {{status: {status}}}, code: {code}, retryable: {str(retryable).lower()}, "
            f"recovery: {recovery}, meaning: {json.dumps(meaning)}}}")


def _edit_add_error(lines: list[str], op: str, rule: dict[str, Any]) -> list[str]:
    start, end, k = _find_op_block(lines, op)
    flow = _is_flow_style(lines, start, end)
    head = _find_key(lines, start, end, k, "errors")
    if head is not None and lines[head].split(":", 1)[1].split("#")[0].strip() == "[]":
        sub = _seq_indent(lines, start, end, k)
        at = _sub_block(lines, head, end, k)
        return lines[:head] + [f"{' ' * k}errors:"] + _error_item(rule, flow, sub) + lines[at:]
    if head is not None:
        if lines[head].split(":", 1)[1].split("#")[0].strip():
            raise _Unsafe("errors is not a block list")
        sub_end = _sub_block(lines, head, end, k)
        item_idx = [i for i in range(head + 1, sub_end) if re.match(r"^\s*- ", lines[i])]
        if not item_idx:
            raise _Unsafe("errors has no items")
        j = _indent(lines[item_idx[0]])
        flow = lines[item_idx[0]].lstrip()[2:].startswith("{")
        at = sub_end
        new_lines = _error_item(rule, flow, j)
    else:
        sub = _seq_indent(lines, start, end, k)
        at = end
        new_lines = [f"{' ' * k}errors:"] + _error_item(rule, flow, sub)
    return lines[:at] + new_lines + lines[at:]


def _error_item(rule: dict[str, Any], flow: bool, j: int) -> list[str]:
    pad = " " * j
    if flow:
        return [f"{pad}- " + _flow_error(rule["when"]["status"], rule["code"], rule["retryable"],
                                          rule["recovery"], rule["meaning"])]
    return [f"{pad}- when:", f"{pad}    status: {rule['when']['status']}", f"{pad}  code: {rule['code']}",
            f"{pad}  retryable: {str(rule['retryable']).lower()}", f"{pad}  recovery: {rule['recovery']}",
            f"{pad}  meaning: {json.dumps(rule['meaning'])}"]


def _edit_needs_review(lines: list[str], op: str, reason: str) -> list[str]:
    start, end, k = _find_op_block(lines, op)
    rr = _find_key(lines, start, end, k, "review_reasons")
    if rr is not None and lines[rr].split(":", 1)[1].split("#")[0].strip() != "[]":
        raise _Unsafe("review_reasons already has content")
    flow = _is_flow_style(lines, start, end)
    out = list(lines)
    nr = _find_key(out, start, end, k, "needs_review")
    add: list[str] = []
    if nr is not None:
        if out[nr].split(":", 1)[1].split("#")[0].strip() not in ("false", "False", "no"):
            raise _Unsafe("needs_review has an unexpected value")
        out[nr] = f"{' ' * k}needs_review: true"
    else:
        add.append(f"{' ' * k}needs_review: true")
    if flow:
        rr_lines = [f"{' ' * k}review_reasons: [{json.dumps(reason)}]"]
    else:
        rr_lines = [f"{' ' * k}review_reasons:", f"{' ' * _seq_indent(lines, start, end, k)}- {json.dumps(reason)}"]
    if rr is not None:
        out[rr:rr + 1] = rr_lines
        out[rr:rr] = add  # needs_review (if missing) goes right before the replaced review_reasons
    else:
        out[end:end] = add + rr_lines
    return out


# ---------------------------------------------------------------- proposal logic

def _find_op(spec: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((o for o in spec.get("operations", []) if o.get("name") == name), None)


def _choose(op: dict[str, Any], spec: dict[str, Any], code: str, recs: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """(proposal type, details). First matching type wins."""
    if code in ARG_ERROR_CODES:
        for p in op.get("params", []):
            seen = Counter(r["arg_types"][p["name"]] for r in recs
                           if p["name"] in (r.get("arg_types") or {}))
            cands = {o: n for o, n in seen.items()
                     if o != p["type"] and o in SCALAR_TYPES and not (p["type"] == "number" and o == "integer")}
            if cands:
                best = sorted(cands.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                return "correct_param_type", {"param": p["name"], "old": p["type"], "new": best}
    declared = {e.get("code") for e in op.get("errors", [])} | {e.get("code") for e in spec.get("error_map", [])}
    latest = max(recs, key=lambda r: r.get("ts", ""))
    if (code not in declared and latest.get("status") is not None
            and latest.get("retryable") is not None and latest.get("recovery")):
        return "add_error_code", {"status": int(latest["status"]), "retryable": bool(latest["retryable"]),
                                  "recovery": latest["recovery"]}
    return "mark_needs_review", {"outcome": latest.get("outcome", "unknown")}


def _build(old_text: str, spec: dict[str, Any], op_name: str, code: str, total: int, kind: str,
           d: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """(new_text, expected dict, rationale). Raises _Unsafe."""
    lines = old_text.split("\n")
    expected = copy.deepcopy(spec)
    op = _find_op(expected, op_name)
    assert op is not None
    if kind == "correct_param_type":
        new_lines = _edit_param_type(lines, op_name, d["param"], d["old"], d["new"])
        next(p for p in op["params"] if p["name"] == d["param"])["type"] = d["new"]
        why = (f"{code} recurred {total} times; agents sent {d['param']} as {d['new']} but the spec says "
               f"{d['old']}. Change the declared type to {d['new']}.")
    elif kind == "add_error_code":
        rule = {"when": {"status": d["status"]}, "code": code, "retryable": d["retryable"],
                "recovery": d["recovery"],
                "meaning": f"Observed {total} times in agent handoffs; review the wording."}
        new_lines = _edit_add_error(lines, op_name, rule)
        op.setdefault("errors", []).append(rule)
        why = f"{code} (HTTP {d['status']}) recurred {total} times and is not declared; add an error rule."
    else:
        reason = (f"{code} recurred {total} times in agent handoffs ({d['outcome']}); "
                  "a person should review this operation")
        new_lines = _edit_needs_review(lines, op_name, reason)
        op["needs_review"] = True
        op["review_reasons"] = [reason]
        why = f"{code} recurred {total} times and no mechanical fix applies; flag the operation for review."
    return "\n".join(new_lines), expected, why


def _already_proposed(out_dir: Path, name: str) -> bool:
    return any((out_dir / sub / name).exists() for sub in DONE_DIRS)


def propose(failures_path: Path, specs_dir: Path, out_dir: Path, report: list[str] | None = None) -> list[Path]:
    """Write one proposal file per recurrent failure key; returns the files newly written. Never writes to specs_dir."""
    say = report.append if report is not None else (lambda _m: None)
    records = load_records(Path(failures_path))
    totals = recurrence_totals(records)
    written: list[Path] = []
    for key in sorted(totals):
        system, op_name, code = key
        total = totals[key]
        label = f"{system}/{op_name}/{code}"
        if total < MIN_RECURRENCE:
            say(f"skip {label}: recurrence {total} < {MIN_RECURRENCE}")
            continue
        spec_path = Path(specs_dir) / f"{system}.yaml"
        if not spec_path.exists():
            say(f"skip {label}: no spec for system {system!r}")
            continue
        raw = spec_path.read_bytes()
        old_text = raw.decode("utf-8").replace("\r\n", "\n")
        spec = yaml.safe_load(old_text)
        op = _find_op(spec, op_name) if isinstance(spec, dict) else None
        if op is None:
            say(f"skip {label}: operation not in spec")
            continue
        if op.get("needs_review") is True:
            say(f"skip {label}: operation already needs_review")
            continue
        recs = [r for r in records if (r["system"], r["operation"], r["error_code"]) == key]
        kind, details = _choose(op, spec, code, recs)
        name = f"{system}__{op_name}__{code}__{kind}.patch"
        if _already_proposed(Path(out_dir), name):
            say(f"skip {label}: {name} already exists (proposed, approved, rejected or applied)")
            continue
        try:
            new_text, expected, why = _build(old_text, spec, op_name, code, total, kind, details)
            if yaml.safe_load(new_text) != expected:
                raise _Unsafe("edited text does not parse to the intended change")
            validate_schema(expected)
            header = {"proposal": kind, "system": system, "operation": op_name, "error_code": code,
                      "recurrence": total, "spec": f"specs/{system}.yaml",
                      "base_sha256": patchfmt.sha256_bytes(raw), "rationale": why}
            text = patchfmt.make_proposal(header, old_text, new_text)
        except (_Unsafe, patchfmt.PatchError, yaml.YAMLError, Exception) as e:  # noqa: BLE001 - any failure means skip
            say(f"skip {label}: cannot patch safely ({type(e).__name__}: {e})")
            continue
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        target = Path(out_dir) / name
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)
        say(f"proposal {label}: {kind} -> {target}")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--failures", type=Path, default=ROOT / "learning" / "failures.jsonl")
    ap.add_argument("--specs", type=Path, default=ROOT / "specs")
    ap.add_argument("--out", type=Path, default=ROOT / "proposals")
    args = ap.parse_args()
    lines: list[str] = []
    files = propose(args.failures, args.specs, args.out, report=lines)
    for line in lines:
        print(line)
    print(f"{len(files)} proposal(s) written to {args.out}")


if __name__ == "__main__":
    main()
