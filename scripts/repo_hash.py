"""The code a live-judgment record attests (CLAUDE.md D27).

A record that says "these tests passed against the real model" is only worth moving a row for if the code that passed is
still the code in the repo. So every record carries `repo_hash`, and scripts/move_audit_row.py refuses (CODE_CHANGED) if the
current code hashes differently, naming the files that differ.

THE FORMULA (deterministic, no timestamps, no absolute paths):
  1. For each subcommand there are ROOT FILES (the test files it runs, the production entry points it drives) and DATA GLOBS
     (files that are loaded at run time rather than imported: servers, specs, fixtures, scenario files, duties).
  2. The manifest is the import closure of the root files (a static `ast` scan; modules are resolved against the repo root,
     servers/, tests/, tests/scenarios/ and scripts/, the places pytest.ini and the harness put on sys.path; a package's
     `__init__.py` files are included) plus the files matching the data globs plus the harness itself (scripts/live_judgment.py,
     live_attest.py, live_attest_site/sitecustomize.py, repo_hash.py: they decide the verdict) plus pytest.ini.
  3. Each file is hashed as SHA-256 of its bytes with CRLF normalised to LF (a Windows checkout must not differ from a Linux one).
  4. repo_hash = SHA-256 of the text made of one line `<posix path relative to the repo>\\t<file sha256>\\n` per file, sorted by path.
The manifest (roots, globs, and path -> sha256) is stored beside the record as `<stamp>_<sub>.manifest.json`; the record holds its
path, its SHA-256 and `repo_hash`. Comparing later reports files that CHANGED, were DELETED, or were ADDED to the closure (a new import).

What it does not cover: the third-party packages in .venv, the Python version, and the remote services. It attests the repo's
own code, not the world it ran in.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
SEARCH_DIRS = ("", "servers", "tests", "tests/scenarios", "scripts")  # where an import is looked for, relative to the code root
HARNESS = ("scripts/live_judgment.py", "scripts/live_attest.py", "scripts/live_attest_site/sitecustomize.py",
           "scripts/repo_hash.py", "agent/provider_config.py")  # the last is loaded by path (it names the production host)
COMMON_DATA = ("pytest.ini",)
SKIP_PARTS = {"__pycache__", ".venv", ".git", ".perf"}
REL = "\0"  # marks a relative import inside the name set; NUL cannot appear in a file path (":" can, on Windows)

LIVE_API_FILES = ("tests/test_weather.py", "tests/test_jsonplaceholder.py", "tests/test_catfact.py", "tests/test_dogceo.py",
                  "tests/test_zippopotamus.py")

#: subcommand -> the files whose content its verdict depends on (roots are imported; data globs are loaded)
SUBCOMMAND_CODE: dict[str, dict[str, list[str]]] = {
    "planner": {
        "roots": ["tests/scenarios/test_planner_contract.py", "tests/scenarios/test_cross_industry_planners.py", "tests/conftest.py"],
        "data": ["scenarios/*.yaml", "servers/*.py", "specs/*.yaml", "specs/schema.json", "duties/*.yaml", "duties/schema.json"]},
    "doc": {
        "roots": ["tests/ingest/test_doc_contract.py", "tests/conftest.py", "pipeline.py", "tests/test_contract.py"],
        "data": ["tests/ingest/fixtures/*", "servers/*.py", "specs/*.yaml", "specs/schema.json"]},
    "apply": {
        "roots": ["pipeline.py", "learning/collect.py", "learning/propose.py", "learning/apply.py", "learning/patchfmt.py",
                  "agent/orchestrator.py", "agent/tool_registry.py", "tests/test_contract.py", "tests/conftest.py",
                  "servers/_http.py", "generators/from_spec.py"],
        "data": ["servers/*.py", "specs/schema.json"]},
    "live_apis": {
        "roots": [*LIVE_API_FILES, "tests/conftest.py"],
        "data": ["servers/*.py", "specs/*.yaml", "specs/schema.json"]},
}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _imported_names(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: resolve against the file's own package
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                names.add(f"{REL}{base}{REL}{node.module or ''}")  # REL cannot occur in a path, unlike ":" in "C:\..."
                names.update(f"{REL}{base}{REL}{(node.module + '.' if node.module else '')}{a.name}" for a in node.names)
            elif node.module:
                names.add(node.module)
                names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def _resolve(name: str, root: Path) -> list[Path]:
    """Files in the code root that `name` can be: dotted module -> .py or package __init__.py, plus parent packages."""
    if name.startswith(REL):
        _, base, dotted = name.split(REL, 2)
        candidates = [Path(base) / Path(*dotted.split(".")) if dotted else Path(base)]
    else:
        candidates = [root / d / Path(*name.split(".")) for d in SEARCH_DIRS]
    found: list[Path] = []
    for stem in candidates:
        for cand in (stem.with_suffix(".py"), stem / "__init__.py"):
            if cand.is_file():
                found.append(cand)
                parent = cand.parent
                while parent != root and root in parent.parents and (parent / "__init__.py").is_file():
                    found.append(parent / "__init__.py")
                    parent = parent.parent
    return found


def closure(roots: Iterable[Path], root: Path) -> set[Path]:
    seen: set[Path] = set()
    todo = [r for r in roots if r.is_file()]
    while todo:
        path = todo.pop()
        if path in seen or SKIP_PARTS & set(path.relative_to(root).parts):
            continue
        seen.add(path)
        for name in _imported_names(path):
            for target in _resolve(name, root):
                if target not in seen:
                    todo.append(target)
    return seen


def _expand(patterns: Iterable[str], root: Path) -> set[Path]:
    out: set[Path] = set()
    for pat in patterns:
        out.update(p for p in root.glob(pat) if p.is_file() and not SKIP_PARTS & set(p.relative_to(root).parts))
    return out


def manifest(roots: list[str], data: list[str], root: Path = ROOT, harness: Iterable[str] = HARNESS) -> dict[str, Any]:
    """{"roots", "data", "harness", "files": {posix path: sha256}} for a code root."""
    root = Path(root)
    files = closure([root / r for r in roots], root) | _expand([*data, *harness, *COMMON_DATA], root)
    return {"roots": list(roots), "data": list(data), "harness": list(harness),
            "files": {p.relative_to(root).as_posix(): file_sha256(p) for p in sorted(files)}}


def for_subcommand(sub: str, root: Path = ROOT) -> dict[str, Any]:
    spec = SUBCOMMAND_CODE[sub]
    return manifest(spec["roots"], spec["data"], root)


def digest(man: dict[str, Any]) -> str:
    text = "".join(f"{p}\t{h}\n" for p, h in sorted(man["files"].items()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compare(then: dict[str, Any], now: dict[str, Any]) -> dict[str, list[str]]:
    """Two manifests: which files changed, were deleted, or were added (a new file in the closure)."""
    a, b = then["files"], now["files"]
    return {"changed": sorted(p for p in a if p in b and b[p] != a[p]),
            "deleted": sorted(p for p in a if p not in b),
            "added": sorted(p for p in b if p not in a)}


def diff(recorded: dict[str, Any], root: Path = ROOT) -> dict[str, list[str]]:
    """How the code root differs NOW from the manifest a record attested."""
    return compare(recorded, manifest(recorded["roots"], recorded["data"], Path(root), recorded.get("harness", HARNESS)))


def write_sidecar(path: Path, man: dict[str, Any]) -> str:
    """Write the manifest beside a record; returns the file's SHA-256 (what the record stores)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(man, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()
