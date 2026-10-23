# host/setup.py — py2app configuration for Clawd Tank menubar app
import os
import re
import subprocess
from pathlib import Path
from setuptools import setup


def _write_version(version):
    path = Path(__file__).parent / "clawd_tank_menubar" / "_version_info.py"
    path.write_text(f'VERSION = "{version}"\n')
    print(f"Baked version: {version}")


def _bake_version():
    """Generate _version_info.py with the current git version.

    CLAWD_TANK_VERSION, when set, is baked as it is and git is not asked. The CI
    workflow sets it before it replaces the app icon and writes its own files
    into the checkout, which would otherwise make every build read "-dirty".
    """
    override = re.sub(r"[^A-Za-z0-9._+@-]", "-", os.environ.get("CLAWD_TANK_VERSION", "").strip())
    if override:
        _write_version(override)
        return

    def _run(args):
        return subprocess.run(args, capture_output=True, text=True, timeout=5)

    def _is_dirty():
        r = _run(["git", "status", "--porcelain"])
        return bool(r.stdout.strip())

    try:
        tag = _run(["git", "describe", "--tags", "--exact-match", "HEAD"])
        if tag.returncode == 0 and tag.stdout.strip():
            version = tag.stdout.strip()
            if _is_dirty():
                version += "-dirty"
        else:
            branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
            sha = _run(["git", "rev-parse", "--short", "HEAD"])
            b = branch.stdout.strip() if branch.returncode == 0 else "unknown"
            # On master/main, count against remote to show unpushed commits
            if b in ("master", "main"):
                count = _run(["git", "rev-list", "--count", "HEAD", f"^origin/{b}"])
                if count.returncode != 0:
                    count = _run(["git", "rev-list", "--count", "HEAD"])
            else:
                count = _run(["git", "rev-list", "--count", "HEAD", "^master"])
                if count.returncode != 0:
                    count = _run(["git", "rev-list", "--count", "HEAD", "^main"])
            s = sha.stdout.strip() if sha.returncode == 0 else "??????"
            n = count.stdout.strip() if count.returncode == 0 else "?"
            dirty = "-dirty" if _is_dirty() else ""
            version = f"{b}+{n}@{s}{dirty}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        version = "unknown"

    _write_version(version)


_bake_version()

APP = ["launcher.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "AppIcon.icns",
    "plist": {
        "CFBundleName": "Clawd Tank",
        "CFBundleDisplayName": "Clawd Tank",
        "CFBundleIdentifier": "com.clawd-tank.menubar",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,  # menu-bar-only app (no Dock icon)
        "NSHighResolutionCapable": True,  # draw the flyout at Retina size
        "NSBluetoothAlwaysUsageDescription": (
            "Clawd Tank uses Bluetooth to communicate with the ESP32 display."
        ),
    },
    "packages": ["clawd_tank_daemon", "clawd_tank_menubar"],
    "includes": [
        "rumps", "bleak", "objc",
        # The flyout's AppKit code: py2app does not always find PyObjC's lazily
        # loaded framework modules by itself.
        "AppKit", "Foundation", "PyObjCTools", "PyObjCTools.AppHelper",
        # The menu bar app reuses two pure-logic modules of the Windows tray app:
        # the daemon-event-to-text mapping and the hook installer's consent flow.
        # Only these: the rest of clawd_tank_tray needs tkinter, pystray and Win32.
        "clawd_tank_tray", "clawd_tank_tray.state", "clawd_tank_tray.hookctl",
        "clawd_tank_tray.options",
    ],
    "excludes": ["tkinter", "pystray"],
    "resources": ["clawd_tank_menubar/icons"],  # the menu bar and flyout PNGs
}

setup(
    name="Clawd Tank",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
