"""keep_live_sessions: an open but idle Claude Code session stays on the bar for as
long as its process runs, across the 10 minute silence limit and across a restart.

Nothing here touches ~/.clawd-tank: the daemons get a sessions file in tmp_path and
no transport."""

import asyncio
import json
import subprocess
import sys
import time
from unittest.mock import AsyncMock

import pytest

from clawd_tank_daemon import daemon as daemon_mod
from clawd_tank_daemon import procutil
from clawd_tank_daemon.daemon import ClawdDaemon
from clawd_tank_daemon.session_store import load_sessions, save_sessions

LONG_AGO = 3600.0


def make_daemon(tmp_path, keep, sessions=None):
    path = tmp_path / "sessions.json"
    if sessions is not None:
        save_sessions(sessions, path, order=[(sid, i + 1) for i, sid in enumerate(sessions)],
                      next_id=len(sessions) + 1)
    d = ClawdDaemon(sim_only=True, sessions_path=path, keep_live_sessions=keep)
    d._transports.clear()
    d._transport_queues.clear()
    return d


def idle(pid=None, age=LONG_AGO):
    state = {"state": "idle", "last_event": time.time() - age, "tool_name": ""}
    if pid is not None:
        state["pid"] = pid
    return state


# --- the session file keeps the PID only when asked ---------------------------------------


def test_load_keeps_a_pid_only_when_asked(tmp_path):
    path = tmp_path / "sessions.json"
    save_sessions({"s1": {"state": "idle", "last_event": 1.0, "pid": 4242}}, path)
    assert "pid" not in load_sessions(path)[0]["s1"]                      # as upstream
    assert load_sessions(path, keep_pid=True)[0]["s1"]["pid"] == 4242


@pytest.mark.parametrize("bad", [True, -5, 0, "4242", 4.5, None])
def test_load_drops_a_pid_that_is_not_a_process_id(tmp_path, bad):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"sessions": {"s1": {"state": "idle", "last_event": 1.0, "pid": bad}}}))
    assert "pid" not in load_sessions(path, keep_pid=True)[0]["s1"]


# --- is it still the same process ----------------------------------------------------------


def test_a_running_process_that_started_before_the_last_event_is_the_same_one(monkeypatch):
    monkeypatch.setattr(procutil, "pid_alive", lambda pid: True)
    monkeypatch.setattr(procutil, "process_start_time", lambda pid: 1000.0)
    assert procutil.session_process_alive(4242, last_event=5000.0)


def test_a_process_created_after_the_last_event_has_a_recycled_pid(monkeypatch):
    monkeypatch.setattr(procutil, "pid_alive", lambda pid: True)
    monkeypatch.setattr(procutil, "process_start_time", lambda pid: 5000.0 + procutil.START_GRACE_SECS + 1)
    assert not procutil.session_process_alive(4242, last_event=5000.0)


def test_clock_noise_inside_the_grace_is_not_a_recycled_pid(monkeypatch):
    monkeypatch.setattr(procutil, "pid_alive", lambda pid: True)
    monkeypatch.setattr(procutil, "process_start_time", lambda pid: 5000.0 + procutil.START_GRACE_SECS - 1)
    assert procutil.session_process_alive(4242, last_event=5000.0)


def test_an_unknown_start_time_counts_a_running_process(monkeypatch):
    monkeypatch.setattr(procutil, "pid_alive", lambda pid: True)
    monkeypatch.setattr(procutil, "process_start_time", lambda pid: None)
    assert procutil.session_process_alive(4242, last_event=5000.0)


def test_a_process_that_is_gone_is_not_alive(monkeypatch):
    monkeypatch.setattr(procutil, "pid_alive", lambda pid: False)
    assert not procutil.session_process_alive(4242, last_event=5000.0)
    assert not procutil.session_process_alive(None, last_event=5000.0)


@pytest.mark.skipif(not procutil.IS_WINDOWS, reason="creation time is read through the Windows API")
def test_process_start_time_of_a_real_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = procutil.process_start_time(child.pid)
        assert started is not None and abs(started - time.time()) < 60
        assert procutil.session_process_alive(child.pid, last_event=time.time())
        # A session whose last event is from before the process existed is not this process's.
        assert not procutil.session_process_alive(child.pid, last_event=time.time() - LONG_AGO)
    finally:
        child.kill()
        child.wait(timeout=30)
    assert procutil.process_start_time(child.pid) is None or not procutil.pid_alive(child.pid)
    assert procutil.process_start_time(None) is None
    assert procutil.process_start_time(0) is None


# --- the 10 minute silence limit -----------------------------------------------------------


def _quiet(d, sid, state="idle", pid=None):
    d._session_states[sid] = {
        "state": state, "last_event": time.time() - LONG_AGO,
        "last_event_monotonic": time.monotonic() - LONG_AGO,
        **({"pid": pid} if pid is not None else {}),
    }
    d._session_order.append((sid, len(d._session_order) + 1))
    d._session_staleness_timeout = 600


def test_a_quiet_session_whose_process_runs_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "pid_alive", lambda pid: True)
    d = make_daemon(tmp_path, keep=True)
    _quiet(d, "open", pid=4242)
    d._evict_stale_sessions()
    assert "open" in d._session_states
    assert d._compute_display_state()["anims"] == ["idle"]


def test_a_quiet_session_without_a_pid_still_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "pid_alive", lambda pid: pid is not None)   # as the real one
    d = make_daemon(tmp_path, keep=True)
    _quiet(d, "no-pid")
    d._evict_stale_sessions()
    assert "no-pid" not in d._session_states


def test_a_quiet_session_whose_process_died_goes(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "pid_alive", lambda pid: False)
    d = make_daemon(tmp_path, keep=True)
    _quiet(d, "closed", pid=4242)
    d._evict_stale_sessions()
    assert "closed" not in d._session_states


def test_the_terminal_daemon_is_as_upstream(tmp_path, monkeypatch):
    """Without the option a quiet session times out whatever its process is doing."""
    monkeypatch.setattr(daemon_mod, "pid_alive", lambda pid: True)
    d = make_daemon(tmp_path, keep=False)
    _quiet(d, "open", pid=4242)
    d._evict_stale_sessions()
    assert "open" not in d._session_states


def test_the_liveness_check_still_drops_a_kept_session_when_its_process_exits(tmp_path, monkeypatch):
    alive = {"yes": True}
    monkeypatch.setattr(daemon_mod, "pid_alive", lambda pid: alive["yes"])
    d = make_daemon(tmp_path, keep=True)
    _quiet(d, "open", pid=4242)
    d._evict_stale_sessions()
    assert "open" in d._session_states
    alive["yes"] = False
    assert d._check_liveness() == ["open"]
    assert d._compute_display_state() == {"status": "sleeping"}


# --- a restart ------------------------------------------------------------------------------


def test_a_restart_keeps_a_quiet_session_whose_process_is_the_same(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "session_process_alive", lambda pid, last_event: True)
    d = make_daemon(tmp_path, keep=True, sessions={"open": idle(pid=4242)})
    assert "open" in d._session_states and d._session_states["open"]["pid"] == 4242
    assert d._compute_display_state()["anims"] == ["idle"]


def test_a_restart_drops_a_session_whose_process_is_gone_or_recycled(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "session_process_alive", lambda pid, last_event: False)
    # Even a session that spoke a moment ago: its process is not the one that spoke.
    d = make_daemon(tmp_path, keep=True, sessions={"fresh": idle(pid=4242, age=5)})
    assert d._session_states == {}


def test_a_restart_judges_a_session_without_a_pid_by_its_age(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "session_process_alive", lambda pid, last_event: True)
    d = make_daemon(tmp_path, keep=True, sessions={"old": idle(), "new": idle(age=30)})
    assert set(d._session_states) == {"new"}


def test_the_terminal_daemon_restart_is_as_upstream(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "session_process_alive", lambda pid, last_event: True)
    d = make_daemon(tmp_path, keep=False, sessions={"old": idle(pid=4242), "new": idle(pid=4243, age=30)})
    assert set(d._session_states) == {"new"}
    assert "pid" not in d._session_states["new"]


# --- the bar that connects after all that ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_bar_that_connects_gets_the_quiet_open_session(tmp_path, monkeypatch):
    """The reported bug: plug the bar in and it showed 'no active session', Clawd asleep."""
    monkeypatch.setattr(daemon_mod, "session_process_alive", lambda pid, last_event: True)
    d = make_daemon(tmp_path, keep=True, sessions={"open": idle(pid=4242)})
    transport = AsyncMock()
    transport.is_connected = True
    d._transport_versions["bar"] = 2
    await d._replay_active_for(transport, "bar")
    sent = json.loads(transport.write_notification.await_args.args[0])
    assert sent["action"] == "set_sessions" and sent["anims"] == ["idle"]
