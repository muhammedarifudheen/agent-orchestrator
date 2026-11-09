"""Shared fixtures for tests that run the orchestrator against real in-memory MCP servers."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

import retail_server
from errors import ToolFailure


@pytest.fixture(autouse=True)
def fresh_retail_state():
    retail_server._reset_state()
    yield
    retail_server._reset_state()


@pytest.fixture(autouse=True)
def fresh_http_pool():
    """servers/_http.py keeps one httpx client per upstream for the life of the process (D17). Tests inject fake
    transports and patch httpx.Client, so no pooled client may survive from one test into the next."""
    import _http
    _http.close_pool()
    yield
    _http.close_pool()


@pytest.fixture
def card_declined():
    """Monkeypatch charge_payment to raise ToolFailure(CARD_DECLINED, retryable=False, recovery="human")."""
    original = retail_server.charge_payment

    def charge_payment(order_id: str, amount: float) -> dict:
        """Charge the customer for an order. Call after create_order. (patched to fail)"""
        raise ToolFailure(code="CARD_DECLINED", message="Issuer declined the card.",
                          retryable=False, recovery="human")

    retail_server.mcp.remove_tool("charge_payment")
    retail_server.mcp.add_tool(charge_payment, name="charge_payment")
    yield
    retail_server.mcp.remove_tool("charge_payment")
    retail_server.mcp.add_tool(original, name="charge_payment")


# -- slow-run capture (D17): a diagnostic, never a gate ------------------------------------------------------
import os  # noqa: E402

PERF_DIR = Path(os.environ.get("AGENT_PERF_DIR") or Path(__file__).parent.parent / ".perf")
_perf: dict = {"t0": None, "durations": {}}


def _perf_clock() -> float:
    return time.perf_counter()


def pytest_sessionstart(session):
    _perf["t0"] = _perf_clock()
    _perf["durations"] = {}


def pytest_runtest_logreport(report):
    try:
        _perf["durations"][(report.nodeid, report.when)] = report.duration
    except Exception:  # never let the diagnostic touch the run
        pass


def pytest_sessionfinish(session, exitstatus):
    try:
        import perf_capture
        total = _perf_clock() - _perf["t0"]
        n_tests = len({nodeid for nodeid, _ in _perf["durations"]})
        perf_capture.record_run(PERF_DIR, total, n_tests, _perf["durations"])
    except Exception as exc:  # a diagnostic must never fail a run
        print(f"perf capture skipped: {type(exc).__name__}: {exc}")


# -- gate tests: offline and time-boxed (D17) ------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent / "offline_guard"))
import guard  # noqa: E402  (offline_guard/guard.py)

GATE_MODULES = {"test_review_gate", "test_pipeline", "test_pipeline_fixtures", "test_pipeline_relative_url",
                "test_doc_unverified", "test_collect", "test_patchfmt",
                "test_propose", "test_apply", "test_http_status",
                "test_cross_industry", "test_orchestrator_generality", "test_cross_industry_handoff",
                "test_retail_failures", "test_planner_wiring", "test_http_pool", "test_perf_capture"}
DEFAULT_BUDGET_S = 2.0


def budget_violation(nodeid: str, elapsed: float, budget: float) -> str | None:
    """The failure message when a gate test overran its budget, else None. Names the test and the duration."""
    if elapsed <= budget:
        return None
    return (f"GATE TEST TOO SLOW: {nodeid} took {elapsed:.1f}s (budget {budget:g}s). A gate test that stalls usually "
            "waited on a network call, a subprocess or a retry: find out which before raising the budget (CLAUDE.md D17).")


@pytest.fixture(autouse=True)
def gate_guard(request):
    """Gate tests may not touch the network and may not exceed their time budget (2 s unless they carry
    @pytest.mark.budget(N), which only tests that run the pipeline's subprocess gates may do).
    A leak or a stall fails the test BY NAME instead of being absorbed as a slow run."""
    if request.module.__name__.split(".")[-1] not in GATE_MODULES:
        yield
        return
    marker = request.node.get_closest_marker("budget")
    budget = float(marker.args[0]) if marker else DEFAULT_BUDGET_S
    uninstall = guard.install()
    started = time.perf_counter()
    try:
        yield
    finally:
        uninstall()
    message = budget_violation(request.node.nodeid, time.perf_counter() - started, budget)
    if message:
        pytest.fail(message, pytrace=False)
