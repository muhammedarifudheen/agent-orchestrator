"""scripts/repo_hash.py: the formula for the code a live-judgment record attests (CLAUDE.md D27)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import repo_hash as rh  # noqa: E402


@pytest.fixture
def tree(tmp_path):
    files = {"tests/test_a.py": "import helper\nfrom pkg import mod\n", "tests/helper.py": "X = 1\n",
             "pkg/__init__.py": "", "pkg/mod.py": "from . import sibling\n", "pkg/sibling.py": "Y = 2\n",
             "servers/s_server.py": "S = 1\n", "unrelated.py": "Z = 3\n", "pytest.ini": "[pytest]\n"}
    for rel, body in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body, encoding="utf-8", newline="\n")
    return tmp_path


def man(tree):
    return rh.manifest(["tests/test_a.py"], ["servers/*.py"], tree, harness=())


def test_the_manifest_is_the_import_closure_plus_the_data_globs_and_nothing_else(tree):
    assert sorted(man(tree)["files"]) == ["pkg/__init__.py", "pkg/mod.py", "pkg/sibling.py", "pytest.ini", "servers/s_server.py",
                                          "tests/helper.py", "tests/test_a.py"]  # not unrelated.py


def test_the_digest_is_deterministic_and_independent_of_where_the_tree_lives(tree, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    for p in tree.rglob("*"):
        if p.is_file():
            (other / p.relative_to(tree)).parent.mkdir(parents=True, exist_ok=True)
            (other / p.relative_to(tree)).write_bytes(p.read_bytes())
    assert rh.digest(man(tree)) == rh.digest(man(tree)) == rh.digest(man(other))


def test_the_digest_is_exactly_the_documented_formula(tree):
    import hashlib
    m = man(tree)
    lines = "".join(f"{p}\t{h}\n" for p, h in sorted(m["files"].items()))
    assert rh.digest(m) == hashlib.sha256(lines.encode("utf-8")).hexdigest()
    assert all(len(h) == 64 for h in m["files"].values())


def test_crlf_and_lf_hash_the_same_but_any_other_edit_changes_the_digest(tree):
    before = rh.digest(man(tree))
    f = tree / "tests" / "helper.py"
    f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
    assert rh.digest(man(tree)) == before
    f.write_text("X = 2\n", encoding="utf-8", newline="\n")
    assert rh.digest(man(tree)) != before


def test_diff_names_changed_deleted_and_added_files(tree):
    recorded = man(tree)
    assert rh.diff(recorded, tree) == {"changed": [], "deleted": [], "added": []}
    (tree / "pkg" / "sibling.py").write_text("Y = 3\n", encoding="utf-8", newline="\n")
    (tree / "servers" / "new_server.py").write_text("N = 1\n", encoding="utf-8", newline="\n")
    (tree / "tests" / "helper.py").unlink()
    d = rh.diff(recorded, tree)
    assert d["changed"] == ["pkg/sibling.py"] and d["deleted"] == ["tests/helper.py"] and d["added"] == ["servers/new_server.py"]


def test_the_harness_files_are_part_of_every_real_manifest():
    for sub in rh.SUBCOMMAND_CODE:
        files = rh.for_subcommand(sub)["files"]
        for h in rh.HARNESS:
            assert h in files, f"{sub}: {h} decides the verdict, so changing it must change the hash"
        assert "pytest.ini" in files


def test_each_real_manifest_holds_the_tests_it_runs_and_what_they_exercise():
    planner = rh.for_subcommand("planner")["files"]
    assert {"tests/scenarios/test_planner_contract.py", "tests/scenarios/test_cross_industry_planners.py", "tests/scenarios/planners.py",
            "agent/orchestrator.py", "scenarios/cross_industry.yaml", "servers/retail_server.py"} <= set(planner)
    assert {"tests/ingest/test_doc_contract.py", "ingest/doc.py", "tests/ingest/fixtures/dogceo.doc.md", "pipeline.py"} <= set(rh.for_subcommand("doc")["files"])
    assert {"learning/apply.py", "learning/propose.py", "learning/collect.py", "pipeline.py", "generators/from_spec.py"} <= set(rh.for_subcommand("apply")["files"])
    live = rh.for_subcommand("live_apis")["files"]
    assert set(rh.LIVE_API_FILES) <= set(live) and "specs/weather.yaml" in live and "servers/_http.py" in live


def test_the_real_manifests_are_deterministic_and_the_repo_matches_itself():
    for sub in rh.SUBCOMMAND_CODE:
        a, b = rh.for_subcommand(sub), rh.for_subcommand(sub)
        assert rh.digest(a) == rh.digest(b)
        assert rh.diff(a) == {"changed": [], "deleted": [], "added": []}


def test_editing_a_real_test_file_changes_the_planner_digest(tmp_path):
    """On a COPY of the files, so the real repo is never touched."""
    import shutil
    copy = tmp_path / "repo"
    for rel in rh.for_subcommand("planner")["files"]:
        (copy / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, copy / rel)
    spec = rh.SUBCOMMAND_CODE["planner"]
    before = rh.manifest(spec["roots"], spec["data"], copy)
    target = copy / "tests" / "scenarios" / "test_planner_contract.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8", newline="\n")
    after = rh.manifest(spec["roots"], spec["data"], copy)
    assert rh.digest(after) != rh.digest(before)
    assert rh.diff(before, copy)["changed"] == ["tests/scenarios/test_planner_contract.py"]
