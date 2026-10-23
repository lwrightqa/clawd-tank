"""Small Windows helpers (ctypes, nothing to install)."""

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import Callable, Dict, Optional, Sequence, Tuple

from .flyout import Rect

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


# --- the flyout's window: styles, DWM, focus, and the questions place_flyout asks ---------------
#
# Every function here is best effort: it returns None / False / an HRESULT instead
# of raising, because nothing about how the flyout looks or where it goes may stop
# it from opening.

GWL_STYLE = -16
GWL_EXSTYLE = -20
GA_ROOT = 2

WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000          # WS_BORDER | WS_DLGFRAME: the title bar
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080    # no taskbar button, not in Alt+Tab
WS_EX_APPWINDOW = 0x00040000     # would force a taskbar button
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

DWMWA_NCRENDERING_POLICY = 2
DWMWA_WINDOW_CORNER_PREFERENCE = 33   # Windows 11 (build 22000) and later
DWMWA_BORDER_COLOR = 34               # Windows 11 (build 22000) and later
DWMNCRP_ENABLED = 2
DWMWCP_ROUND = 2

ABM_GETTASKBARPOS = 5
ABE_EDGES = {0: "left", 1: "top", 2: "right", 3: "bottom"}
MONITOR_DEFAULTTONEAREST = 2
SPI_GETWORKAREA = 0x0030
SPI_GETCLIENTAREAANIMATION = 0x1042
ASFW_ANY = 0xFFFFFFFF
LWA_ALPHA = 0x2

_w = None   # the loaded libraries, made on first use


class _MARGINS(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


class _APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uCallbackMessage", wintypes.UINT), ("uEdge", wintypes.UINT),
                ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.ULONG), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("guidItem", _GUID)]


def _libs():
    """(user32, dwmapi, shell32) with the argument types the 64-bit handles need."""
    global _w
    if _w is None:
        u = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        u.GetAncestor.restype = wintypes.HWND
        u.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
        u.SetLayeredWindowAttributes.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte,
                                                 wintypes.DWORD)
        u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        u.GetForegroundWindow.restype = wintypes.HWND
        u.SetForegroundWindow.argtypes = (wintypes.HWND,)
        u.BringWindowToTop.argtypes = (wintypes.HWND,)
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        u.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
        u.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
        u.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        u.MonitorFromPoint.restype = wintypes.HANDLE
        u.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
        u.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_MONITORINFO))
        u.SystemParametersInfoW.argtypes = (wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                            wintypes.UINT)
        d = ctypes.WinDLL("dwmapi")  # type: ignore[attr-defined]
        d.DwmSetWindowAttribute.restype = ctypes.c_long
        d.DwmSetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p,
                                            wintypes.DWORD)
        d.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
        d.DwmExtendFrameIntoClientArea.argtypes = (wintypes.HWND, ctypes.POINTER(_MARGINS))
        s = ctypes.WinDLL("shell32")  # type: ignore[attr-defined]
        s.SHAppBarMessage.restype = ctypes.c_size_t
        s.SHAppBarMessage.argtypes = (wintypes.DWORD, ctypes.POINTER(_APPBARDATA))
        s.Shell_NotifyIconGetRect.restype = ctypes.c_long
        s.Shell_NotifyIconGetRect.argtypes = (ctypes.POINTER(_NOTIFYICONIDENTIFIER),
                                              ctypes.POINTER(wintypes.RECT))
        _w = (u, d, s)
    return _w


def top_level_hwnd(widget_id: int) -> int:
    """The real top-level window of a tkinter window. ``winfo_id()`` of a Tk
    toplevel is the inner window Tk draws in; the one Windows styles, moves and
    activates is its parent."""
    return _libs()[0].GetAncestor(widget_id, GA_ROOT) or widget_id


def window_styles(hwnd: int) -> Tuple[int, int]:
    """(WS_* style, WS_EX_* extended style) of a window."""
    u = _libs()[0]
    return (u.GetWindowLongPtrW(hwnd, GWL_STYLE) & 0xFFFFFFFF,
            u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF)


def ensure_tool_window(hwnd: int) -> bool:
    """Give the window WS_EX_TOOLWINDOW and take WS_EX_APPWINDOW away: no taskbar
    button, not in Alt+Tab. Tk gives a borderless window the first, but drops it again
    when its own ``-alpha`` is used, so this is asserted before every show (and the
    flyout never calls ``-alpha``: see ``set_window_alpha``). True if it had to change
    anything."""
    try:
        u = _libs()[0]
        ex = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        want = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        if want != ex:
            u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want)
        return want != ex
    except Exception:  # noqa: BLE001
        return False


def set_window_alpha(hwnd: int, alpha: Optional[float]) -> None:
    """The window's opacity: None or 1.0 is plain and opaque (not a layered window),
    anything below is a layered window at that opacity. Used for the fade-in, and by
    tests to keep their windows invisible. Not Tk's ``-alpha``, which also rewrites
    the other extended styles."""
    try:
        u = _libs()[0]
        ex = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if alpha is None or alpha >= 1.0:
            if ex & WS_EX_LAYERED:
                u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
            return
        if not ex & WS_EX_LAYERED:
            u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        u.SetLayeredWindowAttributes(hwnd, 0, max(0, min(255, round(alpha * 255))), LWA_ALPHA)
    except Exception:  # noqa: BLE001
        logger.debug("setting the window opacity failed", exc_info=True)


def window_rect(hwnd: int) -> Optional[Rect]:
    r = wintypes.RECT()
    if not _libs()[0].GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return Rect(r.left, r.top, r.right, r.bottom)


def colorref(rgb: str) -> int:
    """'#RRGGBB' as the 0x00BBGGRR a COLORREF wants."""
    value = int(rgb.lstrip("#"), 16)
    return ((value & 0xFF) << 16) | (value & 0xFF00) | (value >> 16)


def style_flyout(hwnd: int, border: str) -> Dict[str, int]:
    """Make a borderless popup look like a Windows flyout. Returns the HRESULT of
    each step (0 = done); what an older Windows does not know simply fails, and the
    flyout stays square, which is how Windows 10 draws its own.

    * rounded corners (Windows 11);
    * the window border in `border` ('#RRGGBB'), drawn by Windows along the rounded
      outline (Windows 11);
    * the drop shadow: DWM draws it for a borderless window once the frame reaches
      1 px into the client area.
    """
    out: Dict[str, int] = {}
    try:
        _u, dwm, _s = _libs()
        for name, attr, value in (
                ("frame", DWMWA_NCRENDERING_POLICY, DWMNCRP_ENABLED),
                ("corners", DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND),
                ("border", DWMWA_BORDER_COLOR, colorref(border))):
            v = ctypes.c_uint(value)
            out[name] = dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
        margins = _MARGINS(1, 1, 1, 1)
        out["shadow"] = dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    except Exception:  # noqa: BLE001
        logger.debug("styling the flyout failed", exc_info=True)
    return out


def allow_set_foreground_window() -> bool:
    """Let any process take the foreground. Called by a second launch (which the
    user's click just started, so it holds the right) before it tells the running
    instance to show the flyout; the right is then good for that process's first
    SetForegroundWindow."""
    try:
        return bool(_libs()[0].AllowSetForegroundWindow(ASFW_ANY))
    except Exception:  # noqa: BLE001
        return False


def force_foreground(hwnd: int) -> bool:
    """Make `hwnd` the foreground window, if Windows lets this process. When a plain
    SetForegroundWindow is refused, share the foreground thread's input queue for a
    moment, which is the documented way around the foreground lock. True if `hwnd`
    is in front afterwards."""
    try:
        u = _libs()[0]
        if u.GetForegroundWindow() == hwnd:
            return True
        u.SetForegroundWindow(hwnd)
        if u.GetForegroundWindow() == hwnd:
            return True
        front = u.GetForegroundWindow()
        mine = ctypes.windll.kernel32.GetCurrentThreadId()
        theirs = u.GetWindowThreadProcessId(front, None) if front else 0
        attached = bool(theirs and theirs != mine and u.AttachThreadInput(mine, theirs, True))
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(mine, theirs, False)
        return u.GetForegroundWindow() == hwnd
    except Exception:  # noqa: BLE001
        return False


def foreground_window() -> int:
    try:
        return _libs()[0].GetForegroundWindow() or 0
    except Exception:  # noqa: BLE001
        return 0


def animations_enabled() -> bool:
    """False when Windows' 'Animation effects' setting is off (Settings > Accessibility
    > Visual effects): the flyout then opens without moving or fading."""
    try:
        flag = wintypes.BOOL(1)
        if _libs()[0].SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0,
                                            ctypes.byref(flag), 0):
            return bool(flag.value)
    except Exception:  # noqa: BLE001
        pass
    return True


class Win32Shell:
    """What ``flyout.place_flyout`` asks, answered by Windows. `icon_ids` returns the
    (window handle, id) pairs the notification icon may be registered with, once the
    icon has them; the first one the shell knows is used."""

    def __init__(self, icon_ids: Optional[Callable[[], Sequence[Tuple[int, int]]]] = None):
        self._icon_ids = icon_ids

    def icon_rect(self) -> Optional[Rect]:
        """Shell_NotifyIconGetRect: where the shell drew Clawd's icon, in screen pixels.
        The rectangle can be more than the icon (on Windows 11 a 16 px icon was answered
        with 168 x 40 px, the width of the notification area); the flyout is centred on
        whatever it gets."""
        for hwnd, uid in (self._icon_ids() if self._icon_ids else ()):
            try:
                ident = _NOTIFYICONIDENTIFIER(cbSize=ctypes.sizeof(_NOTIFYICONIDENTIFIER),
                                              hWnd=hwnd, uID=uid)
                r = wintypes.RECT()
                if _libs()[2].Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(r)) == 0 \
                        and r.right > r.left and r.bottom > r.top:
                    return Rect(r.left, r.top, r.right, r.bottom)
            except Exception:  # noqa: BLE001
                continue
        return None

    def taskbar(self) -> Optional[Tuple[str, Rect]]:
        """(edge, rectangle) of the main taskbar: ABM_GETTASKBARPOS."""
        try:
            data = _APPBARDATA(cbSize=ctypes.sizeof(_APPBARDATA))
            if not _libs()[2].SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(data)):
                return None
            rc = data.rc
            return ABE_EDGES.get(data.uEdge, "bottom"), Rect(rc.left, rc.top, rc.right, rc.bottom)
        except Exception:  # noqa: BLE001
            return None

    def monitor_at(self, x: int, y: int) -> Optional[Tuple[Rect, Rect]]:
        """(monitor, work area) of the monitor nearest to the point."""
        try:
            u = _libs()[0]
            hmon = u.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST)
            info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
            if not hmon or not u.GetMonitorInfoW(hmon, ctypes.byref(info)):
                return None
            m, w = info.rcMonitor, info.rcWork
            return Rect(m.left, m.top, m.right, m.bottom), Rect(w.left, w.top, w.right, w.bottom)
        except Exception:  # noqa: BLE001
            return None

    def cursor(self) -> Optional[Tuple[int, int]]:
        try:
            p = wintypes.POINT()
            return (p.x, p.y) if _libs()[0].GetCursorPos(ctypes.byref(p)) else None
        except Exception:  # noqa: BLE001
            return None

    def fallback_work(self) -> Rect:
        """The primary monitor's work area; as a last resort a screen-sized box."""
        try:
            r = wintypes.RECT()
            if _libs()[0].SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
                return Rect(r.left, r.top, r.right, r.bottom)
        except Exception:  # noqa: BLE001
            pass
        return Rect(0, 0, 1280, 720)
