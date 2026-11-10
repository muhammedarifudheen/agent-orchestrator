"""PROBE, not a test of the code: exceeds its own budget on purpose. Runs only when tests/test_budget_policy.py starts it in
a subprocess (RUN_PROBE=1), to show what an overrun does in the default run and under --strict-budgets (CLAUDE.md D17)."""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_PROBE") != "1", reason="probe: started by tests/test_budget_policy.py only")


@pytest.mark.budget(0.1)
def test_probe_exceeds_its_budget():
    time.sleep(0.3)  # 3x its 0.1 s budget, deterministically
