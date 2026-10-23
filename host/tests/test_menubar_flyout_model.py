"""The menu bar flyout's content, as data (no AppKit: runs on every platform)."""

from pathlib import Path

from clawd_tank_menubar import flyout_model as fm
from clawd_tank_tray.hookctl import HookStatus
from clawd_tank_tray.state import EngineStatus, ViewState


def hooks(installed=True, error=""):
    return HookStatus(Path("settings.json"), Path("notify"), settings_error=error,
                      hooks_present=installed, script_present=installed, script_current=installed)


def running(**kw):
    return ViewState(engine=EngineStatus.RUNNING, **kw)


def ids(content):
    return [r.id for r in content.rows]


def test_a_connected_bar_with_sessions():
    v = running(bar_connected=True, display={"anims": ["idle", "typing"], "ids": [1, 2]})
    c = fm.build_content(v, hooks(), login_enabled=False)
    assert c.title == "Clawd Tank" and c.dot == fm.DOT_CONNECTED
    assert [(ln.label, ln.value) for ln in c.lines][:3] == [
        ("Bar", "Connected"), ("Hooks", "Installed"), ("Claude Code", "2 active sessions")]
    assert c.lines[3].label == "Last event" and c.lines[3].value == "no event yet"
    assert c.problem == ""


def test_scanning_is_a_grey_dot_and_a_connected_bar_is_green():
    assert fm.build_content(running(), hooks(), False).dot == fm.DOT_IDLE
    assert fm.build_content(running(bar_connected=True), hooks(), False).dot == fm.DOT_CONNECTED
    assert fm.build_content(ViewState(engine=EngineStatus.STOPPED), hooks(), False).dot == fm.DOT_IDLE


def test_an_engine_error_is_a_red_dot_a_message_and_a_retry_row_first():
    v = ViewState(engine=EngineStatus.CONFLICT)
    c = fm.build_content(v, hooks(), False)
    assert c.dot == fm.DOT_ERROR
    assert "Another Clawd Tank daemon" in c.problem
    assert ids(c)[0] == fm.ROW_RETRY
    assert not c.rows[0].separator_before and c.rows[1].separator_before


def test_no_retry_row_when_nothing_is_wrong():
    c = fm.build_content(running(bar_connected=True), hooks(), False)
    assert fm.ROW_RETRY not in ids(c) and c.problem == ""


def test_the_menu_rows_and_their_order():
    c = fm.build_content(running(), hooks(), login_enabled=True)
    assert ids(c) == [fm.ROW_HOOKS_INSTALL, fm.ROW_HOOKS_REMOVE, fm.ROW_OPEN_LOG,
                      fm.ROW_LOGIN, fm.ROW_QUIT]
    assert [r.label for r in c.rows] == [
        "Install or repair hooks", "Remove hooks", "Open log", "Start at login", "Quit"]
    assert [r.separator_before for r in c.rows] == [True, False, False, False, True]


def test_start_at_login_is_checked_only_when_it_is_on():
    on = fm.build_content(running(), hooks(), True)
    off = fm.build_content(running(), hooks(), False)
    assert [r.checked for r in on.rows] == [False, False, False, True, False]
    assert not any(r.checked for r in off.rows)


def test_the_hooks_line_follows_the_hook_status():
    assert fm.build_content(running(), None, False).lines[1].value == "Checking..."
    assert fm.build_content(running(), hooks(installed=False), False).lines[1].value == "Not installed"
    bad = fm.build_content(running(), hooks(error="bad json"), False)
    assert bad.lines[1].value == "settings.json is not valid JSON"
    assert bad.problem == "bad json"          # the hooks warning fills the red area


def test_the_engine_message_wins_over_the_hooks_warning():
    v = ViewState(engine=EngineStatus.ERROR, engine_message="The engine crashed: boom")
    c = fm.build_content(v, hooks(error="bad json"), False)
    assert c.problem == "The engine crashed: boom"


def test_content_is_immutable_data():
    c = fm.build_content(running(), hooks(), False)
    assert isinstance(c.lines, tuple) and isinstance(c.rows, tuple)
