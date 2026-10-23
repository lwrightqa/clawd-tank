"""TrayApp's decisions without a window: the hooks consent flow, Start with
Windows, the tray menu, how engine events reach the state, what a left click on the
tray icon and the other messages do to the flyout, and that the tray icon is a still
picture that changes with the connection and nothing else. A fake stands in for the
dialogs; settings.json, the notify script, the registry key and the state file are all
temporary."""

import json
import os
import queue
import uuid
import winreg
from datetime import datetime

import pytest

from clawd_tank_menubar import hooks
from clawd_tank_tray.app import TrayApp
from clawd_tank_tray.autostart import Autostart
from clawd_tank_tray.config import AppConfig
from clawd_tank_tray.hookctl import HookManager
from clawd_tank_tray.options import Options
from clawd_tank_tray.state import ICON_NAMES, EngineStatus


class FakeDialogs:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def ask_yes_no(self, title, text):
        self.calls.append(("ask", title, text))
        return self.answers.pop(0) if self.answers else False

    def info(self, title, text):
        self.calls.append(("info", title, text))

    def error(self, title, text):
        self.calls.append(("error", title, text))

    def kinds(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def run_key():
    path = rf"Software\ClawdTankTest\{uuid.uuid4().hex}\Run"
    yield path
    for p in (path, path.rsplit("\\", 1)[0], r"Software\ClawdTankTest"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, p)
        except OSError:
            pass


@pytest.fixture
def ctx(tmp_path, monkeypatch, run_key):
    monkeypatch.setattr(hooks, "NOTIFY_SCRIPT_PATH", tmp_path / "home" / ".clawd-tank" / "n.py")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    settings = tmp_path / "claude" / "settings.json"
    mgr = HookManager(settings, now=lambda: datetime(2026, 10, 21, 9, 0, 0),
                      which=lambda n: "py.exe")
    app = TrayApp(Options(no_prompt=True), instance=object(), hooks=mgr,
                  config=AppConfig(tmp_path / "state.json"),
                  autostart=Autostart(run_key, command="cmd-x"))
    app.dialogs = FakeDialogs()
    app.refresh = lambda: None  # no window here
    app.ctx = type("Ctx", (), {"settings": settings, "tmp": tmp_path})()
    return app


# --- the first-run question ---------------------------------------------------------------


def test_first_run_asks_and_yes_installs(ctx):
    ctx.ctx.settings.parent.mkdir(parents=True)
    ctx.ctx.settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")  # the usual case
    ctx.dialogs.answers = [True]
    ctx._startup_hook_check()
    kinds = ctx.dialogs.kinds()
    assert kinds == ["ask", "info"]
    _, title, text = ctx.dialogs.calls[0]
    assert str(ctx.ctx.settings) in text and "backup" in text.lower()
    assert "other settings" in text
    saved = json.loads(ctx.ctx.settings.read_text(encoding="utf-8"))
    assert saved["model"] == "opus" and hooks.settings_have_our_hooks(saved)
    assert len(list(ctx.ctx.settings.parent.glob("settings.json.bak-*"))) == 1
    assert ctx.hook_status.installed
    assert ctx.config.hooks_declined is False


def test_first_run_no_changes_nothing_and_is_not_asked_again(ctx):
    ctx.dialogs.answers = [False]
    ctx._startup_hook_check()
    assert ctx.dialogs.kinds() == ["ask"]
    assert not ctx.ctx.settings.exists()  # not created
    assert not hooks.NOTIFY_SCRIPT_PATH.exists()  # no script either
    assert ctx.config.hooks_declined is True

    ctx.dialogs.calls.clear()
    ctx._startup_hook_check()  # the next launch
    assert ctx.dialogs.calls == []  # a No sticks


def test_nothing_is_asked_when_the_hooks_are_in_place(ctx):
    ctx.dialogs.answers = [True]
    ctx._startup_hook_check()
    ctx.dialogs.calls.clear()
    ctx._startup_hook_check()
    assert ctx.dialogs.calls == []


def test_a_missing_script_is_asked_about_even_after_a_no(ctx):
    ctx.dialogs.answers = [True]
    ctx._startup_hook_check()
    ctx.config.hooks_declined = True
    hooks.NOTIFY_SCRIPT_PATH.unlink()  # e.g. a cleanup tool deleted ~/.clawd-tank
    ctx.dialogs.calls.clear()
    ctx.dialogs.answers = [True]
    ctx._startup_hook_check()
    assert ctx.dialogs.kinds() == ["ask", "info"]
    assert ctx.hook_status.installed


def test_an_outdated_script_is_repaired_after_a_yes(ctx):
    ctx.dialogs.answers = [True]
    ctx._startup_hook_check()
    hooks.NOTIFY_SCRIPT_PATH.write_text("# old\n", encoding="utf-8")
    ctx.dialogs.calls.clear()
    ctx.dialogs.answers = [True]
    ctx._startup_hook_check()
    assert hooks.NOTIFY_SCRIPT_PATH.read_text(encoding="utf-8") == hooks.NOTIFY_SCRIPT


def test_a_broken_settings_file_is_explained_not_asked_about_or_touched(ctx):
    ctx.ctx.settings.parent.mkdir(parents=True)
    ctx.ctx.settings.write_text("{oops", encoding="utf-8")
    ctx._startup_hook_check()
    assert ctx.dialogs.calls == []  # no Yes/No for something that will be refused
    assert ctx.hook_status.settings_error
    ctx.install_hooks()  # but the button says why
    assert ctx.dialogs.kinds() == ["error"]
    assert "not valid JSON" in ctx.dialogs.calls[0][2] and "Nothing was changed" in ctx.dialogs.calls[0][2]
    assert ctx.ctx.settings.read_text(encoding="utf-8") == "{oops"
    assert not list(ctx.ctx.settings.parent.glob("*.bak-*"))


def test_the_install_button_asks_even_when_not_first_run(ctx):
    ctx.dialogs.answers = [False]
    ctx.install_hooks()
    assert ctx.dialogs.kinds() == ["ask"]  # the click is not consent to edit the file: the dialog is


def test_install_button_when_all_is_current_just_says_so(ctx):
    ctx.dialogs.answers = [True]
    ctx.install_hooks()
    ctx.dialogs.calls.clear()
    ctx.install_hooks()
    assert ctx.dialogs.kinds() == ["info"] and "up to date" in ctx.dialogs.calls[0][2]


def test_a_failed_install_is_reported(ctx, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr("clawd_tank_daemon.install_hooks.apply_install", boom)
    ctx.dialogs.answers = [True]
    ctx.install_hooks()
    assert ctx.dialogs.kinds() == ["ask", "error"]
    assert "read-only" in ctx.dialogs.calls[1][2]


# --- Remove hooks ----------------------------------------------------------------------------


def test_remove_asks_then_removes_and_is_not_nagged_about_afterwards(ctx):
    ctx.dialogs.answers = [True]
    ctx.install_hooks()
    ctx.dialogs.calls.clear()

    ctx.dialogs.answers = [False]
    ctx.remove_hooks()
    assert ctx.dialogs.kinds() == ["ask"]
    assert hooks.settings_have_our_hooks(json.loads(ctx.ctx.settings.read_text(encoding="utf-8")))

    ctx.dialogs.calls.clear()
    ctx.dialogs.answers = [True]
    ctx.remove_hooks()
    assert ctx.dialogs.kinds() == ["ask", "info"]
    assert not hooks.settings_have_our_hooks(json.loads(ctx.ctx.settings.read_text(encoding="utf-8")))
    assert any(ctx.ctx.settings.parent.glob("settings.json.bak-*"))
    assert ctx.config.hooks_declined is True  # removed on purpose

    ctx.dialogs.calls.clear()
    ctx._startup_hook_check()  # next launch: it does not ask for them back
    assert ctx.dialogs.calls == []


# --- Start with Windows --------------------------------------------------------------------------


def test_toggle_autostart_flips_the_registry(ctx):
    assert not ctx.autostart.is_enabled()
    ctx.toggle_autostart()
    assert ctx.autostart.current() == "cmd-x"
    ctx.toggle_autostart()
    assert not ctx.autostart.is_enabled()
    assert ctx.dialogs.calls == []


def test_toggle_autostart_reports_a_registry_error(ctx, monkeypatch):
    monkeypatch.setattr(ctx.autostart, "enable", lambda: (_ for _ in ()).throw(PermissionError("denied")))
    ctx.toggle_autostart()
    assert ctx.dialogs.kinds() == ["error"] and "denied" in ctx.dialogs.calls[0][2]


# --- the tray menu --------------------------------------------------------------------------------


def menu_items(app):
    return list(app._build_menu())


def test_menu_has_the_documented_items_in_order(ctx):
    items = [i for i in menu_items(ctx) if "- -" not in str(i.text)]
    assert [i.text for i in items] == [
        "Open", "Starting...", "Start with Windows", "Install or repair hooks",
        "Remove hooks", "Open log", "Quit"]  # no Retry while nothing is wrong
    assert items[0].default and not items[1].enabled
    assert items[2].checked is False


def test_menu_checkbox_reads_the_registry_each_time(ctx):
    box = next(i for i in menu_items(ctx) if i.text == "Start with Windows")
    assert box.checked is False
    ctx.autostart.enable()
    assert box.checked is True  # same item, new answer: it is read when asked
    ctx.autostart.disable()
    assert box.checked is False


def test_menu_status_line_follows_the_state_and_retry_appears_on_errors(ctx):
    ctx.view.apply("status", {"status": "conflict", "message": "x"})
    ctx._status_line = ctx.view.status_text()
    ctx._can_retry = ctx.view.can_retry
    texts = [i.text for i in menu_items(ctx)]
    assert "Retry" in texts
    assert any("Another Clawd Tank daemon is running" in t for t in texts)


def test_menu_items_post_to_the_ui_thread(ctx):
    posted = []
    ctx.post = lambda kind, payload=None: posted.append(kind)
    for item in menu_items(ctx):
        if item.text in ("Open", "Install or repair hooks", "Remove hooks", "Open log", "Quit",
                         "Start with Windows"):
            item(None)
    assert posted == ["show", "toggle_autostart", "install", "remove", "open_log", "quit"]


# --- engine events -> state -----------------------------------------------------------------------


def test_engine_events_fold_into_the_view(ctx):
    assert ctx.view.engine == EngineStatus.STOPPED
    ctx.post("engine", ("status", {"status": "running", "message": ""}))
    ctx.post("engine", ("connection", {"connected": True, "transport": "ble"}))
    ctx.post("engine", ("display", {"state": {"anims": ["building"], "ids": [0]}}))
    dirty = False
    while True:
        try:
            kind, payload = ctx._queue.get_nowait()
        except queue.Empty:
            break
        dirty |= ctx._handle(kind, payload)
    assert dirty and ctx.view.icon_name() == "idle" and ctx.view.bar_text() == "Connected"
    assert ctx.view.sessions == 1
    assert ctx._handle("engine", ("notifications", {"count": 2})) is False  # nothing to redraw
    assert ctx._handle("engine", ("oneshot", {"anim": "sweeping"})) is False  # no animation to start
    assert ctx._queue.empty()


# --- the tray icon: still, by connection state -----------------------------------------------------


class RecordingTray:
    """Stands in for the pystray icon and counts what is sent to the shell."""

    def __init__(self, icon):
        self._icon, self._title = icon, "Clawd Tank"
        self.icon_updates, self.title_updates = [], []

    @property
    def icon(self):
        return self._icon

    @icon.setter
    def icon(self, value):
        self._icon = value
        self.icon_updates.append(value)

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self._title = value
        self.title_updates.append(value)


class FakeWindow:
    visible = False

    def __init__(self):
        self.renders = 0
        self.calls = []

    def render(self, view, hooks_text, hooks_warning):
        self.renders += 1

    def toggle(self, at=None):
        self.calls.append(("toggle", at))
        return "show"

    def show(self):
        self.calls.append(("show",))

    def hide(self, why="toggle"):
        self.calls.append(("hide", why))


class NoTimerRoot:
    def after(self, *args, **kwargs):
        raise AssertionError(f"a timer was scheduled: {args}")

    after_cancel = after


@pytest.fixture
def live(ctx):
    """The app with its real refresh(), a recording tray, a fake window, and a
    root that refuses every timer."""
    del ctx.refresh                       # back to TrayApp.refresh
    ctx.root, ctx.window = NoTimerRoot(), FakeWindow()
    ctx._tray_size = 16
    ctx._icon_name = ctx.view.icon_name()
    ctx.tray = RecordingTray(ctx.icons.icon(ctx._icon_name, 16))
    return ctx


def feed(app, name, payload):
    """What _pump does with one engine event."""
    if app._handle("engine", (name, payload)):
        app.refresh()


def test_the_icon_follows_the_connection_and_each_picture_is_the_cached_one(live):
    icons, tray = live.icons, live.tray
    assert tray.icon is icons.icon("grey", 16)            # not started: greyed
    feed(live, "status", {"status": "running"})
    assert tray.icon is icons.icon("grey", 16) and tray.icon_updates == []   # scanning: still grey
    feed(live, "connection", {"connected": True})
    assert tray.icon is icons.icon("idle", 16) and len(tray.icon_updates) == 1
    feed(live, "connection", {"connected": False})
    assert tray.icon is icons.icon("grey", 16) and len(tray.icon_updates) == 2
    feed(live, "status", {"status": "conflict", "message": "x"})
    assert tray.icon is icons.icon("error", 16) and len(tray.icon_updates) == 3
    feed(live, "status", {"status": "running"})
    feed(live, "connection", {"connected": True})
    assert tray.icon is icons.icon("idle", 16)            # the very same object: one native handle
    assert len({id(icons.icon(n, 16)) for n in ICON_NAMES}) == 3


def test_connected_shows_the_same_image_whatever_claude_is_doing(live):
    feed(live, "status", {"status": "running"})
    feed(live, "connection", {"connected": True})
    connected = live.tray.icon
    assert connected is live.icons.icon("idle", 16)
    updates = len(live.tray.icon_updates)
    for anim in ("thinking", "typing", "debugger", "building", "conducting", "wizard", "beacon",
                 "confused", "sweeping", "happy", "alert", "dizzy", "idle"):
        feed(live, "display", {"state": {"anims": [anim], "ids": [0]}})
        assert live.tray.icon is connected
    feed(live, "display", {"state": {"status": "sleeping"}})
    assert live.tray.icon is connected
    assert len(live.tray.icon_updates) == updates, "a display event must not update the icon"


def test_display_and_hook_events_send_nothing_to_the_notification_area(live):
    feed(live, "status", {"status": "running"})
    feed(live, "connection", {"connected": True})
    icon_updates, title_updates = len(live.tray.icon_updates), len(live.tray.title_updates)
    renders = live.window.renders
    for n in range(20):
        feed(live, "display", {"state": {"anims": ["thinking", "typing"][: n % 3], "ids": [0, 1]}})
        feed(live, "hook", {"msg": {"event": "tool_use", "tool_name": "Bash", "project": "p"}})
    assert len(live.tray.icon_updates) == icon_updates
    assert len(live.tray.title_updates) == title_updates, "the tooltip follows the connection too"
    assert live.window.renders > renders                  # the window text still follows


def test_greyed_has_no_colour_and_error_has_the_red_dot(live):
    grey = live.icons.icon("grey", 24)
    raw = grey.tobytes()
    assert all(r == g == b for r, g, b, a in zip(raw[0::4], raw[1::4], raw[2::4], raw[3::4]) if a)
    err = live.icons.icon("error", 24)
    r, g, b, a = err.getpixel((24 - 6, 24 - 6))
    assert a == 255 and r > 200 and g < 80 and b < 80
    idle = live.icons.icon("idle", 24)
    assert idle.getpixel((24 - 6, 24 - 6)) != err.getpixel((24 - 6, 24 - 6))


def test_no_animation_timer_is_ever_scheduled(live):
    # NoTimerRoot raises on any after(); drive every kind of event through
    for name, payload in (("status", {"status": "starting"}), ("status", {"status": "running"}),
                          ("connection", {"connected": True}),
                          ("display", {"state": {"anims": ["alert"], "ids": [0]}}),
                          ("oneshot", {"anim": "sweeping"}),
                          ("hook", {"msg": {"event": "x"}}),
                          ("connection", {"connected": False}),
                          ("status", {"status": "error", "message": "boom"})):
        feed(live, name, payload)
    live.refresh()
    for gone in ("_tick", "_cancel_anim", "_show_frame", "_anim_job", "_frame", "_sprite"):
        assert not hasattr(live, gone), gone


def test_the_menu_says_what_is_going_on_in_words(live):
    def lines():
        return [i.text for i in menu_items(live)]

    feed(live, "status", {"status": "running"})
    assert "Scanning for the bar..." in lines()
    feed(live, "connection", {"connected": True})
    assert "Connected to Clawd Tank" in lines()
    assert not any(t.startswith("Last event") for t in lines())   # none yet: no line
    feed(live, "hook", {"msg": {"event": "tool_use", "tool_name": "Bash", "project": "demo"}})
    feed(live, "display", {"state": {"anims": ["building"], "ids": [0]}})
    texts = lines()
    assert "Connected to Clawd Tank" in texts                      # what Claude does is not in it
    assert any(t.startswith("Last event: tool_use Bash in demo, ") for t in texts)
    feed(live, "status", {"status": "conflict", "message": "x"})
    assert any("Another Clawd Tank daemon is running" in t for t in lines())
    assert "Retry" in lines()


def test_the_tooltip_is_set_when_the_connection_changes_and_not_otherwise(live):
    feed(live, "status", {"status": "running"})
    feed(live, "connection", {"connected": True})
    assert live.tray.title_updates[-1] == "Clawd Tank: Connected to Clawd Tank"
    n = len(live.tray.title_updates)
    feed(live, "hook", {"msg": {"event": "x"}})
    assert len(live.tray.title_updates) == n
    feed(live, "connection", {"connected": False})
    assert live.tray.title_updates[-1] == "Clawd Tank: Scanning for the bar..."


def test_open_log_button_and_menu_item_open_the_file_the_log_is_written_to(ctx, monkeypatch):
    from pathlib import Path

    from clawd_tank_tray.options import log_path
    from clawd_tank_tray.logsetup import setup_logging

    import logging
    level = logging.getLogger().level
    handler = setup_logging(log_path())          # what main() does, in this temp LOCALAPPDATA
    try:
        opened = []
        monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
        ctx._handle("open_log", None)             # the tray menu item posts this message
        ctx._window_actions()["open_log"]()       # the window's button
        assert [Path(p) for p in opened] == [Path(handler.baseFilename)] * 2
        assert Path(handler.baseFilename).name == "tray.log"
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(level)
        handler.close()


def test_open_log_opens_the_log_in_local_app_data(ctx, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    ctx.open_log()
    expected = tmp_path / "local" / "ClawdTank" / "tray.log"
    assert opened == [str(expected)] and expected.exists()
    monkeypatch.setattr(os, "startfile", lambda p: (_ for _ in ()).throw(OSError("no app")),
                        raising=False)
    ctx.open_log()
    assert ctx.dialogs.kinds() == ["error"]


# --- the flyout: what the tray click and the other messages do ----------------------------------


def test_a_toggle_message_toggles_the_flyout_with_the_time_of_the_click(live):
    assert live._handle("toggle", 1234.5) is False
    assert live.window.calls == [("toggle", 1234.5)]


def test_show_messages_open_the_flyout_and_do_not_toggle_it(live):
    """The Open menu item, a second launch (the instance event "show") and --show all
    arrive as "show": open it, never close it."""
    live._handle("show", None)
    live._handle("show", None)
    assert live.window.calls == [("show",), ("show",)]


def test_a_left_click_on_the_tray_icon_posts_a_toggle_stamped_with_the_click_time(ctx, monkeypatch):
    import time

    import clawd_tank_tray.app as app_mod

    made = {}

    class FakeTray:
        def __init__(self, *args, **kwargs):
            made.update(kwargs)

        def run_detached(self, setup=None):
            pass

    monkeypatch.setattr(app_mod, "ClawdTrayIcon", FakeTray)
    ctx._start_tray()
    before = time.monotonic()
    made["on_click"]()                                   # what the tray thread calls on WM_LBUTTONUP
    after = time.monotonic()
    kind, stamp = ctx._queue.get_nowait()
    assert kind == "toggle" and before <= stamp <= after
    assert ctx._queue.empty()


def test_the_right_click_menu_still_opens_with_open_not_toggle(ctx):
    posted = []
    ctx.post = lambda kind, payload=None: posted.append(kind)
    next(i for i in menu_items(ctx) if i.text == "Open")(None)
    assert posted == ["show"]


def test_the_flyout_rows_do_what_the_menu_items_do(ctx):
    actions = ctx._window_actions()
    assert set(actions) == {"install", "remove", "open_log", "toggle_autostart", "quit", "retry"}
    assert actions["install"] == ctx.install_hooks and actions["remove"] == ctx.remove_hooks
    assert actions["open_log"] == ctx.open_log and actions["toggle_autostart"] == ctx.toggle_autostart
    assert actions["quit"] == ctx.quit and actions["retry"] == ctx.retry
    assert "hide" not in actions                         # no Hide button: click away or Escape


def test_the_start_with_windows_row_flips_the_registry_like_the_menu_item(ctx):
    assert not ctx._autostart_enabled()
    ctx._window_actions()["toggle_autostart"]()
    assert ctx._autostart_enabled() and ctx.autostart.current() == "cmd-x"
    ctx._handle("toggle_autostart", None)                # the tray menu's way
    assert not ctx._autostart_enabled()


def test_dialogs_close_the_flyout_before_they_open(monkeypatch):
    from tkinter import messagebox

    from clawd_tank_tray.app import Dialogs

    order = []
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: order.append("ask") or True)
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: order.append("info"))
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: order.append("error"))
    d = Dialogs(None, before=lambda: order.append("hide flyout"))
    assert d.ask_yes_no("t", "q") is True
    d.info("t", "x")
    d.error("t", "x")
    assert order == ["hide flyout", "ask", "hide flyout", "info", "hide flyout", "error"]


def test_a_dialog_is_never_parented_to_the_flyout(monkeypatch):
    """A dialog owned by a window that then hides would be hidden with it (and, being
    modal, would hang the app): no parent is passed at all."""
    from tkinter import messagebox

    from clawd_tank_tray.app import Dialogs

    seen = []
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: seen.append(k) or True)
    Dialogs(object()).ask_yes_no("t", "q")
    assert seen == [{}]


def test_the_flyout_is_placed_next_to_the_tray_icon_on_this_machine(ctx):
    ids = []
    ctx.tray = type("T", (), {"shell_ids": lambda self: ids.append(1) or []})()
    placement = ctx._place_flyout((300, 360), 1.0)
    assert ids == [1]                                     # asked the tray icon where it is
    work = placement.work
    assert work.left <= placement.x and placement.x + 300 <= work.right
    assert work.top <= placement.y and placement.y + 360 <= work.bottom
    ctx.tray = None
    assert ctx._tray_icon_ids() == []
