"""PROBE, not a test of the code: fails with the exact signature seen in the real incident while the elapsed-time computation
reports a negative value. Runs only when tests/test_budget_policy.py starts it in a subprocess (RUN_PROBE=1); the runner must
refuse to FAIL it and mark the whole run invalid (CLAUDE.md D17)."""
import os
import subprocess

import pytest

import clock_guard

pytestmark = pytest.mark.skipif(os.environ.get("RUN_PROBE") != "1", reason="probe: started by tests/test_budget_policy.py only")


def test_probe_fails_during_a_clock_event(monkeypatch):
    monkeypatch.setattr(clock_guard, "elapsed", lambda start: -267.8)  # the elapsed-time computation goes negative
    raise subprocess.TimeoutExpired(cmd="python -c 'import servers.x'", timeout=-267.8)
