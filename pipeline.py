"""Inbox pipeline: drop an OpenAPI document, pasted API docs, or recorded HTTP traffic into inbox/ and
get a working MCP server that passes the same contract test as every other server.

    python pipeline.py --once            # process what is in inbox/ now, then exit
    python pipeline.py                   # keep watching inbox/

Flow per file:  detect type -> ingestor (DRAFT spec) -> schema validation (the existing validator)
-> REVIEW GATE -> stage -> generate (generators/from_spec.py, unchanged except for the shared
validator) -> run the generated tests + tests/test_contract.py on the staged spec -> promote -> re-run.

Outcomes:  inbox/done/ (generated)   inbox/review/ (blocked: a person must decide)   inbox/failed/ (+ .reason.txt)

The review gate is the point of this module. An operation blocks generation when it is flagged
`needs_review`, or when its flag was cleared without a named reviewer although it was flagged for a reason,
had confidence below ingest.common.CONFIDENCE_THRESHOLD (decision D12), or is a write.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from generators.from_spec import ROOT, SpecError, generate, validate_schema
from ingest.common import CONFIDENCE_THRESHOLD
from ingest.doc import UNVERIFIED_SOURCE, DocNoToolCall

KINDS = ("openapi", "trace", "doc", "spec")


@dataclass
class Layout:
    """Where things live. Tests pass a temporary layout; the default is this project."""

    root: Path = ROOT
    inbox: Path | None = None
    specs: Path | None = None
    servers: Path | None = None
    tests: Path | None = None

    def __post_init__(self) -> None:
        self.inbox = self.inbox or self.root / "inbox"
        self.specs = self.specs or self.root / "specs"
        self.servers = self.servers or self.root / "servers"
        self.tests = self.tests or self.root / "tests"


@dataclass
class Outcome:
    status: str  # "generated" | "review" | "failed"
    source: Path
    kind: str | None = None
    name: str | None = None
    detail: str = ""
    tools: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    review_file: Path | None = None
    warnings: list[str] = field(default_factory=list)  # printed by the CLI next to the result


# -- detection ---------------------------------------------------------------------------------

def _load_structured(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))  # YAML is a superset of JSON


def detect_type(path: Path) -> str:
    """One of KINDS, or raise ValueError("cannot detect ...")."""
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt", ".rst"):
        return "doc"
    if suffix == ".har":
        return "trace"
    if suffix in (".json", ".yaml", ".yml"):
        data = _load_structured(path)
        if isinstance(data, dict):
            if "openapi" in data or "swagger" in data:
                return "openapi"
            if isinstance(data.get("log"), dict) and "entries" in data["log"]:
                return "trace"
            if "system_name" in data and "operations" in data:
                return "spec"
        if isinstance(data, list) and data and all(isinstance(e, dict) and "request" in e and "response" in e
                                                   for e in data):
            return "trace"
    raise ValueError(f"cannot detect the input type of {path.name} (expected OpenAPI/Swagger, HAR or "
                     "request/response pairs, API documentation text, or a finished spec)")


def run_ingestor(kind: str, path: Path, doc_client: Any = None, notes: list[str] | None = None) -> dict[str, Any]:
    """Route to the ingestor for `kind`; returns a draft spec dict. `notes` collects visible warnings."""
    if kind == "openapi":
        from ingest import openapi
        return openapi.ingest(openapi.load(path))
    if kind == "trace":
        from ingest import trace
        return trace.ingest(trace.load(path))
    if kind == "doc":
        from ingest import doc
        text = path.read_text(encoding="utf-8-sig")
        try:
            return doc.ingest(text, client=doc_client)
        except DocNoToolCall as first:  # explicit, recoverable, retried ONCE with a stricter prompt, and reported
            if notes is not None:
                notes.append(f"RETRIED once with a stricter prompt: the model did not call the extraction tool ({first})")
            return doc.ingest(text, client=doc_client, strict=True)
    if kind == "spec":
        return _load_structured(path)
    raise ValueError(kind)


# -- review gate -------------------------------------------------------------------------------

def review_findings(spec: dict[str, Any]) -> list[str]:
    """Everything that must be decided by a person before generation. Empty list = nothing blocks."""
    findings: list[str] = []
    for op in spec.get("operations", []):
        name = op["name"]
        confidence = op.get("confidence")
        reasons = list(op.get("review_reasons") or [])
        reviewer = (op.get("review") or {}).get("reviewer")
        if op.get("needs_review"):
            why = "; ".join(reasons) or "flagged by the ingestor"
            findings.append(f"{name} (confidence {confidence}): needs_review is true: {why}")
            continue
        must_name_reviewer = bool(reasons) or op.get("side_effect") != "read" or (
            confidence is not None and confidence < CONFIDENCE_THRESHOLD)
        if must_name_reviewer and not reviewer:
            findings.append(f"{name} (confidence {confidence}): the review flag was cleared but no reviewer is named "
                            "(`review.reviewer`), although this operation was flagged "
                            f"({'; '.join(reasons) or 'low confidence or a write'})")
        if op.get("side_effect") != "read" and (op.get("review") or {}).get("safety_fields") != "reviewed":
            findings.append(f"{name}: a {op.get('side_effect')} operation needs review.safety_fields: reviewed")
    return findings


def _write_review_files(layout: Layout, spec: dict[str, Any], source: Path, kind: str, findings: list[str]) -> Path:
    review_dir = layout.inbox / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    name = spec["system_name"]
    draft = review_dir / f"{name}.spec.yaml"
    draft.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    flagged = [o for o in spec["operations"] if o.get("needs_review")]
    rows = "\n".join(f"| `{o['name']}` | {o['request']['method']} `{o['request']['path']}` | {o.get('confidence')} | "
                     f"{'; '.join(o.get('review_reasons') or []) or '(flag set)'} |" for o in flagged)
    note = review_dir / f"{name}.review.md"
    note.write_text(f"""# Review required: `{name}`

Source: `{source.name}` (detected as **{kind}**). Generation was **blocked**: {len(findings)} finding(s) across
{len(spec['operations'])} operation(s). Nothing was written to `servers/`, `specs/` or `tests/`.

| operation | request | confidence | why it is flagged |
|---|---|---|---|
{rows}

## Findings
{chr(10).join('- ' + f for f in findings)}

## How to approve
1. Open `{draft.name}` (next to this file) and correct anything that is wrong.
2. For each flagged operation set `needs_review: false` and name yourself: `review: {{source: <unchanged>, safety_fields: reviewed, reviewer: <your name>}}`.
   For a write also confirm `side_effect` (read | write | money) and `idempotent`.
3. Move `{draft.name}` back into `inbox/`. The pipeline validates, generates and tests it.

The confidence threshold ({CONFIDENCE_THRESHOLD}) is a recorded decision (CLAUDE.md D12). Do not change it to get past
this review.
""", encoding="utf-8")
    return note


# -- helpers -----------------------------------------------------------------------------------

def _fail(layout: Layout, source: Path, kind: str | None, name: str | None, reason: str) -> Outcome:
    failed = layout.inbox / "failed"
    failed.mkdir(parents=True, exist_ok=True)
    (failed / f"{source.stem}.reason.txt").write_text(reason + "\n", encoding="utf-8")
    if source.exists():
        shutil.move(str(source), str(failed / source.name))
    return Outcome("failed", source, kind, name, reason)


def _pytest(layout: Layout, args: list[str], servers_dirs: list[Path], spec_dir: Path) -> tuple[bool, str]:
    """Run pytest in a subprocess against the project's pytest.ini. Returns (green, tail of output)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "offline_guard")] + [str(p) for p in servers_dirs]
                                        + [str(ROOT / "servers"), str(ROOT)])  # guard first: gates run offline
    env["CONTRACT_SPEC_DIRS"] = str(spec_dir)
    env.pop("LIVE_TESTS", None)  # gates run offline; live checks are a separate, explicit step
    # `-o pythonpath=.` keeps pytest.ini from putting the real servers/ ahead of the staged one (PYTHONPATH order rules)
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ROOT / "pytest.ini"),
           "-o", "pythonpath=.", *args]
    done = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-25:])
    return done.returncode == 0, tail


def _verify(layout: Layout, name: str, servers_dir: Path, tests_dir: Path, spec_dir: Path) -> tuple[bool, str]:
    """The two gates: the server's own generated tests, then the shared tests/test_contract.py (same file)."""
    ok, out = _pytest(layout, [str(tests_dir / f"test_{name}.py")], [servers_dir], spec_dir)
    if not ok:
        return False, f"generated tests failed:\n{out}"
    ok, out = _pytest(layout, [str(ROOT / "tests" / "test_contract.py"), "-k", name], [servers_dir], spec_dir)
    if not ok:
        return False, f"tests/test_contract.py failed for {name}:\n{out}"
    return True, out


# -- the pipeline ------------------------------------------------------------------------------

def process(source: Path, layout: Layout | None = None, doc_client: Any = None) -> Outcome:
    """Run one inbox file through the whole pipeline. Never raises: every failure is an Outcome."""
    warnings: list[str] = []
    outcome = _process(source, layout or Layout(), doc_client, warnings)
    outcome.warnings = warnings
    return outcome


def _process(source: Path, layout: Layout, doc_client: Any, warnings: list[str]) -> Outcome:
    kind = name = None
    try:
        kind = detect_type(source)
        spec = run_ingestor(kind, source, doc_client, warnings)
    except Exception as exc:  # ingestors raise IngestError; anything else is still a reportable failure
        return _fail(layout, source, kind, None, f"{kind or 'detect'}: {type(exc).__name__}: {exc}")

    try:
        validate_schema(spec)  # the existing validator (specs/schema.json)
    except SpecError as exc:
        return _fail(layout, source, kind, None, f"the {kind} ingestor produced an invalid spec:\n{exc}")
    name = spec["system_name"]
    if spec.get("source") == UNVERIFIED_SOURCE:
        warnings.append(f"{name} was extracted from documentation by an LLM and is UNVERIFIED ({UNVERIFIED_SOURCE}): "
                        "no real-model run has checked this path; read the spec before relying on it")

    findings = review_findings(spec)
    if findings:  # REVIEW GATE: nothing is generated
        note = _write_review_files(layout, spec, source, kind, findings)
        review_dir = layout.inbox / "review"
        if source.exists() and source.parent == layout.inbox:
            shutil.move(str(source), str(review_dir / source.name))
        return Outcome("review", source, kind, name, f"{len(findings)} finding(s); see {note}", findings=findings,
                       review_file=note)

    if (layout.servers / f"{name}_server.py").exists() or (layout.specs / f"{name}.yaml").exists():
        return _fail(layout, source, kind, name, f"a server or spec named '{name}' already exists; refusing to "
                     "overwrite (rename system_name or remove the old one)")

    stage = layout.inbox / ".staging" / name
    shutil.rmtree(stage, ignore_errors=True)
    for sub in ("specs", "servers", "tests"):
        (stage / sub).mkdir(parents=True)
    promoted: list[Path] = []
    try:
        staged_spec = stage / "specs" / f"{name}.yaml"
        staged_spec.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
        generate(staged_spec, stage / "servers", stage / "tests")
        green, out = _verify(layout, name, stage / "servers", stage / "tests", stage / "specs")
        if not green:
            return _fail(layout, source, kind, name, "staged verification failed (nothing was promoted): " + out)

        # promote: spec first, then regenerate from the promoted spec so headers/hash match the real path
        layout.specs.mkdir(parents=True, exist_ok=True)
        real_spec = layout.specs / f"{name}.yaml"
        shutil.copy2(staged_spec, real_spec)
        promoted.append(real_spec)
        generate(real_spec, layout.servers, layout.tests)
        promoted += [layout.servers / f"{name}_server.py", layout.tests / f"test_{name}.py"]
        green, out = _verify(layout, name, layout.servers, layout.tests, layout.specs)
        if not green:
            for p in promoted:
                p.unlink(missing_ok=True)
            return _fail(layout, source, kind, name, "post-promotion verification failed (rolled back): " + out)
    except SpecError as exc:
        for p in promoted:
            p.unlink(missing_ok=True)
        return _fail(layout, source, kind, name, f"generator refused the spec: {exc}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    done = layout.inbox / "done"
    done.mkdir(parents=True, exist_ok=True)
    if source.exists():
        shutil.move(str(source), str(done / source.name))
    tools = [f"{name}__{op['name']}" for op in spec["operations"]]
    return Outcome("generated", source, kind, name, f"{len(tools)} tools", tools=tools)


def pending(layout: Layout, settle_s: float = 1.0) -> list[Path]:
    """Top-level inbox files that are finished being written.

    With settle_s <= 0 there is NO age check. (A age check of ``now - mtime >= 0`` is wrong: on Windows the wall clock
    ticks every ~15.6 ms, so a freshly written file's mtime is ahead of time.time() about 15% of the time and the
    file would be skipped. CLAUDE.md D17.)"""
    if not layout.inbox.is_dir():
        return []
    now = time.time()
    return sorted(p for p in layout.inbox.iterdir()
                  if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".reason.txt")
                  and (settle_s <= 0 or now - p.stat().st_mtime >= settle_s))


def watch(layout: Layout | None = None, interval: float = 2.0, once: bool = False, doc_client: Any = None) -> list[Outcome]:
    layout = layout or Layout()
    outcomes: list[Outcome] = []
    while True:
        for path in pending(layout, settle_s=0 if once else 1.0):
            outcome = process(path, layout, doc_client)
            outcomes.append(outcome)
            print(f"[{outcome.status.upper():9s}] {path.name} ({outcome.kind}) -> {outcome.name}: {outcome.detail.splitlines()[0] if outcome.detail else ''}")
            for tool in outcome.tools:
                print(f"    {tool}")
            for warning in outcome.warnings:
                print(f"    WARNING: {warning}")
            for finding in outcome.findings:
                print(f"    REVIEW: {finding}")
        if once:
            return outcomes
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="process the current inbox contents and exit")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args(argv)
    outcomes = watch(once=args.once, interval=args.interval)
    return 1 if any(o.status == "failed" for o in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
