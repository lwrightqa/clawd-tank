"""TrayApp's decisions without a window: the hooks consent flow, Start with
Windows, the tray menu, and how engine events reach the state. A fake stands in
for the dialogs; settings.json, the notify script, the registry key and the state
file are all temporary."""

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
from clawd_tank_tray.state import EngineStatus


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
    ctx._status_line = ctx.view.tooltip().removeprefix("Clawd Tank: ")
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
    assert dirty and ctx.view.sprite() == "building" and ctx.view.bar_text() == "Connected"
    assert ctx._handle("engine", ("notifications", {"count": 2})) is False  # nothing to redraw


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
