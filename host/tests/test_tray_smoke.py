"""Headless smoke test of the whole tray app, as a real process.

It runs the app from source (``python -m clawd_tank_tray``) and, if it has been
built and is not older than the source, the packaged
``host/tray/dist/ClawdTank/ClawdTank.exe`` (or the exe that ``CLAWD_TANK_TEST_EXE``
names, e.g. a staged build in ``host/tray/dist-next``, which is always run; the value
``none`` skips the exe run). Both are fully isolated from the user's real setup:

* ``CLAWD_TANK_NO_BLE=1``: no Bluetooth transport, so the bar is never touched;
* ``CLAWD_TANK_TCP_PORT``: a free port, never 47421;
* ``USERPROFILE`` and ``LOCALAPPDATA`` point into a temp folder, so the daemon's
  lock, PID file and sessions, the log, and the settings.json the hooks would go
  into are all temporary (and ``CLAWD_TANK_CLAUDE_DIR`` says the same);
* ``CLAWD_TANK_INSTANCE`` gives the mutex and events a name of their own, and
  ``CLAWD_TANK_RUN_KEY`` a temp registry key;
* ``--minimized --no-prompt``: no window and no dialog opens by itself.

The tray icon does appear in the notification area for a few seconds.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
import winreg
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parent.parent
_EXE_OVERRIDE = os.environ.get("CLAWD_TANK_TEST_EXE", "")
EXE = (Path(_EXE_OVERRIDE) if _EXE_OVERRIDE and _EXE_OVERRIDE.lower() != "none"
       else HOST / "tray" / "dist" / "ClawdTank" / "ClawdTank.exe")
REAL_HOME = Path.home()

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the tray app is Windows only")


def free_port(start: int = 47499) -> int:
    for port in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port")


def listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def wait_until(pred, timeout, step=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return pred()


def real_state():
    """What the user's real files look like (existence, size, mtime), to prove a
    run did not touch them."""
    out = {}
    # Not sessions.json: a daemon the user has running keeps rewriting it as they work.
    # That the run was isolated is shown by its own daemon.pid in the temp home.
    for rel in (".clawd-tank/daemon.pid", ".clawd-tank/daemon.lock",
                ".clawd-tank/clawd-tank-notify.py", ".claude/settings.json"):
        p = REAL_HOME / rel
        if p.is_file():
            st = p.stat()
            out[rel] = (st.st_size, st.st_mtime_ns)
        else:
            out[rel] = None
    return out


class Run:
    def __init__(self, command, tmp_path):
        self.tmp = tmp_path
        self.port = free_port()
        self.run_key = rf"Software\ClawdTankTest\{uuid.uuid4().hex}\Run"
        self.home = tmp_path / "home"
        self.local = tmp_path / "local"
        self.claude = tmp_path / "claude"
        for d in (self.home, self.local, self.claude):
            d.mkdir()
        self.command = command
        env = dict(os.environ)
        env.update(
            USERPROFILE=str(self.home), HOME=str(self.home), LOCALAPPDATA=str(self.local),
            CLAWD_TANK_NO_BLE="1", CLAWD_TANK_TCP_PORT=str(self.port),
            CLAWD_TANK_TRANSPORT="tcp", CLAWD_TANK_INSTANCE=f"smoke-{uuid.uuid4().hex[:8]}",
            CLAWD_TANK_CLAUDE_DIR=str(self.claude), CLAUDE_CONFIG_DIR=str(self.claude),
            CLAWD_TANK_RUN_KEY=self.run_key, CLAWD_TANK_NO_PROMPT="1")
        env.pop("PYTHONPATH", None)
        self.env = env
        self.proc = None

    @property
    def log(self) -> Path:
        return self.local / "ClawdTank" / "tray.log"

    def log_text(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def start(self, *flags):
        """Launch with `flags` (default: --minimized --no-prompt)."""
        self.proc = subprocess.Popen(
            self.command + list(flags or ("--minimized", "--no-prompt")), env=self.env,
            cwd=str(HOST),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.proc

    def control(self, flag: str, timeout=15) -> int:
        return subprocess.run(self.command + [flag], env=self.env, cwd=str(HOST),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout).returncode

    def send(self, msg: dict) -> None:
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
            s.sendall(json.dumps(msg).encode() + b"\n")

    def cleanup(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        try:  # the temp registry key was never created by this app unless Autostart ran
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.run_key)
        except OSError:
            pass


def _older_than_the_source(exe: Path) -> bool:
    """A packaged exe built before the newest change to the package cannot show what the
    source now does (it still runs, as it always did, on whoever's machine it sits)."""
    newest = max(p.stat().st_mtime for p in (HOST / "clawd_tank_tray").glob("*.py"))
    return exe.stat().st_mtime < newest


LAUNCHERS = [pytest.param([sys.executable, "-m", "clawd_tank_tray"], id="source")]
if not EXE.exists() or _EXE_OVERRIDE.lower() == "none":
    LAUNCHERS.append(pytest.param([str(EXE)], id="exe", marks=pytest.mark.skip(
        reason="ClawdTank.exe is not built (host/tray/build-tray.ps1)")))
elif not _EXE_OVERRIDE and _older_than_the_source(EXE):
    # the default exe (host/tray/dist) only; one named with CLAWD_TANK_TEST_EXE is always run
    LAUNCHERS.append(pytest.param([str(EXE)], id="exe", marks=pytest.mark.skip(
        reason="host/tray/dist is older than the source: rebuild it (host/tray/build-tray.ps1), or "
               "point CLAWD_TANK_TEST_EXE at a fresh build such as host/tray/dist-next")))
else:
    LAUNCHERS.append(pytest.param([str(EXE)], id="exe"))


@pytest.mark.parametrize("command", LAUNCHERS)
def test_app_starts_serves_hooks_and_quits(command, tmp_path):
    before = real_state()
    run = Run(command, tmp_path)
    try:
        proc = run.start()

        # alive, and the hook port listens (within 15 s)
        assert wait_until(lambda: listening(run.port), 15), run.log_text()
        assert proc.poll() is None, run.log_text()

        # the log exists and has the engine-started line (and the tray's)
        assert wait_until(lambda: "Engine started" in run.log_text(), 5), run.log_text()
        assert wait_until(lambda: "Tray icon ready" in run.log_text(), 10), run.log_text()
        assert "Clawd Tank is running" in run.log_text()
        # no Bluetooth to wait for: the icon is the connected one, and it is set once
        assert wait_until(lambda: "Tray icon: idle" in run.log_text(), 10), run.log_text()

        # the daemon wrote its lock and PID into the temp home, not the real one
        pid_file = run.home / ".clawd-tank" / "daemon.pid"
        assert wait_until(pid_file.exists, 5)
        assert pid_file.read_text().strip().isdigit()

        # a notify message is accepted and the daemon acts on it
        run.send({"event": "dismiss", "hook": "UserPromptSubmit",
                  "session_id": "smoke-session", "project": "smoke"})
        assert wait_until(lambda: "Display: anims=['thinking']" in run.log_text(), 5), run.log_text()
        assert "Socket msg: event=dismiss hook=UserPromptSubmit" in run.log_text()

        # what Claude does is text only: more events, and the icon is not touched
        icon_changes = run.log_text().count("Tray icon: ")
        for tool in ("Bash", "Edit", "Read"):
            run.send({"event": "tool_use", "session_id": "smoke-session", "tool_name": tool,
                      "project": "smoke"})
        assert wait_until(lambda: run.log_text().count("Socket msg: event=tool_use") >= 3, 5), (
            run.log_text())
        time.sleep(1.0)   # ten pump cycles
        assert run.log_text().count("Tray icon: ") == icon_changes, run.log_text()

        # a second launch exits at once, leaves the first one running, and makes
        # it open its flyout (as does --show)
        second = subprocess.run(command + ["--minimized", "--no-prompt"], env=run.env,
                                cwd=str(HOST), timeout=15, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert second.returncode == 0
        assert proc.poll() is None
        assert wait_until(lambda: "Show requested" in run.log_text(), 5), run.log_text()
        assert run.control("--show") == 0
        assert wait_until(lambda: run.log_text().count("Show requested") >= 2, 5), run.log_text()
        # ... and the flyout opened: it was placed (next to the tray icon or in the corner)
        assert wait_until(lambda: re.search(r"Flyout \d+x\d+ at -?\d+,-?\d+ \(\w+ taskbar",
                                            run.log_text()), 5), run.log_text()

        # Quit through the control message: clean stop, clean exit
        assert run.control("--quit") == 0
        assert proc.wait(timeout=15) == 0, run.log_text()
        text = run.log_text()
        assert "Quit requested" in text and "Clawd Tank exited" in text
        assert not listening(run.port)
        assert not pid_file.exists()
        assert "Traceback" not in text, text
    finally:
        run.cleanup()
    assert real_state() == before, "the run touched the user's real files"


def started_the_way_the_user_does(run: Run):
    """`ClawdTank.exe --minimized` and nothing else: no --no-prompt on the command
    line (the temp Claude folder has no hooks, so the environment says no-prompt
    instead, or a Yes/No box would open), no --selftest. Then one hook message over
    TCP, as the notify script sends it."""
    proc = run.start("--minimized")
    assert wait_until(lambda: "Engine started" in run.log_text(), 10), run.log_text()
    assert wait_until(lambda: listening(run.port), 10), run.log_text()
    run.send({"event": "dismiss", "hook": "UserPromptSubmit",
              "session_id": "user-launch", "project": "userlike"})
    assert wait_until(lambda: "Socket msg: event=dismiss hook=UserPromptSubmit" in run.log_text(),
                      10), run.log_text()
    return proc


@pytest.mark.parametrize("command", LAUNCHERS)
def test_started_as_the_user_does_it_the_log_has_the_engine_and_the_hook_lines(command, tmp_path):
    """The regression that went unseen: the app ran and served hooks while its
    log stayed empty. Both the engine-started line and the Socket msg line for a
    message sent over TCP must be in the log file within 10 s."""
    run = Run(command, tmp_path)
    try:
        proc = started_the_way_the_user_does(run)
        text = run.log_text()
        assert "Clawd Tank is running" in text and "Tray icon: idle" in text
        assert run.control("--quit") == 0
        assert proc.wait(timeout=15) == 0
    finally:
        run.cleanup()


@pytest.mark.parametrize("command", LAUNCHERS)
def test_a_nearly_full_log_that_another_program_holds_open_does_not_silence_the_app(command, tmp_path):
    """The log sits just under its 1 MB limit and something (Notepad after Open
    log, an editor, a tail) has it open, so it cannot be rotated. The standard
    rotating handler dropped every line then; the app must log regardless."""
    run = Run(command, tmp_path)
    run.log.parent.mkdir(parents=True)
    run.log.write_bytes((b"old log line " + b"x" * 86 + b"\n") * 9999)   # 999,900 bytes
    with open(run.log, "rb"):
        try:
            proc = started_the_way_the_user_does(run)
            assert wait_until(lambda: "Could not rotate" in run.log_text(), 5), run.log_text()[-600:]
            assert not (run.log.parent / "tray.log.1").exists()   # it could not, and said so
            assert run.control("--quit") == 0
            assert proc.wait(timeout=15) == 0
            assert "Clawd Tank exited" in run.log_text()          # even the last line landed
        finally:
            run.cleanup()


@pytest.mark.parametrize("command", LAUNCHERS)
def test_quit_with_nothing_running_reports_it(command, tmp_path):
    run = Run(command, tmp_path)
    assert run.control("--quit") == 1


def test_selftest_of_the_source_app():
    """--selftest imports everything and reads the assets, no window, no radio."""
    result = subprocess.run(
        [sys.executable, "-m", "clawd_tank_tray", "--selftest"], cwd=str(HOST), timeout=60,
        capture_output=True, text=True,
        env={**os.environ, "LOCALAPPDATA": str(HOST / ".pytest_cache" / "selftest-local")})
    assert result.returncode == 0, result.stderr
