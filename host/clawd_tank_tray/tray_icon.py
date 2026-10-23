"""The notification-area icon: pystray, with the icon handling replaced.

pystray's Windows backend turns every image you give it into a temporary .ico
file and loads it at the *system default icon size* (32 px), then Windows
shrinks that to the 16 px (or 20/24 px) the tray wants: a file written and read
for every change, and a blurry result for pixel art. Here each of the three
pictures becomes a native HICON once, at exactly the size the tray asks for,
from the pre-scaled PNGs; changing the picture is then one NIM_MODIFY message
with a cached handle, and nothing at all happens while the picture stays the same.

Two other changes: the menu is rebuilt just before it opens, so its checkbox
and status line show the state of that moment (the Start with Windows box reads
the registry then), and a left click calls ``on_click`` (the app toggles the
flyout with it), or runs the default item ("Open") when there is none.
"""

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Tuple

import pystray
from PIL import Image

if sys.platform == "win32":
    from pystray import _win32 as _backend
else:  # pragma: no cover - the app is Windows only
    _backend = None

logger = logging.getLogger("clawd-tank.tray.icon")

BI_RGB = 0
DIB_RGB_COLORS = 0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP),
    ]


_gdi = _user = None


def _libs():
    global _gdi, _user
    if _gdi is None:
        g = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]
        g.CreateDIBSection.restype = wintypes.HBITMAP
        g.CreateDIBSection.argtypes = (
            wintypes.HDC, ctypes.POINTER(_BITMAPINFOHEADER), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
        g.CreateBitmap.restype = wintypes.HBITMAP
        g.CreateBitmap.argtypes = (ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT,
                                   ctypes.c_void_p)
        g.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
        u = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        u.CreateIconIndirect.restype = wintypes.HICON
        u.CreateIconIndirect.argtypes = (ctypes.POINTER(_ICONINFO),)
        u.DestroyIcon.argtypes = (wintypes.HICON,)
        _gdi, _user = g, u
    return _gdi, _user


def hicon_from_image(image: Image.Image) -> int:
    """A native icon handle from an RGBA image, at the image's own size (a 32-bit
    colour bitmap with alpha; the AND mask is all zeros). The caller owns the
    handle: free it with ``destroy_hicon``."""
    gdi, user = _libs()
    rgba = image.convert("RGBA")
    w, h = rgba.size
    header = _BITMAPINFOHEADER(
        biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=w, biHeight=-h,  # top-down
        biPlanes=1, biBitCount=32, biCompression=BI_RGB)
    bits = ctypes.c_void_p()
    color = gdi.CreateDIBSection(None, ctypes.byref(header), DIB_RGB_COLORS,
                                 ctypes.byref(bits), None, 0)
    if not color or not bits:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        data = rgba.tobytes("raw", "BGRA")
        ctypes.memmove(bits, data, len(data))
        mask_bytes = ((w + 15) // 16) * 2 * h
        mask = gdi.CreateBitmap(w, h, 1, 1, ctypes.create_string_buffer(mask_bytes))
        if not mask:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _ICONINFO(fIcon=True, xHotspot=0, yHotspot=0, hbmMask=mask, hbmColor=color)
            hicon = user.CreateIconIndirect(ctypes.byref(info))
            if not hicon:
                raise ctypes.WinError(ctypes.get_last_error())
            return hicon
        finally:
            gdi.DeleteObject(mask)
    finally:
        gdi.DeleteObject(color)


def destroy_hicon(hicon: int) -> None:
    _libs()[1].DestroyIcon(hicon)


class HiconCache:
    """One HICON per image object (so one per tray state), made on first use and
    kept until ``clear``."""

    def __init__(self) -> None:
        self._handles: Dict[int, Tuple[Image.Image, int]] = {}

    def get(self, image: Image.Image) -> int:
        entry = self._handles.get(id(image))
        if entry is None:
            # keep the image alive with its handle, so its id() cannot be reused
            entry = (image, hicon_from_image(image))
            self._handles[id(image)] = entry
        return entry[1]

    def __len__(self) -> int:
        return len(self._handles)

    def clear(self) -> None:
        for _image, hicon in self._handles.values():
            destroy_hicon(hicon)
        self._handles.clear()


class ClawdTrayIcon(_backend.Icon if _backend else pystray.Icon):  # type: ignore[misc]
    """pystray's Windows icon with cached native handles and a menu that is
    refreshed as it opens."""

    def __init__(self, *args, hicons: Optional[HiconCache] = None,
                 on_menu_open: Optional[Callable[[], None]] = None,
                 on_click: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        # not `hicons or HiconCache()`: an empty cache has len 0 and is falsy
        self._hicons = hicons if hicons is not None else HiconCache()
        self._on_menu_open = on_menu_open
        self._on_click = on_click

    def shell_ids(self) -> List[Tuple[int, int]]:
        """The (window handle, id) pairs this icon may be registered with in the
        notification area, which is what Shell_NotifyIconGetRect needs to say where
        the icon is; empty until the icon's thread has made its window. pystray
        passes ``hID=id(self)``, but ``NOTIFYICONDATAW`` has no such field (it is
        ``uID``), so the id the shell holds is 0; the cut-down ``id(self)`` is listed
        too, in case a later pystray sets it. The first pair the shell knows wins."""
        hwnd = getattr(self, "_hwnd", None)
        if not hwnd:
            return []
        return [(hwnd, 0), (hwnd, ctypes.c_uint(id(self)).value)]

    # pystray calls these around every image change.
    def _release_icon(self) -> None:
        self._icon_handle = None  # the cache owns the handles

    def _assert_icon_handle(self) -> None:
        if not self._icon_handle:
            self._icon_handle = self._hicons.get(self.icon)

    def _on_notify(self, wparam, lparam):
        if lparam == _backend.win32.WM_LBUTTONUP and self._on_click is not None:
            try:
                self._on_click()   # on the tray thread: only post a message
            except Exception:
                logger.exception("the tray click handler failed")
            return None
        if lparam == _backend.win32.WM_RBUTTONUP:
            try:
                if self._on_menu_open:
                    self._on_menu_open()
                self.update_menu()  # re-evaluate the checkbox and the status line now
            except Exception:
                logger.exception("refreshing the tray menu failed")
        return super()._on_notify(wparam, lparam)

    def shutdown(self) -> None:
        """Remove the icon from the tray and free the handles."""
        try:
            self.visible = False
        except Exception:
            logger.debug("hiding the tray icon failed", exc_info=True)
        self.stop()
        self._hicons.clear()
