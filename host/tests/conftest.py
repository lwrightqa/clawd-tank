"""Shared test fixtures."""

import sys

import pytest

import clawd_tank_daemon.session_store as session_store

# These test the Windows tray app and fail while the file is imported anywhere
# else (winreg, ctypes.WinDLL at module level, tkinter on Win32 handles), before
# a skip mark could apply, so they are not collected there.
if sys.platform != "win32":
    collect_ignore = [
        "test_tray_app.py",
        "test_tray_autostart.py",
        "test_tray_icon.py",
        "test_tray_integration.py",
        "test_tray_smoke.py",
        "test_tray_window.py",
    ]


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    """Redirect session persistence to a temp dir for all tests.

    Prevents tests from reading/writing the real ~/.clawd-tank/sessions.json.
    """
    monkeypatch.setattr(session_store, "SESSIONS_PATH", tmp_path / "sessions.json")
