# host/clawd_tank_menubar/app.py
"""Clawd Tank macOS status bar application.

A left click on the menu bar item opens the flyout (flyout.py), the same panel as
the Windows tray app's. A right click, or a control-click, opens the original rumps
menu. What the app decides lives in appstate.py and hookflow.py; this file is the
glue: rumps, the status item, the daemon thread and the main-thread hops.

Threads: the main thread owns every AppKit call. The daemon runs in its own thread
with its own asyncio loop and calls the observer methods below from there; they
hop to the main thread with ``_on_main`` before they touch the state or the UI.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import rumps

from clawd_tank_daemon.daemon import ClawdDaemon, DaemonObserver
from clawd_tank_tray.state import EngineStatus
from . import dialogs, hooks, launchd
from .appstate import (
    ROW_HANDLERS,
    HEADER_ICON,
    ICON_NAMES,
    MenuBarState,
    Options,
    classify_daemon_failure,
    log_path,
    open_command,
    parse_options,
    sim_binary_available,
)
from .flyout import FlyoutController, icon_path
from .hookflow import HookFlow, make_manager
from .preferences import load_preferences, save_preferences
from .slider import create_slider_menu_item
from .statusitem import StatusItemClicks, make_status_image
from .version import get_version

logger = logging.getLogger("clawd-tank.menubar")

SESSION_TIMEOUT_OPTIONS = [
    ("1 minute", 60),
    ("2 minutes", 120),
    ("5 minutes", 300),
    ("10 minutes", 600),
    ("30 minutes", 1800),
    ("Never", 0),
]

# One-shot timers after launch, in seconds.
STATUS_ITEM_POLL_S = 0.2        # look for rumps' status item this often ...
STATUS_ITEM_POLL_MAX = 50       # ... for this many tries (10 s) before giving up
FIRST_RUN_DELAY_S = 1.5         # the hooks question
OPEN_FLYOUT_DELAY_S = 2.0       # CLAWD_TANK_OPEN_FLYOUT=1


def _get_pref(key, default=None):
    return load_preferences().get(key, default)


def _set_pref(key, value):
    save_preferences(updates={key: value})


class ClawdTankApp(rumps.App, DaemonObserver):
    def __init__(self, options: Optional[Options] = None):
        super().__init__("Clawd Tank", quit_button=None)

        self._opts = options or Options()
        self._daemon: Optional[ClawdDaemon] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()
        self._transport_status: dict[str, bool] = {}
        self._notification_count = 0
        self._current_config: dict = {}
        self._sim_process = None
        self._quitting = False

        prefs = load_preferences()

        # What the flyout and the icon show. Test mode (CLAWD_TANK_NO_BLE=1) has no
        # bar to wait for; the menu's BLE switch is a different thing.
        self._mb_state = MenuBarState(ble_enabled=not self._opts.no_ble)
        self._mb_state.set_ble_switched_off(
            not self._opts.no_ble and not prefs.get("ble_enabled", True))
        self._flyout: Optional[FlyoutController] = None
        self._flyout_content = None          # what the flyout was last given
        self._clicks = StatusItemClicks()
        self._icon_name = ""                 # the picture shown now (appstate.ICON_NAMES)
        self._status_images: dict = {}       # name -> NSImage, kept alive here
        self._oneshots: list = []            # started one-shot timers, kept alive here

        # The hooks: asked for, never edited silently (hookflow.py).
        self._hook_flow = HookFlow(make_manager(), _get_pref, _set_pref,
                                   dialogs.ask_yes_no, dialogs.show_message)
        self._hooks_status = self._hook_flow.status()

        # The simulator needs the SDL binary, which this fork's build leaves out.
        if self._opts.no_ble:
            self._sim_off_reason = "Off (test mode)"
        elif not sim_binary_available():
            logger.info("The simulator binary (clawd-tank-sim) was not found: "
                        "the Simulator submenu is disabled")
            self._sim_off_reason = "Not available"
        else:
            self._sim_off_reason = None

        # --- BLE submenu ---
        self._ble_menu = rumps.MenuItem("BLE \u2014 Disabled")
        self._ble_status = rumps.MenuItem("Status: Initializing...")
        self._ble_status.set_callback(None)
        self._ble_enabled_toggle = rumps.MenuItem(
            "Enabled", callback=self._on_toggle_ble_enabled
        )
        self._ble_enabled_toggle.state = prefs.get("ble_enabled", True) and not self._opts.no_ble
        if self._opts.no_ble:
            self._ble_enabled_toggle.set_callback(None)
        self._ble_reconnect = rumps.MenuItem("Reconnect", callback=None)
        self._ble_menu.update([
            self._ble_status,
            None,
            self._ble_enabled_toggle,
            None,
            self._ble_reconnect,
        ])

        # --- Simulator submenu ---
        self._sim_menu = rumps.MenuItem("Simulator \u2014 Disabled")
        self._sim_status = rumps.MenuItem("Status: Initializing...")
        self._sim_status.set_callback(None)
        self._sim_enabled_toggle = rumps.MenuItem(
            "Enabled", callback=self._on_toggle_sim_enabled
        )
        self._sim_enabled_toggle.state = (
            prefs.get("sim_enabled", False) and self._sim_off_reason is None)
        if self._sim_off_reason is not None:
            self._sim_enabled_toggle.set_callback(None)
        self._sim_window_toggle = rumps.MenuItem(
            "Show Window", callback=None
        )
        self._sim_window_toggle.state = prefs.get("sim_window_visible", True)
        self._sim_pinned_toggle = rumps.MenuItem(
            "Always on Top", callback=None
        )
        self._sim_pinned_toggle.state = prefs.get("sim_always_on_top", True)
        self._sim_menu.update([
            self._sim_status,
            None,
            self._sim_enabled_toggle,
            None,
            self._sim_window_toggle,
            self._sim_pinned_toggle,
        ])

        # Brightness slider - rumps MenuItem with custom NSView
        self._brightness_slider = create_slider_menu_item(
            "Brightness", min_val=0, max_val=255, initial=102,
            on_change=self._on_brightness_change,
        )
        self._brightness_item = rumps.MenuItem("Brightness")
        self._brightness_item._menuitem.setView_(self._brightness_slider.view)

        # Session timeout submenu
        self._session_timeout_menu = rumps.MenuItem("Session Timeout")
        self._session_timeout_value = 300
        for label, seconds in SESSION_TIMEOUT_OPTIONS:
            item = rumps.MenuItem(label, callback=self._on_session_timeout_select)
            item._seconds = seconds
            if seconds == 300:
                item.state = True
            self._session_timeout_menu.add(item)

        # Claude Code hooks
        self._hooks_item = rumps.MenuItem(
            "Install Claude Code Hooks",
            callback=self._on_install_hooks,
        )
        self._hooks_item.state = bool(self._hooks_status and self._hooks_status.installed)

        # Launch at login
        self._login_item = rumps.MenuItem(
            "Launch at Login",
            callback=self._on_toggle_login,
        )
        self._login_item.state = launchd.is_enabled()

        if launchd.is_enabled() and launchd.is_stale():
            logger.info("Launchd plist is stale, updating to current executable")
            launchd.enable()

        # Version
        self._version_item = rumps.MenuItem(f"Version: {get_version()}")
        self._version_item.set_callback(None)

        # Quit
        self._quit_item = rumps.MenuItem("Quit Clawd Tank", callback=self._on_quit)

        # Assemble menu
        self.menu = [
            self._ble_menu,
            self._sim_menu,
            None,
            self._brightness_item,
            self._session_timeout_menu,
            None,
            self._hooks_item,
            self._login_item,
            None,
            self._version_item,
            self._quit_item,
        ]

        # Clawd in the menu bar, as it is until the status item is taken over
        # (_install_status_item). Not a template image: the colours are drawn as they
        # are. The text title stays empty so only the picture shows.
        self.title = ""
        self._update_menu_state()

    @property
    def _connected(self) -> bool:
        return any(self._transport_status.values()) if self._transport_status else False

    @property
    def _daemon_alive(self) -> bool:
        return (
            hasattr(self, '_daemon_thread')
            and self._daemon_thread.is_alive()
        )

    # --- Main thread ---

    def _on_main(self, fn, *args) -> None:
        """Run ``fn(*args)`` on the main thread (the daemon's observer methods and
        the flyout's callbacks may arrive on another one). Errors are logged."""
        def run():
            try:
                fn(*args)
            except Exception:
                logger.exception("%s failed", getattr(fn, "__name__", fn))

        try:
            from PyObjCTools.AppHelper import callAfter
        except ImportError:
            run()
            return
        callAfter(run)

    def _after(self, seconds: float, fn) -> None:
        """Run ``fn()`` once, ``seconds`` from now, on the main thread (a one-shot
        rumps timer)."""
        def fire(timer):
            timer.stop()
            try:
                self._oneshots.remove(timer)
            except ValueError:
                pass
            try:
                fn()
            except Exception:
                logger.exception("%s failed", getattr(fn, "__name__", fn))

        timer = rumps.Timer(fire, seconds)
        self._oneshots.append(timer)
        timer.start()

    def schedule_startup(self) -> None:
        """Queue what happens just after launch. Call before ``run()``."""
        self._after(STATUS_ITEM_POLL_S, self._install_status_item)
        if not self._opts.no_prompt:
            self._after(FIRST_RUN_DELAY_S, self._first_run)
        if self._opts.open_flyout:
            self._after(OPEN_FLYOUT_DELAY_S, self._open_flyout_for_test)

    # --- Lifecycle ---

    def _start_daemon_thread(self):
        """Start the daemon's asyncio event loop in a background thread."""
        # keep_live_sessions: an open Claude Code session that has gone quiet stays
        # on the bar for as long as its process runs (see ClawdDaemon).
        daemon = ClawdDaemon(observer=self, headless=False, keep_live_sessions=True)
        self._daemon = daemon
        self._loop = None
        self._loop_ready.clear()
        self._transport_status = {}
        self._mb_state.set_engine(EngineStatus.STARTING)

        def run_loop():
            error = None
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._loop_ready.set()
                loop.run_until_complete(daemon.run())
                logger.info("Daemon thread exited normally")
            except BaseException as e:  # SystemExit too: see classify_daemon_failure
                error = e
                if isinstance(e, SystemExit):
                    logger.error("The daemon could not take the lock over from a running one")
                else:
                    logger.exception("Daemon thread crashed")
            finally:
                self._loop_ready.set()  # unblock main thread if still waiting
                self._on_main(self._daemon_ended, daemon, error)

        self._daemon_thread = threading.Thread(target=run_loop, daemon=True)
        self._daemon_thread.start()
        self._loop_ready.wait(timeout=5)

        # Create transports based on preferences. Test mode has none: no Bluetooth,
        # no simulator, only the hook listener.
        if self._opts.no_ble:
            logger.info("Test mode (CLAWD_TANK_NO_BLE=1): no Bluetooth, no simulator")
            return
        prefs = load_preferences()

        if prefs.get("ble_enabled", True) and self._loop:
            from clawd_tank_daemon.ble_client import ClawdBleClient
            client = ClawdBleClient()
            self._transport_status["ble"] = False
            asyncio.run_coroutine_threadsafe(
                daemon.add_transport("ble", client), self._loop
            )

        if prefs.get("sim_enabled", False) and self._sim_off_reason is None:
            self._start_simulator()

    def _retry(self):
        """The flyout's Retry row: start the daemon thread again if it died.

        Best effort. A running daemon is left alone. A new one takes the lock over
        from whatever holds it, and the transports are created again from the
        preferences. A simulator process that outlived the old daemon is not
        cleaned up here (its loop is gone); a new one then waits for the port and
        gives up with a log line."""
        if self._daemon_alive:
            logger.info("Retry: the daemon is already running")
        else:
            logger.info("Retry: starting the daemon again")
            self._start_daemon_thread()
        self._update_menu_state()

    # --- DaemonObserver callbacks (called from asyncio thread) ---

    def on_connection_change(self, connected: bool, transport: str = "") -> None:
        if transport:
            self._transport_status[transport] = connected
        if connected and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._read_device_config(), self._loop
            )
        self._on_main(self._apply_connection, connected, transport)

    def on_notification_change(self, count: int) -> None:
        self._notification_count = count
        self._schedule_menu_update()

    # Optional observer methods (see DaemonObserver): the daemon calls them only
    # because they exist.

    def on_started(self, endpoint: str) -> None:
        """The hook listener is up. Read the restored sessions here, on the daemon's
        own thread, where display_state() may be called."""
        state = None
        daemon = self._daemon
        if daemon is not None:
            try:
                state = daemon.display_state()
            except Exception:
                logger.exception("could not read the restored sessions")
        logger.info("Daemon started: hooks reach it at %s", endpoint)
        self._on_main(self._daemon_started, state)

    def on_display_state_change(self, state: dict) -> None:
        self._on_main(self._apply_display, dict(state))

    def on_hook_event(self, msg: dict) -> None:
        self._on_main(self._apply_hook, dict(msg))

    # --- State changes, on the main thread ---

    def _apply_connection(self, connected: bool, transport: str) -> None:
        self._mb_state.on_transport(transport, connected)
        self._update_menu_state()

    def _apply_display(self, state: dict) -> None:
        self._mb_state.on_display(state)
        self._refresh_flyout()

    def _apply_hook(self, msg: dict) -> None:
        self._mb_state.on_hook(msg)
        self._refresh_flyout()

    def _daemon_started(self, state: Optional[dict]) -> None:
        self._mb_state.set_engine(EngineStatus.RUNNING)
        if state is not None:
            self._mb_state.on_display(state)
        self._update_menu_state()

    def _daemon_ended(self, daemon, error) -> None:
        """The daemon thread is over. Unless the user is quitting that is a conflict
        or an error, and the flyout shows it with a Retry row."""
        if daemon is not self._daemon:
            return                      # an older daemon, replaced by Retry
        verdict = classify_daemon_failure(error, quitting=self._quitting)
        if verdict is None:
            return
        status, message = verdict
        logger.warning("Daemon ended: %s: %s", status.value, message)
        self._mb_state.set_engine(status, message)
        self._mb_state.on_display(None)
        for name in self._transport_status:
            self._transport_status[name] = False
        self._update_menu_state()

    # --- Config ---

    async def _read_device_config(self):
        """Read config from device and update menu."""
        if self._daemon:
            config = await self._daemon.read_config()
            if config:
                self._current_config = config
                self._schedule_menu_update()

    def _schedule_menu_update(self):
        """Thread-safe menu update via PyObjC main thread dispatch."""
        self._on_main(self._update_menu_state)

    @rumps.timer(30)
    def _health_check(self, _):
        """Periodic check to detect daemon thread death."""
        if (hasattr(self, '_daemon_thread') and not self._daemon_alive
                and self._mb_state.view.engine in (EngineStatus.STARTING, EngineStatus.RUNNING)):
            self._daemon_ended(self._daemon, None)   # it ended without saying so
        if not self._daemon_alive:
            self._update_menu_state()

    def _update_menu_state(self):
        """Update the menu items, the menu bar picture and the flyout from the
        current state. Must run on main thread."""
        self._update_menu_items()
        self._apply_icon()
        self._refresh_flyout()

    def _update_menu_items(self):
        """Update all menu items based on current state."""
        if not self._daemon_alive:
            if hasattr(self, '_daemon_thread'):
                # It ran and has ended: do not leave the last "Connected" in the menu.
                self._ble_menu.title = "BLE \u2014 Daemon stopped"
                self._ble_status.title = "Status: Daemon stopped"
                self._ble_reconnect.set_callback(None)
                self._brightness_slider.set_enabled(False)
            return
        connected = self._connected

        # --- BLE submenu state ---
        if self._opts.no_ble:
            self._ble_menu.title = "BLE \u2014 Off (test mode)"
            self._ble_status.title = "Status: Off (test mode)"
            self._ble_reconnect.set_callback(None)
        elif not self._ble_enabled_toggle.state:
            self._ble_menu.title = "BLE \u2014 Disabled"
            self._ble_status.title = "Status: Disabled"
            self._ble_reconnect.set_callback(None)
        else:
            ble_connected = self._transport_status.get("ble", False)
            if ble_connected:
                self._ble_menu.title = "BLE \U0001F7E2 Connected"
                self._ble_status.title = "Status: Connected"
            else:
                self._ble_menu.title = "BLE \U0001F7E1 Connecting..."
                self._ble_status.title = "Status: Connecting..."
            self._ble_reconnect.set_callback(self._on_reconnect)

        # --- Simulator submenu state ---
        if self._sim_off_reason is not None:
            self._sim_menu.title = f"Simulator \u2014 {self._sim_off_reason}"
            self._sim_status.title = f"Status: {self._sim_off_reason}"
            self._sim_window_toggle.set_callback(None)
            self._sim_pinned_toggle.set_callback(None)
        elif not self._sim_enabled_toggle.state:
            self._sim_menu.title = "Simulator \u2014 Disabled"
            self._sim_status.title = "Status: Disabled"
            self._sim_window_toggle.set_callback(None)
            self._sim_pinned_toggle.set_callback(None)
        else:
            sim_connected = self._transport_status.get("sim", False)
            if sim_connected:
                self._sim_menu.title = "Simulator \U0001F7E2 Running"
                self._sim_status.title = "Status: Running"
            else:
                self._sim_menu.title = "Simulator \U0001F7E1 Connecting..."
                self._sim_status.title = "Status: Connecting..."
            self._sim_window_toggle.set_callback(self._on_toggle_sim_window)
            self._sim_pinned_toggle.set_callback(self._on_toggle_sim_pinned)

        # --- Brightness and session timeout ---
        if connected:
            brightness = self._current_config.get("brightness", 102)
            self._brightness_slider.set_value(brightness)
            self._brightness_slider.set_enabled(True)

            timeout = self._current_config.get("sleep_timeout", 300)
            self._session_timeout_value = timeout
            for key, item in self._session_timeout_menu.items():
                item.state = (item._seconds == timeout)
        else:
            self._brightness_slider.set_enabled(False)

    # --- The menu bar item ---

    def _status_image(self, name: str):
        """The NSImage of a menu bar picture (cached), or None if it cannot be built."""
        if name not in self._status_images:
            try:
                path = icon_path(name)
                self._status_images[name] = make_status_image(
                    path, icon_path(name + "@2x")) if path else None
            except Exception:
                logger.exception("could not load the icon %s", name)
                self._status_images[name] = None
        return self._status_images[name]

    def _apply_icon(self):
        """Show the picture for the connection state: one still Clawd, changed only
        when the state is (like the Windows tray icon)."""
        name = self._mb_state.icon_name()
        if name == self._icon_name:
            return
        if self._icon_name:
            logger.info("Menu bar icon: %s", name)
        self._icon_name = name
        if self._clicks.installed:
            image = self._status_image(name)
            if image is not None:
                self._clicks.set_image(image)
                return
        # Before the status item is taken over, or if the picture cannot be built:
        # the plain rumps icon.
        path = icon_path(name)
        if path is not None:
            self.icon = path
            self.template = False
        else:
            self.title = "Clawd Tank"       # no picture to show: a text item is still reachable

    def _install_status_item(self, attempt: int = 0):
        """Take over the status item rumps made: left click opens the flyout, right
        click the original menu. rumps creates the item when the app starts to run, so
        this is called from a timer and tries again until it exists."""
        item = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
        if item is None:
            if attempt < STATUS_ITEM_POLL_MAX:
                self._after(STATUS_ITEM_POLL_S, lambda: self._install_status_item(attempt + 1))
            else:
                logger.error("rumps made no status item; left click keeps the menu")
            return
        try:
            self._hide_dock_icon()
            self._clicks.install(item, self._on_left, self._on_right)
        except Exception:
            logger.exception("could not take over the status item; left click keeps the menu")
            return
        self._ensure_flyout()
        self._icon_name = ""            # show the sized, Retina picture now
        self._update_menu_state()
        logger.info("Status item ready: left click opens the flyout, right click the menu")

    @staticmethod
    def _hide_dock_icon():
        """Run from source, the app would have a Dock icon; the bundle has none
        (LSUIElement). The accessory policy makes both the same."""
        try:
            import AppKit
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            logger.debug("could not set the activation policy", exc_info=True)

    def _on_left(self, button):
        logger.info("Left click on the status item")
        flyout = self._ensure_flyout()
        if not flyout.is_shown:
            # What it shows is current when it opens: the hooks file or the login
            # item may have changed since the last event.
            self._refresh_hooks_status()
            self._refresh_flyout()
        flyout.toggle(button)

    def _on_right(self, button):
        if self._flyout is not None:
            self._flyout.close()
        self._refresh_hooks_status()
        self._login_item.state = launchd.is_enabled()
        self._clicks.show_menu()

    def _open_flyout_for_test(self, attempt: int = 0):
        """CLAWD_TANK_OPEN_FLYOUT=1: open the flyout without a click, once the status
        item is on the menu bar (a popover opened before that lands in a screen corner)."""
        from .flyout import anchor_is_placed
        button = self._clicks.button()
        if button is None:
            logger.warning("Cannot open the flyout: the status item is not ready")
            return
        if not anchor_is_placed(button) and attempt < STATUS_ITEM_POLL_MAX:
            self._after(STATUS_ITEM_POLL_S, lambda: self._open_flyout_for_test(attempt + 1))
            return
        logger.info("Opening the flyout for the test aid (after %d waits)", attempt)
        self._refresh_hooks_status()
        self._refresh_flyout()
        self._ensure_flyout().show(button)

    # --- The flyout ---

    def _ensure_flyout(self) -> FlyoutController:
        """The flyout, made on first use (after the app is running, not in __init__)."""
        if self._flyout is None:
            self._flyout = FlyoutController(self._on_flyout_row)
            self._flyout_content = self._current_content()
            self._flyout.update(self._flyout_content)
        return self._flyout

    def _current_content(self):
        return self._mb_state.content(self._hooks_status, launchd.is_enabled())

    def _refresh_flyout(self):
        """Push the current state to the flyout (redrawn in place if it is open),
        when something it shows has changed."""
        if self._flyout is None:
            return
        content = self._current_content()
        if content != self._flyout_content:
            self._flyout_content = content
            self._flyout.update(content)

    def _refresh_hooks_status(self):
        """Read settings.json again: it may have changed since the app started."""
        self._hooks_status = self._hook_flow.status()
        self._hooks_item.state = bool(self._hooks_status and self._hooks_status.installed)

    def _on_flyout_row(self, row_id: str):
        # The flyout has closed itself (except for Start at login). Leave its click
        # handler first, then act: an action may open a dialog.
        self._on_main(self._run_row, row_id)

    def _run_row(self, row_id: str):
        name = ROW_HANDLERS.get(row_id)
        if name is None:
            logger.warning("Flyout row %r has no action", row_id)
            return
        getattr(self, name)()

    # --- Menu bar item and flyout actions ---

    def _install_hooks(self):
        """Install or repair the hooks, after asking (the menu item and the flyout row)."""
        self._hook_flow.install()
        self._refresh_hooks_status()
        self._update_menu_state()

    def _remove_hooks(self):
        self._hook_flow.remove()
        self._refresh_hooks_status()
        self._update_menu_state()

    def _first_run(self):
        """The hooks question at launch, when one is due."""
        self._hook_flow.first_run()
        self._refresh_hooks_status()
        self._update_menu_state()

    def _open_log(self):
        path = log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            result = subprocess.run(open_command(path), capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise OSError(result.stderr.strip() or f"open exited with {result.returncode}")
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("Could not open %s: %s", path, e)
            dialogs.show_message("Clawd Tank", f"Could not open {path}:\n{e}")

    def _toggle_login(self):
        try:
            if launchd.is_enabled():
                launchd.disable()
            else:
                launchd.enable()
        except Exception as e:
            logger.exception("Launch at login could not be changed")
            dialogs.show_message("Clawd Tank", f"Could not change Start at login:\n{e}")
        self._login_item.state = launchd.is_enabled()
        self._refresh_flyout()      # the check mark at the right edge

    def _on_quit_row(self):
        self._on_quit(None)

    # --- Menu callbacks ---

    def _on_brightness_change(self, value: int):
        """Called from slider on main thread. Send config write via asyncio."""
        if self._loop and self._connected:
            payload = json.dumps({"brightness": value})
            asyncio.run_coroutine_threadsafe(
                self._daemon.write_config(payload), self._loop
            )

    def _on_session_timeout_select(self, sender):
        seconds = sender._seconds
        self._session_timeout_value = seconds

        for key, item in self._session_timeout_menu.items():
            item.state = (item._seconds == seconds)

        if self._loop and self._connected:
            payload = json.dumps({"sleep_timeout": seconds})
            asyncio.run_coroutine_threadsafe(
                self._daemon.write_config(payload), self._loop
            )

            # Update daemon staleness timeout (0, "Never", means no time limit)
            if self._daemon:
                self._daemon.set_session_timeout(seconds)

    def _on_toggle_ble_enabled(self, sender):
        """Toggle BLE transport on/off."""
        sender.state = not sender.state
        save_preferences(updates={"ble_enabled": sender.state})
        self._mb_state.set_ble_switched_off(not sender.state)

        if sender.state:
            # Enable: create BLE client and add transport
            from clawd_tank_daemon.ble_client import ClawdBleClient
            client = ClawdBleClient()
            self._transport_status["ble"] = False
            self._schedule_menu_update()
            if self._loop and self._daemon:
                asyncio.run_coroutine_threadsafe(
                    self._daemon.add_transport("ble", client), self._loop
                )
        else:
            # Disable: remove transport
            self._transport_status.pop("ble", None)
            self._schedule_menu_update()
            if self._loop and self._daemon:
                asyncio.run_coroutine_threadsafe(
                    self._daemon.remove_transport("ble"), self._loop
                )

    def _on_toggle_sim_enabled(self, sender):
        """Toggle simulator transport on/off."""
        sender.state = not sender.state
        save_preferences(updates={"sim_enabled": sender.state})

        if sender.state:
            self._start_simulator()
            self._schedule_menu_update()
        else:
            self._stop_simulator()
            self._schedule_menu_update()

    def _on_toggle_sim_window(self, sender):
        """Toggle simulator window visibility."""
        sender.state = not sender.state
        save_preferences(updates={"sim_window_visible": sender.state})

        if self._sim_process and self._loop:
            if sender.state:
                asyncio.run_coroutine_threadsafe(
                    self._sim_process.show_window(), self._loop
                )
            else:
                asyncio.run_coroutine_threadsafe(
                    self._sim_process.hide_window(), self._loop
                )

    def _on_toggle_sim_pinned(self, sender):
        """Toggle simulator always-on-top."""
        sender.state = not sender.state
        save_preferences(updates={"sim_always_on_top": sender.state})

        if self._sim_process and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._sim_process.set_pinned(sender.state), self._loop
            )

    def _on_sim_window_event(self, event: dict):
        """Handle events from the simulator process (e.g. window_hidden)."""
        event_type = event.get("event")
        if event_type == "window_hidden":
            self._sim_window_toggle.state = False
            save_preferences(updates={"sim_window_visible": False})
            self._schedule_menu_update()

    def _on_install_hooks(self, sender):
        self._install_hooks()

    def _on_toggle_login(self, sender):
        self._toggle_login()

    def _on_reconnect(self, _):
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._daemon.reconnect(), self._loop
            )

    # --- Simulator lifecycle ---

    def _start_simulator(self):
        """Start the simulator process and add it as a transport."""
        if self._sim_off_reason is not None:
            return
        from clawd_tank_daemon.sim_process import SimProcessManager
        prefs = load_preferences()
        self._sim_process = SimProcessManager(
            on_window_event=self._on_sim_window_event,
            start_pinned=prefs.get("sim_always_on_top", True),
        )
        self._transport_status["sim"] = False

        async def _do_start():
            client = await self._sim_process.start()
            if client:
                await self._daemon.add_transport("sim", client)
                # Wait for the sender task to establish the TCP connection.
                # Don't call ensure_connected() here - it races with the
                # sender task's connect() and causes duplicate background readers.
                for _ in range(100):  # up to 10 seconds
                    if client.is_connected:
                        break
                    await asyncio.sleep(0.1)
                prefs = load_preferences()
                if prefs.get("sim_window_visible", True):
                    await self._sim_process.show_window()
                    # Let macOS finish window presentation before setting level
                    await asyncio.sleep(0.2)
                await self._sim_process.set_pinned(prefs.get("sim_always_on_top", True))
            else:
                # start() says why in the log (no binary, port in use). Do not wait
                # for a transport that will not come.
                self._transport_status.pop("sim", None)
                self._sim_process = None
                self._schedule_menu_update()

        if self._loop and self._daemon:
            asyncio.run_coroutine_threadsafe(_do_start(), self._loop)

    def _stop_simulator(self):
        """Stop the simulator process and remove it as a transport."""
        async def _do_stop():
            await self._daemon.remove_transport("sim")
            if self._sim_process:
                await self._sim_process.stop()
                self._sim_process = None
            self._transport_status.pop("sim", None)

        if self._loop and self._daemon:
            asyncio.run_coroutine_threadsafe(_do_stop(), self._loop)

    def _on_quit(self, _):
        self._quitting = True
        if self._flyout is not None:
            self._flyout.close()
        try:
            # A dead daemon thread has no loop to run the shutdown: skip it, or the
            # wait below would time out and force-kill the app with status 1.
            if self._loop and self._daemon and self._daemon_alive:
                async def _shutdown_all():
                    # Remove sim transport from daemon first (avoids double-disconnect)
                    await self._daemon.remove_transport("sim")
                    # Kill sim process (client already disconnected, just kill the process)
                    if self._sim_process:
                        await self._sim_process.kill()
                        self._sim_process = None
                    # Now shut down daemon (BLE disconnect, socket cleanup)
                    await self._daemon._shutdown()
                future = asyncio.run_coroutine_threadsafe(
                    _shutdown_all(), self._loop
                )
                future.result(timeout=8)
            rumps.quit_application()
        except Exception:
            logger.exception("Error during quit, force-killing")
            logging.shutdown()
            os._exit(1)


def run_selftest() -> int:
    """--selftest: import everything, build the flyout for sample content, touch
    no Bluetooth, open no window and no popover. Prints 'selftest ok' and returns 0,
    or prints what failed and returns 1."""
    problems = []

    def step(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - report every step, not just the first
            problems.append(f"{name}: {type(e).__name__}: {e}")

    def icons():
        for name in ICON_NAMES + (HEADER_ICON,):
            for suffix in ("", "@2x"):
                path = icon_path(name + suffix)
                if path is None or not os.path.isfile(path):
                    raise FileNotFoundError(f"icons/{name}{suffix}.png")

    def status_images():
        for name in ICON_NAMES:
            if make_status_image(icon_path(name), icon_path(name + "@2x")) is None:
                raise RuntimeError(f"cannot build the menu bar picture {name}")

    def sample_content():
        from clawd_tank_tray.hookctl import HookStatus
        state = MenuBarState()
        state.set_engine(EngineStatus.RUNNING)
        state.on_transport("ble", True)
        state.on_display({"anims": ["idle", "typing"], "ids": [1, 2]})
        state.on_hook({"event": "tool_use", "tool_name": "Bash", "project": "demo"})
        status = HookStatus(Path("settings.json"), Path("notify"),
                            hooks_present=True, script_present=True, script_current=True)
        content = state.content(status, True)
        if state.icon_name() != "clawd-connected" or len(content.rows) < 4:
            raise AssertionError("the sample state does not give the expected icon and rows")
        return content

    def flyout_view():
        import AppKit
        from .flyout import build_flyout_view
        AppKit.NSApplication.sharedApplication()
        view = build_flyout_view(sample_content(), lambda row_id: None)
        if view is None:
            raise AssertionError("build_flyout_view returned no view")
        width = float(view.frame().size.width)
        if abs(width - 300.0) > 0.5:
            raise AssertionError(f"the flyout is {width} points wide, not 300")

    def hooks_and_launchd():
        manager = make_manager()
        if "settings.json" not in manager.consent_text():
            raise AssertionError("the consent text does not name settings.json")
        args = launchd.program_arguments(True, "/App/Contents/MacOS/Clawd Tank", sys.executable)
        if args != ["/App/Contents/MacOS/Clawd Tank"]:
            raise AssertionError(f"unexpected program arguments {args}")
        if not hooks.NOTIFY_SCRIPT:
            raise AssertionError("the notify script is empty")

    step("icons", icons)
    step("menu bar pictures", status_images)
    step("flyout", flyout_view)
    step("hooks and launchd", hooks_and_launchd)
    if problems:
        print("selftest failed:")
        for p in problems:
            print(f"  {p}")
        return 1
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    options = parse_options(argv)
    if options.selftest:
        return run_selftest()

    log_file = log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file),
        ],
    )
    logger = logging.getLogger("clawd-tank.menubar")
    logger.info("Clawd Tank %s starting", get_version())

    # No more silent edits of ~/.claude/settings.json: the hooks question is asked
    # after launch (ClawdTankApp._first_run), and only a Yes writes anything.

    # Kill stale sim processes synchronously before anything else (only when there
    # is a simulator to start, and not in test mode)
    if not options.no_ble and sim_binary_available():
        from clawd_tank_daemon.sim_process import SimProcessManager
        SimProcessManager.kill_stale_sims()

    app = ClawdTankApp(options)
    app._start_daemon_thread()
    app.schedule_startup()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
