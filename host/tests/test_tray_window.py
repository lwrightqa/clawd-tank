"""The tkinter window: minimise/close hide it to the tray; plain text lines and a status dot.

The windows these tests make are fully transparent (alpha 0), so nothing shows on
the desktop while they run."""

import time
import tkinter as tk
from tkinter import ttk

import pytest

from clawd_tank_tray.icons import IconSet
from clawd_tank_tray.state import CONFLICT_MESSAGE, ViewState
from clawd_tank_tray.window import BASE_WIDTH, DOT_COLOURS, DOT_OTHER, RED, MainWindow


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
    r.attributes("-alpha", 0.0)
    yield r
    r.destroy()


@pytest.fixture
def calls():
    return []


@pytest.fixture
def win(root, calls):
    actions = {name: (lambda n=name: calls.append(n))
               for name in ("install", "open_log", "hide", "quit", "retry")}
    return MainWindow(root, IconSet(), actions, scale=1.0)


def pump(root, until, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if until():
            return True
        time.sleep(0.02)
    return until()


def buttons(widget):
    out = {}
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button):
            out[child.cget("text")] = child
        out.update(buttons(child))
    return out


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def running(**kw):
    v = ViewState(**kw)
    v.apply("status", {"status": "running"})
    v.apply("connection", {"connected": True})
    return v


# --- size and buttons -----------------------------------------------------------------------


def test_title_size_and_buttons(root, win, calls):
    win.show()
    root.update()
    assert root.title() == "Clawd Tank"
    assert root.geometry().startswith(f"{BASE_WIDTH}x")
    assert root.resizable() == (False, False) or root.resizable() == (0, 0)
    btns = buttons(root)
    assert {"Install or repair hooks", "Open log", "Hide to tray", "Quit", "Retry"} <= set(btns)
    for text, action in (("Install or repair hooks", "install"), ("Open log", "open_log"),
                         ("Hide to tray", "hide"), ("Quit", "quit")):
        btns[text].invoke()
        assert calls[-1] == action


def test_the_window_is_sized_to_its_content_with_no_empty_column(root, win):
    win.show()
    root.update()
    assert root.winfo_height() == win._outer.winfo_reqheight()   # as tall as its content needs
    assert root.winfo_height() < 300 and root.winfo_width() == BASE_WIDTH
    # the first label starts at the left edge: nothing is reserved for a picture there
    heading = next(w for w in walk(root) if isinstance(w, ttk.Label) and w.cget("text") == "Bar")
    assert heading.winfo_rootx() - root.winfo_rootx() < 30


def test_everything_fits_inside_the_window_even_with_a_red_message_and_retry(root, win):
    v = running()
    v.apply("status", {"status": "conflict", "message": CONFLICT_MESSAGE + " (port 47421 is in use)"})
    win.render(v, "Not installed", "")
    win.show()
    root.update()
    height, width = root.winfo_height(), root.winfo_width()
    for widget in list(buttons(root).values()) + [win._problem, win._dot]:
        assert widget.winfo_ismapped(), widget
        x, y = widget.winfo_rootx() - root.winfo_rootx(), widget.winfo_rooty() - root.winfo_rooty()
        assert 0 <= x and x + widget.winfo_width() <= width, widget
        assert 0 <= y and y + widget.winfo_height() <= height, widget
    assert root.winfo_height() == height  # the message appearing did not resize it


# --- no picture, no timer ---------------------------------------------------------------------


def test_the_window_has_no_picture_at_all(root, calls):
    images_before = set(root.tk.call("image", "names"))   # Tk's own message-box icons
    actions = {n: (lambda: None) for n in ("install", "open_log", "hide", "quit", "retry")}
    win = MainWindow(root, IconSet(), actions, scale=1.0)
    win.render(running(), "Installed", "")
    win.show()
    root.update()
    assert set(root.tk.call("image", "names")) == images_before, "the window made a tk image"
    canvases = [w for w in walk(root) if isinstance(w, tk.Canvas)]
    assert canvases == [win._dot], "the only graphic is the status dot"
    assert [win._dot.type(i) for i in win._dot.find_all()] == ["oval"]   # drawn, not an image
    for w in walk(root):
        if isinstance(w, (tk.Label, ttk.Label)):
            assert not str(w.cget("image")), f"{w} shows an image"
    assert not any(isinstance(v, tk.PhotoImage) for v in vars(win).values())
    assert not hasattr(win, "image") and not hasattr(win, "set_frame")


def test_the_window_schedules_no_timer_while_rendering(root, monkeypatch):
    pending_before = set(root.tk.call("after", "info"))
    scheduled = []
    real_after = root.after
    monkeypatch.setattr(root, "after", lambda *a, **k: scheduled.append(a) or real_after(*a, **k))
    actions = {n: (lambda: None) for n in ("install", "open_log", "hide", "quit", "retry")}
    win = MainWindow(root, IconSet(), actions, scale=1.0)
    v = running()
    for step in range(5):
        win.render(v, "Installed", "")
        v.apply("display", {"state": {"anims": ["thinking", "typing"][: step % 3], "ids": [0, 1]}})
        v.apply("hook", {"msg": {"event": "tool_use", "tool_name": "Bash"}}, now=1_700_000_000.0 + step)
    v.apply("connection", {"connected": False})
    win.render(v, "Installed", "")
    v.apply("status", {"status": "conflict", "message": "x"})
    win.render(v, "Installed", "")
    root.update()
    assert scheduled == [], f"after() was called: {scheduled}"
    assert set(root.tk.call("after", "info")) == pending_before, "a timer is pending in Tk"


def test_show_uses_one_single_shot_after_and_nothing_repeats(root, win, monkeypatch):
    scheduled = []
    real_after = root.after
    monkeypatch.setattr(root, "after", lambda *a, **k: scheduled.append(a[0]) or real_after(*a, **k))
    pending_before = set(root.tk.call("after", "info"))
    win.show()
    assert scheduled == [300]   # un-pins the window from the top once; no repeating timer
    assert pump(root, lambda: set(root.tk.call("after", "info")) == pending_before)


# --- hiding to the tray -----------------------------------------------------------------------


def test_the_close_button_hides_to_the_tray_and_does_not_quit(root, win, calls):
    win.show()
    root.update()
    assert root.state() == "normal"
    command = root.protocol("WM_DELETE_WINDOW")
    assert command, "the X button must be handled"
    root.tk.call(command)  # what the X button does
    root.update()
    assert root.state() == "withdrawn"
    assert calls == []  # nothing quit, nothing asked


def test_minimising_hides_to_the_tray_with_no_taskbar_button(root, win):
    win.show()
    root.update()
    root.iconify()
    assert pump(root, lambda: root.state() == "withdrawn"), root.state()
    # withdrawn = no taskbar button (an iconified window would keep one)
    assert not win.visible


def test_show_brings_it_back_from_withdrawn_and_from_minimised(root, win):
    win.hide()
    root.update()
    assert root.state() == "withdrawn"
    win.show()
    assert pump(root, lambda: root.state() == "normal")
    win.hide()
    win.show()
    assert pump(root, lambda: root.state() == "normal" and win.visible)


# --- the lines and the dot --------------------------------------------------------------------


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
    assert not hasattr(win, "caption")


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


def test_a_port_conflict_shows_the_message_in_red_and_a_retry_button(root, win, calls):
    v = running()
    win.render(v, "Installed", "")
    assert not win._retry_shown
    v.apply("status", {"status": "conflict", "message": CONFLICT_MESSAGE + " (port 47421 is in use)"})
    win.render(v, "Installed", "")
    assert win._retry_shown and win._retry.winfo_manager() == "pack"
    assert win._problem.cget("text").startswith(CONFLICT_MESSAGE)
    assert str(win._problem.cget("foreground")) == "#b3261e"
    assert win._lines["bar"].cget("text") == "Disconnected"
    root.update()
    win._retry.invoke()
    assert calls == ["retry"]
    v.apply("status", {"status": "running"})
    win.render(v, "Installed", "")
    assert not win._retry_shown and win._retry.winfo_manager() == ""


def test_a_hook_warning_such_as_a_missing_py_launcher_is_shown(root, win):
    win.render(running(), "Installed", "The py launcher was not found, so the hooks cannot run.")
    assert "py launcher" in win._problem.cget("text")


def test_scaled_window_for_high_dpi(root, calls):
    actions = {n: (lambda: None) for n in ("install", "open_log", "hide", "quit", "retry")}
    win = MainWindow(root, IconSet(), actions, scale=1.5)
    win.show()
    root.update()
    assert root.geometry().startswith(f"{round(BASE_WIDTH * 1.5)}x")
