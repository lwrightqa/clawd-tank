"""TrayApp: the window, the tray icon and the engine, tied together.

Threads
-------
* **main**: tkinter. Owns the flyout, the two housekeeping timers (`after`: the
  queue pump and the engine watchdog), and all decisions. Nothing else may touch
  a widget. There is no animation and no timer for one: the tray icon is a still
  picture that changes only when the connection state does (the flyout's short
  opening animation is a chain of timers that ends by itself).
* **pystray**: the notification-area message loop (``run_detached``). Its menu
  callbacks and the left click only ``post()`` a message.
* **engine**: the daemon's asyncio loop (engine.py). Its events are ``post()``ed.
* **instance**: waits for the show/quit events of a second launch; ``post()``s.

``post()`` is a thread-safe queue; the main thread drains it every 100 ms
(``after``), applies the events to one ``ViewState`` and, only when something
changed, updates the tray icon, tooltip and flyout. The tray is told about a new
icon or tooltip only when the connection state changed; what Claude is doing
reaches the window and the menu as text and leaves the icon alone.
"""

import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox
from typing import Callable, Optional

import pystray

from . import APP_NAME, __version__
from .autostart import Autostart
from .config import AppConfig
from .engine import DaemonEngine
from .flyout import Placement, place_flyout
from .hookctl import HookManager, HookStatus, should_prompt
from .icons import IconSet, tray_icon_size
from .logsetup import tk_exception_handler
from .options import Options, app_dir, log_path, settings_path
from .single_instance import SingleInstance
from .state import EngineStatus, ViewState
from .theme import current_theme
from .tray_icon import ClawdTrayIcon, HiconCache
from .window import FlyoutWindow
from .winutil import ErrorBoxes, Win32Shell

logger = logging.getLogger("clawd-tank.tray.app")

PUMP_MS = 100          # how often the UI thread drains the queue
WATCHDOG_MS = 2000     # how often it checks that the engine thread is alive
STARTUP_HOOK_CHECK_MS = 800
QUIT_TIMEOUT_SECS = 10.0


class Dialogs:
    """The Yes/No and message dialogs. The flyout is closed first (``before``): a
    dialog taking the focus would close it anyway, and a flyout that is hidden
    while a dialog it owns is open would hide the dialog with it."""

    def __init__(self, root: tk.Tk, before: Optional[Callable[[], None]] = None):
        self._root = root
        self.before = before

    def _prepare(self) -> None:
        if self.before is not None:
            self.before()

    def ask_yes_no(self, title: str, text: str) -> bool:
        self._prepare()
        return bool(messagebox.askyesno(title, text))

    def info(self, title: str, text: str) -> None:
        self._prepare()
        messagebox.showinfo(title, text)

    def error(self, title: str, text: str) -> None:
        self._prepare()
        messagebox.showerror(title, text)


class TrayApp:
    def __init__(self, options: Options, *, instance: SingleInstance,
                 errors: Optional[ErrorBoxes] = None,
                 icons: Optional[IconSet] = None,
                 hooks: Optional[HookManager] = None,
                 autostart: Optional[Autostart] = None,
                 config: Optional[AppConfig] = None,
                 window_options: Optional[dict] = None):
        self.options = options
        self.window_options = dict(window_options or {})   # FlyoutWindow keywords (tests)
        self.instance = instance
        self.errors = errors or ErrorBoxes(enabled=not options.no_prompt)
        self.icons = icons or IconSet()
        self.hooks = hooks or HookManager(settings_path())
        self.autostart = autostart or Autostart()
        self.config = config or AppConfig(app_dir() / "state.json")

        self.view = ViewState(ble_enabled=not options.no_ble)
        self.hook_status: Optional[HookStatus] = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._quitting = False       # Quit was asked for: the engine is being stopped
        self._quitting_done = False  # the engine has stopped: the loop is ending
        self._icon_name = ""         # the picture the tray shows (state.ICON_NAMES)
        self._tooltip = ""
        self._status_line = "Starting..."
        self._event_line = ""        # the menu's "Last event: ..." line; empty = hidden
        self._can_retry = False

        self.root: Optional[tk.Tk] = None
        self.window: Optional[FlyoutWindow] = None
        self.dialogs: Optional[Dialogs] = None
        self.tray: Optional[ClawdTrayIcon] = None
        self.engine: Optional[DaemonEngine] = None
        self._tray_size = 16

    # --- thread-safe entry point --------------------------------------------

    def post(self, kind: str, payload=None) -> None:
        """From any thread: ask the UI thread to do something."""
        self._queue.put((kind, payload))

    # --- start up ------------------------------------------------------------

    def run(self) -> int:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = tk_exception_handler(self.errors)
        self.dialogs = Dialogs(self.root)
        scale = self.root.winfo_fpixels("1i") / 96.0
        self.window = FlyoutWindow(
            self.root, self.icons, self._window_actions(), scale,
            **{"theme_source": current_theme, "autostart": self._autostart_enabled,
               "placer": self._place_flyout, "before_show": self._before_window_show,
               **self.window_options})
        self.dialogs.before = lambda: self.window.hide("dialog")
        self._tray_size = tray_icon_size()
        logger.info("Clawd Tank %s starting (pid %d, tray icon %dpx, ui scale %.2f)",
                    __version__, os.getpid(), self._tray_size, scale)

        self.engine = DaemonEngine(
            lambda name, payload: self.post("engine", (name, payload)),
            no_ble=self.options.no_ble, tcp_port=self.options.tcp_port)
        self.engine.start()

        self.hook_status = self._safe_hook_status()
        self._start_tray()
        self.window.autohide = self.tray is not None   # with no tray icon, only Quit closes it
        self.instance.start_listener(lambda: self.post("show"), lambda: self.post("quit"))
        try:
            if self.autostart.repair():
                logger.info("Start with Windows pointed elsewhere; now points here")
        except OSError:
            logger.warning("could not check the Start with Windows entry", exc_info=True)

        try:
            self.refresh()
            if not self.options.minimized or self.tray is None:
                self.window_show()
            if not self.options.no_prompt:
                self.root.after(STARTUP_HOOK_CHECK_MS, self._startup_hook_check)
            self.root.after(PUMP_MS, self._pump)
            self.root.after(WATCHDOG_MS, self._watchdog)
            logger.info("Clawd Tank is running")
            self.root.mainloop()
        finally:
            self._cleanup()
        logger.info("Clawd Tank exited")
        return 0

    def _window_actions(self) -> dict:
        """What the flyout's rows do (the tray menu posts the same actions)."""
        return {"install": self.install_hooks, "remove": self.remove_hooks,
                "open_log": self.open_log, "toggle_autostart": self.toggle_autostart,
                "quit": self.quit, "retry": self.retry}

    def _cleanup(self) -> None:
        """However the main loop ended (Quit, or a crash): the tray icon goes, the
        engine stops (which clears the bar), the single-instance names are freed.
        Safe to repeat. The tray icon's thread is not a daemon thread, so skipping
        this would leave an invisible process behind."""
        if self.tray is not None:
            try:
                self.tray.shutdown()
            except Exception:
                logger.exception("removing the tray icon failed")
            self.tray = None
        if self.engine is not None and (self.engine.is_alive or
                                        self.engine.status != EngineStatus.STOPPED):
            try:
                self.engine.stop(QUIT_TIMEOUT_SECS)
            except Exception:
                logger.exception("stopping the engine failed")
        self.instance.release()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _start_tray(self) -> None:
        try:
            self._icon_name = self.view.icon_name()
            self.tray = ClawdTrayIcon(
                "ClawdTank", icon=self.icons.icon(self._icon_name, self._tray_size),
                title="Clawd Tank", menu=self._build_menu(), hicons=HiconCache(),
                on_click=lambda: self.post("toggle", time.monotonic()))

            def ready(icon) -> None:
                icon.visible = True
                logger.info("Tray icon ready")

            self.tray.run_detached(setup=ready)
        except Exception:
            logger.exception("The tray icon could not be created; showing the flyout instead")
            self.tray = None

    def _build_menu(self) -> pystray.Menu:
        Item = pystray.MenuItem
        return pystray.Menu(
            Item("Open", lambda: self.post("show"), default=True),
            Item(lambda item: self._status_line, None, enabled=False),
            Item(lambda item: self._event_line, None, enabled=False,
                 visible=lambda item: bool(self._event_line)),
            Item("Retry", lambda: self.post("retry"), visible=lambda item: self._can_retry),
            pystray.Menu.SEPARATOR,
            Item("Start with Windows", lambda: self.post("toggle_autostart"),
                 checked=lambda item: self._autostart_enabled()),
            Item("Install or repair hooks", lambda: self.post("install")),
            Item("Remove hooks", lambda: self.post("remove")),
            Item("Open log", lambda: self.post("open_log")),
            pystray.Menu.SEPARATOR,
            Item("Quit", lambda: self.post("quit")),
        )

    def _autostart_enabled(self) -> bool:
        """Read the registry each time the menu opens, from the tray thread."""
        try:
            return self.autostart.is_enabled()
        except OSError:
            return False

    # --- the UI-thread loop ---------------------------------------------------

    def _pump(self) -> None:
        dirty = False
        try:
            while True:
                try:
                    kind, payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                dirty |= self._handle(kind, payload)
        finally:
            if dirty and not self._quitting_done:
                self.refresh()
            if not self._quitting_done:
                self.root.after(PUMP_MS, self._pump)

    def _handle(self, kind: str, payload) -> bool:
        """Apply one posted message. Returns True if what is on screen may change."""
        if kind == "engine":
            name, data = payload
            self.view.apply(name, data)
            return name in ("status", "connection", "display", "hook")
        if kind == "show":
            logger.info("Show requested")
            self.window_show()
        elif kind == "toggle":
            logger.info("Tray click: %s", self.window.toggle(payload))
        elif kind == "quit":
            self.quit()
        elif kind == "retry":
            self.retry()
        elif kind == "toggle_autostart":
            self.toggle_autostart()
        elif kind == "install":
            self.install_hooks()
        elif kind == "remove":
            self.remove_hooks()
        elif kind == "open_log":
            self.open_log()
        elif kind == "quit_done":
            self._finish_quit()
        return False

    def _watchdog(self) -> None:
        if self._quitting:
            return
        try:
            self.engine.watchdog()
        finally:
            self.root.after(WATCHDOG_MS, self._watchdog)

    # --- what is on screen ------------------------------------------------------

    def refresh(self) -> None:
        """Bring the tray icon, its tooltip, the menu lines and the window in line
        with the state. Cheap when nothing changed: each step compares first, and
        the tray (a message to the shell) is only touched when the icon or the
        tooltip is different. A display or hook event changes neither."""
        name = self.view.icon_name()
        if name != self._icon_name:
            logger.info("Tray icon: %s", name)
            self._icon_name = name
            if self.tray is not None:
                try:
                    self.tray.icon = self.icons.icon(name, self._tray_size)
                except Exception:
                    logger.exception("could not update the tray icon")
        tooltip = self.view.tooltip()
        if tooltip != self._tooltip and self.tray is not None:
            self._tooltip = tooltip
            self.tray.title = tooltip
        self._status_line = self.view.status_text()
        self._event_line = (f"Last event: {self.view.last_event_text()}"
                            if self.view.last_event else "")
        self._can_retry = self.view.can_retry
        hs = self.hook_status
        self.window.render(self.view, hs.text if hs else "Checking...",
                           hs.warning if hs else "")

    # --- the flyout -----------------------------------------------------------------

    def _before_window_show(self) -> None:
        """Called as the flyout opens, before it is placed: what it shows is current."""
        self.hook_status = self._safe_hook_status()  # the file may have changed since
        self.refresh()

    def _tray_icon_ids(self):
        return self.tray.shell_ids() if self.tray is not None else []

    def _place_flyout(self, size, scale) -> Placement:
        placement = place_flyout(Win32Shell(self._tray_icon_ids), size, scale)
        logger.info("Flyout %dx%d at %d,%d (%s taskbar, work area %s, %s)", size[0], size[1],
                    placement.x, placement.y, placement.edge, tuple(placement.work),
                    f"next to the tray icon at {tuple(placement.anchor)}" if placement.anchored
                    else "in the corner")
        return placement

    def window_show(self) -> None:
        self.window.show()

    def window_hide(self) -> None:
        self.window.hide("app")

    # --- actions ----------------------------------------------------------------------

    def retry(self) -> None:
        logger.info("Retry clicked")
        if self.engine.retry():
            self.refresh()

    def open_log(self) -> None:
        path = log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as e:
            self.dialogs.error(APP_NAME, f"Could not open {path}:\n{e}")

    def toggle_autostart(self) -> None:
        try:
            target = not self.autostart.is_enabled()
            self.autostart.set_enabled(target)
            logger.info("Start with Windows %s", "on" if target else "off")
        except OSError as e:
            logger.exception("Start with Windows could not be changed")
            self.dialogs.error(APP_NAME, f"Could not change Start with Windows:\n{e}")

    def _safe_hook_status(self) -> Optional[HookStatus]:
        try:
            return self.hooks.status()
        except Exception:
            logger.exception("could not read the hooks status")
            return None

    def _startup_hook_check(self) -> None:
        status = self.hook_status = self._safe_hook_status()
        if status is None:
            return
        if should_prompt(status, self.config.hooks_declined):
            logger.info("Hooks need attention (%s); asking", status.text)
            self.install_hooks()
        else:
            logger.info("Hooks: %s", status.text)
        self.refresh()

    def install_hooks(self) -> None:
        """Install or repair, after a Yes. Used by the first-run check, the window
        button and the tray menu alike: the dialog is the user's consent."""
        status = self.hook_status = self._safe_hook_status()
        if status is not None and status.installed:
            self.dialogs.info(APP_NAME, "The Clawd Tank hooks are installed and up to date.")
            return
        if status is not None and status.settings_error:
            self.dialogs.error(APP_NAME, f"{status.settings_error}\n\nNothing was changed.")
            return
        if not self.dialogs.ask_yes_no(f"{APP_NAME}: add the hooks?",
                                       self.hooks.consent_text("install")):
            logger.info("Hooks: the user said No")
            self.config.hooks_declined = True
            self.refresh()
            return
        result = self.hooks.install()
        logger.info("Hooks install: %s %s", "ok" if result.ok else "failed", result.message)
        if result.ok:
            self.config.hooks_declined = False
            self.dialogs.info(APP_NAME, result.message)
        else:
            self.dialogs.error(APP_NAME, result.message)
        self.hook_status = self._safe_hook_status()
        self.refresh()

    def remove_hooks(self) -> None:
        if not self.dialogs.ask_yes_no(f"{APP_NAME}: remove the hooks?",
                                       self.hooks.consent_text("remove")):
            return
        result = self.hooks.remove()
        logger.info("Hooks remove: %s %s", "ok" if result.ok else "failed", result.message)
        if result.ok:
            self.config.hooks_declined = True  # removed on purpose: do not ask again
            self.dialogs.info(APP_NAME, result.message)
        else:
            self.dialogs.error(APP_NAME, result.message)
        self.hook_status = self._safe_hook_status()
        self.refresh()

    # --- quitting ------------------------------------------------------------------------

    def quit(self) -> None:
        """Quit for real: stop the engine (which clears the bar), take the icon
        away, leave the main loop. The engine stops on a helper thread so the
        window does not freeze while the bar is told."""
        if self._quitting:
            return
        self._quitting = True
        logger.info("Quit requested")
        self.window.hide("quit")
        self._status_line = "Quitting..."

        def stop_engine() -> None:
            try:
                if not self.engine.stop(QUIT_TIMEOUT_SECS):
                    logger.warning("The engine did not stop in time; exiting anyway")
            except Exception:
                logger.exception("stopping the engine failed")
            self.post("quit_done")

        threading.Thread(target=stop_engine, name="clawd-quit", daemon=True).start()

    def _finish_quit(self) -> None:
        self._quitting_done = True
        self.root.quit()  # mainloop returns; run() then cleans up
