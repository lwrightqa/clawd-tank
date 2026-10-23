"""set_session_timeout: the menu's "Never" passes 0, which must mean no time limit.

Before the fix, 0 meant every quiet session was older than the limit and the next
30 s sweep dropped all of them. Nothing here touches ~/.clawd-tank: the daemons get
a sessions file in tmp_path and no transport."""

import logging
import time

import pytest

from clawd_tank_daemon import daemon as daemon_mod
from clawd_tank_daemon.daemon import ClawdDaemon

LONG_AGO = 3600.0


def make_daemon(tmp_path, keep=False):
    d = ClawdDaemon(sim_only=True, sessions_path=tmp_path / "sessions.json", keep_live_sessions=keep)
    d._transports.clear()
    d._transport_queues.clear()
    return d


def quiet(d, sid, age=LONG_AGO, state="idle", pid=None):
    """A session that has said nothing for `age` seconds."""
    d._session_states[sid] = {
        "state": state, "last_event": time.time() - age,
        "last_event_monotonic": time.monotonic() - age,
        **({"pid": pid} if pid is not None else {}),
    }
    d._session_order.append((sid, len(d._session_order) + 1))


def test_zero_means_no_time_limit(tmp_path):
    d = make_daemon(tmp_path)
    d.set_session_timeout(0)
    assert d._session_staleness_timeout == float("inf")
    quiet(d, "old")
    d._evict_stale_sessions()
    assert "old" in d._session_states
    assert d._compute_display_state()["anims"] == ["idle"]


@pytest.mark.parametrize("never", [0, -1, -300])
def test_zero_or_less_is_never(tmp_path, never):
    d = make_daemon(tmp_path)
    d.set_session_timeout(never)
    assert d._session_staleness_timeout == float("inf")


def test_a_positive_timeout_is_kept_as_it_was(tmp_path):
    d = make_daemon(tmp_path)
    d.set_session_timeout(120)
    assert d._session_staleness_timeout == 120.0
    quiet(d, "old", age=LONG_AGO)
    quiet(d, "recent", age=30)
    d._evict_stale_sessions()
    assert set(d._session_states) == {"recent"}


def test_never_can_be_changed_back_to_a_limit(tmp_path):
    d = make_daemon(tmp_path)
    d.set_session_timeout(0)
    quiet(d, "old")
    d._evict_stale_sessions()
    assert "old" in d._session_states
    d.set_session_timeout(300)
    assert d._session_staleness_timeout == 300.0
    d._evict_stale_sessions()
    assert "old" not in d._session_states


def test_never_keeps_a_session_that_has_no_pid_and_a_waiting_one(tmp_path):
    d = make_daemon(tmp_path)
    d.set_session_timeout(0)
    quiet(d, "no-pid")
    quiet(d, "waiting", state="waiting")
    d._evict_stale_sessions()
    assert set(d._session_states) == {"no-pid", "waiting"}


def test_never_does_not_stop_the_process_check(tmp_path, monkeypatch):
    """A session whose Claude Code process has exited still goes, whatever the limit."""
    monkeypatch.setattr(daemon_mod, "pid_alive", lambda pid: False)
    d = make_daemon(tmp_path, keep=True)
    d.set_session_timeout(0)
    quiet(d, "closed", pid=4242)
    assert d._check_liveness() == ["closed"]
    assert d._session_states == {}


def test_a_session_whose_process_runs_stays_with_a_limit_or_without(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "pid_alive", lambda pid: True)
    d = make_daemon(tmp_path, keep=True)
    quiet(d, "open", pid=4242)
    for seconds in (300, 0):
        d.set_session_timeout(seconds)
        d._evict_stale_sessions()
        assert "open" in d._session_states


def test_the_setting_is_logged(tmp_path, caplog):
    d = make_daemon(tmp_path)
    with caplog.at_level(logging.INFO, logger="clawd-tank"):
        d.set_session_timeout(0)
        d.set_session_timeout(300)
    lines = [r.getMessage() for r in caplog.records]
    assert any("never" in line for line in lines)
    assert any("300s" in line for line in lines)
