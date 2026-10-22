"""One tray app at a time, and a way for a second launch to talk to the first.

* ``Local\\ClawdTankTray`` is a named mutex (CreateMutexW). Whoever creates it
  first is the instance; a second process sees ERROR_ALREADY_EXISTS.
* ``Local\\ClawdTankTray.Show`` and ``.Quit`` are named events the instance
  waits on in a small thread. A second launch sets Show and exits, so the
  running window comes forward; ``ClawdTank.exe --quit`` sets Quit. Events keep
  this free of ports and of anything another user could reach: ``Local\\`` names
  belong to the sign-in session.

``CLAWD_TANK_INSTANCE`` appends a suffix to all three names, so a test or a
smoke run never meets the user's real instance.
"""

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from typing import Callable, Mapping, Optional

from .options import ENV_INSTANCE

logger = logging.getLogger("clawd-tank.tray.instance")

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF

_k32 = None


def _kernel32():
    global _k32
    if _k32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k.CreateEventW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        k.OpenEventW.restype = wintypes.HANDLE
        k.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        k.SetEvent.argtypes = (wintypes.HANDLE,)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        k.WaitForMultipleObjects.restype = wintypes.DWORD
        k.WaitForMultipleObjects.argtypes = (
            wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD)
        _k32 = k
    return _k32


def names(instance: Optional[str] = None, env: Optional[Mapping[str, str]] = None):
    """(mutex, show event, quit event) names."""
    env = os.environ if env is None else env
    instance = env.get(ENV_INSTANCE, "") if instance is None else instance
    base = "Local\\ClawdTankTray" + (f"-{instance}" if instance else "")
    return base, base + ".Show", base + ".Quit"


class SingleInstance:
    def __init__(self, instance: Optional[str] = None):
        self.mutex_name, self.show_name, self.quit_name = names(instance)
        self._mutex = None
        self._events = []
        self._stop_event = None
        self._thread: Optional[threading.Thread] = None

    # --- being the instance -------------------------------------------------

    def acquire(self) -> bool:
        """True if this process is now the instance; False if another one is."""
        k = _kernel32()
        handle = k.CreateMutexW(None, False, self.mutex_name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            k.CloseHandle(handle)
            return False
        self._mutex = handle
        return True

    def release(self) -> None:
        self.stop_listener()
        if self._mutex:
            _kernel32().CloseHandle(self._mutex)
            self._mutex = None

    # --- hearing the other launches ----------------------------------------

    def start_listener(self, on_show: Callable[[], None],
                       on_quit: Callable[[], None]) -> None:
        """Call on_show / on_quit (on a helper thread) when another process
        signals. They must only post to the UI, not touch it."""
        k = _kernel32()
        show = k.CreateEventW(None, False, False, self.show_name)
        quit_ = k.CreateEventW(None, False, False, self.quit_name)
        stop = k.CreateEventW(None, True, False, None)
        if not (show and quit_ and stop):
            raise ctypes.WinError(ctypes.get_last_error())
        self._events = [show, quit_, stop]
        self._stop_event = stop
        callbacks = {0: on_show, 1: on_quit}

        def loop() -> None:
            arr = (wintypes.HANDLE * 3)(show, quit_, stop)
            while True:
                r = k.WaitForMultipleObjects(3, arr, False, INFINITE)
                idx = r - WAIT_OBJECT_0
                if idx not in (0, 1):
                    return  # the stop event, or a failed wait
                try:
                    callbacks[idx]()
                except Exception:
                    logger.exception("instance callback raised")

        self._thread = threading.Thread(target=loop, name="clawd-instance", daemon=True)
        self._thread.start()

    def stop_listener(self) -> None:
        k = _kernel32()
        if self._stop_event:
            k.SetEvent(self._stop_event)
        if self._thread:
            self._thread.join(2.0)
            self._thread = None
        for h in self._events:
            k.CloseHandle(h)
        self._events = []
        self._stop_event = None

    # --- talking to the instance --------------------------------------------

    def signal(self, which: str, wait: float = 3.0) -> bool:
        """Set the instance's ``show`` or ``quit`` event. Retries for `wait`
        seconds, because a launch right behind the first can beat the
        listener's setup. False if nobody is listening."""
        name = {"show": self.show_name, "quit": self.quit_name}[which]
        k = _kernel32()
        deadline = time.monotonic() + wait
        while True:
            handle = k.OpenEventW(EVENT_MODIFY_STATE, False, name)
            if handle:
                try:
                    return bool(k.SetEvent(handle))
                finally:
                    k.CloseHandle(handle)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
