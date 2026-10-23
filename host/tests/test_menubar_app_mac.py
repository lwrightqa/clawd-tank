"""app.py on a Mac, with the real rumps: what its timers do.

Skipped everywhere else (app.py imports rumps and AppKit)."""

import time
import types

import pytest

rumps = pytest.importorskip("rumps")
AppKit = pytest.importorskip("AppKit")
Foundation = pytest.importorskip("Foundation")

from clawd_tank_menubar import app as app_mod  # noqa: E402


def spin(seconds):
    Foundation.NSRunLoop.currentRunLoop().runUntilDate_(
        Foundation.NSDate.dateWithTimeIntervalSinceNow_(seconds))


def test_after_runs_the_function_once_and_not_before_its_delay():
    """rumps.Timer ticks at once and uses its interval only for the next ticks; _after
    must still wait."""
    fake = types.SimpleNamespace(_oneshots=[])
    fired = []
    t0 = time.monotonic()
    app_mod.ClawdTankApp._after(fake, 0.4, lambda: fired.append(time.monotonic() - t0))
    spin(0.15)
    assert fired == []                       # not at once
    spin(1.0)
    assert len(fired) == 1 and fired[0] >= 0.38
    assert fake._oneshots == []              # the timer is dropped when it has run
