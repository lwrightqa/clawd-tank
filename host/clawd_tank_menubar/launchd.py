# host/clawd_tank_menubar/launchd.py
"""Manage Launch at Login via launchd user agent plist."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

PLIST_LABEL = "com.clawd-tank.menubar"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"

# What `python -m` starts when the app runs from source.
MODULE_PATH = "clawd_tank_menubar.app"


def program_arguments(frozen: bool, executable: str, python: str) -> List[str]:
    """The command the launch agent runs.

    Inside a py2app bundle (``frozen``) that is the app's own executable: there
    is no ``python -m`` to run, and the bundle's ``sys.executable`` is not the
    program to start. From source it is ``python -m clawd_tank_menubar.app``.
    """
    if frozen:
        return [executable]
    return [python, "-m", MODULE_PATH]


def _bundle_executable() -> Optional[str]:
    """The running .app's own executable (Contents/MacOS/<name>), or None.

    py2app's ``sys.executable`` can be the bundled interpreter rather than the
    launcher the user starts, so ask the bundle first."""
    try:
        from Foundation import NSBundle
        path = NSBundle.mainBundle().executablePath()
        return str(path) if path else None
    except Exception:
        return None


def current_program_arguments() -> List[str]:
    frozen = bool(getattr(sys, "frozen", False))
    executable = (_bundle_executable() if frozen else None) or sys.executable
    return program_arguments(frozen, executable, sys.executable)


def is_enabled() -> bool:
    """Check if the launch agent plist exists."""
    return PLIST_PATH.exists()


def enable() -> None:
    """Write the launch agent's plist; launchd starts the app from it at the next login.

    The agent is not loaded now. ``launchctl bootstrap`` of a RunAtLoad job starts it at
    once, and a second copy of the app taking over from the first (the menu bar daemon
    stops the daemon it finds, with SIGTERM) would end the copy the user is looking at.
    """
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": PLIST_LABEL,
        "ProgramArguments": current_program_arguments(),
        "RunAtLoad": True,
        "KeepAlive": False,
    }

    # Written whole, then renamed, so launchd never reads half a file.
    tmp = PLIST_PATH.with_name(PLIST_PATH.name + ".tmp")
    with open(tmp, "wb") as f:
        plistlib.dump(plist, f)
    os.replace(tmp, PLIST_PATH)


def is_stale() -> bool:
    """Check if the plist starts something other than the current program."""
    if not PLIST_PATH.exists():
        return False
    try:
        with open(PLIST_PATH, "rb") as f:
            plist = plistlib.load(f)
        program_args = plist.get("ProgramArguments", [])
        if program_args and program_args != current_program_arguments():
            return True
    except (OSError, plistlib.InvalidFileException):
        pass
    return False


def _is_the_job() -> bool:
    """True when launchd started this process as the launch agent (at login). launchd
    puts the job's label in XPC_SERVICE_NAME; an app started from Finder or a terminal
    gets "application.<bundle id>..." or "0"."""
    return os.environ.get("XPC_SERVICE_NAME") == PLIST_LABEL


def disable() -> None:
    """Remove the launch agent's plist, so the app is not started at the next login.

    When this process is the agent's job, ``launchctl bootout`` would stop the job, which
    is the app itself, before it had removed the file. Then only the file goes: the loaded
    job ends with the login session and nothing starts it again. Otherwise a stale loaded
    job (an older version loaded it) is unloaded too."""
    if not PLIST_PATH.exists():
        return

    PLIST_PATH.unlink(missing_ok=True)
    if _is_the_job():
        return
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{PLIST_LABEL}"],
        capture_output=True,
    )
