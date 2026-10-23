"""Daemon start, lock, signals and shutdown, with the TCP hook transport.

These drive ClawdDaemon.run() in-process (sim-only, no radio) with the PID and
lock files redirected into tmp_path.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clawd_tank_daemon import daemon as daemon_mod
from clawd_tank_daemon import hook_transport
from clawd_tank_daemon.daemon import ClawdDaemon

from .test_socket_server import free_port

HOST_DIR = Path(__file__).resolve().parent.parent
IS_WINDOWS = sys.platform == "win32"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """PID and lock files under tmp_path, never ~/.clawd-tank."""
    monkeypatch.setattr(daemon_mod, "PID_PATH", tmp_path / "daemon.pid")
    monkeypatch.setattr(daemon_mod, "LOCK_PATH", tmp_path / "daemon.lock")
    return tmp_path


def kill_tree(proc):
    """Kill proc and its children. A venv's python.exe on Windows is a launcher that runs
    the real interpreter as a child, so Popen.kill() alone would leave that child alive."""
    if proc.poll() is None:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            proc.kill()
    proc.wait(timeout=30)
    if proc.stdout is not None:
        proc.stdout.close()


def make_daemon(tmp_path, port=None):
    d = ClawdDaemon(sim_only=True, sessions_path=tmp_path / "sessions.json",
                    hook_transport="tcp", hook_tcp_port=port or free_port())
    d._transports.clear()
    d._transport_queues.clear()
    return d


async def send_line(port, obj):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(json.dumps(obj).encode() + b"\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()


# --- the lock ------------------------------------------------------------------------


def test_lock_is_exclusive_and_released_on_close(state_dir):
    fd = daemon_mod._acquire_lock(takeover=False)
    try:
        with pytest.raises(SystemExit) as exc:
            daemon_mod._acquire_lock(takeover=False)  # headless second daemon: exit quietly
        assert exc.value.code == 0
    finally:
        os.close(fd)
    fd2 = daemon_mod._acquire_lock(takeover=False)  # free again once the first closed
    os.close(fd2)


def test_lock_is_released_when_the_holder_dies(state_dir):
    """A crashed daemon must not leave a stale lock behind."""
    code = (
        "import sys, time; sys.path.insert(0, %r); "
        "from pathlib import Path; from clawd_tank_daemon import daemon as d; "
        "d.LOCK_PATH = Path(%r); d._acquire_lock(); print('locked', flush=True); time.sleep(60)"
    ) % (str(HOST_DIR), str(state_dir / "daemon.lock"))
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, cwd=str(HOST_DIR))
    try:
        assert child.stdout.readline().strip() == b"locked"
        with pytest.raises(SystemExit):
            daemon_mod._acquire_lock(takeover=False)  # held by another process
    finally:
        kill_tree(child)
    # The OS drops the lock with the process; allow a moment for its teardown.
    for _ in range(50):
        try:
            fd = daemon_mod._acquire_lock(takeover=False)
            break
        except SystemExit:
            time.sleep(0.1)
    else:
        pytest.fail("the lock was still held after its owner was killed")
    os.close(fd)


# --- run() / shutdown over TCP ------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_serves_hook_messages_over_tcp_then_shuts_down_cleanly(state_dir, monkeypatch):
    d = make_daemon(state_dir)
    monkeypatch.setattr(d, "_install_signal_handlers", lambda loop: None)  # keep pytest's own handlers
    task = asyncio.create_task(d.run())
    for _ in range(100):
        if daemon_mod.PID_PATH.exists() and d._socket._server is not None:
            break
        await asyncio.sleep(0.05)
    assert daemon_mod.PID_PATH.read_text() == str(os.getpid())

    await send_line(d._socket.tcp_port, {"event": "session_start", "session_id": "t1", "project": "p", "pid": os.getpid()})
    await send_line(d._socket.tcp_port, {"event": "tool_use", "session_id": "t1", "tool_name": "Bash", "pid": os.getpid()})
    await asyncio.sleep(0.2)
    assert d._session_states["t1"]["state"] == "working"
    assert d._compute_display_state()["anims"] == ["building"]

    d._request_shutdown()
    await asyncio.wait_for(task, timeout=10)
    assert not daemon_mod.PID_PATH.exists()
    with pytest.raises(OSError):  # the port is released (nobody accepts any more)
        hook_transport.connect("tcp", Path("unused"), d._socket.tcp_port)
    fd = daemon_mod._acquire_lock(takeover=False)  # and so is the lock
    os.close(fd)


@pytest.mark.asyncio
async def test_a_second_shutdown_request_is_ignored(state_dir, monkeypatch):
    d = make_daemon(state_dir)
    calls = []

    async def fake_shutdown():
        calls.append(1)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(d, "_shutdown", fake_shutdown)
    d._request_shutdown()
    d._request_shutdown()
    await d._shutdown_task
    assert calls == [1]


@pytest.mark.asyncio
async def test_a_signal_triggers_the_shutdown(state_dir):
    """SIGINT (Ctrl+C) reaches _request_shutdown: through loop.add_signal_handler on
    POSIX and through signal.signal on Windows, where asyncio has no signal support."""
    d = make_daemon(state_dir)
    requested = []
    d._request_shutdown = lambda: requested.append(1)  # not the real shutdown: the loop must survive
    saved = {s: signal.getsignal(s) for s in daemon_mod._shutdown_signals()}
    loop = asyncio.get_running_loop()
    try:
        d._install_signal_handlers(loop)
        signal.raise_signal(signal.SIGINT)
        for _ in range(40):
            if requested:
                break
            await asyncio.sleep(0.05)
        assert requested == [1]
    finally:
        for s in saved:
            if not IS_WINDOWS:
                try:
                    loop.remove_signal_handler(s)
                except Exception:
                    pass
            signal.signal(s, saved[s])


def test_windows_listens_for_ctrl_break():
    sigs = daemon_mod._shutdown_signals()
    assert signal.SIGTERM in sigs and signal.SIGINT in sigs
    if hasattr(signal, "SIGBREAK"):          # Windows only: Ctrl+Break, and console close
        assert signal.SIGBREAK in sigs
    else:
        assert len(sigs) == 2


# --- the real entry point, as a user starts it --------------------------------------------------


def _entry_env(home, port):
    env = os.environ.copy()
    env.update(USERPROFILE=str(home), HOME=str(home), CLAWD_TANK_TCP_PORT=str(port), PYTHONUNBUFFERED="1")
    env.pop("CLAWD_TANK_TRANSPORT", None)
    env.pop("CLAWD_TANK_SOCKET", None)
    return env


def test_python_dash_m_starts_listens_logs_and_stops_on_ctrl_break(tmp_path):
    """`python -m clawd_tank_daemon` end to end, in an isolated home: banner and
    listener in the log, a hook message handled, a clean exit on the stop signal."""
    port = free_port()
    log = tmp_path / "out.log"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    with open(log, "wb") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "clawd_tank_daemon", "--sim-only", "--transport", "tcp"],
            cwd=str(HOST_DIR), env=_entry_env(tmp_path, port), stdout=lf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=flags)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and b"Listening on" not in log.read_bytes():
                assert proc.poll() is None, log.read_text(errors="replace")
                time.sleep(0.1)
            msg = {"event": "session_start", "session_id": "e2e", "project": "demo", "pid": os.getpid()}
            with hook_transport.connect("tcp", Path("unused"), port) as s:
                s.sendall(json.dumps(msg).encode() + b"\n")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and b"session=e2e" not in log.read_bytes():
                time.sleep(0.1)
            proc.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGTERM)
            rc = proc.wait(timeout=20)
        finally:
            kill_tree(proc)
    text = log.read_text(encoding="utf-8", errors="replace")
    assert rc == 0, text
    assert f"tcp://127.0.0.1:{port}" in text
    assert "Listening on" in text and "Socket msg: event=session_start hook= session=e2e" in text
    assert "Shutting down" in text and "Daemon run() finished" in text
    assert not (tmp_path / ".clawd-tank" / "daemon.pid").exists()


def test_a_taken_port_exits_with_a_clear_message_instead_of_a_traceback(tmp_path):
    import socket
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        r = subprocess.run(
            [sys.executable, "-m", "clawd_tank_daemon", "--sim-only", "--transport", "tcp"],
            cwd=str(HOST_DIR), env=_entry_env(tmp_path, port), capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL)
    finally:
        blocker.close()
    out = r.stdout + r.stderr
    assert r.returncode == 1
    assert "CLAWD_TANK_TCP_PORT" in out and str(port) in out
    assert "Traceback" not in out


def test_bad_transport_flag_is_a_usage_error():
    r = subprocess.run([sys.executable, "-m", "clawd_tank_daemon", "--transport", "smoke-signals"],
                       cwd=str(HOST_DIR), capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    assert "invalid choice" in r.stderr


@pytest.mark.skipif(not IS_WINDOWS, reason="asks for a Unix socket on a platform that has none")
def test_unix_transport_on_windows_is_a_usage_error(tmp_path):
    r = subprocess.run([sys.executable, "-m", "clawd_tank_daemon", "--transport", "unix"],
                       cwd=str(HOST_DIR), env=_entry_env(tmp_path, free_port()),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    assert "Unix sockets are not available" in r.stderr
