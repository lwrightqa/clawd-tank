"""The menu bar app's own logic, on any machine: the state behind the icon and the
flyout, the command line, a dead daemon thread, the status item's click handling,
launchd, the icons, and the shape of app.py (read as text: it imports rumps, which
only exists on a Mac).

Nothing here touches ~/.clawd-tank, ~/.claude or ~/Library: files go to tmp_path.
Tests that need AppKit start with pytest.importorskip("AppKit") and skip elsewhere.
"""

import ast
import importlib.util
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawd_tank_menubar import appstate, flyout_model as fm, launchd, statusitem
from clawd_tank_menubar.appstate import MenuBarState
from clawd_tank_menubar.preferences import DEFAULTS
from clawd_tank_tray.hookctl import HookStatus
from clawd_tank_tray.state import CONFLICT_MESSAGE, EngineStatus

HOST = Path(__file__).resolve().parents[1]
MENUBAR = HOST / "clawd_tank_menubar"


def hooks_status(installed=True, error=""):
    return HookStatus(Path("settings.json"), Path("notify"), settings_error=error,
                      hooks_present=installed, script_present=installed, script_current=installed)


def running(**kw):
    state = MenuBarState(**kw)
    state.set_engine(EngineStatus.RUNNING)
    return state


def line(content, label):
    return next(ln.value for ln in content.lines if ln.label == label)


# --- MenuBarState: the icon -----------------------------------------------------------------


def test_the_icon_follows_the_connection_alone():
    state = MenuBarState()
    assert state.icon_name() == "clawd-disconnected"          # engine not started
    state.set_engine("starting")
    assert state.icon_name() == "clawd-disconnected"
    state.set_engine("running")
    assert state.icon_name() == "clawd-disconnected"          # scanning
    state.on_transport("ble", True)
    assert state.icon_name() == "clawd-connected"
    # What Claude is doing never changes it.
    state.on_display({"anims": ["typing", "alert"], "ids": [1, 2]})
    state.on_hook({"event": "tool_use", "tool_name": "Bash"})
    assert state.icon_name() == "clawd-connected"
    state.on_transport("ble", False)
    assert state.icon_name() == "clawd-disconnected"


def test_only_the_ble_transport_is_the_bar():
    state = running()
    state.on_transport("sim", True)
    assert state.icon_name() == "clawd-disconnected"
    assert line(state.content(None, False), "Bar") == "Scanning..."
    state.on_transport("ble", True)
    state.on_transport("sim", False)                           # the simulator going away
    assert state.icon_name() == "clawd-connected"
    assert line(state.content(None, False), "Bar") == "Connected"


@pytest.mark.parametrize("status", [EngineStatus.CONFLICT, EngineStatus.ERROR])
def test_a_conflict_or_an_error_is_the_red_dot_picture(status):
    state = running()
    state.on_transport("ble", True)
    state.set_engine(status, "something is wrong")
    assert state.icon_name() == "clawd-error"
    content = state.content(None, False)
    assert content.dot == fm.DOT_ERROR and content.problem == "something is wrong"
    assert content.rows[0].id == fm.ROW_RETRY                  # the Retry row is first
    assert line(content, "Bar") == "Disconnected"              # the link is gone with the engine


def test_the_engine_status_can_be_given_as_text():
    state = MenuBarState()
    state.set_engine("running")
    assert state.view.engine is EngineStatus.RUNNING
    with pytest.raises(ValueError):
        state.set_engine("exploded")


def test_every_picture_name_is_a_state_of_the_icon():
    seen = set()
    for engine in EngineStatus:
        for bar in (True, False):
            state = MenuBarState()
            state.set_engine(engine)
            state.on_transport("ble", bar)
            seen.add(state.icon_name())
    assert seen == set(appstate.ICON_NAMES)


# --- MenuBarState: test mode and the BLE switch ------------------------------------------------


def test_test_mode_has_no_bar_to_wait_for():
    """CLAWD_TANK_NO_BLE=1: the connected picture, and the line says why."""
    state = running(ble_enabled=False)
    assert state.icon_name() == "clawd-connected"
    content = state.content(None, False)
    assert line(content, "Bar") == "Bluetooth off (test mode)"
    assert content.dot == fm.DOT_CONNECTED


def test_set_ble_enabled_false_is_the_test_mode_switch():
    state = running()
    state.set_ble_enabled(False)
    assert state.icon_name() == "clawd-connected"
    state.set_ble_enabled(True)
    assert state.icon_name() == "clawd-disconnected"


def test_the_menus_ble_switch_off_is_not_the_test_mode():
    state = running()
    state.on_transport("ble", True)
    state.set_ble_switched_off(True)
    assert state.icon_name() == "clawd-disconnected"           # no bar: greyed, not "connected"
    assert line(state.content(None, False), "Bar") == "Bluetooth off"
    state.on_transport("ble", True)                            # a late event does not revive it
    assert state.icon_name() == "clawd-disconnected"
    state.set_ble_switched_off(False)
    assert line(state.content(None, False), "Bar") == "Scanning..."
    state.on_transport("ble", True)
    assert state.icon_name() == "clawd-connected"


def test_the_ble_switch_does_not_hide_an_engine_problem():
    state = running()
    state.set_ble_switched_off(True)
    state.set_engine(EngineStatus.ERROR, "boom")
    assert line(state.content(None, False), "Bar") == "Disconnected"


# --- MenuBarState: the flyout's content --------------------------------------------------------


def test_content_shows_sessions_hooks_and_the_login_check():
    state = running()
    state.on_transport("ble", True)
    state.on_display({"anims": ["idle", "typing"], "ids": [1, 2], "overflow": 1})
    content = state.content(hooks_status(), login_enabled=True)
    assert line(content, "Claude Code") == "3 active sessions"
    assert line(content, "Hooks") == "Installed"
    login = next(r for r in content.rows if r.id == fm.ROW_LOGIN)
    assert login.checked
    assert not next(r for r in state.content(None, False).rows if r.id == fm.ROW_LOGIN).checked
    assert line(state.content(None, False), "Hooks") == "Checking..."


def test_a_display_state_of_none_means_no_session():
    state = running()
    state.on_display({"anims": ["idle"], "ids": [1]})
    assert line(state.content(None, False), "Claude Code") == "1 active session"
    state.on_display(None)
    assert line(state.content(None, False), "Claude Code") == "no active session"


def test_the_last_event_is_described_and_stripped_of_the_rest():
    state = running()
    state.on_hook({"event": "tool_use", "tool_name": "Bash", "project": "demo",
                   "session_id": "s1", "pid": 4242, "message": "ignored"})
    assert state.view.last_event == "tool_use Bash in demo"
    assert line(state.content(None, False), "Last event").startswith("tool_use Bash in demo, ")
    state.on_hook({"event": "add", "hook": "Stop", "project": "demo"})
    assert state.view.last_event == "Stop in demo"
    state.on_hook(None)                                         # a message with nothing in it
    assert state.view.last_event == "event"


def test_a_hooks_warning_is_the_red_message():
    state = running()
    content = state.content(hooks_status(installed=False, error="settings.json is broken"), False)
    assert content.problem == "settings.json is broken"


# --- command line ----------------------------------------------------------------------------


def test_options_default_to_all_off():
    assert appstate.parse_options([], {}) == appstate.Options(False, False, False, False)


def test_options_read_the_command_line_and_the_environment():
    assert appstate.parse_options(["--selftest"], {}).selftest
    env = {appstate.ENV_NO_BLE: "1", appstate.ENV_NO_PROMPT: "true", appstate.ENV_OPEN_FLYOUT: "yes"}
    opts = appstate.parse_options([], env)
    assert opts.no_ble and opts.no_prompt and opts.open_flyout and not opts.selftest
    off = {appstate.ENV_NO_BLE: "0", appstate.ENV_NO_PROMPT: "", appstate.ENV_OPEN_FLYOUT: "no"}
    assert appstate.parse_options([], off) == appstate.Options()


def test_options_ignore_arguments_they_do_not_know():
    assert appstate.parse_options(["-psn_0_12345", "--selftest", "--whatever"], {}).selftest


def test_the_switches_are_the_documented_names():
    assert appstate.ENV_NO_BLE == "CLAWD_TANK_NO_BLE"
    assert appstate.ENV_NO_PROMPT == "CLAWD_TANK_NO_PROMPT"
    assert appstate.ENV_OPEN_FLYOUT == "CLAWD_TANK_OPEN_FLYOUT"


# --- a daemon thread that ended -----------------------------------------------------------------


def test_a_daemon_that_could_not_take_the_lock_over_is_a_conflict():
    status, message = appstate.classify_daemon_failure(SystemExit(1))
    assert status is EngineStatus.CONFLICT and message == CONFLICT_MESSAGE


def test_a_hook_socket_that_is_in_use_is_a_conflict():
    import errno
    status, message = appstate.classify_daemon_failure(OSError(errno.EADDRINUSE, "in use"))
    assert status is EngineStatus.CONFLICT and message.startswith(CONFLICT_MESSAGE)
    assert "socket" in message


def test_any_other_failure_is_an_error_with_its_reason():
    status, message = appstate.classify_daemon_failure(RuntimeError("boom"))
    assert status is EngineStatus.ERROR and "boom" in message and "Retry" in message
    status, message = appstate.classify_daemon_failure(None)    # run() returned by itself
    assert status is EngineStatus.ERROR and "Retry" in message


def test_nothing_is_wrong_when_the_user_is_quitting():
    assert appstate.classify_daemon_failure(None, quitting=True) is None
    assert appstate.classify_daemon_failure(SystemExit(0), quitting=True) is None


def test_the_flyout_shows_a_dead_daemon_with_a_retry_row():
    state = running()
    verdict = appstate.classify_daemon_failure(RuntimeError("boom"))
    state.set_engine(*verdict)
    content = state.content(None, False)
    assert content.rows[0].id == fm.ROW_RETRY and "boom" in content.problem


# --- the rows and app.py ---------------------------------------------------------------------


def app_tree():
    return ast.parse((MENUBAR / "app.py").read_text(encoding="utf-8"))


def app_class():
    return next(n for n in app_tree().body if isinstance(n, ast.ClassDef) and n.name == "ClawdTankApp")


def method_names(cls):
    return {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_row_of_the_flyout_has_a_handler():
    ids = {value for name, value in vars(fm).items() if name.startswith("ROW_")}
    assert set(appstate.ROW_HANDLERS) == ids
    state = running()
    state.set_engine(EngineStatus.ERROR, "x")                   # the longest list: Retry too
    shown = [row.id for row in state.content(None, False).rows]
    assert fm.ROW_RETRY in shown and set(shown) == ids


def test_the_app_defines_what_the_rows_call():
    methods = method_names(app_class())
    for row_id, name in appstate.ROW_HANDLERS.items():
        assert name in methods, f"ClawdTankApp has no {name} for the {row_id} row"


def test_the_app_listens_to_the_daemon_for_the_flyout():
    methods = method_names(app_class())
    assert {"on_connection_change", "on_notification_change", "on_started",
            "on_display_state_change", "on_hook_event"} <= methods


def test_the_daemon_keeps_idle_open_sessions_and_is_not_headless():
    calls = [n for n in ast.walk(app_tree())
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ClawdDaemon"]
    assert len(calls) == 1
    kw = {k.arg: getattr(k.value, "value", k.value) for k in calls[0].keywords}
    assert kw["headless"] is False and kw["keep_live_sessions"] is True
    assert isinstance(kw["observer"], ast.Name) and kw["observer"].id == "self"


def test_the_app_no_longer_edits_settings_json_on_its_own():
    """The old main() called hooks.install_notify_script() and hooks.install_hooks()
    at every launch. Only HookFlow, after a Yes, writes the file now."""
    attrs = {n.attr for n in ast.walk(app_tree()) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(app_tree()) if isinstance(n, ast.Name)}
    for forbidden in ("install_hooks", "install_notify_script", "are_hooks_installed"):
        assert forbidden not in attrs and forbidden not in names


def test_the_icon_is_not_a_template_image():
    source = (MENUBAR / "app.py").read_text(encoding="utf-8")
    assert "self.template = False" in source and "self.template = True" not in source


def test_the_entry_points_pass_through_to_main():
    assert "sys.exit(main())" in (MENUBAR / "__main__.py").read_text(encoding="utf-8")
    assert "main()" in (HOST / "launcher.py").read_text(encoding="utf-8")


# --- small helpers ---------------------------------------------------------------------------


def test_the_log_is_where_main_writes_it():
    assert appstate.log_path(Path("/Users/me")) == Path("/Users/me/Library/Logs/ClawdTank/clawd-tank.log")
    assert appstate.open_command(Path("/x/y.log")) == ["open", str(Path("/x/y.log"))]


def test_the_simulator_check_follows_the_binary():
    class Found:
        def _find_binary(self):
            return "/Applications/Clawd Tank.app/Contents/Resources/clawd-tank-sim"

    class Missing:
        def _find_binary(self):
            return None

    class Broken:
        def _find_binary(self):
            raise OSError("no")

    assert appstate.sim_binary_available(Found)
    assert not appstate.sim_binary_available(Missing)
    assert not appstate.sim_binary_available(Broken)


def test_the_simulator_is_off_by_default():
    assert DEFAULTS["sim_enabled"] is False
    assert DEFAULTS["ble_enabled"] is True


# --- the status item's clicks --------------------------------------------------------------


def test_classify_click():
    assert statusitem.classify_click(statusitem.EVENT_LEFT_MOUSE_UP, 0) == "left"
    assert statusitem.classify_click(statusitem.EVENT_RIGHT_MOUSE_UP, 0) == "right"
    assert statusitem.classify_click(statusitem.EVENT_LEFT_MOUSE_UP, statusitem.FLAG_CONTROL) == "right"
    assert statusitem.classify_click(statusitem.EVENT_LEFT_MOUSE_UP, 0x100 | 0x20000) == "left"  # shift etc.
    assert statusitem.classify_click(None) == "left"            # an action sent by code


def test_the_event_constants_are_the_appkit_ones():
    assert statusitem.MASK_LEFT_MOUSE_UP == 4 and statusitem.MASK_RIGHT_MOUSE_UP == 16
    assert statusitem.FLAG_CONTROL == 0x40000
    assert statusitem.retina_sibling("/a/b/clawd-error.png") == "/a/b/clawd-error@2x.png"


class FakeButton:
    def __init__(self, log):
        self.log = log

    def setTarget_(self, target):
        self.log.append(("target", target))

    def setAction_(self, action):
        self.log.append(("action", action))

    def sendActionOn_(self, mask):
        self.log.append(("mask", mask))

    def setImage_(self, image):
        self.log.append(("image", image))

    def performClick_(self, sender):
        self.log.append(("click", None))


class FakeItem:
    def __init__(self, menu="the-menu"):
        self.log = []
        self._menu = menu
        self._button = FakeButton(self.log)

    def button(self):
        return self._button

    def menu(self):
        return self._menu

    def setMenu_(self, menu):
        self.log.append(("menu", menu))
        self._menu = menu


def make_clicks():
    targets = []

    def factory(on_click):
        targets.append(on_click)
        return SimpleNamespace(on_click=on_click)

    return statusitem.StatusItemClicks(factory), targets


def test_install_takes_the_menu_off_and_listens_on_mouse_up():
    clicks, targets = make_clicks()
    item = FakeItem()
    clicks.install(item, on_left=lambda b: None, on_right=lambda b: None)
    assert clicks.installed
    assert item.menu() is None
    assert ("action", "statusItemClicked:") in item.log
    assert ("mask", statusitem.MASK_LEFT_MOUSE_UP | statusitem.MASK_RIGHT_MOUSE_UP) in item.log
    assert item.log[-1] == ("menu", None)                       # the menu goes last
    assert clicks.button() is item.button()


def test_a_second_install_changes_nothing():
    clicks, targets = make_clicks()
    item = FakeItem()
    clicks.install(item, lambda b: None)
    n = len(item.log)
    clicks.install(item, lambda b: None)
    assert len(item.log) == n and len(targets) == 1


def test_a_failed_install_leaves_the_menu_on_the_item():
    class Broken(FakeButton):
        def sendActionOn_(self, mask):
            raise RuntimeError("no")

    clicks, _ = make_clicks()
    item = FakeItem()
    item._button = Broken(item.log)
    with pytest.raises(RuntimeError):
        clicks.install(item, lambda b: None)
    assert item.menu() == "the-menu" and not clicks.installed


def test_left_and_right_clicks_go_to_their_callbacks():
    clicks, targets = make_clicks()
    item = FakeItem()
    seen = []
    clicks.install(item, on_left=lambda b: seen.append(("left", b)),
                   on_right=lambda b: seen.append(("right", b)))
    on_click = targets[0]
    on_click("left", "button")
    on_click("right", "button")
    assert seen == [("left", "button"), ("right", "button")]


def test_with_no_right_callback_the_right_click_shows_the_original_menu():
    clicks, targets = make_clicks()
    item = FakeItem()
    clicks.install(item, on_left=lambda b: None)
    del item.log[:]
    targets[0]("right", "button")
    assert item.log == [("menu", "the-menu"), ("click", None), ("menu", None)]
    assert item.menu() is None


def test_show_menu_puts_the_menu_back_even_if_the_click_fails():
    class Failing(FakeButton):
        def performClick_(self, sender):
            raise RuntimeError("no")

    clicks, _ = make_clicks()
    item = FakeItem()
    clicks.install(item, lambda b: None)
    item._button = Failing(item.log)
    with pytest.raises(RuntimeError):
        clicks.show_menu()
    assert item.menu() is None
    assert not clicks._showing_menu                             # the guard was released too


def test_a_click_made_by_show_menu_itself_is_ignored():
    clicks, targets = make_clicks()
    item = FakeItem()
    seen = []
    clicks.install(item, on_left=lambda b: seen.append("left"), on_right=lambda b: seen.append("right"))

    def clicking(sender):                                       # the action fires inside performClick_
        targets[0]("left", "button")

    item._button.performClick_ = clicking
    clicks.show_menu()
    assert seen == []


def test_set_image_goes_to_the_button():
    clicks, _ = make_clicks()
    item = FakeItem()
    clicks.install(item, lambda b: None)
    clicks.set_image("an-image")
    clicks.set_image(None)                                      # nothing to show: unchanged
    assert [e for e in item.log if e[0] == "image"] == [("image", "an-image")]


def test_the_appkit_target_and_the_status_image_on_a_mac():
    AppKit = pytest.importorskip("AppKit")
    icons = MENUBAR / "icons"
    image = statusitem.make_status_image(str(icons / "clawd-connected.png"),
                                         str(icons / "clawd-connected@2x.png"))
    assert image is not None and not image.isTemplate()
    assert (image.size().width, image.size().height) == (18.0, 18.0)
    assert len(image.representations()) == 2
    assert statusitem.make_status_image(str(icons / "missing.png")) is None
    target = statusitem._make_click_target(lambda kind, sender: None)
    assert hasattr(target, "statusItemClicked_")
    assert isinstance(target, AppKit.NSObject)


def test_the_dialogs_import_on_a_mac():
    pytest.importorskip("AppKit")
    from clawd_tank_menubar import dialogs
    assert callable(dialogs.ask_yes_no) and callable(dialogs.show_message)


# --- launchd ------------------------------------------------------------------------------------


def test_program_arguments_from_source_run_the_module():
    assert launchd.program_arguments(False, "/App/Contents/MacOS/Clawd Tank", "/usr/bin/python3") == [
        "/usr/bin/python3", "-m", "clawd_tank_menubar.app"]


def test_program_arguments_in_a_bundle_run_the_apps_own_executable():
    assert launchd.program_arguments(True, "/App/Contents/MacOS/Clawd Tank", "/App/Contents/MacOS/python") == [
        "/App/Contents/MacOS/Clawd Tank"]


@pytest.fixture
def agent(tmp_path, monkeypatch):
    path = tmp_path / "LaunchAgents" / "com.clawd-tank.menubar.plist"
    monkeypatch.setattr(launchd, "PLIST_PATH", path)
    ran = []
    monkeypatch.setattr(launchd, "subprocess", SimpleNamespace(run=lambda cmd, **kw: ran.append(cmd)))
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501, raising=False)
    return path, ran


def test_enable_in_a_bundle_starts_the_bundle_executable(agent, monkeypatch):
    path, ran = agent
    exe = "/Applications/Clawd Tank.app/Contents/MacOS/Clawd Tank"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(launchd, "_bundle_executable", lambda: exe)
    launchd.enable()
    plist = plistlib.loads(path.read_bytes())
    assert plist["ProgramArguments"] == [exe]
    assert plist["Label"] == launchd.PLIST_LABEL and plist["RunAtLoad"] is True
    assert ran == []                      # not loaded now: a second copy would end this one
    assert launchd.is_enabled() and not launchd.is_stale()
    assert not list(path.parent.glob("*.tmp"))          # written whole, then renamed


def test_in_a_bundle_without_a_bundle_path_sys_executable_is_the_fallback(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(launchd, "_bundle_executable", lambda: None)
    assert launchd.current_program_arguments() == [sys.executable]


def test_enable_from_source_runs_python_dash_m(agent, monkeypatch):
    path, ran = agent
    monkeypatch.delattr(sys, "frozen", raising=False)
    launchd.enable()
    assert plistlib.loads(path.read_bytes())["ProgramArguments"] == [
        sys.executable, "-m", "clawd_tank_menubar.app"]


def test_from_source_the_bundle_is_not_asked_for_its_executable(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(launchd, "_bundle_executable", lambda: pytest.fail("asked the bundle"))
    assert launchd.current_program_arguments()[0] == sys.executable


def write_plist(path, args):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": launchd.PLIST_LABEL, "ProgramArguments": args}))


def test_a_plist_for_another_program_is_stale(agent, monkeypatch):
    path, _ = agent
    exe = "/Applications/Clawd Tank.app/Contents/MacOS/Clawd Tank"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(launchd, "_bundle_executable", lambda: exe)
    # What the old code wrote for a bundle: the interpreter and -m, which cannot work there.
    write_plist(path, ["/Applications/Clawd Tank.app/Contents/MacOS/python", "-m", "clawd_tank_menubar.app"])
    assert launchd.is_stale()
    write_plist(path, [exe])
    assert not launchd.is_stale()


def test_a_plist_the_old_code_wrote_from_source_is_not_stale(agent, monkeypatch):
    path, _ = agent
    monkeypatch.delattr(sys, "frozen", raising=False)
    write_plist(path, [sys.executable, "-m", "clawd_tank_menubar.app"])
    assert not launchd.is_stale()
    write_plist(path, ["/somewhere/else/python3", "-m", "clawd_tank_menubar.app"])
    assert launchd.is_stale()


def test_no_plist_is_not_stale_and_a_broken_one_is_not_a_crash(agent):
    path, _ = agent
    assert not launchd.is_stale()
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a plist")
    assert not launchd.is_stale()


def test_disable_removes_the_plist_and_unloads_a_stale_job(agent, monkeypatch):
    path, ran = agent
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    write_plist(path, ["x"])
    launchd.disable()
    assert not path.exists()
    assert ran == [["launchctl", "bootout", "gui/501/com.clawd-tank.menubar"]]
    launchd.disable()                                           # nothing there: nothing to do
    assert len(ran) == 1


def test_disable_from_the_launch_agent_job_only_removes_the_file(agent, monkeypatch):
    """bootout would stop the job, which is this process, before the file was removed."""
    path, ran = agent
    monkeypatch.setenv("XPC_SERVICE_NAME", launchd.PLIST_LABEL)
    write_plist(path, ["x"])
    launchd.disable()
    assert not path.exists()
    assert ran == []


def test_an_app_started_by_hand_is_not_the_job(monkeypatch):
    for value in ("0", "application.com.clawd-tank.menubar.123.456", ""):
        monkeypatch.setenv("XPC_SERVICE_NAME", value)
        assert not launchd._is_the_job()
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert not launchd._is_the_job()
    monkeypatch.setenv("XPC_SERVICE_NAME", launchd.PLIST_LABEL)
    assert launchd._is_the_job()


# --- the icons -------------------------------------------------------------------------------


def make_mac_icons():
    spec = importlib.util.spec_from_file_location("make_mac_icons", HOST / "tray" / "make_mac_icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ICON_SIZES = {"clawd-connected": 18, "clawd-disconnected": 18, "clawd-error": 18, "clawd-header": 20}


def test_the_icons_are_there_at_one_and_two_times():
    from PIL import Image
    for name, points in ICON_SIZES.items():
        for suffix, scale in (("", 1), ("@2x", 2)):
            image = Image.open(MENUBAR / "icons" / f"{name}{suffix}.png")
            assert image.size == (points * scale, points * scale), f"{name}{suffix}"
            assert image.mode == "RGBA"
    assert set(ICON_SIZES) == set(appstate.ICON_NAMES) | {appstate.HEADER_ICON}


def test_the_icons_are_what_make_mac_icons_builds_from_the_tray_pictures():
    """A stale icon (the tray pictures changed, the script was not run) fails here."""
    from PIL import Image, ImageChops
    module = make_mac_icons()
    built = module.build()
    assert set(built) == {f"{n}{s}.png" for n in ICON_SIZES for s in ("", "@2x")}
    for file_name, image in built.items():
        on_disk = Image.open(MENUBAR / "icons" / file_name).convert("RGBA")
        assert on_disk.size == image.size
        assert ImageChops.difference(on_disk, image).getbbox() is None, f"{file_name} is stale"


def test_the_three_pictures_differ_and_the_error_one_has_a_red_dot():
    from PIL import Image
    pics = {n: Image.open(MENUBAR / "icons" / f"{n}@2x.png").convert("RGBA")
            for n in ("clawd-connected", "clawd-disconnected", "clawd-error")}
    assert len({p.tobytes() for p in pics.values()}) == 3
    w, h = pics["clawd-error"].size
    r, g, b, a = pics["clawd-error"].getpixel((w - 5, h - 5))      # the dot is bottom right
    assert a == 255 and r > 200 and g < 90 and b < 90
    # The greyed one has no colour of its own: every pixel is a grey.
    raw = pics["clawd-disconnected"].tobytes()
    pixels = [raw[i:i + 4] for i in range(0, len(raw), 4)]
    assert any(p[3] for p in pixels)
    assert all(abs(p[0] - p[1]) < 3 and abs(p[1] - p[2]) < 3 for p in pixels if p[3])


def test_make_mac_icons_writes_into_a_folder_of_our_choice(tmp_path):
    module = make_mac_icons()
    written = module.write(out_dir=tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").glob("*.png")) == sorted(written)
    assert len(written) == 8
