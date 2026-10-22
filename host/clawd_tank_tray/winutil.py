"""Small Windows helpers (ctypes, nothing to install)."""

import ctypes
import logging
import sys
import threading

logger = logging.getLogger("clawd-tank.tray")

MB_OK = 0x0
MB_ICONERROR = 0x10
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000


def set_dpi_aware() -> None:
    """Make the process DPI-aware, before any window or icon exists. Without it
    Windows reports 16 px for the notification area at every scale and stretches
    the icon (blurry); with it the tray gets 20/24/32 px at 125/150/200 %, and the
    crisp frame of that size is used. Safe to call twice; failures are ignored
    (the manifest may already have set it)."""
    if sys.platform != "win32":
        return
    try:
        # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-2))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system aware
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def message_box(title: str, text: str, flags: int = MB_OK | MB_ICONERROR) -> int:
    """A native message box, for failures where tkinter itself may be broken."""
    if sys.platform != "win32":
        return 0
    return ctypes.windll.user32.MessageBoxW(
        None, text, title, flags | MB_SETFOREGROUND | MB_TOPMOST)


class ErrorBoxes:
    """The 'something went wrong' message box for uncaught exceptions: never more
    than `limit` per run and never two at once, so a failing timer cannot bury
    the screen. Silent when `enabled` is False (--no-prompt)."""

    def __init__(self, enabled: bool = True, limit: int = 3):
        self.enabled = enabled
        self.limit = limit
        self._shown = 0
        self._lock = threading.Lock()
        self._busy = False

    def show(self, title: str, text: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._busy or self._shown >= self.limit:
                return False
            self._busy, self._shown = True, self._shown + 1
        try:
            message_box(title, text)
        finally:
            with self._lock:
                self._busy = False
        return True
