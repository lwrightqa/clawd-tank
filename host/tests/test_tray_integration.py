"""The whole app in this process, with a real tkinter loop and a real tray icon:
a terminal daemon holds the lock (the user's usual situation), the app shows the
red state and does not touch it, the other daemon goes away, Retry brings the
engine up, hooks and Claude's activity arrive without touching the tray icon (a
still picture; no animation timer exists), Quit ends the loop and cleans up.

No Bluetooth (``no_ble``), a free port, the lock and PID file in a temp folder, a
temp settings.json and Run key. The tray icon is visible for a second or two."""

import json
import os
import socket
import threading
import time
import tkinter as tk
import uuid
import winreg

import pytest

import clawd_tank_daemon.daemon as daemon_mod
from clawd_tank_tray.app import TrayApp
from clawd_tank_tray.autostart import Autostart
from clawd_tank_tray.config import AppConfig
from clawd_tank_tray.hookctl import HookManager
from clawd_tank_tray.options import Options
from clawd_tank_tray.single_instance import SingleInstance
from clawd_tank_tray.state import EngineStatus
from clawd_tank_tray.tray_icon import ClawdTrayIcon


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(pred, timeout=15.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def test_conflict_then_retry_then_a_hook_then_quit(tmp_path, monkeypatch, capfd):
    # Spies: every message to the notification area, every Tk timer.
    shell, timers = [], []
    real_icon, real_title = ClawdTrayIcon._update_icon, ClawdTrayIcon._update_title
    monkeypatch.setattr(ClawdTrayIcon, "_update_icon",
                        lambda self: (shell.append("icon"), real_icon(self))[1])
    monkeypatch.setattr(ClawdTrayIcon, "_update_title",
                        lambda self: (shell.append("title"), real_title(self))[1])
    real_after = tk.Misc.after
    monkeypatch.setattr(tk.Misc, "after", lambda self, ms, func=None, *a: (
        timers.append(getattr(func, "__name__", repr(func))), real_after(self, ms, func, *a))[1])

    monkeypatch.setattr(daemon_mod, "PID_PATH", tmp_path / "daemon.pid")
    monkeypatch.setattr(daemon_mod, "LOCK_PATH", tmp_path / "daemon.lock")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    port = free_port()
    run_key = rf"Software\ClawdTankTest\{uuid.uuid4().hex}\Run"

    instance = SingleInstance(f"itest-{uuid.uuid4().hex[:8]}")
    assert instance.acquire()
    app = TrayApp(
        Options(no_ble=True, no_prompt=True, minimized=True, tcp_port=port),
        instance=instance,
        hooks=HookManager(tmp_path / "claude" / "settings.json", which=lambda n: "py.exe"),
        config=AppConfig(tmp_path / "state.json"),
        autostart=Autostart(run_key, command="x"))

    holder = daemon_mod._acquire_lock(takeover=False)  # "the terminal daemon"
    seen, errors = {}, []

    def drive():
        try:
            wait_for(lambda: app.view.engine == EngineStatus.CONFLICT, what="the conflict")
            wait_for(lambda: app._icon_name == "error", what="the red-dot icon")
            seen["conflict_problem"] = app.view.problem()
            seen["conflict_tooltip"] = app.view.tooltip()
            seen["can_retry"] = app.view.can_retry
            seen["pid_untouched"] = not daemon_mod.PID_PATH.exists()
            os.close(holder)                       # the user closes the other daemon ...
            app.post("retry")                      # ... and clicks Retry
            wait_for(lambda: app.view.engine == EngineStatus.RUNNING, what="the engine running")
            wait_for(lambda: app._icon_name == "idle", what="the connected icon (no BLE)")
            time.sleep(0.5)                        # let the tray settle
            quiet = len(shell)
            for msg, marker in (({"event": "dismiss", "hook": "UserPromptSubmit"}, "UserPromptSubmit"),
                                ({"event": "tool_use", "tool_name": "Bash"}, "tool_use Bash"),
                                ({"event": "tool_use", "tool_name": "Edit"}, "tool_use Edit")):
                with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                    s.sendall(json.dumps({**msg, "session_id": "it", "project": "demo"}).encode()
                              + b"\n")
                wait_for(lambda: marker in app.view.last_event, what=f"the hook {marker}")
            wait_for(lambda: app.view.sessions == 1, what="the session")
            time.sleep(0.5)                        # five pump cycles: the window has caught up
            seen["sessions"] = app.view.sessions_text()
            seen["last_event"] = app.view.last_event_text()
            seen["status_line"] = app._status_line
            seen["icon_after_hooks"] = app._icon_name
            seen["shell_updates_by_hooks"] = len(shell) - quiet
        except BaseException as e:  # noqa: BLE001 - reported to the test below
            errors.append(e)
        finally:
            app.post("quit")

    driver = threading.Thread(target=drive, name="itest-driver", daemon=True)
    driver.start()
    try:
        with capfd.disabled():  # tkinter starts reliably with fd capture off
            code = app.run()
    finally:
        driver.join(30)
        try:
            os.close(holder)
        except OSError:
            pass  # the driver already did
        instance.release()
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, run_key)
        except OSError:
            pass

    assert not errors, errors
    assert code == 0
    assert seen["conflict_problem"].startswith("Another Clawd Tank daemon is running; close it.")
    assert "another clawd tank daemon" in seen["conflict_tooltip"].lower()
    assert seen["can_retry"] is True
    assert seen["pid_untouched"] is True
    assert seen["sessions"] == "1 active session"
    assert "tool_use Edit in demo" in seen["last_event"]
    # Claude's activity changed the text only: the icon stayed the connected one and
    # the notification area was not sent a single icon or tooltip update for it
    assert seen["icon_after_hooks"] == "idle"
    assert seen["shell_updates_by_hooks"] == 0, shell
    assert shell.count("icon") >= 3 and "title" in shell, shell   # the spies do see real changes
    assert seen["status_line"] == "Bluetooth off (test mode)"
    # and no timer other than the housekeeping ones was ever scheduled
    assert timers and set(timers) <= {"_pump", "_watchdog", "<lambda>", "hide"}, set(timers)
    # Quit stopped the engine and gave everything back
    assert app.engine.status == EngineStatus.STOPPED and not app.engine.is_alive
    assert not daemon_mod.PID_PATH.exists()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    os.close(daemon_mod._acquire_lock(takeover=False))
    assert instance.acquire()  # the single-instance name was released
    instance.release()
