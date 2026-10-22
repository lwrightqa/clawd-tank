"""The daemon's hooks for living inside another program (the Windows tray app):
embedded mode, the display-state observer, and a start that fails cleanly."""

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest

import clawd_tank_daemon.daemon as daemon_mod
from clawd_tank_daemon.daemon import ClawdDaemon, DaemonAlreadyRunning, DaemonObserver


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "PID_PATH", tmp_path / "daemon.pid")
    monkeypatch.setattr(daemon_mod, "LOCK_PATH", tmp_path / "daemon.lock")
    return tmp_path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make(state_dir, observer=None, **kw):
    return ClawdDaemon(observer=observer, headless=True, sim_only=True, hook_transport="tcp",
                       hook_tcp_port=free_port(), sessions_path=state_dir / "s.json", **kw)


class Full:
    """An observer with every optional method."""

    def __init__(self):
        self.log = []

    def on_connection_change(self, connected, transport=""):
        self.log.append(("connection", connected, transport))

    def on_notification_change(self, count):
        self.log.append(("notifications", count))

    def on_started(self, endpoint):
        self.log.append(("started", endpoint))

    def on_display_state_change(self, state):
        self.log.append(("display", state))

    def on_display_oneshot(self, anim):
        self.log.append(("oneshot", anim))

    def on_hook_event(self, msg):
        self.log.append(("hook", msg.get("event"), msg.get("hook")))


class Minimal:
    """The two methods of the original Protocol, nothing more (the macOS app)."""

    def __init__(self):
        self.log = []

    def on_connection_change(self, connected, transport=""):
        self.log.append(("connection", connected))

    def on_notification_change(self, count):
        self.log.append(("notifications", count))


def test_the_new_observer_methods_are_optional(state_dir):
    assert isinstance(Minimal(), DaemonObserver)  # the Protocol did not grow


@pytest.mark.asyncio
async def test_display_state_changes_reach_the_observer(state_dir):
    obs = Full()
    d = make(state_dir, obs)
    await d._handle_message({"event": "dismiss", "hook": "UserPromptSubmit", "session_id": "s1"})
    await d._handle_message({"event": "tool_use", "session_id": "s1", "tool_name": "Edit"})
    await d._handle_message({"event": "tool_use", "session_id": "s1", "tool_name": "Edit"})  # no change
    await d._handle_message({"event": "dismiss", "hook": "SessionEnd", "session_id": "s1"})
    displays = [e[1] for e in obs.log if e[0] == "display"]
    assert [x.get("anims") or x["status"] for x in displays] == [["thinking"], ["typing"], "sleeping"]
    hooks = [e for e in obs.log if e[0] == "hook"]
    assert len(hooks) == 4 and hooks[0] == ("hook", "dismiss", "UserPromptSubmit")


@pytest.mark.asyncio
async def test_compact_reports_the_sweeping_one_shot(state_dir):
    obs = Full()
    d = make(state_dir, obs)
    await d._handle_message({"event": "session_start", "session_id": "s1"})
    await d._handle_message({"event": "compact", "session_id": "s1"})
    assert ("oneshot", "sweeping") in obs.log


@pytest.mark.asyncio
async def test_an_observer_without_the_new_methods_is_untouched(state_dir):
    obs = Minimal()
    d = make(state_dir, obs)
    await d._handle_message({"event": "dismiss", "hook": "UserPromptSubmit", "session_id": "s1"})
    await d._handle_message({"event": "compact", "session_id": "s1"})
    assert obs.log == [("notifications", 0), ("notifications", 0)]


@pytest.mark.asyncio
async def test_an_observer_that_raises_cannot_stop_the_daemon(state_dir):
    class Angry(Full):
        def on_display_state_change(self, state):
            raise RuntimeError("boom")

        def on_hook_event(self, msg):
            raise RuntimeError("boom")

    d = make(state_dir, Angry())
    await d._handle_message({"event": "dismiss", "hook": "UserPromptSubmit", "session_id": "s1"})
    assert d._session_states["s1"]["state"] == "thinking"


def test_display_state_is_public_and_read_only(state_dir):
    d = make(state_dir)
    assert d.display_state() == {"status": "sleeping"}
    d._session_states = {"s": {"state": "thinking", "last_event": 0}}
    d._session_order = [("s", 0)]
    assert d.display_state()["anims"] == ["thinking"]


def test_embedded_lock_conflict_raises_instead_of_exiting(state_dir):
    holder = daemon_mod._acquire_lock(takeover=False)
    try:
        with pytest.raises(DaemonAlreadyRunning):
            daemon_mod._acquire_lock(takeover=False, raise_on_conflict=True)
        with pytest.raises(SystemExit):  # the command line keeps its behaviour
            daemon_mod._acquire_lock(takeover=False)
    finally:
        os.close(holder)


@pytest.mark.asyncio
async def test_embedded_run_installs_no_signal_handlers_and_reports_started(state_dir, monkeypatch):
    def boom(loop):
        raise AssertionError("an embedded daemon must not install signal handlers")

    obs = Full()
    d = make(state_dir, obs, embedded=True)
    monkeypatch.setattr(d, "_install_signal_handlers", boom)
    task = asyncio.create_task(d.run())
    for _ in range(100):
        if any(e[0] == "started" for e in obs.log):
            break
        await asyncio.sleep(0.05)
    assert ("started", f"tcp://127.0.0.1:{d._socket.tcp_port}") in obs.log
    d._request_shutdown()
    await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
async def test_embedded_run_with_a_held_lock_raises(state_dir):
    holder = daemon_mod._acquire_lock(takeover=False)
    try:
        d = make(state_dir, embedded=True)
        with pytest.raises(DaemonAlreadyRunning):
            await d.run()
        assert not daemon_mod.PID_PATH.exists()  # the other daemon's PID file is not touched
    finally:
        os.close(holder)


@pytest.mark.asyncio
async def test_a_failed_listener_gives_back_the_lock_and_the_pid_file(state_dir):
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    try:
        d = ClawdDaemon(headless=True, sim_only=True, hook_transport="tcp",
                        hook_tcp_port=other.getsockname()[1], embedded=True,
                        sessions_path=state_dir / "s.json")
        with pytest.raises(OSError):
            await d.run()
    finally:
        other.close()
    assert not daemon_mod.PID_PATH.exists()
    os.close(daemon_mod._acquire_lock(takeover=False))  # the lock is free again
