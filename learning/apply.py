"""Apply HUMAN-APPROVED spec proposals, and nothing else.

    python -m learning.apply [--proposals proposals]

A proposal (see learning/patchfmt.py) is approved by a person moving it into `proposals/approved/`; moving it
into `proposals/rejected/` rejects it. Only `approved/*.patch` is ever acted on. Files left directly in
`proposals/` (waiting for approval) and in `rejected/` are never read for action and never cause a write or a commit.

For each approved proposal:
  1. refuse (reject) if the header does not name specs/<system>.yaml, the spec is missing, the proposal is stale
     (base_sha256 != sha256 of the spec's current bytes), the diff does not apply, or the patched spec is invalid;
  2. refuse a `mark_needs_review` proposal or any patched spec holding an operation with `needs_review: true`
     (CLAUDE.md D14: the generator refuses those; a person must review the operation and edit the spec by hand);
  3. stage the patched spec, generate server + tests under inbox/.staging/<system>-learn/, run pipeline._verify
     (the generated tests AND the shared tests/test_contract.py); red -> reject, nothing promoted;
  4. green -> promote: write the spec, regenerate servers/<system>_server.py and tests/test_<system>.py (they carry
     the GENERATED header, so overwriting them is allowed; they are never hand-edited), run _verify AGAIN on the
     real directories; red -> restore the original bytes exactly and reject;
  5. green -> `git add` exactly those three files + the moved proposal and commit. The commit happens only after the
     final green verify, and never on a reject.
Rejected proposals move to rejected/ with `<name>.reason.txt`; applied ones move to applied/.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

import pipeline
from generators.from_spec import SpecError, check_semantics, generate, validate_schema
from learning.patchfmt import PatchError, apply_diff, parse_proposal, sha256_bytes

NEEDS_REVIEW_REASON = ("needs_review: the generator refuses needs_review specs (CLAUDE.md D14), so this cannot be "
                       "applied automatically. A person must review the operation and edit the spec by hand "
                       "(set needs_review: false and name a reviewer in review.reviewer).")
SYSTEM_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class Result:
    proposal: str
    status: str  # "applied" | "rejected"
    reason: str = ""


def waiting(proposals_dir: Path) -> list[Path]:
    """Proposals still waiting for a human decision (directly in proposals_dir). Never acted on."""
    return sorted(proposals_dir.glob("*.patch")) if proposals_dir.is_dir() else []


def apply_approved(proposals_dir: Path, layout: pipeline.Layout, *, commit: bool = True) -> list[Result]:
    approved = proposals_dir / "approved"
    results: list[Result] = []
    if not approved.is_dir():
        return results
    for path in sorted(approved.glob("*.patch")):
        try:
            reason = _try_apply(path, proposals_dir, layout, commit)
        except Exception as exc:  # never leave a half state: an unexpected fault is a rejection with its reason
            reason = f"unexpected {type(exc).__name__}: {exc}"
        results.append(_finish(path, proposals_dir, reason))
    return results


def _finish(path: Path, proposals_dir: Path, reason: str | None) -> Result:
    """reason None = applied (file already moved by _try_apply unless it was a late commit note)."""
    if reason is None or reason.startswith("COMMIT-NOTE:"):
        return Result(path.name, "applied", (reason or "")[len("COMMIT-NOTE:"):].strip())
    rejected = proposals_dir / "rejected"
    rejected.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.move(str(path), str(rejected / path.name))
    (rejected / f"{path.name}.reason.txt").write_text(reason + "\n", encoding="utf-8")
    return Result(path.name, "rejected", reason)


def _try_apply(path: Path, proposals_dir: Path, layout: pipeline.Layout, commit: bool) -> str | None:
    """None when applied (proposal moved to applied/), else the rejection reason. Writes nothing before staging."""
    try:
        header, diff = parse_proposal(path.read_text(encoding="utf-8-sig"))
    except (PatchError, UnicodeDecodeError) as exc:
        return f"malformed proposal: {exc}"
    system = header["system"]
    if not SYSTEM_RE.fullmatch(system):
        return f"refused: system {system!r} is not a plain system name"
    if header["spec"] != f"specs/{system}.yaml":
        return f"refused: header spec {header['spec']!r} is not specs/{system}.yaml (no other path is ever written)"
    if header["proposal"] == "mark_needs_review":
        return NEEDS_REVIEW_REASON + " (proposal type mark_needs_review)"

    spec_path = layout.specs / f"{system}.yaml"
    if not spec_path.is_file():
        return f"refused: {spec_path.name} does not exist in {layout.specs}"
    original_spec = spec_path.read_bytes()
    if sha256_bytes(original_spec) != header["base_sha256"]:
        return "stale: the spec changed since the proposal was made (base_sha256 differs); regenerate the proposal"
    try:
        patched = apply_diff(original_spec.decode("utf-8").replace("\r\n", "\n"), diff)
    except (PatchError, UnicodeDecodeError) as exc:
        return f"the diff does not apply: {exc}"

    try:
        data = yaml.safe_load(patched)
        validate_schema(data)
    except (yaml.YAMLError, SpecError) as exc:
        return f"the patched spec is invalid: {exc}"
    flagged = [o["name"] for o in data["operations"] if o.get("needs_review")]
    if flagged:
        return f"{NEEDS_REVIEW_REASON} Operation(s) with needs_review: {', '.join(flagged)}"
    if data.get("system_name") != system:
        return f"refused: the patched spec names system {data.get('system_name')!r}, not {system!r}"
    try:
        check_semantics(data)
    except SpecError as exc:
        return f"the generator refuses the patched spec: {exc}"

    server_path = layout.servers / f"{system}_server.py"
    test_path = layout.tests / f"test_{system}.py"
    stage = layout.inbox / ".staging" / f"{system}-learn"
    shutil.rmtree(stage, ignore_errors=True)
    for sub in ("specs", "servers", "tests"):
        (stage / sub).mkdir(parents=True)
    originals = {p: (p.read_bytes() if p.exists() else None) for p in (spec_path, server_path, test_path)}
    try:
        staged_spec = stage / "specs" / f"{system}.yaml"
        staged_spec.write_text(patched, encoding="utf-8", newline="\n")
        generate(staged_spec, stage / "servers", stage / "tests")
        green, out = pipeline._verify(layout, system, stage / "servers", stage / "tests", stage / "specs")
        if not green:
            return "staged verification failed (nothing was promoted):\n" + out

        try:
            spec_path.write_text(patched, encoding="utf-8", newline="\n")
            generate(spec_path, layout.servers, layout.tests)
            green, out = pipeline._verify(layout, system, layout.servers, layout.tests, layout.specs)
        except BaseException:
            _restore(originals)
            raise
        if not green:
            _restore(originals)
            return "post-promotion verification failed (original spec, server and tests restored):\n" + out
    except SpecError as exc:
        _restore(originals)
        return f"generator refused the patched spec: {exc}"
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    applied = proposals_dir / "applied"
    applied.mkdir(parents=True, exist_ok=True)
    dest = applied / path.name
    shutil.move(str(path), str(dest))
    if commit:
        note = _commit(layout, [spec_path, server_path, test_path], path, dest, f"learn: apply {path.name} to {system}")
        if note:
            return "COMMIT-NOTE: " + note
    return None


def _restore(originals: dict[Path, bytes | None]) -> None:
    for p, data in originals.items():
        if data is None:
            p.unlink(missing_ok=True)
        else:
            p.write_bytes(data)


def _git(root: Path, *args: str, config: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *(config or []), "-C", str(root), *args], capture_output=True, text=True)


def _commit(layout: pipeline.Layout, files: list[Path], old: Path, new: Path, message: str) -> str:
    """Commit exactly `files` + the moved proposal. Returns "" on success or when not in a git work tree."""
    root = layout.root
    try:
        inside = _git(root, "rev-parse", "--is-inside-work-tree")
    except OSError:
        return ""
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return ""
    top = Path(_git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    paths: list[Path] = []
    for p in [*files, new]:
        try:
            p.resolve().relative_to(top)
        except ValueError:
            continue
        paths.append(p)
    try:
        old.resolve().relative_to(top)
        if _git(root, "ls-files", "--error-unmatch", "--", str(old)).returncode == 0:
            paths.append(old)  # tracked: stage its deletion too
    except ValueError:
        pass
    specs = [str(p) for p in paths]
    added = _git(root, "add", "-A", "--", *specs)
    if added.returncode != 0:
        return f"applied, but git add failed: {added.stderr.strip()}"
    config: list[str] = []
    if not _git(root, "config", "user.name").stdout.strip():
        config += ["-c", "user.name=learning-apply"]
    if not _git(root, "config", "user.email").stdout.strip():
        config += ["-c", "user.email=learning-apply@localhost"]
    done = _git(root, "commit", "-m", message, "--", *specs, config=config)
    if done.returncode != 0:
        return f"applied, but git commit failed: {(done.stderr or done.stdout).strip()}"
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proposals", type=Path, default=Path("proposals"))
    args = ap.parse_args(argv)
    layout = pipeline.Layout()
    results = apply_approved(args.proposals, layout)
    for r in results:
        print(f"[{r.status.upper():8s}] {r.proposal}" + (f": {r.reason.splitlines()[0]}" if r.reason else ""))
    wait = waiting(args.proposals)
    print(f"{len(results)} approved proposal(s) processed; {len(wait)} waiting for approval "
          "(move into proposals/approved/ to approve, proposals/rejected/ to reject)")
    return 1 if any(r.status == "rejected" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
