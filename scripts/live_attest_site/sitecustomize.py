"""Loaded at interpreter start by every Python process whose PYTHONPATH starts with this directory (see scripts/live_attest.py):
when LIVE_ATTEST_FILE is set, attest every httpx request this process makes. A no-op otherwise, and it never raises."""
import os
import sys

try:
    _path = os.environ.get("LIVE_ATTEST_FILE")
    if _path:
        _scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _scripts not in sys.path:
            sys.path.insert(0, _scripts)
        import live_attest

        live_attest.install(_path)
except Exception:  # noqa: BLE001 - attestation must never break the process it observes
    pass
