"""The files that deploy the minimal API (Dockerfile, .dockerignore, .github/workflows/api.yml) are held to the rules of the brief (CLAUDE.md D39):

  * the image is python:3.12-slim, installs api/requirements.txt, copies an explicit list (no tests, no site, no .git), runs as a non-root user,
    and starts `uvicorn api.app:app --host 0.0.0.0 --port $PORT`;
  * NO model key anywhere in them: not in the image, not in the workflow, not as an env var, a secret or a variable;
  * the workflow tests on every push to main and every pull request (tests/test_api_flows.py), deploys only from a push to main, `needs` the tests,
    cannot ignore a failing test, fails loudly when the deploy credential is missing, pins every action to a full commit SHA with a version comment
    and the Railway CLI to an exact version, and smoke-tests /healthz for the three flows after a deploy when API_URL is set;
  * no railway.json: Railway's config-as-code is deprecated (working only until 2026-12-01), and a Dockerfile alone is enough to build.
`problems()` is shown to fail on each kind of edit below. Does NOT prove: that a deploy works. None has run (AUDIT.md 3a C70): this needs a Railway
account, which is the owner's to create."""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

from test_site_publishing import SHA_PINNED, USES, VERSION_COMMENT

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
WORKFLOW = ROOT / ".github" / "workflows" / "api.yml"
COPY_ALLOWED = {"api/requirements.txt", "api/", "demo/", "agent/", "servers/", "learning/", "generators/", "duties/", "specs/"}
NO_KEYS = re.compile(r"ANTHROPIC|PROVIDER_|OPENAI|_API_KEY", re.I)
EXPECTED_ACTIONS = {"actions/checkout", "actions/setup-python", "actions/setup-node"}


def dockerfile_problems(text: str) -> list[str]:
    out = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines or lines[0] != "FROM python:3.12-slim":
        out.append("the base image must be python:3.12-slim")
    if "RUN pip install -r api/requirements.txt" not in lines:
        out.append("the image must install api/requirements.txt")
    copies = [ln.split()[1] for ln in lines if ln.startswith("COPY ")]
    for src in copies:
        if src not in COPY_ALLOWED:
            out.append(f"COPY {src}: not on the allow-list {sorted(COPY_ALLOWED)} (no tests, site, .git or local state in the image)")
    if not any(ln.startswith("USER ") and ln.split()[1] not in ("root", "0") for ln in lines):
        out.append("the image must run as a non-root USER")
    cmd = next((ln for ln in lines if ln.startswith("CMD ")), "")
    if not all(part in cmd for part in ("uvicorn api.app:app", "--host 0.0.0.0", "--port ${PORT}")):
        out.append("CMD must start `uvicorn api.app:app --host 0.0.0.0 --port ${PORT}` (the host supplies PORT)")
    if NO_KEYS.search("\n".join(lines)):
        out.append("the image must not mention a model key or provider variable")
    return out


def dockerignore_problems(text: str) -> list[str]:
    have = {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")}
    return [f".dockerignore must exclude {need}" for need in (".git", ".venv", ".perf", "tests", "site") if need not in have]


def workflow_problems(raw: str) -> list[str]:
    out, wf = [], yaml.safe_load(raw)
    on = wf.get("on", wf.get(True)) or {}
    if "main" not in ((on.get("push") or {}).get("branches") or []):
        out.append("the workflow must run on push to main")
    if "pull_request" not in on:
        out.append("the workflow must also run on pull requests")
    jobs = wf.get("jobs") or {}
    test, deploy = jobs.get("test") or {}, jobs.get("deploy") or {}
    run_all = "\n".join(str(s.get("run", "")) for s in test.get("steps") or [])
    if "tests/test_api_flows.py" not in run_all or "pip install -r api/requirements.txt" not in run_all:
        out.append("the test job must install api/requirements.txt and run tests/test_api_flows.py")
    if "continue-on-error" in raw or "|| true" in raw or "exit 0" in raw:
        out.append("no step may be made to pass whatever happens (continue-on-error, || true, exit 0)")
    needs = deploy.get("needs")
    if "test" not in ([needs] if isinstance(needs, str) else needs or []):
        out.append("the deploy job must need the test job")
    cond = str(deploy.get("if", ""))
    if "github.event_name == 'push'" not in cond or "github.ref == 'refs/heads/main'" not in cond:
        out.append("the deploy job must run only for a push to main (its `if` must say event_name == 'push' and ref == 'refs/heads/main')")
    steps = deploy.get("steps") or []
    first = str((steps[0] if steps else {}).get("run", ""))
    if "RAILWAY_TOKEN" not in first or "exit 1" not in first:
        out.append("the deploy job's first step must fail loudly when RAILWAY_TOKEN is missing")
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    if not re.search(r"npm install --global @railway/cli@\d+\.\d+\.\d+\b", runs):
        out.append("the Railway CLI must be pinned to an exact version")
    if "railway up --ci" not in runs:
        out.append("the deploy step must run `railway up --ci`")
    if "/healthz" not in runs or '"flows": ["duty-match", "write-check", "propose"]' not in runs or "planner" not in runs:
        out.append("the smoke test must check /healthz for the three flows, with no planner")
    if NO_KEYS.search(raw.replace("RAILWAY_TOKEN", "")):
        out.append("the workflow must not mention a model key or provider variable (no key may be configured on the host)")
    for needle in ("secrets.ANTHROPIC", "vars.ANTHROPIC"):
        if needle in raw:
            out.append(f"{needle}: no model key, secret or variable")
    seen = set()
    for n, line in enumerate(raw.splitlines(), 1):
        m = USES.match(line)
        if m:
            seen.add(m["ref"].split("@")[0])
            if not SHA_PINNED.match(m["ref"]):
                out.append(f"line {n}: {m['ref']} is not pinned to a full 40-hex commit SHA")
            elif not VERSION_COMMENT.match(m["rest"]):
                out.append(f"line {n}: {m['ref']} has no version comment")
    if seen != EXPECTED_ACTIONS:
        out.append(f"the workflow uses {sorted(seen)}, expected {sorted(EXPECTED_ACTIONS)}")
    return out


DOCKER = DOCKERFILE.read_text(encoding="utf-8") if DOCKERFILE.is_file() else ""
IGNORE = DOCKERIGNORE.read_text(encoding="utf-8") if DOCKERIGNORE.is_file() else ""
RAW = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else ""


def test_the_real_files_follow_the_rules():
    assert DOCKER and IGNORE and RAW, "Dockerfile, .dockerignore and .github/workflows/api.yml must exist"
    assert dockerfile_problems(DOCKER) == []
    assert dockerignore_problems(IGNORE) == []
    assert workflow_problems(RAW) == []


def test_there_is_no_railway_json_because_config_as_code_is_deprecated():
    assert not (ROOT / "railway.json").exists() and not (ROOT / "railway.toml").exists()


def test_the_requirements_the_image_installs_carry_no_model_sdk_and_no_http_client():
    names = {re.split(r"[<>=\[\s]", ln.strip(), maxsplit=1)[0].lower() for ln in (ROOT / "api" / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")}
    assert not names & {"anthropic", "httpx", "requests", "gradio", "openai"}, names


DOCKER_EDITS = [
    ("a different base image", lambda d: d.replace("python:3.12-slim", "python:3.12"), "base image"),
    ("the requirements are not installed", lambda d: d.replace("RUN pip install -r api/requirements.txt", "RUN true"), "install api/requirements.txt"),
    ("the tests are copied into the image", lambda d: d.replace("COPY specs/ specs/", "COPY specs/ specs/\nCOPY tests/ tests/"), "COPY tests/"),
    ("the whole repo is copied", lambda d: d.replace("COPY specs/ specs/", "COPY specs/ specs/\nCOPY . ."), "COPY ."),
    ("it runs as root", lambda d: d.replace("USER app", "USER root"), "non-root"),
    ("it listens on a fixed port", lambda d: d.replace("--port ${PORT}", "--port 8000"), "CMD must start"),
    ("a model key is set in the image", lambda d: d.replace("ENV PORT=8000", "ENV PORT=8000\nENV ANTHROPIC_API_KEY=x"), "model key"),
]


@pytest.mark.parametrize("label,edit,needle", DOCKER_EDITS, ids=[e[0] for e in DOCKER_EDITS])
def test_dockerfile_drift_is_caught(label, edit, needle):
    assert dockerfile_problems(DOCKER) == [], "the control"
    changed = edit(DOCKER)
    assert changed != DOCKER
    assert any(needle in p for p in dockerfile_problems(changed)), dockerfile_problems(changed)


def _wf(edit):
    wf = yaml.safe_load(RAW)
    edit(wf)
    return yaml.safe_dump(wf, sort_keys=False)


WORKFLOW_EDITS = [
    ("pull requests are not tested", lambda raw: _wf(lambda w: (w.get("on", w.get(True))).pop("pull_request")), "pull requests"),
    ("the deploy does not need the tests", lambda raw: _wf(lambda w: w["jobs"]["deploy"].pop("needs")), "must need the test job"),
    ("the deploy runs for every event", lambda raw: _wf(lambda w: w["jobs"]["deploy"].pop("if")), "only for a push to main"),
    ("the deploy runs for pull requests", lambda raw: _wf(lambda w: w["jobs"]["deploy"].update({"if": "github.event_name == 'pull_request'"})), "only for a push to main"),
    ("the tests can fail without failing the job", lambda raw: raw.replace("python -m pytest tests/test_api_flows.py", "python -m pytest tests/test_api_flows.py || true"), "pass whatever happens"),
    ("the credential check is gone", lambda raw: _wf(lambda w: w["jobs"]["deploy"]["steps"].pop(0)), "first step must fail loudly"),
    ("the Railway CLI is unpinned", lambda raw: raw.replace("@railway/cli@5.75.0", "@railway/cli"), "pinned to an exact version"),
    ("an action is a moving tag", lambda raw: re.sub(r"(actions/checkout)@[0-9a-f]{40} # v[\d.]+", r"\1@v4", raw, count=1), "not pinned"),
    ("a model key variable is added to the deploy job", lambda raw: raw.replace("API_URL: ${{ vars.API_URL }}", "API_URL: ${{ vars.API_URL }}\n      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}"), "model key"),
    ("the smoke test no longer checks the flows", lambda raw: raw.replace('"flows": ["duty-match", "write-check", "propose"]', '"flows": []'), "smoke test must check"),
]


@pytest.mark.parametrize("label,edit,needle", WORKFLOW_EDITS, ids=[e[0] for e in WORKFLOW_EDITS])
def test_workflow_drift_is_caught(label, edit, needle):
    assert workflow_problems(RAW) == [], "the control"
    changed = edit(RAW)
    assert changed != RAW
    assert any(needle in p for p in workflow_problems(changed)), workflow_problems(changed)


def test_dockerignore_must_keep_local_state_and_tests_out():
    assert any("tests" in p for p in dockerignore_problems(IGNORE.replace("\ntests\n", "\n")))
    assert any(".venv" in p for p in dockerignore_problems(IGNORE.replace(".venv\n", "")))
