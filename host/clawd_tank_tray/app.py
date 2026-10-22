"""TrayApp: the window, the tray icon and the engine, tied together.

Threads
-------
* **main**: tkinter. Owns the window, every timer (`after`), the animation, and
  all decisions. Nothing else may touch a widget.
* **pystray**: the notification-area message loop (``run_detached``). Its menu
  callbacks only ``post()`` a message.
* **engine**: the daemon's asyncio loop (engine.py). Its events are ``post()``ed.
* **instance**: waits for the show/quit events of a second launch; ``post()``s.

``post()`` is a thread-safe queue; the main thread drains it every 100 ms
(``after``), applies the events to one ``ViewState`` and, only when something
changed, updates the tray icon, tooltip and window.
"""

import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox
from typing import Optional

import pystray

from . import APP_NAME, __version__
from .autostart import Autostart
from .config import AppConfig
from .engine import DaemonEngine
from .hookctl import HookManager, HookStatus, should_prompt
from .icons import IconSet, tray_icon_size
from .logsetup import tk_exception_handler
from .options import Options, app_dir, log_path, settings_path
from .single_instance import SingleInstance
from .state import ONESHOT_SECONDS, EngineStatus, ViewState
from .tray_icon import ClawdTrayIcon, HiconCache
from .window import MainWindow
from .winutil import ErrorBoxes

logger = logging.getLogger("clawd-tank.tray.app")

PUMP_MS = 100          # how often the UI thread drains the queue
WATCHDOG_MS = 2000     # how often it checks that the engine thread is alive
ANIM_MS = 333          # about 3 frames a second
STARTUP_HOOK_CHECK_MS = 800
QUIT_TIMEOUT_SECS = 10.0


class Dialogs:
    """The Yes/No and message dialogs. Parent-less when the window is hidden, so
    a question from the tray still appears."""

    def __init__(self, root: tk.Tk):
        self._root = root

    def _parent(self):
        return self._root if self._root.state() == "normal" else None

    def ask_yes_no(self, title: str, text: str) -> bool:
        return bool(messagebox.askyesno(title, text, parent=self._parent()))

    def info(self, title: str, text: str) -> None:
        messagebox.showinfo(title, text, parent=self._parent())

    def error(self, title: str, text: str) -> None:
        messagebox.showerror(title, text, parent=self._parent())


class TrayApp:
    def __init__(self, options: Options, *, instance: SingleInstance,
                 errors: Optional[ErrorBoxes] = None,
                 icons: Optional[IconSet] = None,
                 hooks: Optional[HookManager] = None,
                 autostart: Optional[Autostart] = None,
                 config: Optional[AppConfig] = None):
        self.options = options
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
        self._sprite = ""
        self._frame = 0
        self._anim_job: Optional[str] = None
        self._tooltip = ""
        self._status_line = "Starting..."
        self._can_retry = False

        self.root: Optional[tk.Tk] = None
        self.window: Optional[MainWindow] = None
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
        self.window = MainWindow(self.root, self.icons, {
            "install": self.install_hooks, "open_log": self.open_log,
            "hide": self.window_hide, "quit": self.quit, "retry": self.retry,
        }, scale)
        self._tray_size = tray_icon_size()
        logger.info("Clawd Tank %s starting (pid %d, tray icon %dpx, ui scale %.2f)",
                    __version__, os.getpid(), self._tray_size, scale)

        self.engine = DaemonEngine(
            lambda name, payload: self.post("engine", (name, payload)),
            no_ble=self.options.no_ble, tcp_port=self.options.tcp_port)
        self.engine.start()

        self.hook_status = self._safe_hook_status()
        self._start_tray()
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
            first = self.icons.frame("grey", 0, self._tray_size)
            self.tray = ClawdTrayIcon(
                "ClawdTank", icon=first, title="Clawd Tank", menu=self._build_menu(),
                hicons=HiconCache())

            def ready(icon) -> None:
                icon.visible = True
                logger.info("Tray icon ready")

            self.tray.run_detached(setup=ready)
        except Exception:
            logger.exception("The tray icon could not be created; showing the window instead")
            self.tray = None

    def _build_menu(self) -> pystray.Menu:
        Item = pystray.MenuItem
        return pystray.Menu(
            Item("Open", lambda: self.post("show"), default=True),
            Item(lambda item: self._status_line, None, enabled=False),
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
            if name == "oneshot":
                self.root.after(int((ONESHOT_SECONDS + 0.1) * 1000), self.refresh)
            return name in ("status", "connection", "display", "oneshot", "hook")
        if kind == "show":
            logger.info("Show requested")
            self.window_show()
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
        """Bring the tray icon, its tooltip and the window in line with the state.
        Cheap when nothing changed: each step compares first."""
        now = time.time()
        sprite = self.view.sprite(now)
        if sprite != self._sprite:
            self._sprite, self._frame = sprite, 0
            self._cancel_anim()
            self._show_frame()
            if self.icons.is_animated(sprite):
                self._anim_job = self.root.after(ANIM_MS, self._tick)
        tooltip = self.view.tooltip(now)
        if tooltip != self._tooltip and self.tray is not None:
            self._tooltip = tooltip
            self.tray.title = tooltip
        self._status_line = tooltip.removeprefix("Clawd Tank: ")
        self._can_retry = self.view.can_retry
        hs = self.hook_status
        self.window.render(self.view, hs.text if hs else "Checking...",
                           hs.warning if hs else "", now)

    def _cancel_anim(self) -> None:
        if self._anim_job is not None:
            self.root.after_cancel(self._anim_job)
            self._anim_job = None

    def _tick(self) -> None:
        """One animation step: the next frame, once, into the tray and the window."""
        self._anim_job = None
        if self._quitting_done:
            return
        count = self.icons.frame_count(self._sprite)
        if count > 1:
            self._frame = (self._frame + 1) % count
            self._show_frame()
            self._anim_job = self.root.after(ANIM_MS, self._tick)

    def _show_frame(self) -> None:
        if self.tray is not None:
            try:
                self.tray.icon = self.icons.frame(self._sprite, self._frame, self._tray_size)
            except Exception:
                logger.exception("could not update the tray icon")
        if self.window.visible:
            self.window.set_frame(self._sprite, self._frame)

    # --- window ---------------------------------------------------------------------

    def window_show(self) -> None:
        self.hook_status = self._safe_hook_status()  # the file may have changed since
        self.window.show()
        self.window.set_frame(self._sprite or "grey", self._frame)
        self.refresh()

    def window_hide(self) -> None:
        self.window.hide()

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
        self.window.hide()
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
        self._cancel_anim()
        self.root.quit()  # mainloop returns; run() then cleans up
