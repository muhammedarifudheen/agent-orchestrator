"""Blocks outbound network use (DNS lookups and connects to anything but loopback).

Used two ways:
  * in-process by tests/conftest.py for the gate tests;
  * in every subprocess pipeline.py starts for its verification gates, via sitecustomize.py in this directory
    (put on PYTHONPATH).
Why: the pipeline's gates are offline by design. If one ever reaches for the network it must fail at once with a
message naming the host, instead of hanging on a DNS or connect timeout (the signature of the 164 s gate run).
"""
from __future__ import annotations

import socket

_LOCAL = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}


def is_local(host: object) -> bool:
    if host is None or isinstance(host, bytes):
        return True if host is None else host.decode(errors="ignore") in _LOCAL
    return str(host) in _LOCAL or str(host).startswith("127.")


class NetworkBlocked(OSError):
    """Raised when something tries to leave the machine while the guard is installed."""


def install() -> "callable":
    """Install the guard; returns a function that removes it."""
    real_getaddrinfo, real_connect = socket.getaddrinfo, socket.socket.connect

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not is_local(host):
            raise NetworkBlocked(f"offline_guard: DNS lookup for {host!r} blocked (gates must run offline)")
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self, address):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address  # str = AF_UNIX path
        if isinstance(address, tuple) and not is_local(host):
            raise NetworkBlocked(f"offline_guard: connect to {address!r} blocked (gates must run offline)")
        return real_connect(self, address)

    socket.getaddrinfo, socket.socket.connect = getaddrinfo, connect

    def uninstall() -> None:
        socket.getaddrinfo, socket.socket.connect = real_getaddrinfo, real_connect

    return uninstall
