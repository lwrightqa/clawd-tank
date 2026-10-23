"""The Windows calls behind the flyout, against the real system: the taskbar, the monitors,
the window styles, DWM, the opacity. Nothing here shows a window or takes the focus (the
one Tk window these tests make stays withdrawn)."""

import ctypes
import sys

import pytest

from clawd_tank_tray import flyout as fl
from clawd_tank_tray import winutil as wu
from clawd_tank_tray.flyout import Rect

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows calls")


@pytest.fixture
def hidden_window(capfd):
    import tkinter as tk
    try:
        with capfd.disabled():
            root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display for tkinter: {e}")
    root.overrideredirect(True)
    root.geometry("+-24000+-24000")
    root.update_idletasks()
    hwnd = wu.top_level_hwnd(root.winfo_id())
    yield root, hwnd
    root.destroy()


def test_colorref_is_blue_green_red():
    assert wu.colorref("#3F3F3F") == 0x3F3F3F
    assert wu.colorref("#D1D1D1") == 0xD1D1D1
    assert wu.colorref("#112233") == 0x332211            # 0x00BBGGRR
    assert wu.colorref("FF0000") == 0x0000FF


def test_the_taskbar_is_found_with_an_edge_and_a_rectangle():
    shell = wu.Win32Shell()
    found = shell.taskbar()
    if found is None:
        pytest.skip("no taskbar (a headless session)")
    edge, rect = found
    assert edge in fl.EDGES
    assert rect.width > 0 and rect.height > 0
    # a taskbar lies along one edge of its monitor
    monitor = shell.monitor_at(*rect.centre)
    assert monitor is not None and monitor[0].contains(*rect.centre)


def test_monitors_report_a_work_area_inside_the_monitor():
    shell = wu.Win32Shell()
    found = shell.monitor_at(0, 0)
    assert found is not None
    monitor, work = found
    assert work.left >= monitor.left and work.top >= monitor.top
    assert work.right <= monitor.right and work.bottom <= monitor.bottom
    assert work.width > 0 and work.height > 0
    # a point far outside every monitor still gets the nearest one
    far = shell.monitor_at(-30000, -30000)
    assert far is not None and far[0].width > 0


def test_the_inferred_edge_agrees_with_the_taskbar_when_it_takes_space():
    shell = wu.Win32Shell()
    found = shell.taskbar()
    if found is None:
        pytest.skip("no taskbar")
    edge, rect = found
    monitor, work = shell.monitor_at(*rect.centre)
    inferred = fl.infer_taskbar_edge(monitor, work)
    if inferred is not None:                              # None: auto-hide, no space is taken
        assert inferred == edge


def test_the_cursor_and_the_fallback_work_area_are_plausible():
    shell = wu.Win32Shell()
    cursor = shell.cursor()
    assert cursor is None or all(isinstance(v, int) for v in cursor)
    work = shell.fallback_work()
    assert work.width > 0 and work.height > 0


def test_there_is_no_icon_rectangle_without_an_icon_or_for_one_that_is_not_there():
    assert wu.Win32Shell().icon_rect() is None
    assert wu.Win32Shell(lambda: []).icon_rect() is None
    assert wu.Win32Shell(lambda: [(0x1234, 0), (0x1234, 99)]).icon_rect() is None   # not a window, not an icon


def test_a_flyout_is_placed_inside_the_work_area_of_this_machine():
    p = fl.place_flyout(wu.Win32Shell(), (300, 360), 1.0)
    assert p.work.left <= p.x and p.x + 300 <= p.work.right
    assert p.work.top <= p.y and p.y + 360 <= p.work.bottom
    assert p.edge in fl.EDGES and not p.anchored


def test_the_top_level_window_is_the_parent_of_tks_inner_window(hidden_window):
    root, hwnd = hidden_window
    assert hwnd and hwnd != root.winfo_id()


def test_styles_can_be_read_and_the_tool_window_bit_can_be_asserted(hidden_window):
    _root, hwnd = hidden_window
    style, ex = wu.window_styles(hwnd)
    assert style & wu.WS_POPUP and not style & wu.WS_CAPTION
    wu.ensure_tool_window(hwnd)
    _style, ex = wu.window_styles(hwnd)
    assert ex & wu.WS_EX_TOOLWINDOW and not ex & wu.WS_EX_APPWINDOW
    assert wu.ensure_tool_window(hwnd) is False           # already so: nothing to change
    # a window that had the other bit gets it taken away
    user = ctypes.WinDLL("user32")
    user.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowLongPtrW(hwnd, wu.GWL_EXSTYLE, (ex | wu.WS_EX_APPWINDOW) & ~wu.WS_EX_TOOLWINDOW)
    assert wu.ensure_tool_window(hwnd) is True
    assert wu.window_styles(hwnd)[1] & wu.WS_EX_TOOLWINDOW


def test_the_opacity_uses_a_layered_window_and_not_tks_alpha(hidden_window):
    root, hwnd = hidden_window
    wu.set_window_alpha(hwnd, 0.0)
    assert wu.window_styles(hwnd)[1] & wu.WS_EX_LAYERED
    wu.set_window_alpha(hwnd, 0.5)
    assert wu.window_styles(hwnd)[1] & wu.WS_EX_LAYERED
    wu.set_window_alpha(hwnd, 1.0)
    assert not wu.window_styles(hwnd)[1] & wu.WS_EX_LAYERED
    wu.set_window_alpha(hwnd, None)
    assert not wu.window_styles(hwnd)[1] & wu.WS_EX_LAYERED
    wu.ensure_tool_window(hwnd)
    wu.set_window_alpha(hwnd, 0.3)
    assert wu.window_styles(hwnd)[1] & wu.WS_EX_TOOLWINDOW   # the fade does not cost the tool window


def test_dwm_styling_reports_each_step(hidden_window):
    _root, hwnd = hidden_window
    result = wu.style_flyout(hwnd, "#3F3F3F")
    assert set(result) == {"frame", "corners", "border", "shadow"}
    assert all(isinstance(v, int) for v in result.values())
    assert result["frame"] == 0 and result["shadow"] == 0     # Windows 10 and 11 both do these
    if sys.getwindowsversion().build >= 22000:
        assert result["corners"] == 0 and result["border"] == 0
    # a window handle that is not one: reported, never raised
    bad = wu.style_flyout(0x10, "#000000")
    assert set(bad) <= {"frame", "corners", "border", "shadow"}


def test_animation_effects_are_a_plain_bool():
    assert wu.animations_enabled() in (True, False)


def test_the_window_rect_is_that_of_the_window(hidden_window):
    root, hwnd = hidden_window
    rect = wu.window_rect(hwnd)
    assert isinstance(rect, Rect) and rect.width == root.winfo_width()
    assert wu.window_rect(0x10) is None
