"""What the menu bar app knows and decides, with no AppKit and no threads.

app.py is the glue (rumps, the status item, the daemon thread); everything it
decides lives here so it can be tested on any machine:

* ``MenuBarState``: the daemon's events folded into one picture of the app (the
  Windows tray app's ``ViewState``), and from it the menu bar icon and the
  flyout's content.
* ``parse_options``: the command line and the developer switches.
* ``classify_daemon_failure``: what a dead daemon thread means for the user.
* ``ROW_HANDLERS``: which method answers each flyout row.
* small helpers: the log file, the ``open`` command, the simulator binary check.
"""

import argparse
import errno
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Tuple

from clawd_tank_tray.hookctl import HookStatus
from clawd_tank_tray.state import (
    CONFLICT_MESSAGE,
    ICON_CONNECTED,
    ICON_DISCONNECTED,
    ICON_ERROR,
    EngineStatus,
    ViewState,
)

from . import flyout_model as fm

# Developer and test switches, as in the Windows tray app (clawd_tank_tray.options).
ENV_NO_BLE = "CLAWD_TANK_NO_BLE"            # 1: no Bluetooth and no simulator
ENV_NO_PROMPT = "CLAWD_TANK_NO_PROMPT"      # 1: no first-run hooks question
ENV_OPEN_FLYOUT = "CLAWD_TANK_OPEN_FLYOUT"  # 1: open the flyout about 2 s after launch

# The menu bar pictures, by connection state (host/tray/make_mac_icons.py builds them).
ICON_FILES = {
    ICON_CONNECTED: "clawd-connected",
    ICON_DISCONNECTED: "clawd-disconnected",
    ICON_ERROR: "clawd-error",
}
ICON_NAMES = tuple(ICON_FILES.values())
HEADER_ICON = "clawd-header"

# What the flyout's rows call. The values are method names of ClawdTankApp.
ROW_HANDLERS = {
    fm.ROW_RETRY: "_retry",
    fm.ROW_HOOKS_INSTALL: "_install_hooks",
    fm.ROW_HOOKS_REMOVE: "_remove_hooks",
    fm.ROW_OPEN_LOG: "_open_log",
    fm.ROW_LOGIN: "_toggle_login",
    fm.ROW_QUIT: "_on_quit_row",
}


# --- command line -------------------------------------------------------------------------


def env_flag(name: str, env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Options:
    selftest: bool = False
    no_ble: bool = False        # test mode: no Bluetooth, no simulator
    no_prompt: bool = False     # no dialog opens by itself
    open_flyout: bool = False   # open the flyout shortly after launch


def parse_options(argv: Optional[List[str]] = None,
                  env: Optional[Mapping[str, str]] = None) -> Options:
    """Options from the command line and the environment. Unknown arguments are
    ignored: Finder and ``open`` may pass some of their own."""
    parser = argparse.ArgumentParser(
        prog="clawd_tank_menubar", description="Clawd Tank for the macOS menu bar.")
    parser.add_argument("--selftest", action="store_true",
                        help="check the imports, build the flyout for sample content "
                             "(no Bluetooth, no window), print 'selftest ok' and exit 0")
    args, _unknown = parser.parse_known_args(argv)
    return Options(
        selftest=args.selftest,
        no_ble=env_flag(ENV_NO_BLE, env),
        no_prompt=env_flag(ENV_NO_PROMPT, env),
        open_flyout=env_flag(ENV_OPEN_FLYOUT, env),
    )


# --- the state ----------------------------------------------------------------------------

# The daemon's hook messages carry more than the app shows; keep what ViewState reads
# (the Windows engine's observer keeps the same keys).
_HOOK_KEYS = ("event", "hook", "tool_name", "project", "session_id")


class MenuBarState:
    """The daemon's events folded into one picture, and what to show for it.

    Wraps the Windows tray app's ``ViewState``, so the words and the icon logic
    are the same on both systems. Not thread-safe: the app updates it on the
    main thread only.
    """

    def __init__(self, ble_enabled: bool = True):
        self.view = ViewState(ble_enabled=ble_enabled)
        self._ble_switched_off = False

    # --- events in -------------------------------------------------------------------

    def on_transport(self, name: str, connected: bool) -> None:
        """A transport connected or dropped. Only the Bluetooth one is the bar:
        the simulator ("sim") says nothing about whether the bar is there."""
        if name != "ble":
            return
        self.view.apply("connection", {"connected": bool(connected) and not self._ble_switched_off})

    def on_display(self, state: Optional[dict]) -> None:
        """What the bar shows changed: the daemon's display state."""
        self.view.apply("display", {"state": dict(state) if state else {"status": "sleeping"}})

    def on_hook(self, msg: Optional[dict]) -> None:
        """A Claude Code hook message reached the daemon."""
        msg = msg or {}
        self.view.apply("hook", {"msg": {k: msg.get(k) for k in _HOOK_KEYS if msg.get(k)}})

    def set_engine(self, status, message: str = "") -> None:
        """The daemon thread's state: an ``EngineStatus`` or its value ("running")."""
        self.view.apply("status", {"status": EngineStatus(status).value, "message": message})

    def set_ble_enabled(self, enabled: bool) -> None:
        """False is the developer test mode (CLAWD_TANK_NO_BLE=1): there is no bar
        to wait for, so the icon shows the connected picture and the Bar line says
        so. For the menu's BLE > Enabled switch see ``set_ble_switched_off``."""
        self.view.ble_enabled = bool(enabled)

    def set_ble_switched_off(self, off: bool) -> None:
        """The user turned BLE > Enabled off in the menu. That is not the test mode:
        the icon stays greyed (no bar) and the Bar line says Bluetooth is off
        instead of 'Scanning...'."""
        self._ble_switched_off = bool(off)
        if off:
            self.view.bar_connected = False

    # --- what to show ----------------------------------------------------------------

    def icon_name(self) -> str:
        """The menu bar picture: 'clawd-connected', 'clawd-disconnected' or
        'clawd-error'. Chosen by the connection alone, like the Windows tray."""
        return ICON_FILES[self.view.icon_name()]

    def content(self, hooks_status: Optional[HookStatus], login_enabled: bool) -> fm.FlyoutContent:
        """The flyout for the current state. ``hooks_status`` is None while unknown."""
        content = fm.build_content(self.view, hooks_status, login_enabled)
        if self._ble_switched_off and self.view.bar_text() == "Scanning...":
            lines = tuple(fm.Line(ln.label, "Bluetooth off") if ln.label == "Bar" else ln
                          for ln in content.lines)
            content = replace(content, lines=lines)
        return content


# --- a daemon thread that ended -----------------------------------------------------------

_ADDRESS_IN_USE = {errno.EADDRINUSE, 10048}   # 10048 is Winsock's


def classify_daemon_failure(exc: Optional[BaseException],
                            quitting: bool = False) -> Optional[Tuple[EngineStatus, str]]:
    """What the end of the daemon thread means: ``(status, message)`` for the
    state, or None when nothing is wrong (the user is quitting).

    * ``SystemExit``: the daemon exits the process when it cannot take over the
      lock file from a running daemon. In a thread that only ends the thread.
    * the hook socket's address is in use: another program holds it.
    * anything else, or no exception at all (``run()`` returned by itself), is an
      error the Retry row can try again.
    """
    if quitting:
        return None
    if isinstance(exc, SystemExit):
        return EngineStatus.CONFLICT, CONFLICT_MESSAGE
    if isinstance(exc, OSError) and exc.errno in _ADDRESS_IN_USE:
        return EngineStatus.CONFLICT, f"{CONFLICT_MESSAGE} (the hook socket is in use)"
    if exc is None:
        return EngineStatus.ERROR, "The daemon stopped. Retry starts it again."
    return EngineStatus.ERROR, f"The daemon stopped: {exc}. Retry starts it again."


# --- small helpers ------------------------------------------------------------------------


def log_path(home: Optional[Path] = None) -> Path:
    """~/Library/Logs/ClawdTank/clawd-tank.log, where main() writes the log."""
    return (Path(home) if home is not None else Path.home()) / "Library" / "Logs" / "ClawdTank" / "clawd-tank.log"


def open_command(path: Path) -> List[str]:
    """The command that opens a file in its default application."""
    return ["open", str(path)]


def sim_binary_available(manager_factory: Optional[Callable[[], object]] = None) -> bool:
    """Whether the SDL simulator binary can be found. The fork builds the app
    without it, and the Simulator submenu then says so instead of waiting for a
    process that never starts."""
    if manager_factory is None:
        from clawd_tank_daemon.sim_process import SimProcessManager as manager_factory
    try:
        return manager_factory()._find_binary() is not None
    except Exception:
        return False
