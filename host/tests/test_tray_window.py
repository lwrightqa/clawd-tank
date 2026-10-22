"""The tkinter window: minimise/close hide it to the tray, the lines and the sprite.

The windows these tests make are fully transparent (alpha 0), so nothing shows on
the desktop while they run."""

import time
import tkinter as tk
from tkinter import ttk

import pytest

from clawd_tank_tray.icons import IconSet
from clawd_tank_tray.state import CONFLICT_MESSAGE, ViewState
from clawd_tank_tray.window import SPRITE_PX, ZOOM, MainWindow


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


def running(**kw):
    v = ViewState(**kw)
    v.apply("status", {"status": "running"})
    v.apply("connection", {"connected": True})
    return v


def test_title_size_and_buttons(root, win, calls):
    root.update()
    assert root.title() == "Clawd Tank"
    assert root.geometry().startswith("360x300")
    assert root.resizable() == (False, False) or root.resizable() == (0, 0)
    btns = buttons(root)
    assert {"Install or repair hooks", "Open log", "Hide to tray", "Quit", "Retry"} <= set(btns)
    for text, action in (("Install or repair hooks", "install"), ("Open log", "open_log"),
                         ("Hide to tray", "hide"), ("Quit", "quit")):
        btns[text].invoke()
        assert calls[-1] == action


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


def test_render_sets_the_status_lines(root, win):
    v = running()
    v.apply("display", {"state": {"anims": ["thinking", "idle"], "ids": [0, 1]}})
    v.apply("hook", {"msg": {"event": "tool_use", "tool_name": "Bash"}})
    win.render(v, "Installed", "")
    assert win._lines["bar"].cget("text") == "Connected"
    assert win._lines["hooks"].cget("text") == "Installed"
    assert win._lines["claude"].cget("text") == "2 active sessions"
    assert win._detail.cget("text").startswith("Last event: tool_use Bash, ")
    assert win.caption.cget("text") == "Thinking"
    assert win._problem.cget("text") == ""


def test_render_touches_only_what_changed(root, win, monkeypatch):
    v = running()
    win.render(v, "Installed", "")
    touched = []
    for key, label in win._lines.items():
        orig = label.configure
        monkeypatch.setattr(label, "configure", lambda *a, _o=orig, _k=key, **k: (touched.append(_k), _o(*a, **k))[1])
    win.render(v, "Installed", "")
    assert touched == []  # same state: no widget work
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


def test_the_sprite_is_a_3x_enlargement_and_cached(root, win):
    win.set_frame("thinking", 2)
    photo = win._photos[("thinking", 2)]
    assert (photo.width(), photo.height()) == (SPRITE_PX * ZOOM, SPRITE_PX * ZOOM) == (144, 144)
    assert str(win.image.cget("image")[0]) == str(photo)  # and it is the one on the label
    assert len(win._photos) == 1
    win.set_frame("thinking", 2)  # the same frame again: nothing to do
    assert len(win._photos) == 1
    win.set_frame("thinking", 3)
    assert len(win._photos) == 2
    win.set_frame("thinking", 3 + IconSet().frame_count("thinking"))  # wraps to a cached frame
    assert len(win._photos) == 2


def test_the_sprite_is_crisp_each_source_pixel_becomes_a_3x3_block(root, win):
    icons = IconSet()
    win.set_frame("idle", 0)
    photo = win._photos[("idle", 0)]
    src = icons.frame("idle", 0, SPRITE_PX)
    # find an opaque pixel and compare the 3x3 block at its position
    for y in range(SPRITE_PX):
        for x in range(SPRITE_PX):
            if src.getpixel((x, y))[3] == 255:
                r, g, b, _ = src.getpixel((x, y))
                want = "#%02x%02x%02x" % (r, g, b)
                for dy in range(ZOOM):
                    for dx in range(ZOOM):
                        got = photo.get(x * ZOOM + dx, y * ZOOM + dy)
                        assert tuple(got[:3]) == (r, g, b), (x, y, dx, dy, want)
                return
    pytest.fail("no opaque pixel in the idle frame")


def test_scaled_window_for_high_dpi(root, calls):
    actions = {n: (lambda: None) for n in ("install", "open_log", "hide", "quit", "retry")}
    MainWindow(root, IconSet(), actions, scale=1.5)
    root.update()
    assert root.geometry().startswith("540x450")
