"""The tray app's engine: the daemon in a thread. No Bluetooth, no real state
files, no port 47421: every test runs the daemon with its BLE transport off, on a
free port, with the lock and PID file in a temp folder."""

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

import clawd_tank_daemon.daemon as daemon_mod
from clawd_tank_daemon import hook_transport
from clawd_tank_tray.engine import DaemonEngine
from clawd_tank_tray.state import CONFLICT_MESSAGE, EngineStatus


class Recorder:
    """Collects engine events and lets a test wait for one."""

    def __init__(self):
        self.events = []
        self._cond = threading.Condition()

    def __call__(self, name, payload):
        with self._cond:
            self.events.append((name, payload))
            self._cond.notify_all()

    def wait_for(self, pred, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for ev in self.events:
                    if pred(*ev):
                        return ev
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"timed out; events so far: {self.events}")
                self._cond.wait(left)

    def wait_status(self, status, timeout=10.0):
        return self.wait_for(
            lambda n, p: n == "status" and p["status"] == status.value, timeout)

    def statuses(self):
        return [p["status"] for n, p in self.events if n == "status"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def send(port: int, msg: dict) -> None:
    sock = hook_transport.connect("tcp", Path("unused"), port, timeout=3.0, connect_timeout=2.0)
    with sock:
        sock.sendall(json.dumps(msg).encode() + b"\n")


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "PID_PATH", tmp_path / "daemon.pid")
    monkeypatch.setattr(daemon_mod, "LOCK_PATH", tmp_path / "daemon.lock")
    return tmp_path


@pytest.fixture
def make_engine(state_dir):
    made = []

    def make(**kw):
        rec = Recorder()
        port = kw.pop("tcp_port", None) or free_port()
        eng = DaemonEngine(rec, no_ble=True, tcp_port=port, transport="tcp",
                           sessions_path=state_dir / "sessions.json", **kw)
        made.append(eng)
        return eng, rec, port

    yield make
    for eng in made:  # never leave a daemon thread, a port or a lock behind
        eng.stop()


def test_starts_serves_hooks_and_stops_cleanly(make_engine, state_dir):
    eng, rec, port = make_engine()
    assert eng.status == EngineStatus.STOPPED
    assert eng.start() is True
    rec.wait_status(EngineStatus.RUNNING)
    assert eng.is_alive and eng.endpoint == f"tcp://127.0.0.1:{port}"
    assert daemon_mod.PID_PATH.exists()
    # The first display event is the daemon's current state: nothing to show.
    assert rec.wait_for(lambda n, p: n == "display")[1]["state"] == {"status": "sleeping"}

    send(port, {"event": "dismiss", "hook": "UserPromptSubmit", "session_id": "s1",
                "project": "demo", "pid": None})
    ev = rec.wait_for(lambda n, p: n == "display" and "anims" in p["state"])
    assert ev[1]["state"]["anims"] == ["thinking"]
    send(port, {"event": "tool_use", "session_id": "s1", "tool_name": "Bash", "project": "demo"})
    rec.wait_for(lambda n, p: n == "display" and p["state"].get("anims") == ["building"])
    hook = rec.wait_for(lambda n, p: n == "hook" and p["msg"].get("tool_name") == "Bash")
    assert hook[1]["msg"]["project"] == "demo"

    assert eng.stop() is True
    assert eng.status == EngineStatus.STOPPED and not eng.is_alive
    assert not daemon_mod.PID_PATH.exists()
    with pytest.raises(OSError):  # the port is free again
        send(port, {"event": "clear"})
    os.close(daemon_mod._acquire_lock(takeover=False))  # and so is the lock


def test_compact_reports_the_sweeping_one_shot(make_engine):
    eng, rec, port = make_engine()
    eng.start()
    rec.wait_status(EngineStatus.RUNNING)
    send(port, {"event": "session_start", "session_id": "s1", "project": "p"})
    send(port, {"event": "compact", "session_id": "s1"})
    assert rec.wait_for(lambda n, p: n == "oneshot")[1] == {"anim": "sweeping"}


def test_runs_without_a_transport_when_ble_is_off(make_engine):
    eng, rec, _ = make_engine()
    eng.start()
    rec.wait_status(EngineStatus.RUNNING)
    assert eng._daemon._transports == {}  # nothing here can connect to a bar


def test_port_in_use_is_a_conflict_not_a_crash_and_retry_recovers(make_engine, state_dir):
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    port = other.getsockname()[1]
    eng, rec, _ = make_engine(tcp_port=port)
    try:
        eng.start()
        _, payload = rec.wait_status(EngineStatus.CONFLICT)
        assert payload["message"].startswith(CONFLICT_MESSAGE)
        assert str(port) in payload["message"]
        eng._thread.join(5)
        assert not eng.is_alive
        assert eng.watchdog(now=time.monotonic() + 1000) is False  # a conflict waits for Retry
        # The failed start gave the lock and the PID file back ...
        assert not daemon_mod.PID_PATH.exists()
        try:
            os.close(daemon_mod._acquire_lock(takeover=False))
        except SystemExit:
            pytest.fail("the failed start kept the lock")
        # ... and never touched the other program's socket: it still accepts.
        assert other.getsockname()[1] == port
    finally:
        other.close()
    rec.events.clear()
    assert eng.retry() is True  # the user closed the other program and clicked Retry
    rec.wait_status(EngineStatus.RUNNING)


def test_a_lock_held_by_another_daemon_is_a_conflict(make_engine, state_dir):
    holder = daemon_mod._acquire_lock(takeover=False)  # "the terminal daemon"
    try:
        eng, rec, _ = make_engine()
        eng.start()
        _, payload = rec.wait_status(EngineStatus.CONFLICT)
        assert payload["message"] == CONFLICT_MESSAGE
        assert not daemon_mod.PID_PATH.exists()  # the other daemon's file is not ours to write
        eng.stop()
        assert eng.status == EngineStatus.STOPPED
    finally:
        os.close(holder)
    rec.events.clear()
    eng.retry()
    rec.wait_status(EngineStatus.RUNNING)


def test_watchdog_restarts_a_dead_thread_with_a_back_off(make_engine):
    eng, rec, port = make_engine()
    eng.start()
    rec.wait_status(EngineStatus.RUNNING)
    # Kill the daemon the way a bug would: its run() ends, nobody asked for it.
    eng._loop.call_soon_threadsafe(eng._daemon._request_shutdown)
    rec.wait_status(EngineStatus.ERROR)
    eng._thread.join(10)
    assert not eng.is_alive
    t0 = time.monotonic()
    assert eng.watchdog(now=eng._died_at) is False          # too soon: back-off
    assert eng.watchdog(now=eng._died_at + 5) is True       # long enough
    rec.events.clear()
    rec.wait_status(EngineStatus.RUNNING)
    send(port, {"event": "session_start", "session_id": "again"})
    rec.wait_for(lambda n, p: n == "hook")


def test_watchdog_does_nothing_after_stop_or_before_start(make_engine):
    eng, rec, _ = make_engine()
    assert eng.watchdog(now=1e9) is False  # never started
    eng.start()
    rec.wait_status(EngineStatus.RUNNING)
    eng.stop()
    assert eng.watchdog(now=time.monotonic() + 1e6) is False  # the user stopped it


def test_stop_is_safe_when_nothing_runs(make_engine):
    eng, rec, _ = make_engine()
    assert eng.stop() is True
    assert eng.stop() is True


def test_a_bad_port_setting_is_an_error_not_an_exception(state_dir, monkeypatch):
    monkeypatch.setenv("CLAWD_TANK_TCP_PORT", "banana")
    rec = Recorder()
    eng = DaemonEngine(rec, no_ble=True, sessions_path=state_dir / "s.json")
    eng.start()
    _, payload = rec.wait_status(EngineStatus.ERROR)
    assert "CLAWD_TANK_TCP_PORT" in payload["message"]
    eng.stop()


def test_an_observer_that_raises_does_not_stop_the_daemon(state_dir):
    calls = []

    def angry(name, payload):
        calls.append(name)
        if name == "display":
            raise RuntimeError("boom")

    eng = DaemonEngine(angry, no_ble=True, tcp_port=free_port(), transport="tcp",
                       sessions_path=state_dir / "s.json")
    eng.start()
    try:
        deadline = time.monotonic() + 10
        while eng.status != EngineStatus.RUNNING and time.monotonic() < deadline:
            time.sleep(0.05)
        assert eng.status == EngineStatus.RUNNING and eng.is_alive
    finally:
        eng.stop()
