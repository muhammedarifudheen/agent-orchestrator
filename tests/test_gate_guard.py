"""The guards that keep the pipeline's gates honest (D17) are themselves tested: they must fire."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import guard
from conftest import GATE_MODULES, budget_violation

ROOT = Path(__file__).parent.parent
GUARD_DIR = ROOT / "offline_guard"


def test_budget_violation_names_the_test_and_the_duration():
    msg = budget_violation("tests/test_x.py::test_y", 3.2, 2.0)
    assert msg and "tests/test_x.py::test_y" in msg and "3.2s" in msg and "budget 2s" in msg and "D17" in msg
    assert budget_violation("tests/test_x.py::test_y", 2.0, 2.0) is None  # exactly on budget is fine


def test_every_gate_module_exists():
    for name in GATE_MODULES:
        assert list((ROOT / "tests").rglob(f"{name}.py")), f"GATE_MODULES lists {name} but there is no such test file"


def test_in_process_guard_blocks_dns_and_connects_but_allows_loopback():
    undo = guard.install()
    try:
        with pytest.raises(guard.NetworkBlocked, match="example.com"):
            socket.getaddrinfo("example.com", 80)
        with pytest.raises(guard.NetworkBlocked):
            socket.create_connection(("93.184.216.34", 80), timeout=1)
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=2)  # loopback still works
        client.close()
        server.close()
    finally:
        undo()
    assert socket.getaddrinfo("localhost", 80)  # guard removed again


def test_subprocess_guard_via_sitecustomize_blocks_the_network_in_every_gate_subprocess():
    env = dict(os.environ, PYTHONPATH=str(GUARD_DIR))
    done = subprocess.run([sys.executable, "-c", "import socket; socket.getaddrinfo('example.com', 80)"],
                          env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "offline_guard" in done.stderr and "example.com" in done.stderr


def test_pipeline_puts_the_guard_first_on_the_gates_pythonpath():
    import pipeline
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    assert 'str(ROOT / "offline_guard")' in source
