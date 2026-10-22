"""Daemon events -> what the tray shows. Pure logic, no window."""

import pytest

from clawd_tank_tray.state import (
    CONFLICT_MESSAGE,
    ICON_CONNECTED,
    ICON_DISCONNECTED,
    ICON_ERROR,
    ICON_NAMES,
    EngineStatus,
    ViewState,
    describe_hook,
)

EVERY_ANIM = ["idle", "thinking", "typing", "debugger", "building", "conducting", "wizard",
              "beacon", "confused", "sweeping", "happy", "alert", "dizzy"]


def running(ble=True, connected=True, **kw) -> ViewState:
    v = ViewState(ble_enabled=ble, **kw)
    v.apply("status", {"status": "running"})
    if connected:
        v.apply("connection", {"connected": True, "transport": "ble"})
    return v


def show(v: ViewState, *anims, **extra) -> ViewState:
    v.apply("display", {"state": {"anims": list(anims), "ids": list(range(len(anims))), **extra}})
    return v


# --- the icon for each state: connection only -------------------------------------------------


def test_three_icons_and_their_asset_names():
    assert ICON_NAMES == ("idle", "grey", "error")
    assert (ICON_CONNECTED, ICON_DISCONNECTED, ICON_ERROR) == ICON_NAMES


def test_connected_is_the_idle_icon():
    assert running().icon_name() == ICON_CONNECTED


@pytest.mark.parametrize("anim", EVERY_ANIM)
def test_connected_is_the_same_icon_whatever_claude_is_doing(anim):
    assert show(running(), anim).icon_name() == ICON_CONNECTED
    assert show(running(), "idle", anim, "thinking").icon_name() == ICON_CONNECTED


def test_no_session_one_session_or_many_is_the_same_icon():
    v = running()
    v.apply("display", {"state": {"status": "sleeping"}})
    assert v.icon_name() == ICON_CONNECTED
    show(v, "alert", "dizzy", overflow=3)
    assert v.icon_name() == ICON_CONNECTED


def test_display_and_hook_events_never_change_the_icon():
    v = running()
    before = v.icon_name()
    for anim in EVERY_ANIM:
        show(v, anim)
        v.apply("hook", {"msg": {"event": "tool_use", "tool_name": "Bash", "project": "p"}})
        v.apply("oneshot", {"anim": "sweeping"})   # the daemon still sends these; ignored
        v.apply("notifications", {"count": 2})
        assert v.icon_name() == before == ICON_CONNECTED


def test_scanning_or_disconnected_is_greyed():
    assert running(connected=False).icon_name() == ICON_DISCONNECTED   # scanning
    v = show(running(), "thinking")
    v.apply("connection", {"connected": False, "transport": "ble"})
    assert v.icon_name() == ICON_DISCONNECTED                          # the link dropped
    assert ViewState().icon_name() == ICON_DISCONNECTED                # engine not started
    v = ViewState()
    v.apply("status", {"status": "starting"})
    assert v.icon_name() == ICON_DISCONNECTED


def test_greyed_stays_greyed_whatever_claude_is_doing():
    for anim in EVERY_ANIM:
        assert show(running(connected=False), anim).icon_name() == ICON_DISCONNECTED


@pytest.mark.parametrize("status", ["conflict", "error"])
def test_port_in_use_and_a_dead_engine_are_the_error_icon(status):
    v = show(running(), "thinking")
    v.apply("status", {"status": status, "message": "x"})
    assert v.icon_name() == ICON_ERROR
    assert not v.bar_connected  # nothing is connected through a dead engine


def test_the_error_icon_wins_over_a_stale_connection():
    v = running()
    v.apply("connection", {"connected": True})
    v.apply("status", {"status": "conflict", "message": CONFLICT_MESSAGE})
    assert v.icon_name() == ICON_ERROR


def test_without_ble_the_icon_is_the_connected_one():
    v = ViewState(ble_enabled=False)
    v.apply("status", {"status": "running"})
    assert v.icon_name() == ICON_CONNECTED
    assert show(v, "building").icon_name() == ICON_CONNECTED
    assert v.bar_text() == "Bluetooth off (test mode)"


def test_the_state_has_nothing_left_of_the_animations():
    v = ViewState()
    for gone in ("sprite", "caption", "oneshot", "oneshot_until", "oneshot_active"):
        assert not hasattr(v, gone), gone


# --- the status lines ---------------------------------------------------------------------


def test_bar_line():
    assert ViewState().bar_text() == "Disconnected"
    v = ViewState()
    v.apply("status", {"status": "running"})
    assert v.bar_text() == "Scanning..."
    v.apply("connection", {"connected": True})
    assert v.bar_text() == "Connected"
    v.apply("connection", {"connected": False})
    assert v.bar_text() == "Scanning..."  # the daemon rescans at once
    v.apply("status", {"status": "conflict", "message": CONFLICT_MESSAGE})
    assert v.bar_text() == "Disconnected"


def test_session_count_and_last_event():
    v = running()
    assert v.sessions_text() == "no active session" and v.last_event_text() == "no event yet"
    show(v, "thinking")
    assert v.sessions_text() == "1 active session"
    show(v, "thinking", "idle", overflow=2)
    assert v.sessions == 4 and v.sessions_text() == "4 active sessions"
    v.apply("hook", {"msg": {"event": "tool_use", "tool_name": "Bash", "project": "demo"}},
            now=1_700_000_000.0)
    assert v.last_event == "tool_use Bash in demo"
    assert v.last_event_text().startswith("tool_use Bash in demo, ")
    assert len(v.last_event_text().split(", ")[-1]) == 8  # HH:MM:SS


def test_describe_hook():
    assert describe_hook({"event": "dismiss", "hook": "UserPromptSubmit"}) == "UserPromptSubmit"
    assert describe_hook({"event": "tool_use", "tool_name": "Edit"}) == "tool_use Edit"
    assert describe_hook({}) == "event"


def test_problem_line_and_retry():
    v = running()
    assert v.problem() == "" and not v.can_retry
    v.apply("status", {"status": "conflict", "message": CONFLICT_MESSAGE + " (port 47421 is in use)"})
    assert v.problem().startswith("Another Clawd Tank daemon is running; close it.")
    assert v.can_retry
    v.apply("status", {"status": "error", "message": "The engine crashed: boom"})
    assert "boom" in v.problem() and v.can_retry
    v.apply("status", {"status": "running"})
    assert v.problem() == "" and not v.can_retry


def test_status_text_says_what_is_going_on_in_words():
    assert ViewState().status_text() == "Starting..."
    v = ViewState()
    v.apply("status", {"status": "starting"})
    assert v.status_text() == "Starting..."
    v.apply("status", {"status": "running"})
    assert v.status_text() == "Scanning for the bar..."
    v.apply("connection", {"connected": True})
    assert v.status_text() == "Connected to Clawd Tank"
    show(v, "building")
    assert v.status_text() == "Connected to Clawd Tank"   # Claude's activity is not in it
    v.apply("connection", {"connected": False})
    assert v.status_text() == "Scanning for the bar..."
    v.apply("status", {"status": "conflict", "message": CONFLICT_MESSAGE + " (port 1 is in use)"})
    assert v.status_text() == CONFLICT_MESSAGE
    v.apply("status", {"status": "error", "message": "The engine crashed: boom"})
    assert "stopped" in v.status_text().lower() and "restart" in v.status_text().lower()
    off = ViewState(ble_enabled=False)
    off.apply("status", {"status": "running"})
    assert off.status_text() == "Bluetooth off (test mode)"


def test_tooltip_fits_windows_limit_and_follows_the_connection_only():
    v = running()
    assert "connected" in v.tooltip().lower()
    tip = v.tooltip()
    for anim in EVERY_ANIM:
        show(v, anim)
        v.apply("hook", {"msg": {"event": "tool_use", "tool_name": "Bash"}})
        assert v.tooltip() == tip, "the tooltip must not change with Claude's activity"
    v.apply("status", {"status": "conflict", "message": CONFLICT_MESSAGE})
    assert "another clawd tank daemon" in v.tooltip().lower()
    assert "scanning" in running(connected=False).tooltip().lower()
    for state in (v, ViewState(), running(connected=False)):
        assert len(state.tooltip()) <= 127


def test_unknown_events_are_ignored():
    v = running()
    before = v.icon_name()
    v.apply("who-knows", {"x": 1})
    v.apply("notifications", {"count": 3})
    assert v.icon_name() == before and v.engine == EngineStatus.RUNNING
