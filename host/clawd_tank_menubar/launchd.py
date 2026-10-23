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
    """Write the launchd plist and load the agent."""
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": PLIST_LABEL,
        "ProgramArguments": current_program_arguments(),
        "RunAtLoad": True,
        "KeepAlive": False,
    }

    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
        capture_output=True,
    )


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


def disable() -> None:
    """Unload and remove the launchd plist."""
    if not PLIST_PATH.exists():
        return

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(PLIST_PATH)],
        capture_output=True,
    )

    PLIST_PATH.unlink(missing_ok=True)
