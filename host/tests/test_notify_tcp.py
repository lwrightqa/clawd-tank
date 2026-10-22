"""The hook side of the TCP transport: a notify script talking to a live SocketServer.

Two scripts reach the daemon and both are exercised here, as real subprocesses
(or, for the repo script's helpers, as a loaded module):

  * NOTIFY_SCRIPT, the standalone copy the installer writes (hooks.py);
  * host/clawd-tank-notify, the repo script that can also start the daemon.

These run on every platform; they are the Windows equivalent of
test_notify_script.py, which needs Unix sockets.
"""

import asyncio
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

from clawd_tank_daemon.protocol import hook_payload_to_daemon_message
from clawd_tank_daemon.socket_server import SocketServer
from clawd_tank_menubar import hooks
from clawd_tank_menubar.hooks import NOTIFY_SCRIPT

from .test_socket_server import free_port

HOST_DIR = Path(__file__).resolve().parent.parent
IS_WINDOWS = sys.platform == "win32"


def _env(port, **extra):
    env = os.environ.copy()
    env.pop("CLAWD_TANK_SOCKET", None)
    env["CLAWD_TANK_TRANSPORT"] = "tcp"
    env["CLAWD_TANK_TCP_PORT"] = str(port)
    env["CLAUDE_PID"] = "4242"
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


@pytest.fixture
def notify_py(tmp_path):
    path = tmp_path / "clawd-tank-notify.py"
    hooks.write_notify_script(path)
    return path


class Daemon:
    """A real SocketServer on a free TCP port that records what it receives."""

    def __init__(self):
        self.port = free_port()
        self.received = []

        async def on_message(msg):
            self.received.append(msg)

        self.server = SocketServer(on_message=on_message, transport="tcp", tcp_port=self.port)

    async def run_script(self, script, stdin_bytes, env=None, timeout=15):
        """Run a notify script as a subprocess without blocking the server's loop."""
        env = _env(self.port) if env is None else env
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: subprocess.run(
            [sys.executable, str(script)], input=stdin_bytes, env=env,
            capture_output=True, timeout=timeout, cwd=str(HOST_DIR),
        ))

    async def send_hook(self, script, hook, **kw):
        result = await self.run_script(script, json.dumps(hook).encode("utf-8"), **kw)
        await asyncio.sleep(0.1)  # let the server handle the line
        return result


@pytest_asyncio.fixture
async def daemon():
    d = Daemon()
    await d.server.start()
    yield d
    await d.server.stop()


# --- NOTIFY_SCRIPT -> SocketServer round trips ---------------------------------


@pytest.mark.asyncio
async def test_round_trip_session_start(daemon, notify_py, tmp_path):
    r = await daemon.send_hook(notify_py, {
        "hook_event_name": "SessionStart", "session_id": "tcp-1",
        "cwd": str(tmp_path / "myproj"), "source": "startup",
    })
    assert r.returncode == 0, r.stderr.decode()
    assert len(daemon.received) == 1
    msg = daemon.received[0]
    assert msg["event"] == "session_start"
    assert msg["session_id"] == "tcp-1"
    assert msg["project"] == "myproj"
    assert msg["source"] == "startup"


_CROSS_CHECK_HOOKS = [
    {"hook_event_name": "SessionStart", "session_id": "s", "cwd": "/x/proj", "source": "startup"},
    {"hook_event_name": "PreToolUse", "session_id": "s", "cwd": "/x/proj", "tool_name": "Bash"},
    {"hook_event_name": "PostToolUse", "session_id": "s", "cwd": "/x/proj", "tool_name": "AskUserQuestion"},
    {"hook_event_name": "PermissionRequest", "session_id": "s", "cwd": "/x/proj", "tool_name": "Bash"},
    {"hook_event_name": "PostToolUseFailure", "session_id": "s", "cwd": "/x/proj", "tool_name": "Read"},
    {"hook_event_name": "PreCompact", "session_id": "s", "cwd": "/x/proj"},
    {"hook_event_name": "Stop", "session_id": "s", "cwd": "/x/proj"},
    {"hook_event_name": "StopFailure", "session_id": "s", "cwd": "/x/proj", "error": "boom"},
    {"hook_event_name": "Notification", "notification_type": "idle_prompt",
     "session_id": "s", "cwd": "/x/proj", "message": "m"},
    {"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": "/x/proj"},
    {"hook_event_name": "SessionEnd", "session_id": "s", "cwd": "/x/proj", "reason": "logout"},
    {"hook_event_name": "SubagentStart", "session_id": "s", "cwd": "/x/proj", "agent_id": "a"},
    {"hook_event_name": "SubagentStop", "session_id": "s", "cwd": "/x/proj", "agent_id": "a"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("hook", _CROSS_CHECK_HOOKS, ids=[h["hook_event_name"] for h in _CROSS_CHECK_HOOKS])
async def test_wire_message_matches_the_protocol_converter(daemon, notify_py, hook):
    """The wire protocol is identical to the Unix one: the message that arrives over
    TCP is exactly what protocol.hook_payload_to_daemon_message builds (modulo pid)."""
    r = await daemon.send_hook(notify_py, hook)
    assert r.returncode == 0, r.stderr.decode()
    assert len(daemon.received) == 1
    got = {k: v for k, v in daemon.received[0].items() if k != "pid"}
    want = {k: v for k, v in (hook_payload_to_daemon_message(hook) or {}).items() if k != "pid"}
    assert got == want


@pytest.mark.asyncio
async def test_irrelevant_hook_sends_nothing(daemon, notify_py):
    r = await daemon.send_hook(notify_py, {"hook_event_name": "SomeUnhandledEvent", "session_id": "x"})
    assert r.returncode == 0
    assert daemon.received == []


@pytest.mark.asyncio
async def test_non_ascii_project_folder_survives(daemon, notify_py):
    """Windows' default text encoding is not UTF-8; the script reads stdin as UTF-8 bytes."""
    hook = {"hook_event_name": "SessionStart", "session_id": "u", "cwd": "C:\\work\\Café-日本"}
    r = await daemon.run_script(notify_py, json.dumps(hook, ensure_ascii=False).encode("utf-8"))
    await asyncio.sleep(0.1)
    assert r.returncode == 0, r.stderr.decode()
    assert daemon.received[0]["project"] == ("Café-日本" if IS_WINDOWS else "C:\\work\\Café-日本")


@pytest.mark.asyncio
async def test_invalid_json_exits_1_and_sends_nothing(daemon, notify_py):
    r = await daemon.run_script(notify_py, b"{not json")
    await asyncio.sleep(0.05)
    assert r.returncode == 1
    assert daemon.received == []


@pytest.mark.asyncio
async def test_empty_stdin_exits_0_and_sends_nothing(daemon, notify_py):
    r = await daemon.run_script(notify_py, b"   \n")
    await asyncio.sleep(0.05)
    assert r.returncode == 0
    assert daemon.received == []


def test_daemon_not_running_is_a_quiet_success(notify_py):
    """With nobody listening the hook must exit 0, silently and quickly: a failing
    hook would show an error in Claude Code on every event."""
    port = free_port()
    start = time.monotonic()
    r = subprocess.run(
        [sys.executable, str(notify_py)], env=_env(port), capture_output=True, timeout=15,
        input=json.dumps({"hook_event_name": "Stop", "session_id": "s", "cwd": "/p"}).encode(),
    )
    assert r.returncode == 0
    assert r.stderr == b""
    assert time.monotonic() - start < 10


def test_bad_port_variable_falls_back_instead_of_crashing(notify_py):
    r = subprocess.run(
        [sys.executable, str(notify_py)], env=_env("not-a-port"), capture_output=True, timeout=15,
        input=json.dumps({"hook_event_name": "Stop", "session_id": "s", "cwd": "/p"}).encode(),
    )
    assert r.returncode == 0  # connects to the default port, finds nobody, exits quietly


@pytest.mark.skipif(not IS_WINDOWS, reason="auto-selects TCP only where Unix sockets are missing")
@pytest.mark.asyncio
async def test_windows_picks_tcp_with_no_transport_variable_at_all(daemon, notify_py):
    env = _env(daemon.port, CLAWD_TANK_TRANSPORT=None)
    r = await daemon.send_hook(notify_py, {"hook_event_name": "Stop", "session_id": "w", "cwd": "/p"}, env=env)
    assert r.returncode == 0, r.stderr.decode()
    assert [m["session_id"] for m in daemon.received] == ["w"]


# --- the Claude Code PID the script stamps -----------------------------------------


@pytest.mark.asyncio
async def test_pid_stamped_on_every_event(daemon, notify_py):
    r = await daemon.send_hook(notify_py, {"hook_event_name": "UserPromptSubmit", "session_id": "p", "cwd": "/p"})
    assert r.returncode == 0, r.stderr.decode()
    pid = daemon.received[0]["pid"]
    if IS_WINDOWS:
        assert pid == 4242  # CLAUDE_PID, which Claude Code exports to what it spawns
    else:
        assert isinstance(pid, int) and pid > 0


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows PID resolution")
@pytest.mark.asyncio
async def test_windows_without_claude_pid_never_reports_the_bash_pid(daemon, notify_py):
    """No CLAUDE_PID: the script walks the ancestors for claude.exe. Under this test
    run there may or may not be one, but the answer is a PID or null, never a crash."""
    env = _env(daemon.port, CLAUDE_PID=None)
    r = await daemon.send_hook(notify_py, {"hook_event_name": "Stop", "session_id": "n", "cwd": "/p"}, env=env)
    assert r.returncode == 0, r.stderr.decode()
    msg = daemon.received[0]
    assert "pid" in msg and (msg["pid"] is None or isinstance(msg["pid"], int))
    assert msg["pid"] != os.getpid()


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows PID resolution")
@pytest.mark.asyncio
async def test_null_pid_is_accepted_by_the_daemon_logic(daemon, notify_py):
    """A null pid (no claude.exe found) must flow through the daemon's session
    tracking without evicting or crashing: it just opts that session out of PID checks."""
    from clawd_tank_daemon.daemon import ClawdDaemon
    d = ClawdDaemon(sim_only=True)
    d._transports.clear()
    d._transport_queues.clear()
    await d._handle_message({"event": "session_start", "session_id": "np", "project": "p", "pid": None})
    await d._handle_message({"event": "tool_use", "session_id": "np", "tool_name": "Bash", "pid": None})
    assert "pid" not in d._session_states["np"]
    assert d._check_liveness() == []


# --- the installed copy ---------------------------------------------------------------


def test_installed_script_has_unix_line_endings_and_no_bom(tmp_path):
    path = tmp_path / "sub" / "clawd-tank-notify.py"
    hooks.write_notify_script(path)
    data = path.read_bytes()
    assert b"\r" not in data
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.decode("utf-8") == NOTIFY_SCRIPT


# --- host/clawd-tank-notify (the repo script) -------------------------------------------


@pytest.fixture
def repo_notify(monkeypatch, tmp_path):
    """host/clawd-tank-notify loaded as a module, with its state dir in tmp_path."""
    path = HOST_DIR / "clawd-tank-notify"
    loader = importlib.machinery.SourceFileLoader("clawd_tank_notify_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "CLAWD_DIR", tmp_path / ".clawd-tank")
    monkeypatch.setattr(module, "PID_PATH", tmp_path / ".clawd-tank" / "daemon.pid")
    return module


@pytest.mark.asyncio
async def test_repo_script_sends_over_tcp(daemon, repo_notify, monkeypatch):
    for k, v in _env(daemon.port).items():
        monkeypatch.setenv(k, v)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: repo_notify.send_to_daemon(
        {"hook_event_name": "UserPromptSubmit", "session_id": "r1", "cwd": "/p"}))
    await asyncio.sleep(0.1)
    assert len(daemon.received) == 1
    msg = daemon.received[0]
    assert msg["event"] == "dismiss" and msg["hook"] == "UserPromptSubmit" and msg["session_id"] == "r1"
    if IS_WINDOWS:
        assert msg["pid"] == 4242  # stamped: there is no `ps` to do it later


def test_repo_script_exits_0_when_daemon_is_down(repo_notify, monkeypatch, capsys):
    for k, v in _env(free_port()).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit) as exc:
        repo_notify.send_to_daemon({"hook_event_name": "Stop", "session_id": "s", "cwd": "/p"})
    assert exc.value.code == 0
    assert "Failed to send to daemon" in capsys.readouterr().err


def test_repo_script_detects_a_live_daemon_by_pid_file(repo_notify):
    repo_notify.CLAWD_DIR.mkdir(parents=True)
    repo_notify.PID_PATH.write_text(str(os.getpid()))
    assert repo_notify.is_daemon_running() is True
    assert repo_notify.PID_PATH.exists()


def test_repo_script_clears_a_stale_pid_file(repo_notify):
    repo_notify.CLAWD_DIR.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    repo_notify.PID_PATH.write_text(str(child.pid))
    assert repo_notify.is_daemon_running() is False
    assert not repo_notify.PID_PATH.exists()


def test_repo_script_clears_a_garbage_pid_file(repo_notify):
    repo_notify.CLAWD_DIR.mkdir(parents=True)
    repo_notify.PID_PATH.write_text("not a pid")
    assert repo_notify.is_daemon_running() is False
    assert not repo_notify.PID_PATH.exists()


def _run_main(repo_notify, monkeypatch, stdin_bytes):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_bytes)))
    calls = {"start": 0, "sent": []}
    monkeypatch.setattr(repo_notify, "start_daemon", lambda: calls.__setitem__("start", calls["start"] + 1))
    monkeypatch.setattr(repo_notify, "send_to_daemon", lambda payload: calls["sent"].append(payload))
    repo_notify.main()
    return calls


def test_repo_script_starts_the_daemon_only_when_it_is_not_running(repo_notify, monkeypatch):
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "s", "cwd": "/p"}).encode()
    monkeypatch.setattr(repo_notify, "is_daemon_running", lambda: False)
    calls = _run_main(repo_notify, monkeypatch, payload)
    assert calls["start"] == 1 and len(calls["sent"]) == 1

    monkeypatch.setattr(repo_notify, "is_daemon_running", lambda: True)
    calls = _run_main(repo_notify, monkeypatch, payload)
    assert calls["start"] == 0 and len(calls["sent"]) == 1


def test_repo_script_misconfigured_transport_is_reported_but_not_fatal(repo_notify, monkeypatch, capsys):
    monkeypatch.setenv("CLAWD_TANK_TRANSPORT", "carrier-pigeon")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"hook_event_name": "Stop"}')))
    started = []
    monkeypatch.setattr(repo_notify, "start_daemon", lambda: started.append(1))
    with pytest.raises(SystemExit) as exc:
        repo_notify.main()
    assert exc.value.code == 0
    assert started == []
    assert "CLAWD_TANK_TRANSPORT" in capsys.readouterr().err
