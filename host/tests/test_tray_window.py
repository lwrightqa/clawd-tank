"""The flyout window: a borderless tool window with menu rows, light or dark, placed by a
function, closed by focus loss / Escape / a second tray click.

Everything runs against a real tkinter window and its real Win32 handle, but the windows
these tests make are invisible and out of the way: fully transparent (a layered window at
alpha 0, never Tk's -alpha), parked far outside every monitor, and they never take the
focus. Nothing shows on the desktop and the user's foreground window is left alone."""

import time
import tkinter as tk
import types
from tkinter import ttk

import pytest

from clawd_tank_tray import flyout as fl
from clawd_tank_tray import theme as th
from clawd_tank_tray import winutil
from clawd_tank_tray.icons import IconSet
from clawd_tank_tray.state import CONFLICT_MESSAGE, ViewState
from clawd_tank_tray.window import (ACTION_ROWS, ANIM_STEPS, BASE_WIDTH, DOT_COLOURS, DOT_OTHER, RED,
                                    FlyoutWindow, closest_size)

ACTIONS = ("install", "remove", "open_log", "toggle_autostart", "quit", "retry")
FAR_AWAY = fl.Rect(-24000, -24000, -22000, -23000)     # nowhere near any monitor


@pytest.fixture
def root(capfd):
    # Tcl looks at the process's standard handles while it starts up, and under
    # pytest's default fd capture that fails now and then ("Can't find a usable
    # tk.tcl"). Starting Tk with capture off is reliable; the app itself has no
    # console at all and is not affected.
    try:
        with capfd.disabled():
            r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display for tkinter: {e}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture
def calls():
    return []


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


def far_away(size, scale):
    """Where the tests' flyouts go: bottom-right of a work area that is not on any monitor."""
    x, y = fl.flyout_position(FAR_AWAY, "bottom", size, scale)
    return fl.Placement(x, y, FAR_AWAY, "bottom", None)


@pytest.fixture
def make(root, calls, clock):
    def factory(scale=1.0, **kw):
        actions = {name: (lambda n=name: calls.append(n)) for name in ACTIONS}
        opts = dict(theme_source=lambda: th.DARK, autostart=lambda: False, placer=far_away,
                    clock=clock, animate=False, opacity=0.0, take_focus=False)
        opts.update(kw)
        return FlyoutWindow(root, IconSet(), actions, scale, **opts)
    return factory


@pytest.fixture
def win(make):
    return make()


def pump(root, until, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if until():
            return True
        time.sleep(0.01)
    return until()


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def pending(root):
    return set(root.tk.call("after", "info"))


def running(**kw):
    v = ViewState(**kw)
    v.apply("status", {"status": "running"})
    v.apply("connection", {"connected": True})
    return v


def conflict_view():
    v = running()
    v.apply("status", {"status": "conflict", "message": CONFLICT_MESSAGE + " (port 47421 is in use)"})
    return v


def open_flyout(root, win):
    win.show()
    root.update()
    return win


# --- a borderless tool window: no title bar, no buttons, no taskbar button -------------------------------


def test_the_real_window_has_no_title_bar_and_is_a_tool_window(root, win):
    open_flyout(root, win)
    style, ex = winutil.window_styles(win.hwnd)
    assert win.hwnd != root.winfo_id()                       # the top-level wrapper, not Tk's inner window
    assert not style & winutil.WS_CAPTION, "a caption (title bar) bit is set"
    assert style & winutil.WS_POPUP
    for bit in (winutil.WS_SYSMENU, winutil.WS_THICKFRAME, winutil.WS_MINIMIZEBOX, winutil.WS_MAXIMIZEBOX):
        assert not style & bit, hex(bit)                     # no icon, no sizing border, no buttons
    assert ex & winutil.WS_EX_TOOLWINDOW, "no taskbar button needs WS_EX_TOOLWINDOW"
    assert not ex & winutil.WS_EX_APPWINDOW
    assert ex & winutil.WS_EX_TOPMOST                        # floats above the others, like a flyout
    assert not ex & winutil.WS_EX_NOACTIVATE                 # it must be able to take the focus
    assert root.overrideredirect()


def test_it_stays_a_tool_window_every_time_it_opens_and_after_the_fade(root, make):
    win = make(animate=True, opacity=0.01)                   # the fade uses a layered window
    for _ in range(3):
        win.show()
        assert pump(root, lambda: not pending(root))         # the opening animation has run out
        style, ex = winutil.window_styles(win.hwnd)
        assert ex & winutil.WS_EX_TOOLWINDOW and not ex & winutil.WS_EX_APPWINDOW
        assert not style & winutil.WS_CAPTION
        win.hide("test")
        root.update()


def test_the_window_is_dwm_styled_before_it_shows(root, win):
    open_flyout(root, win)
    assert set(win.style_result) == {"frame", "corners", "border", "shadow"}
    assert win.style_result["frame"] == 0 and win.style_result["shadow"] == 0
    import sys
    if sys.getwindowsversion().build >= 22000:               # Windows 11: rounded, bordered
        assert win.style_result["corners"] == 0 and win.style_result["border"] == 0
        import ctypes
        value = ctypes.c_int(0)
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmGetWindowAttribute.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint)
        assert dwm.DwmGetWindowAttribute(win.hwnd, winutil.DWMWA_WINDOW_CORNER_PREFERENCE,
                                         ctypes.byref(value), 4) == 0
        assert value.value == winutil.DWMWCP_ROUND
        # (the border colour can be set but not read back: DWM answers E_INVALIDARG)


def test_there_is_no_title_text_bar_of_any_kind_inside_it(root, win):
    open_flyout(root, win)
    assert not [w for w in walk(root) if isinstance(w, (tk.Button, ttk.Button, tk.Menu))]
    assert winutil.window_rect(win.hwnd).width == root.winfo_width()   # no frame around Tk's client area


# --- size ---------------------------------------------------------------------------------------------------


def test_it_is_300_px_wide_at_100_percent_and_as_tall_as_its_content(root, win):
    open_flyout(root, win)
    assert BASE_WIDTH == 300
    assert root.winfo_width() == 300
    assert root.winfo_height() == root.winfo_reqheight()
    assert 280 < root.winfo_height() < 460
    assert winutil.window_rect(win.hwnd).width == 300


@pytest.mark.parametrize("scale", [1.25, 1.5, 2.0])
def test_the_width_and_rows_scale_with_dpi(root, make, scale):
    win = make(scale=scale)
    open_flyout(root, win)
    assert root.winfo_width() == round(300 * scale)
    assert win.rows["quit"].frame.winfo_height() == round(36 * scale)
    assert root.winfo_height() == root.winfo_reqheight()


def test_the_text_is_9_to_10_points_in_a_segoe_font(root, win):
    for name, pt in (("body", 9), ("row", 10), ("title", 10)):
        font = win.fonts[name]
        assert int(font.cget("size")) == pt
        assert font.actual("family").startswith("Segoe UI")


# --- header: the small Clawd mark, the title, the status dot ------------------------------------------------------


def test_the_header_is_a_20px_clawd_mark_the_title_and_the_dot(root, make):
    before = set(root.tk.call("image", "names"))
    win = make()
    open_flyout(root, win)
    images = set(root.tk.call("image", "names")) - before
    assert len(images) == 1, images                          # one picture in the whole flyout
    (name,) = images
    assert int(root.tk.call("image", "width", name)) == int(root.tk.call("image", "height", name)) == 20
    labels = [w for w in walk(root) if isinstance(w, tk.Label)]
    with_image = [w for w in labels if str(w.cget("image"))]
    assert len(with_image) == 1
    header = with_image[0].master
    assert "Clawd Tank" in [w.cget("text") for w in header.winfo_children() if isinstance(w, tk.Label)]
    canvases = [w for w in walk(root) if isinstance(w, tk.Canvas)]
    assert canvases == [win._dot] and win._dot.master is header
    assert [win._dot.type(i) for i in win._dot.find_all()] == ["oval"]    # the dot is drawn, not an image


def test_the_mark_is_the_still_idle_clawd_of_the_tray(root, win):
    expected = IconSet().icon("idle", 20)
    assert closest_size(20) == 20 and closest_size(25) == 24 and closest_size(30) == 32
    name = [n for n in root.tk.call("image", "names") if str(n).startswith("pyimage")][-1]
    assert int(root.tk.call("image", "width", name)) == expected.width
    assert not hasattr(win, "set_frame") and not hasattr(win, "caption")


def test_the_mark_grows_to_the_nearest_built_size_on_a_scaled_screen(root, make):
    before = set(root.tk.call("image", "names"))
    make(scale=1.5)
    (name,) = set(root.tk.call("image", "names")) - before
    assert int(root.tk.call("image", "width", name)) == 32


def test_the_status_dot_follows_the_tray_icon_state(root, win):
    green, grey = DOT_COLOURS["idle"], DOT_OTHER

    def fill():
        return win._dot.itemcget(win._dot_item, "fill")

    assert fill() == grey                                   # nothing known yet
    v = ViewState()
    v.apply("status", {"status": "running"})                # scanning for the bar
    win.render(v, "Installed", "")
    assert fill() == grey
    v.apply("connection", {"connected": True})              # connected
    win.render(v, "Installed", "")
    assert fill() == green
    v.apply("display", {"state": {"anims": ["alert"], "ids": [0]}})   # Claude needs you: still green
    win.render(v, "Installed", "")
    assert fill() == green
    v.apply("connection", {"connected": False})             # the link dropped
    win.render(v, "Installed", "")
    assert fill() == grey
    v.apply("status", {"status": "conflict", "message": "x"})   # error
    win.render(v, "Installed", "")
    assert fill() == DOT_COLOURS["error"] == RED


# --- the status lines --------------------------------------------------------------------------------------------


def test_render_sets_the_status_lines(root, win):
    v = running()
    v.apply("display", {"state": {"anims": ["thinking", "idle"], "ids": [0, 1]}})
    v.apply("hook", {"msg": {"event": "tool_use", "tool_name": "Bash"}})
    win.render(v, "Installed", "")
    assert win._lines["bar"].cget("text") == "Connected"
    assert win._lines["hooks"].cget("text") == "Installed"
    assert win._lines["claude"].cget("text") == "2 active sessions"
    assert win._lines["event"].cget("text").startswith("tool_use Bash, ")
    assert win._problem.cget("text") == ""
    names = [w.cget("text") for w in walk(root) if isinstance(w, tk.Label) and w.grid_info().get("column") == 0]
    assert names == ["Bar", "Hooks", "Claude Code", "Last event"]


def test_render_touches_only_what_changed(root, win, monkeypatch):
    v = running()
    win.render(v, "Installed", "")
    touched = []
    for key, label in win._lines.items():
        orig = label.configure
        monkeypatch.setattr(label, "configure",
                            lambda *a, _o=orig, _k=key, **k: (touched.append(_k), _o(*a, **k))[1])
    dot_touched = []
    orig_dot = win._dot.itemconfigure
    monkeypatch.setattr(win._dot, "itemconfigure",
                        lambda *a, **k: (dot_touched.append(a), orig_dot(*a, **k))[1])
    win.render(v, "Installed", "")
    assert touched == [] and dot_touched == []   # same state: no widget work
    v.apply("display", {"state": {"anims": ["typing"], "ids": [0]}})
    win.render(v, "Installed", "")
    assert touched == ["claude"] and dot_touched == []   # Claude's activity is text only
    touched.clear()
    win.render(v, "Not installed", "")
    assert touched == ["hooks"]


def test_a_hook_warning_such_as_a_missing_py_launcher_is_shown_in_red(root, win):
    win.render(running(), "Installed", "The py launcher was not found, so the hooks cannot run.")
    assert "py launcher" in win._problem.cget("text")
    assert win._problem_box.winfo_manager() == "pack"
    assert not win.retry_row.shown                           # nothing to retry


# --- the port-in-use message and its Retry row -----------------------------------------------------------------


def test_a_port_conflict_shows_the_red_message_and_a_retry_row_under_the_status(root, win, calls):
    open_flyout(root, win)
    v = running()
    win.render(v, "Installed", "")
    assert not win.retry_row.shown and not win._problem_shown
    height = root.winfo_height()
    win.render(conflict_view(), "Installed", "")
    root.update()
    assert win._problem_shown and win.retry_row.shown
    assert win._problem.cget("text").startswith(CONFLICT_MESSAGE)
    assert str(win._problem.cget("foreground")) == th.DARK.critical
    assert win._lines["bar"].cget("text") == "Disconnected"
    assert root.winfo_height() > height                      # room was made for it
    # under the status lines and above the separator and the other rows
    ys = {k: w.winfo_rooty() for k, w in (("bar", win._lines["bar"]), ("msg", win._problem),
                                           ("retry", win.retry_row.frame),
                                           ("install", win.rows["install"].frame))}
    assert ys["bar"] < ys["msg"] < ys["retry"] < ys["install"]
    win.retry_row.invoke()
    assert calls == ["retry"]
    assert win.visible                                       # Retry stays open to show the result
    win.render(running(), "Installed", "")                   # the other daemon went away
    root.update()
    assert not win.retry_row.shown and not win._problem_shown
    assert root.winfo_height() == height


def test_everything_fits_inside_the_window_even_with_a_red_message_and_retry(root, win):
    win.render(conflict_view(), "Not installed", "")
    open_flyout(root, win)
    height, width = root.winfo_height(), root.winfo_width()
    widgets = [win._problem, win._dot, win.retry_row.frame] + [r.frame for r in win.rows.values()]
    for widget in widgets:
        assert widget.winfo_ismapped(), widget
        x, y = widget.winfo_rootx() - root.winfo_rootx(), widget.winfo_rooty() - root.winfo_rooty()
        assert 0 <= x and x + widget.winfo_width() <= width, widget
        assert 0 <= y and y + widget.winfo_height() <= height, widget


def test_a_long_last_event_wraps_instead_of_widening_the_flyout(root, win):
    v = running()
    v.apply("hook", {"msg": {"hook": "PermissionRequest", "tool_name": "mcp__very_long_tool_name_here",
                             "project": "a-project-with-a-really-long-name"}})
    win.render(v, "Installed", "")
    open_flyout(root, win)
    assert root.winfo_width() == 300
    assert win._lines["event"].winfo_width() + win._lines["event"].winfo_x() <= 300


# --- menu rows --------------------------------------------------------------------------------------------------


def test_the_rows_are_in_the_documented_order_with_separators_above_them_and_above_quit(root, win):
    open_flyout(root, win)
    by_position = sorted(win.rows.values(), key=lambda r: r.frame.winfo_rooty())
    assert [r.label.cget("text") for r in by_position] == [
        "Install or repair hooks", "Remove hooks", "Open log", "Start with Windows", "Quit"]
    assert [spec[1] for spec in ACTION_ROWS] == [r.label.cget("text") for r in by_position[:4]]
    last_line = win._lines["event"].winfo_rooty()
    assert last_line < win._sep.winfo_rooty() < win.rows["install"].frame.winfo_rooty()
    assert win.rows["toggle_autostart"].frame.winfo_rooty() < win._sep_quit.winfo_rooty()         < win.rows["quit"].frame.winfo_rooty()
    assert win._sep.winfo_height() == win._sep_quit.winfo_height() == 1


def test_every_row_does_its_action(root, win, calls):
    open_flyout(root, win)
    for key in ("install", "remove", "open_log", "toggle_autostart", "quit"):
        win.show()
        win.rows[key].invoke()
        assert calls[-1] == key
    assert calls == ["install", "remove", "open_log", "toggle_autostart", "quit"]


def test_rows_that_open_something_close_the_flyout_first_the_others_stay(root):
    seen = {}
    win = None

    def action(name):
        return lambda: seen.update({name: win.visible})

    actions = {n: action(n) for n in ACTIONS}
    win = FlyoutWindow(root, IconSet(), actions, 1.0, theme_source=lambda: th.DARK,
                       placer=far_away, animate=False, opacity=0.0, take_focus=False)
    win.render(conflict_view(), "Installed", "")
    for key in ("install", "remove", "open_log", "quit", "toggle_autostart"):
        win.show()
        win.rows[key].invoke()
    win.show()
    win.retry_row.invoke()
    assert seen == {"install": False, "remove": False, "open_log": False, "quit": False,
                    "toggle_autostart": True, "retry": True}


def test_start_with_windows_shows_a_check_mark_when_it_is_on_and_reads_it_each_time(root, make, calls):
    state = {"on": False}

    def flip():
        calls.append("toggle_autostart")
        state["on"] = not state["on"]

    win = make(autostart=lambda: state["on"])
    win.actions["toggle_autostart"] = flip
    row = win.rows["toggle_autostart"]
    open_flyout(root, win)
    assert not row.checked and not row.check.winfo_ismapped()
    row.invoke()                                             # clicked: on, and the mark appears at once
    root.update()
    assert state["on"] and row.checked and row.check.winfo_ismapped()
    assert row.check.cget("text") in ("", "✓")
    assert win.visible
    win.hide("test")
    state["on"] = False                                      # changed behind its back (the tray menu)
    win.show()
    assert not row.checked                                   # read again as it opened
    state["on"] = True
    win.hide("test")
    win.show()
    assert row.checked


def test_the_rows_are_menu_rows_with_a_hand_cursor_not_buttons(root, win):
    open_flyout(root, win)
    for row in win.rows.values():
        assert isinstance(row.frame, tk.Frame) and not isinstance(row.frame, (tk.Button, ttk.Button))
        for w in row.widgets:
            assert str(w.cget("cursor")) == "hand2"
        assert row.frame.winfo_width() >= 300 - 2 * 4 - 2     # full width, less the inset
        assert row.frame.winfo_height() == 36


def test_a_row_is_highlighted_under_the_pointer_and_not_otherwise(root, win):
    open_flyout(root, win)
    row, other = win.rows["open_log"], win.rows["quit"]
    assert str(row.frame.cget("bg")) == th.DARK.background
    row.label.event_generate("<Enter>")
    assert str(row.frame.cget("bg")) == str(row.label.cget("bg")) == th.DARK.hover
    other.frame.event_generate("<Enter>")                    # only one row at a time
    assert str(row.frame.cget("bg")) == th.DARK.background and str(other.frame.cget("bg")) == th.DARK.hover
    gone = types.SimpleNamespace(x_root=-1, y_root=-1)       # the pointer is nowhere over the flyout
    other._leave(gone)
    assert str(other.frame.cget("bg")) == th.DARK.background and win._current is None


def test_moving_from_a_row_onto_its_own_text_does_not_lose_the_highlight(root, win, monkeypatch):
    open_flyout(root, win)
    row, other = win.rows["install"], win.rows["remove"]
    row.frame.event_generate("<Enter>")
    # Tk sends Leave to the row's frame when the pointer moves onto the row's own label: the
    # widget under the pointer is what decides (the window is off-screen here, so say which)
    under = {"w": row.label}
    monkeypatch.setattr(win, "widget_at", lambda x, y: under["w"])
    pointer = types.SimpleNamespace(x_root=1, y_root=1)
    row._leave(pointer)
    assert row.state == "hover" and win._current is row
    under["w"] = other.label                                 # now it really has moved to another row
    row._leave(pointer)
    assert row.state == "normal" and win._current is None
    under["w"] = None                                        # or off the flyout
    other.frame.event_generate("<Enter>")
    other._leave(pointer)
    assert other.state == "normal"


def test_a_click_runs_on_release_inside_the_row_only(root, win, calls):
    open_flyout(root, win)
    row = win.rows["open_log"]
    row.label.event_generate("<ButtonPress-1>", x=5, y=5)
    assert row.state == "pressed" and str(row.frame.cget("bg")) == th.DARK.pressed
    row.label.event_generate("<ButtonRelease-1>", x=-30, y=5)   # dragged off before letting go
    assert calls == [] and row.state == "normal"
    win.show()
    row.label.event_generate("<ButtonPress-1>", x=5, y=5)
    row.label.event_generate("<ButtonRelease-1>", x=5, y=5)
    assert calls == ["open_log"]


def test_the_arrow_keys_and_enter_work_the_rows(root, win, calls):
    win.render(conflict_view(), "Installed", "")
    open_flyout(root, win)
    win._move(1)
    assert win._current is win.retry_row                    # the first row on screen
    win._move(1)
    assert win._current is win.rows["install"]
    win._move(-1)
    win._move(-1)
    assert win._current is win.rows["quit"]                  # wraps round
    win._on_enter_key()
    assert calls == ["quit"] and not win.visible
    win.show()
    win._move(1)
    assert win._current is win.retry_row
    win.render(running(), "Installed", "")                   # the message goes: Retry leaves the order
    assert not win.retry_row.shown and win._current is None


# --- theme ---------------------------------------------------------------------------------------------------------


def test_the_theme_is_read_again_each_time_it_opens(root, make):
    current = [th.DARK]
    reads = []

    def source():
        reads.append(current[0].name)
        return current[0]

    win = make(theme_source=source)
    reads.clear()
    win.show()
    assert reads == ["dark"] and str(root.cget("bg")) == th.DARK.border
    assert str(win._body.cget("bg")) == th.DARK.background
    win.hide("test")
    current[0] = th.LIGHT                                    # the user switched Windows to light mode
    win.show()
    assert reads == ["dark", "light"]
    assert str(root.cget("bg")) == th.LIGHT.border and str(win._body.cget("bg")) == th.LIGHT.background
    assert win.theme is th.LIGHT


@pytest.mark.parametrize("theme", [th.DARK, th.LIGHT], ids=lambda t: t.name)
def test_every_part_of_the_flyout_wears_the_theme(root, make, theme):
    win = make(theme_source=lambda: theme)
    win.render(conflict_view(), "Not installed", "")         # the problem box and Retry too
    open_flyout(root, win)
    allowed = {theme.background, theme.border, theme.hover, theme.pressed}
    for w in walk(root):
        if isinstance(w, (tk.Frame, tk.Label, tk.Canvas)):
            assert str(w.cget("bg")) in allowed, f"{w} is {w.cget('bg')}"
        if isinstance(w, tk.Label) and not str(w.cget("image")):
            assert str(w.cget("fg")) in {theme.text, theme.secondary, theme.critical}, w
    assert str(win._problem.cget("fg")) == theme.critical
    heading = next(w for w in walk(root) if isinstance(w, tk.Label) and w.cget("text") == "Hooks")
    assert str(heading.cget("fg")) == theme.secondary


def test_the_window_asks_windows_for_its_theme_by_default(root, calls, monkeypatch):
    seen = []
    monkeypatch.setattr(th, "read_apps_use_light_theme", lambda: seen.append("read") or 0)
    actions = {n: (lambda: None) for n in ACTIONS}
    win = FlyoutWindow(root, IconSet(), actions, 1.0, placer=far_away, animate=False, opacity=0.0,
                       take_focus=False)
    assert seen and win.theme is th.DARK


# --- placement -------------------------------------------------------------------------------------------------------


def test_it_is_put_where_the_placer_says(root, make):
    asked = []

    def placer(size, scale):
        asked.append((size, scale))
        return far_away(size, scale)

    win = make(placer=placer, scale=1.0)
    open_flyout(root, win)
    (size, scale), = asked
    assert size == (300, root.winfo_height()) and scale == 1.0
    assert (root.winfo_x(), root.winfo_y()) == (win.last_placement.x, win.last_placement.y)
    assert win.last_placement.edge == "bottom"
    assert (root.winfo_x(), root.winfo_y()) == winutil.window_rect(win.hwnd)[:2]


def test_a_taller_flyout_grows_upwards_from_the_taskbar_and_keeps_its_bottom_edge(root, win):
    win.render(running(), "Installed", "")
    open_flyout(root, win)
    bottom, height = root.winfo_y() + root.winfo_height(), root.winfo_height()
    win.render(conflict_view(), "Installed", "")             # the message and Retry make it taller
    root.update()
    assert root.winfo_height() > height
    assert root.winfo_y() + root.winfo_height() == bottom
    win.render(running(), "Installed", "")
    root.update()
    assert root.winfo_y() + root.winfo_height() == bottom and root.winfo_height() == height


def test_rendering_while_hidden_places_nothing(root, make):
    asked = []
    win = make(placer=lambda size, scale: asked.append(size) or far_away(size, scale))
    win.render(conflict_view(), "Installed", "")
    assert asked == []


# --- opening and closing: toggle, focus, Escape ------------------------------------------------------------------------


def test_a_tray_click_opens_it_and_the_next_one_closes_it(root, win, clock):
    assert not win.visible and root.state() == "withdrawn"
    assert win.toggle(clock()) == fl.SHOW
    root.update()
    assert win.visible and root.state() == "normal"
    clock.advance(2.0)
    assert win.toggle(clock()) == fl.HIDE
    assert not win.visible and root.state() == "withdrawn"
    clock.advance(2.0)
    assert win.toggle(clock()) == fl.SHOW and win.visible


def test_the_click_time_is_what_counts_not_when_the_ui_gets_to_it(root, win, clock):
    clicked = clock()
    clock.advance(0.09)                                      # the pump ran 90 ms later
    win.toggle(clicked)                                      # opens
    clock.advance(1.0)
    assert win.toggle(clock()) == fl.HIDE
    clicked = clock()
    clock.advance(0.09)
    win.toggle(clicked)                                      # opens again
    clock.advance(0.2)
    second_click = clicked + 0.2                             # a bounce 200 ms after the first click
    assert win.toggle(second_click) == fl.IGNORE and win.visible


def test_show_opens_it_and_a_second_show_only_brings_it_forward(root, win):
    win.show()
    root.update()
    opened = win.guard.opened_at
    win.show()
    assert win.visible and win.guard.opened_at == opened     # not reopened


def test_losing_focus_closes_it(root, win, clock):
    open_flyout(root, win)
    clock.advance(1.0)
    root.event_generate("<FocusOut>")
    assert not win.visible and root.state() == "withdrawn"
    assert win.guard.closed_by == "focus"


def test_focus_events_of_the_parts_inside_do_not_close_it(root, win, clock):
    open_flyout(root, win)
    clock.advance(1.0)
    win.rows["install"].frame.event_generate("<FocusOut>")
    assert win.visible


def test_the_tray_click_that_closed_it_by_taking_focus_does_not_reopen_it(root, win, clock):
    win.toggle(clock())
    clock.advance(2.0)
    root.event_generate("<FocusOut>")                        # mouse down on the tray icon
    assert not win.visible
    clock.advance(0.12)
    assert win.toggle(clock()) == fl.IGNORE and not win.visible     # mouse up on the icon
    clock.advance(1.0)
    assert win.toggle(clock()) == fl.SHOW and win.visible    # a new click opens it


def test_a_focus_loss_in_the_first_moments_waits_and_is_applied_if_focus_does_not_return(root, win, clock):
    open_flyout(root, win)
    clock.advance(0.05)
    root.event_generate("<FocusOut>")
    assert win.visible and win._settle_job is not None       # one look-again timer, not a decision
    clock.advance(0.3)
    win._settle()                                            # no focus anywhere in this test: close
    assert not win.visible and win._settle_job is None


def test_a_focus_loss_in_the_first_moments_is_forgotten_if_focus_comes_back(root, win, clock):
    open_flyout(root, win)
    clock.advance(0.05)
    root.event_generate("<FocusOut>")
    root.event_generate("<FocusIn>")
    clock.advance(0.3)
    win._settle()
    assert win.visible


def test_escape_closes_it(root, win, clock):
    open_flyout(root, win)
    root.event_generate("<Escape>")
    if win.visible:                                          # Tk drops key events for a window without focus
        win._on_escape()
    assert not win.visible and root.state() == "withdrawn"
    assert win.guard.closed_by == "escape"
    win._on_escape()                                         # when it is closed, Escape does nothing
    assert not win.visible


def test_escape_is_bound_on_the_window(root, win):
    assert root.bind("<Escape>")


def test_alt_f4_hides_it_and_does_not_quit(root, win, calls):
    open_flyout(root, win)
    root.tk.call(root.protocol("WM_DELETE_WINDOW"))
    assert not win.visible and calls == []


def test_with_no_tray_icon_nothing_but_quit_closes_it(root, win, clock):
    win.autohide = False
    open_flyout(root, win)
    clock.advance(1.0)
    root.event_generate("<FocusOut>")
    win._on_escape()
    assert win.visible
    win.rows["quit"].invoke()
    assert not win.visible


def test_hiding_cancels_the_look_again_timer(root, win, clock):
    open_flyout(root, win)
    clock.advance(0.05)
    root.event_generate("<FocusOut>")
    assert win._settle_job is not None
    win.hide("test")
    assert win._settle_job is None and pending(root) == set()


def test_the_state_the_window_reports_is_the_guards(root, win):
    assert win.visible is win.guard.visible is False
    win.show()
    assert win.visible is win.guard.visible is True


def test_before_show_runs_as_it_opens_not_while_it_is_open(root, make):
    order = []
    win = make(before_show=lambda: order.append(("before", win.visible)))
    win.show()
    win.show()
    assert order == [("before", False)]


# --- no timer, ever, while it is hidden or still ----------------------------------------------------------------------


def test_the_window_schedules_no_timer_while_rendering(root, make, monkeypatch):
    pending_before = pending(root)
    scheduled = []
    real_after = root.after
    monkeypatch.setattr(root, "after", lambda *a, **k: scheduled.append(a) or real_after(*a, **k))
    win = make()
    v = running()
    for step in range(5):
        win.render(v, "Installed", "")
        v.apply("display", {"state": {"anims": ["thinking", "typing"][: step % 3], "ids": [0, 1]}})
        v.apply("hook", {"msg": {"event": "tool_use", "tool_name": "Bash"}}, now=1_700_000_000.0 + step)
    v.apply("connection", {"connected": False})
    win.render(v, "Installed", "")
    win.render(conflict_view(), "Installed", "")
    root.update()
    assert scheduled == [], f"after() was called: {scheduled}"
    assert pending(root) == pending_before, "a timer is pending in Tk"


def test_opening_without_animation_schedules_nothing(root, win, clock):
    before = pending(root)
    open_flyout(root, win)
    win.render(conflict_view(), "Installed", "")
    win.render(running(), "Installed", "")
    root.update()
    assert pending(root) == before                           # no animation here: nothing scheduled
    win.hide("test")
    assert pending(root) == before


def test_the_opening_animation_is_one_short_chain_that_ends_by_itself(root, make, monkeypatch):
    win = make(animate=True, opacity=0.01)
    scheduled = []
    real_after = root.after
    monkeypatch.setattr(root, "after", lambda ms, func=None, *a: scheduled.append((ms, func.__name__)) or
                        real_after(ms, func, *a))
    start = time.monotonic()
    win.show()
    assert pump(root, lambda: not pending(root))
    took = time.monotonic() - start
    assert [name for _, name in scheduled] == ["_anim_step"] * (ANIM_STEPS - 1)   # the first frame is direct
    assert all(ms == 16 for ms, _ in scheduled)
    assert took < 0.5                                        # ~120 ms
    assert (root.winfo_x(), root.winfo_y()) == (win.last_placement.x, win.last_placement.y)   # ended in place
    root.update()
    assert pending(root) == set()                            # and nothing is left running


def test_the_animation_starts_nearer_the_taskbar_and_the_flyout_slides_up(root, make):
    win = make(animate=True, opacity=0.01)
    win.show()
    root.update_idletasks()
    first = (root.winfo_x(), root.winfo_y())
    final = (win.last_placement.x, win.last_placement.y)
    assert first[0] == final[0] and 0 < first[1] - final[1] <= 12      # one frame in: still a little low
    assert pump(root, lambda: not pending(root))
    assert (root.winfo_x(), root.winfo_y()) == final


def test_hiding_in_the_middle_of_the_animation_stops_it(root, make):
    win = make(animate=True, opacity=0.01)
    win.show()
    assert pending(root)
    win.hide("test")
    assert pending(root) == set() and root.state() == "withdrawn"


def test_no_animation_when_windows_has_animation_effects_off(root, make, monkeypatch):
    monkeypatch.setattr(winutil, "animations_enabled", lambda: False)
    win = make(animate=None, opacity=0.01)
    win.show()
    assert pending(root) == set()


def test_animations_follow_windows_when_not_forced(root, make, monkeypatch):
    monkeypatch.setattr(winutil, "animations_enabled", lambda: True)
    win = make(animate=None, opacity=0.01)
    win.show()
    assert pending(root)
    win.hide("test")


# --- the invisible-window setup of these tests is itself invisible ------------------------------------------------------


def test_the_test_windows_really_are_transparent_and_off_screen(root, win):
    open_flyout(root, win)
    _style, ex = winutil.window_styles(win.hwnd)
    assert ex & winutil.WS_EX_LAYERED                        # a layered window at alpha 0
    rect = winutil.window_rect(win.hwnd)
    assert rect.right < 0 and rect.bottom < 0
