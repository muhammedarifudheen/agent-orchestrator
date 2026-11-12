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

Duty proposals (CLAUDE.md D23 (3)) follow the same shape under `proposals/duties/`: `apply_approved_duties` acts only on
`proposals/duties/approved/*.duty.yaml`, runs the generated contract test on a staged copy, installs `duties/<name>.yaml` and
`tests/duties/test_duty_<name>.py` only when it is green, verifies the installed files, and commits exactly those files.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

import pipeline
from generators.from_spec import SpecError, check_semantics, generate, validate_schema
from learning.patchfmt import PatchError, apply_diff, parse_proposal, sha256_bytes

NEEDS_REVIEW_REASON = ("needs_review: the generator refuses needs_review specs (CLAUDE.md D14), so this cannot be "
                       "applied automatically. A person must review the operation and edit the spec by hand "
                       "(set needs_review: false and name a reviewer in review.reviewer).")
ROLLED_BACK = "ROLLED-BACK:"
SYSTEM_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class Result:
    proposal: str
    status: str  # "applied" | "rejected" | "rolled_back" (post-promotion verify went red; proposal stays in approved/)
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
    if reason.startswith(ROLLED_BACK):
        # Staged verify was green and the promoted one red: the verification itself is inconsistent, which is not a
        # verdict on the proposal. Files are restored; the proposal STAYS approved (a person decides what next).
        note = reason[len(ROLLED_BACK):].strip()
        path.with_name(path.name + ".reason.txt").write_text(note + "\n", encoding="utf-8")
        return Result(path.name, "rolled_back", note)
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
            return (ROLLED_BACK + " post-promotion verification failed (original spec, server and tests restored; "
                    "the proposal stays in approved/):\n" + out)
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


def _commit(layout: pipeline.Layout, files: list[Path], old: Path | list[Path], new: Path | list[Path],
            message: str) -> str:
    """Commit exactly `files` + the moved proposal file(s). Returns "" on success or when not in a git work tree."""
    olds = old if isinstance(old, list) else [old]
    news = new if isinstance(new, list) else [new]
    root = layout.root
    try:
        inside = _git(root, "rev-parse", "--is-inside-work-tree")
    except OSError:
        return ""
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return ""
    top = Path(_git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    paths: list[Path] = []
    for p in [*files, *news]:
        try:
            p.resolve().relative_to(top)
        except ValueError:
            continue
        paths.append(p)
    for old in olds:
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


# -- duty proposals (CLAUDE.md D23 (3)) ---------------------------------------------------------------------------------

DUTY_SUFFIX = ".duty.yaml"
DUTY_SIBLINGS = (".duty.diff", ".contract_test.py", ".evidence.json")


def waiting_duties(proposals_dir: Path) -> list[Path]:
    """Duty proposals still waiting for a human decision (directly in proposals/duties/). Never acted on."""
    folder = proposals_dir / "duties"
    return sorted(folder.glob("*" + DUTY_SUFFIX)) if folder.is_dir() else []


def _run_contract_test(layout: pipeline.Layout, test_path: Path, duties_dir: Path | None) -> tuple[bool, str]:
    """Run one generated contract test in a pytest subprocess (cwd = the project root). `duties_dir` set: the test reads the
    duty from there (DUTIES_DIR, staging); None: it reads the installed duties/ (the variable is removed)."""
    env = {k: v for k, v in os.environ.items() if k != "DUTIES_DIR"}
    if duties_dir is not None:
        env["DUTIES_DIR"] = str(duties_dir)
    done = subprocess.run([sys.executable, "-m", "pytest", str(test_path), "-q", "-p", "no:cacheprovider"],
                          cwd=layout.root, env=env, capture_output=True, text=True, timeout=600)
    return done.returncode == 0, (done.stdout + done.stderr)[-4000:]


def apply_approved_duties(proposals_dir: Path, layout: pipeline.Layout, *, commit: bool = True) -> list[Result]:
    """Act ONLY on `<proposals_dir>/duties/approved/*.duty.yaml`; see the module docstring and D23 (3)."""
    approved = proposals_dir / "duties" / "approved"
    results: list[Result] = []
    if not approved.is_dir():
        return results
    for path in sorted(approved.glob("*" + DUTY_SUFFIX)):
        try:
            reason = _try_apply_duty(path, proposals_dir, layout, commit)
        except Exception as exc:
            reason = f"unexpected {type(exc).__name__}: {exc}"
        results.append(_finish_duty(path, proposals_dir, reason))
    return results


def _finish_duty(path: Path, proposals_dir: Path, reason: str | None) -> Result:
    base = path.name[: -len(DUTY_SUFFIX)]
    if reason is None or reason.startswith("COMMIT-NOTE:"):
        return Result(path.name, "applied", (reason or "")[len("COMMIT-NOTE:"):].strip())
    if reason.startswith(ROLLED_BACK):
        note = reason[len(ROLLED_BACK):].strip()
        path.with_name(f"{base}.reason.txt").write_text(note + "\n", encoding="utf-8")
        return Result(path.name, "rolled_back", note)
    rejected = proposals_dir / "duties" / "rejected"
    rejected.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.move(str(path), str(rejected / path.name))
    (rejected / f"{base}.reason.txt").write_text(reason + "\n", encoding="utf-8")
    return Result(path.name, "rejected", reason)


def _cleanup_test(test_path: Path, created_dir: bool) -> None:
    test_path.unlink(missing_ok=True)
    cache = test_path.parent / "__pycache__"
    for pyc in cache.glob(f"{test_path.stem}.*") if cache.is_dir() else []:
        pyc.unlink(missing_ok=True)
    for folder in (cache, test_path.parent) if created_dir else (cache,):
        try:
            folder.rmdir()
        except OSError:
            pass


def _try_apply_duty(path: Path, proposals_dir: Path, layout: pipeline.Layout, commit: bool) -> str | None:
    """None when installed (proposal moved to duties/applied/), else the rejection reason. Writes nothing before staging."""
    from agent.duties import DutyError, parse_duty, validate_duty_data
    from learning.induce_duty import UNREVIEWED, contract_test_source

    duties_dir = layout.root / "duties"
    try:
        raw = path.read_bytes()
        data = yaml.safe_load(raw.decode("utf-8-sig"))
        validate_duty_data(data, duties_dir)
        duty = parse_duty(data)
    except (yaml.YAMLError, UnicodeDecodeError, DutyError, OSError) as exc:
        return f"the duty does not load: {exc}"
    name = duty.name
    if path.name != name + DUTY_SUFFIX:
        return f"refused: the file is named {path.name!r} but the duty is {name!r} (expected {name + DUTY_SUFFIX!r})"
    reviewer = duty.reviewer.strip()
    if not reviewer or reviewer == UNREVIEWED:
        return f"refused: review.reviewer is {duty.reviewer!r}; a named person must review the duty and put their name there"
    duty_path = duties_dir / f"{name}.yaml"
    test_dir = layout.tests / "duties"
    test_path = test_dir / f"test_duty_{name}.py"
    if duty_path.exists() or test_path.exists():
        return f"refused: {duty_path.name if duty_path.exists() else test_path.name} already exists (a duty is never overwritten)"

    created_dir = not test_dir.exists()
    stage = Path(tempfile.mkdtemp(prefix=f"duty-{name}-"))
    try:
        shutil.copyfile(duties_dir / "schema.json", stage / "schema.json")
        (stage / f"{name}.yaml").write_bytes(raw)
        test_dir.mkdir(parents=True, exist_ok=True)
        test_path.write_text(contract_test_source(name), encoding="utf-8", newline="\n")
        try:
            green, out = _run_contract_test(layout, test_path, stage)
        except BaseException:
            _cleanup_test(test_path, created_dir)
            raise
        if not green:
            _cleanup_test(test_path, created_dir)
            return "the generated contract test is red on the staged duty (nothing was installed):\n" + out
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    try:
        duty_path.write_bytes(raw)
        green, out = _run_contract_test(layout, test_path, None)
    except BaseException:
        duty_path.unlink(missing_ok=True)
        _cleanup_test(test_path, created_dir)
        raise
    if not green:
        duty_path.unlink(missing_ok=True)
        _cleanup_test(test_path, created_dir)
        return (ROLLED_BACK + " post-install verification failed (duties/%s.yaml and the generated test were removed; "
                "the proposal stays in approved/):\n" % name + out)

    applied = proposals_dir / "duties" / "applied"
    applied.mkdir(parents=True, exist_ok=True)
    olds, news = [], []
    for suffix in (DUTY_SUFFIX, *DUTY_SIBLINGS):
        for folder in (path.parent, proposals_dir / "duties"):
            src = folder / f"{name}{suffix}"
            if src.exists():
                shutil.move(str(src), str(applied / src.name))
                olds.append(src)
                news.append(applied / src.name)
                break
    path.with_name(f"{name}.reason.txt").unlink(missing_ok=True)  # a stale rolled_back note
    if commit:
        note = _commit(layout, [duty_path, test_path], olds, news, f"learn: install duty {name} (reviewed by {reviewer})")
        if note:
            return "COMMIT-NOTE: " + note
    return None


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
    duty_results = apply_approved_duties(args.proposals, layout)
    for r in duty_results:
        print(f"[DUTY {r.status.upper():11s}] {r.proposal}" + (f": {r.reason.splitlines()[0]}" if r.reason else ""))
    print(f"{len(duty_results)} approved duty proposal(s) processed; {len(waiting_duties(args.proposals))} duty proposal(s) "
          "waiting (move into proposals/duties/approved/ to approve, proposals/duties/rejected/ to reject)")
    failed = any(r.status == "rejected" for r in [*results, *duty_results])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
