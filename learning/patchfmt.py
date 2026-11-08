"""The proposal file format shared by propose.py (writes) and apply.py (reads). Pure functions, no I/O policy.

A proposal is a text file `proposals/<name>.patch`: `# key: value` header lines, then a unified diff against
the spec. Standard patch tools ignore the header; apply.py reads it.

    # proposal: add_error_code | correct_param_type | mark_needs_review
    # system: weather
    # operation: get_current_weather
    # error_code: NOT_FOUND
    # recurrence: 2
    # spec: specs/weather.yaml
    # base_sha256: <sha256 of the spec's bytes when the proposal was made>
    # rationale: one line
    --- a/specs/weather.yaml
    +++ b/specs/weather.yaml
    @@ ... @@
"""
from __future__ import annotations

import difflib
import hashlib
import re
from typing import Any

PROPOSAL_TYPES = ("add_error_code", "correct_param_type", "mark_needs_review")
REQUIRED_HEADER = ("proposal", "system", "operation", "error_code", "recurrence", "spec", "base_sha256", "rationale")


class PatchError(ValueError):
    """The proposal is malformed, stale, or its diff does not apply to the file it names."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_proposal(header: dict[str, Any], old_text: str, new_text: str) -> str:
    """Header + unified diff (3 lines of context). `old_text`/`new_text` use LF endings; a missing final newline is added."""
    missing = [k for k in REQUIRED_HEADER if k not in header]
    if missing:
        raise PatchError(f"header missing {missing}")
    if header["proposal"] not in PROPOSAL_TYPES:
        raise PatchError(f"unknown proposal type {header['proposal']!r}")
    old_lines = (old_text if old_text.endswith("\n") else old_text + "\n").splitlines(keepends=True)
    new_lines = (new_text if new_text.endswith("\n") else new_text + "\n").splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{header['spec']}",
                                        tofile=f"b/{header['spec']}", n=3))
    if not diff:
        raise PatchError("old and new text are identical: nothing to propose")
    head = "".join(f"# {k}: {str(header[k]).splitlines()[0]}\n" for k in REQUIRED_HEADER)
    return head + diff


def parse_proposal(text: str) -> tuple[dict[str, str], str]:
    """(header, diff). Raises PatchError if the header is incomplete, has an unknown type, or there is no diff."""
    header: dict[str, str] = {}
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].startswith("--- "):
        m = re.match(r"# ([a-z0-9_]+): ?(.*)$", lines[i])
        if m:
            header[m.group(1)] = m.group(2)
        i += 1
    if i >= len(lines):
        raise PatchError("no diff found (expected a '--- a/<spec>' line)")
    missing = [k for k in REQUIRED_HEADER if k not in header]
    if missing:
        raise PatchError(f"header missing {missing}")
    if header["proposal"] not in PROPOSAL_TYPES:
        raise PatchError(f"unknown proposal type {header['proposal']!r}")
    return header, "\n".join(lines[i:])


def apply_diff(old_text: str, diff: str) -> str:
    """Apply a unified diff to `old_text` ('\n' endings). Context and removed lines must match EXACTLY at the
    position the hunk header names; otherwise PatchError (a stale or hand-damaged proposal is never guessed at)."""
    src = old_text.splitlines()
    out: list[str] = []
    pos = 0  # next unread index in src
    lines = diff.split("\n")
    i = 0
    while i < len(lines) and not lines[i].startswith("@@"):
        i += 1
    if i >= len(lines):
        raise PatchError("diff has no hunks")
    while i < len(lines):
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[i])
        if not m:
            if lines[i] == "":
                i += 1
                continue
            raise PatchError(f"unexpected line in diff: {lines[i]!r}")
        start = int(m.group(1)) - (1 if m.group(2) != "0" else 0)
        if start < pos:
            raise PatchError("overlapping or out-of-order hunks")
        out.extend(src[pos:start])
        pos = start
        i += 1
        while i < len(lines) and not lines[i].startswith("@@"):
            line = lines[i]
            if line.startswith("\\"):
                i += 1
                continue
            tag, body = line[:1], line[1:]
            if tag == " ":
                if pos >= len(src) or src[pos] != body:
                    raise PatchError(f"context mismatch at line {pos + 1}: expected {body!r}")
                out.append(body)
                pos += 1
            elif tag == "-":
                if pos >= len(src) or src[pos] != body:
                    raise PatchError(f"removed line mismatch at line {pos + 1}: expected {body!r}")
                pos += 1
            elif tag == "+":
                out.append(body)
            elif line == "" and i == len(lines) - 1:
                pass
            else:
                raise PatchError(f"bad diff line {line!r}")
            i += 1
    out.extend(src[pos:])
    return "\n".join(out) + "\n"
