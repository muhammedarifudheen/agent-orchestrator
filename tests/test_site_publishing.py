"""The docs deploy workflow is held to the brief's rules by a test, because it cannot be run here (CLAUDE.md D36).

There is no remote on this machine, so `.github/workflows/docs.yml` has never executed. What can be checked without running it is checked:
the drift test is a REQUIRED step (it runs with RUN_SITE=1, after the build and before the upload, with no way to ignore its failure), the deploy job
needs the build job and never runs for a pull request or off main, nothing in the workflow enables Pages or pushes or names a repository, and
docs/PUBLISHING.md is the two-line human step. `problems()` is shown to fail on eleven kinds of drift in the workflow, so a green result means
something. Does NOT prove: that the workflow runs on GitHub, that the pinned action versions still exist, or that Pages is enabled."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "docs.yml"


def triggers(wf: dict) -> dict:
    return wf.get("on", wf.get(True)) or {}  # YAML 1.1 reads the bare key `on` as the boolean True


def _find(steps: list[dict], pred) -> int | None:
    return next((i for i, s in enumerate(steps) if pred(s)), None)


def problems(wf: dict, raw: str) -> list[str]:
    out: list[str] = []
    on = triggers(wf)
    if "main" not in ((on.get("push") or {}).get("branches") or []):
        out.append("the workflow must run on push to main")
    if "pull_request" not in on:
        out.append("the workflow must also build and test pull requests")
    jobs = wf.get("jobs") or {}
    build, deploy = jobs.get("build") or {}, jobs.get("deploy") or {}
    steps = build.get("steps") or []
    i_build = _find(steps, lambda s: "npm run build" in str(s.get("run", "")))
    i_drift = _find(steps, lambda s: "tests/test_site_drift.py" in str(s.get("run", "")))
    i_upload = _find(steps, lambda s: str(s.get("uses", "")).startswith("actions/upload-pages-artifact"))
    if i_build is None:
        out.append("the build job has no `npm run build` step")
    if i_drift is None:
        out.append("the build job has no drift test step (tests/test_site_drift.py)")
    if i_upload is None:
        out.append("the build job has no upload-pages-artifact step")
    if None not in (i_build, i_drift, i_upload) and not i_build < i_drift < i_upload:
        out.append("the drift test step must come after the build and BEFORE the upload (so a failure stops the job before anything is deployed)")
    if i_drift is not None:
        step = steps[i_drift]
        run = str(step.get("run", ""))
        if str((step.get("env") or {}).get("RUN_SITE")) != "1":
            out.append("the drift test step must set RUN_SITE: \"1\" (otherwise the real check is skipped, not run)")
        if step.get("continue-on-error") not in (None, False):
            out.append("the drift test step must not set continue-on-error")
        if any(bad in run for bad in ("|| true", "||true", "exit 0", " -k ", "--deselect", "--ignore", "-p no:randomly")):
            out.append(f"the drift test command can be made to pass or skip: {run!r}")
    if build.get("continue-on-error") not in (None, False):
        out.append("the build job must not set continue-on-error")
    needs = deploy.get("needs")
    if "build" not in ([needs] if isinstance(needs, str) else needs or []):
        out.append("the deploy job must need the build job")
    cond = str(deploy.get("if", ""))
    if "pull_request" not in cond or "refs/heads/main" not in cond:
        out.append("the deploy job must not run for a pull request or off main (its `if` must say so)")
    if not any(str(s.get("uses", "")).startswith("actions/deploy-pages") for s in deploy.get("steps") or []):
        out.append("the deploy job must use actions/deploy-pages")
    if (deploy.get("permissions") or {}).get("pages") != "write":
        out.append("the deploy job needs `pages: write`")
    for s in steps:
        if str((s.get("with") or {}).get("enablement")).lower() == "true":
            out.append("a step enables Pages by itself: that is a one-time human action (docs/PUBLISHING.md)")
    for needle in ("git push", "gh api", "gh repo", "github.com/"):
        if needle in raw:
            out.append(f"the workflow must not contain {needle!r} (no pushing, no repository creation, no repository name)")
    base = next((str((s.get("env") or {}).get("SITE_BASE", "")) for s in steps if "npm run build" in str(s.get("run", ""))), "")
    if "steps.pages.outputs.base_path" not in base:
        out.append("SITE_BASE must come from the configure-pages output, not a literal repository name")
    if str((wf.get("env") or {}).get("ASTRO_TELEMETRY_DISABLED")) != "1":
        out.append("ASTRO_TELEMETRY_DISABLED must be \"1\" in the workflow")
    return out


RAW = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else ""
WF = yaml.safe_load(RAW) if RAW else {}


def test_the_workflow_exists_and_follows_the_rules():
    assert RAW, ".github/workflows/docs.yml is missing"
    assert problems(WF, RAW) == []


def test_the_workflows_dependency_files_exist():
    assert (ROOT / "site" / "package-lock.json").is_file(), "`npm ci` and the npm cache in the workflow need site/package-lock.json"
    assert (ROOT / "requirements.txt").is_file()


def _drop_drift(wf):
    wf["jobs"]["build"]["steps"] = [s for s in wf["jobs"]["build"]["steps"] if "test_site_drift" not in str(s.get("run", ""))]


def _drift_after_upload(wf):
    steps = wf["jobs"]["build"]["steps"]
    drift = next(s for s in steps if "test_site_drift" in str(s.get("run", "")))
    steps.remove(drift)
    steps.append(drift)


def _drift(wf):
    return next(s for s in wf["jobs"]["build"]["steps"] if "test_site_drift" in str(s.get("run", "")))


MUTATIONS = [
    ("the drift step is removed", _drop_drift, "no drift test step"),
    ("the drift step runs after the upload", _drift_after_upload, "BEFORE the upload"),
    ("the drift step cannot fail the job", lambda wf: _drift(wf).update({"continue-on-error": True}), "continue-on-error"),
    ("the drift command ends in || true", lambda wf: _drift(wf).update({"run": _drift(wf)["run"] + " || true"}), "made to pass or skip"),
    ("RUN_SITE is not set, so the real check would skip", lambda wf: _drift(wf).pop("env"), "RUN_SITE"),
    ("the deploy job does not need the build job", lambda wf: wf["jobs"]["deploy"].pop("needs"), "must need the build job"),
    ("the deploy job runs for pull requests", lambda wf: wf["jobs"]["deploy"].update({"if": "always()"}), "pull request or off main"),
    ("a step enables Pages by itself", lambda wf: wf["jobs"]["build"]["steps"][1].setdefault("with", {}).update({"enablement": True}), "enables Pages"),
    ("a step pushes", lambda wf: wf["jobs"]["build"]["steps"].append({"run": "git push origin HEAD"}), "git push"),
    ("the base path is a literal repository name", lambda wf: next(s for s in wf["jobs"]["build"]["steps"] if "npm run build" in str(s.get("run", ""))).update({"env": {"SITE_BASE": "/some-repo/"}}), "configure-pages output"),
    ("push to main is not a trigger", lambda wf: wf["on" if "on" in wf else True].pop("push"), "push to main"),
]


@pytest.mark.parametrize("label,edit,needle", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_drift_in_the_workflow_is_caught(label, edit, needle):
    assert problems(WF, RAW) == [], "the control: the real workflow follows the rules"
    wf = copy.deepcopy(WF)
    edit(wf)
    raw = RAW + ("\n# git push\n" if "pushes" in label else "")
    found = problems(wf, raw)
    assert any(needle in p for p in found), f"expected a problem mentioning {needle!r}, got {found}"


def test_publishing_md_is_the_two_line_human_step():
    text = (ROOT / "docs" / "PUBLISHING.md").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 2, lines
    assert "Settings" in lines[0] and "Pages" in lines[0] and "GitHub Actions" in lines[0]
    assert ".github/workflows/docs.yml" in lines[1] and "drift test" in lines[1] and "only if it passes" in lines[1]
    assert "github.com/" not in text, "no invented repository name"
