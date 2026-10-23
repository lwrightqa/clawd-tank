"""The native tray icon pieces: HICON from a picture, the handle cache, the pystray subclass."""

import ctypes
import sys
import threading
from ctypes import wintypes

import pystray
import pytest
from PIL import Image

from clawd_tank_tray.icons import IconSet
from clawd_tank_tray.tray_icon import (
    ClawdTrayIcon,
    HiconCache,
    _backend,
    destroy_hicon,
    hicon_from_image,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native icons")


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", wintypes.LPVOID)]


user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
user32.GetIconInfo.argtypes = (wintypes.HICON, ctypes.POINTER(ICONINFO))
gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
gdi32.GetObjectW.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p)
gdi32.GetBitmapBits.argtypes = (wintypes.HBITMAP, wintypes.LONG, ctypes.c_void_p)


def icon_size_and_depth(hicon):
    user, gdi = user32, gdi32
    info = ICONINFO()
    assert user.GetIconInfo(hicon, ctypes.byref(info))
    try:
        bmp = BITMAP()
        gdi.GetObjectW(info.hbmColor, ctypes.sizeof(bmp), ctypes.byref(bmp))
        return bmp.bmWidth, bmp.bmHeight, bmp.bmBitsPixel
    finally:
        gdi.DeleteObject(info.hbmColor)
        gdi.DeleteObject(info.hbmMask)


@pytest.mark.parametrize("size", [16, 20, 24, 32])
def test_hicon_has_exactly_the_size_of_the_picture_so_windows_does_not_rescale(size):
    picture = IconSet().icon("idle", size)
    hicon = hicon_from_image(picture)
    try:
        assert hicon
        assert icon_size_and_depth(hicon) == (size, size, 32)
    finally:
        destroy_hicon(hicon)


def test_hicon_keeps_the_alpha_channel():
    user, gdi = user32, gdi32
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    img.putpixel((3, 4), (255, 0, 0, 255))
    hicon = hicon_from_image(img)
    try:
        info = ICONINFO()
        user.GetIconInfo(hicon, ctypes.byref(info))
        buf = ctypes.create_string_buffer(16 * 16 * 4)
        gdi.GetBitmapBits(info.hbmColor, len(buf), buf)
        gdi.DeleteObject(info.hbmColor)
        gdi.DeleteObject(info.hbmMask)
        px = lambda x, y: tuple(buf.raw[(y * 16 + x) * 4:(y * 16 + x) * 4 + 4])  # noqa: E731
        assert px(3, 4) == (0, 0, 255, 255)  # BGRA
        assert px(0, 0)[3] == 0
    finally:
        destroy_hicon(hicon)


def test_cache_makes_one_handle_per_state_and_clears():
    icons = IconSet()
    cache = HiconCache()
    idle, grey, error = (icons.icon(n, 16) for n in ("idle", "grey", "error"))
    handle = cache.get(idle)
    assert cache.get(idle) == handle and cache.get(icons.icon("idle", 16)) == handle  # same object
    assert cache.get(grey) != handle and cache.get(error) not in (handle, cache.get(grey))
    assert len(cache) == 3
    cache.clear()
    assert len(cache) == 0


def test_a_picture_swap_uses_the_cache_not_a_file(monkeypatch):
    """pystray's own path writes a temp .ico per change; this one must not."""
    icons = IconSet()
    cache = HiconCache()
    icon = ClawdTrayIcon("t", icon=icons.icon("idle", 16), title="x", hicons=cache)
    written = []
    monkeypatch.setattr(_backend, "serialized_image",
                        lambda *a, **k: written.append(a) or (_ for _ in ()).throw(AssertionError("temp file")),
                        raising=False)
    icon._assert_icon_handle()
    first = icon._icon_handle
    assert first == cache.get(icons.icon("idle", 16))
    icon._release_icon()
    assert icon._icon_handle is None and len(cache) == 1  # released, but not destroyed
    icon._icon = icons.icon("grey", 16)
    icon._assert_icon_handle()
    assert icon._icon_handle != first and len(cache) == 2
    # and back: the first handle is found again, nothing new is made
    icon._release_icon()
    icon._icon = icons.icon("idle", 16)
    icon._assert_icon_handle()
    assert icon._icon_handle == first and len(cache) == 2
    assert written == []
    cache.clear()


def test_left_click_runs_the_default_item():
    opened = threading.Event()
    menu = pystray.Menu(pystray.MenuItem("Open", lambda: opened.set(), default=True),
                        pystray.MenuItem("Quit", lambda: None))
    icon = ClawdTrayIcon("t", icon=IconSet().icon("idle", 16), title="x", menu=menu)
    icon._on_notify(0, _backend.win32.WM_LBUTTONUP)
    assert opened.is_set()
    icon._hicons.clear()


def test_right_click_refreshes_the_menu_before_it_opens(monkeypatch):
    icon = ClawdTrayIcon("t", icon=IconSet().icon("idle", 16), title="x",
                         menu=pystray.Menu(pystray.MenuItem("A", lambda: None)))
    order = []
    monkeypatch.setattr(icon, "update_menu", lambda: order.append("update_menu"))
    monkeypatch.setattr(_backend.Icon, "_on_notify", lambda self, w, l: order.append("popup"))
    icon._on_notify(0, _backend.win32.WM_RBUTTONUP)
    assert order == ["update_menu", "popup"]
    order.clear()
    icon._on_notify(0, _backend.win32.WM_LBUTTONUP)
    assert order == ["popup"]  # only a right click rebuilds it


def test_a_left_click_calls_on_click_instead_of_the_default_item():
    clicked, opened = [], []
    menu = pystray.Menu(pystray.MenuItem("Open", lambda: opened.append(1), default=True),
                        pystray.MenuItem("Quit", lambda: None))
    icon = ClawdTrayIcon("t", icon=IconSet().icon("idle", 16), title="x", menu=menu,
                         on_click=lambda: clicked.append(1))
    icon._on_notify(0, _backend.win32.WM_LBUTTONUP)
    assert clicked == [1] and opened == []
    icon._hicons.clear()


def test_a_failing_click_handler_is_logged_and_does_not_stop_the_tray_thread(caplog):
    def boom():
        raise RuntimeError("handler broke")

    icon = ClawdTrayIcon("t", icon=IconSet().icon("idle", 16), title="x", on_click=boom)
    with caplog.at_level("ERROR"):
        icon._on_notify(0, _backend.win32.WM_LBUTTONUP)      # must not raise
    assert "click handler failed" in caplog.text
    icon._hicons.clear()


def test_the_right_click_still_opens_the_menu_when_there_is_an_on_click(monkeypatch):
    icon = ClawdTrayIcon("t", icon=IconSet().icon("idle", 16), title="x",
                         menu=pystray.Menu(pystray.MenuItem("A", lambda: None)),
                         on_click=lambda: None)
    order = []
    monkeypatch.setattr(icon, "update_menu", lambda: order.append("update_menu"))
    monkeypatch.setattr(_backend.Icon, "_on_notify", lambda self, w, l: order.append("popup"))
    icon._on_notify(0, _backend.win32.WM_RBUTTONUP)
    assert order == ["update_menu", "popup"]
    icon._hicons.clear()


def test_shell_ids_are_what_shell_notify_icon_get_rect_needs():
    icon = ClawdTrayIcon("t", icon=IconSet().icon("idle", 16), title="x")
    assert icon.shell_ids() == []                            # the tray thread has not made its window
    icon._hwnd = 0x1234
    assert icon.shell_ids() == [(0x1234, 0), (0x1234, id(icon) & 0xFFFFFFFF)]
    icon._hicons.clear()


def test_pystray_registers_its_icon_with_id_0_so_the_shell_ids_start_there():
    """pystray passes hID=id(self) to a structure whose field is called uID, so the shell
    holds id 0. If this ever fails, pystray changed and shell_ids should say so."""
    from ctypes import sizeof

    data = _backend.win32.NOTIFYICONDATAW(cbSize=sizeof(_backend.win32.NOTIFYICONDATAW), hWnd=1,
                                          hID=12345, uFlags=0)
    assert data.uID == 0


def test_the_shell_says_where_the_real_icon_is_and_the_flyout_anchors_to_it():
    """A real icon in the real notification area (for a second): Shell_NotifyIconGetRect
    answers for the id pystray really used, and the placement function anchors to it."""
    import time

    from clawd_tank_tray import flyout as fl
    from clawd_tank_tray.winutil import Win32Shell

    icon = ClawdTrayIcon("ClawdTankRectTest", icon=IconSet().icon("idle", 16), title="rect test",
                         hicons=HiconCache())
    ready = threading.Event()
    icon.run_detached(setup=lambda i: (setattr(i, "visible", True), ready.set()))
    try:
        assert ready.wait(10)
        shell = Win32Shell(icon.shell_ids)
        rect = None
        for _ in range(50):                                  # the shell takes a moment to place it
            rect = shell.icon_rect()
            if rect:
                break
            time.sleep(0.1)
        assert rect is not None and rect.width > 0 and rect.height > 0
        monitor = Win32Shell().monitor_at(*rect.centre)
        assert monitor is not None and monitor[0].contains(*rect.centre)
        placement = fl.place_flyout(shell, (300, 360))
        assert placement.anchored and placement.anchor == rect
        assert placement.work.left <= placement.x and placement.x + 300 <= placement.work.right
    finally:
        icon.shutdown()
