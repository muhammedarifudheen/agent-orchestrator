"""Safety of the offline demo (demo/app.py), CLAUDE.md D35: it cannot be turned into a live-model app by accident, and it cannot write
into the repo's specs/, servers/, handoffs/, learning/ or proposals/.

  * Key refusal: with ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN / PROVIDER_API_KEY) set, `assert_offline_only`, `build_app` and `main` refuse with
    "This demo is offline-only by design; <VAR> must be unset.", BEFORE gradio is imported; `python demo/app.py` as a subprocess exits with that
    message (gradio is not installed in this venv, so a missing refusal would surface as a different error).
  * Specs hash unchanged: each demo function is run and the files under specs/, servers/, handoffs/, learning/, proposals/, duties/ and agent/
    are hashed before and after (Python's own __pycache__ is not the demo's doing and is excluded).
  * The scratch directory is refused when the temp location is itself a protected directory.
  * Importing and running the flows never loads the Anthropic SDK or gradio.
Does not prove: the UI under real gradio (smoke-tested by hand in a scratch environment), or anything about a deployed Space."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WATCHED = ("specs", "servers", "handoffs", "learning", "proposals", "duties", "agent")
KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PROVIDER_API_KEY")
MESSAGE = "This demo is offline-only by design; ANTHROPIC_API_KEY must be unset."


@pytest.fixture(scope="module")
def app():
    spec = importlib.util.spec_from_file_location("offline_demo_app_safety", ROOT / "demo" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["offline_demo_app_safety"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("offline_demo_app_safety", None)


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def tree() -> dict[str, str]:
    out = {}
    for d in WATCHED:
        for p in sorted((ROOT / d).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                out[p.relative_to(ROOT).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# -- (a) key refusal -------------------------------------------------------------------------------------------------------------------

def test_the_mandated_sentence_is_exact_for_anthropic_api_key(app):
    with pytest.raises(SystemExit) as exc:
        app.assert_offline_only({"ANTHROPIC_API_KEY": "sk-test-not-a-real-key"})
    assert str(exc.value) == MESSAGE


@pytest.mark.parametrize("var", KEY_VARS)
def test_any_model_key_variable_refuses_and_names_itself(app, var):
    with pytest.raises(SystemExit) as exc:
        app.assert_offline_only({var: "x"})
    assert str(exc.value) == f"This demo is offline-only by design; {var} must be unset."


@pytest.mark.parametrize("env", [{}, {"ANTHROPIC_API_KEY": ""}, {"ANTHROPIC_API_KEY": "   "}, {"OTHER": "x"}])
def test_no_key_or_an_empty_one_is_allowed(app, env):
    app.assert_offline_only(env)  # an empty value cannot authenticate anything, so it is not a key


def test_build_app_and_main_refuse_before_gradio_is_even_imported(app, monkeypatch):
    monkeypatch.setitem(sys.modules, "gradio", None)  # `import gradio` would raise ImportError: the refusal must come first
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    for fn in (app.build_app, app.main):
        with pytest.raises(SystemExit) as exc:
            fn()
        assert str(exc.value) == MESSAGE
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(ImportError):  # the control: without the key the next thing that happens is the gradio import
        app.build_app()


def test_the_script_itself_refuses_when_run_as_a_subprocess_with_a_key():
    env = {**os.environ, "ANTHROPIC_API_KEY": "sk-test-not-a-real-key"}
    done = subprocess.run([sys.executable, str(ROOT / "demo" / "app.py")], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode != 0
    assert MESSAGE in done.stderr and "ModuleNotFoundError" not in done.stderr and "gradio" not in done.stderr.lower().replace(MESSAGE.lower(), "")


# -- (b) nothing is written to the repo -------------------------------------------------------------------------------------------------

def _flows(app):
    yield "match_duty (match)", lambda: app.match_duty("refund order ORD-ABCD1234")
    yield "match_duty (no match)", lambda: app.match_duty("what is the weather in Pune")
    for tool in ("search_flights", "book_flight", "transfer_funds"):
        yield f"write_safety {tool}", (lambda t=tool: app.write_safety(t))
    for name in ("handoff_undeclared_status", "handoff_wrong_param_type", "handoff_no_spec_system"):
        yield f"learning_proposal {name}", (lambda n=name: app.learning_proposal(app.load_fixture(n)))
    yield "learning_proposal (rejected input)", lambda: app.learning_proposal('{"error_snapshot": {"tool": "weather__x", "error_code": "../../evil"}}')


def test_no_demo_function_changes_a_file_under_the_repos_specs_servers_handoffs_learning_or_proposals(app):
    names = []
    for label, call in _flows(app):
        before = tree()
        call()
        after = tree()
        assert after == before, f"{label} changed the repo: {sorted(set(after.items()) ^ set(before.items()))[:4]}"
        names.append(label)
    assert len(names) == 9, "every demo function, and the rejected input, was run"


def test_the_specs_directory_hash_is_unchanged_by_the_learning_tab_in_particular(app):
    before = {k: v for k, v in tree().items() if k.startswith("specs/")}
    assert before, "there are specs to protect"
    for name in app.fixture_names():
        assert app.learning_proposal(app.load_fixture(name))["specs_written"] is False
    assert {k: v for k, v in tree().items() if k.startswith("specs/")} == before


@pytest.mark.parametrize("protected", ["specs", "servers", "handoffs", "learning", "proposals"])
def test_a_scratch_directory_inside_a_protected_directory_is_refused(app, monkeypatch, protected):
    monkeypatch.setattr(tempfile, "tempdir", str(ROOT / protected))  # as if TMP pointed into the repo
    before = tree()
    with pytest.raises(PermissionError, match=protected):
        app.learning_proposal(app.load_fixture("handoff_undeclared_status"))
    with pytest.raises(PermissionError, match=protected):
        app.write_safety("book_flight")
    assert tree() == before and not any(p.name.startswith("offline_demo_") for p in (ROOT / protected).iterdir()), "nothing was created there"


def test_the_guard_allows_the_normal_temp_location_and_refuses_the_protected_ones(app, tmp_path):
    app._assert_not_protected(tmp_path)
    for name in app.PROTECTED_DIRS:
        with pytest.raises(PermissionError):
            app._assert_not_protected(ROOT / name / "anything.txt")


# -- (c) no model client is loaded ---------------------------------------------------------------------------------------------------------

def test_running_the_flows_loads_neither_the_anthropic_sdk_nor_gradio():
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('offline_demo_app', 'demo/app.py')\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['offline_demo_app'] = m; spec.loader.exec_module(m)\n"
        "m.match_duty('refund order ORD-ABCD1234')\n"
        "for t in m.WRITE_TOOLS: m.write_safety(t)\n"
        "m.learning_proposal(m.load_fixture('handoff_undeclared_status'))\n"
        "bad = [n for n in ('anthropic', 'gradio', 'httpx') if n in sys.modules]\n"
        "print('LOADED:' + ','.join(bad))\n")
    env = {k: v for k, v in os.environ.items() if k not in KEY_VARS}
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-800:]
    assert "LOADED:" in done.stdout and done.stdout.strip().splitlines()[-1] == "LOADED:", done.stdout
