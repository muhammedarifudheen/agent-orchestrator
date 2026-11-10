"""Environmental interruptions: a paused machine or process, or a clock that disagrees with itself (CLAUDE.md D17).

What was observed: `subprocess.TimeoutExpired: ... timed out after -267.8 seconds` in a test that passed `timeout=60`. CPython
reports the REMAINING time (`deadline - now`) there, so the monotonic clock had run 267.8 s PAST the deadline while the call was
blocked: the process or the machine was paused (sleep, a VM pause, a frozen laptop). Neither the test nor the code was slow
or wrong, so such a test must not FAIL the run, and the run must say it is not valid evidence about speed.

Three signals, any of which marks a test window as an environmental interruption:
  * negative elapsed time (`elapsed()` < 0), the signature above and a wall clock stepped backwards;
  * a wall-clock/monotonic disagreement larger than JUMP_S (one of them was stepped or stopped);
  * a HEARTBEAT gap: a daemon thread samples the monotonic clock every TICK_S; if two samples are more than GAP_S apart
    the whole process was not scheduled (suspend, freeze). This is the only signal that sees a pause which advanced both clocks
    equally, which is exactly what happened.
Heavy CPU load does not trigger it: 4-12 busy loops delay the thread by milliseconds, not by GAP_S.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

TICK_S = 0.5
GAP_S = 5.0     # a heartbeat gap above this is a pause, not load
JUMP_S = 5.0    # wall-clock vs monotonic disagreement above this is a clock event


def sample() -> tuple[float, float]:
    """(wall clock, monotonic clock). Tests monkeypatch this to simulate a clock event."""
    return time.time(), time.monotonic()


def elapsed(start: tuple[float, float]) -> float:
    """Monotonic seconds since `start` (a `sample()`). Tests monkeypatch this to return a negative value."""
    return sample()[1] - start[1]


def disagreement(start: tuple[float, float]) -> float:
    """How far the wall clock and the monotonic clock disagree about the time since `start` (seconds, signed)."""
    wall, mono = sample()
    return (wall - start[0]) - (mono - start[1])


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"


def classify(elapsed_s: float, gap_s: float = 0.0, disagree_s: float = 0.0, failure_text: str = "") -> str | None:
    """A reason string when the window is an environmental interruption, else None. Pure."""
    if elapsed_s < 0:
        return f"negative elapsed time ({elapsed_s:.1f} s)"
    if "timed out after -" in failure_text:
        return "a subprocess deadline was passed by the clock while the call was blocked (negative remaining time)"
    if abs(disagree_s) > JUMP_S:
        return f"clock disagreement: wall and monotonic clocks differ by {disagree_s:+.1f} s"
    if gap_s > GAP_S:
        return f"process paused for {gap_s:.1f} s (heartbeat gap: sleep, freeze or VM pause)"
    return None


class Heartbeat:
    """Daemon thread that records every stretch longer than GAP_S in which it was not scheduled."""

    def __init__(self, tick_s: float = TICK_S, gap_s: float = GAP_S, clock=time.monotonic) -> None:
        self.tick_s, self.gap_s, self._clock = tick_s, gap_s, clock  # `clock` is injectable: tests must not patch `time`
        self.gaps: list[tuple[float, float]] = []   # (monotonic time the gap ended, gap length)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="clock-heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        last = self._clock()
        while not self._stop.wait(self.tick_s):
            now = self._clock()
            if now - last - self.tick_s > self.gap_s:
                self.gaps.append((now, now - last))
            last = now

    def stop(self) -> None:
        self._stop.set()

    def longest_gap_since(self, mono_start: float) -> float:
        """The longest pause that ENDED after `mono_start` (it overlaps a window that began then)."""
        return max((g for ended, g in self.gaps if ended >= mono_start), default=0.0)
