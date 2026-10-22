"""Daemon events -> what the tray shows. Pure logic, no window."""

import pytest

from clawd_tank_daemon.daemon import TOOL_ANIMATION_MAP, ClawdDaemon
from clawd_tank_tray.icons import SPRITES
from clawd_tank_tray.state import (
    ANIM_PRIORITY,
    ANIM_SPRITES,
    CONFLICT_MESSAGE,
    ONESHOT_SECONDS,
    EngineStatus,
    ViewState,
    describe_hook,
    pick_anim,
)


def running(ble=True, connected=True, **kw) -> ViewState:
    v = ViewState(ble_enabled=ble, **kw)
    v.apply("status", {"status": "running"})
    if connected:
        v.apply("connection", {"connected": True, "transport": "ble"})
    return v


def show(v: ViewState, *anims, **extra) -> ViewState:
    v.apply("display", {"state": {"anims": list(anims), "ids": list(range(len(anims))), **extra}})
    return v


# --- the sprite for each state --------------------------------------------------------------


def test_connected_and_idle_is_the_idle_sprite():
    assert show(running(), "idle").sprite() == "idle"


def test_connected_with_no_session_is_the_sleeping_sprite():
    v = running()
    v.apply("display", {"state": {"status": "sleeping"}})
    assert v.sprite() == "sleeping"
    assert running().sprite() == "sleeping"  # the default display state is sleeping


@pytest.mark.parametrize("anim", [a for a in ANIM_SPRITES if a != "idle"])
def test_each_working_animation_has_its_own_sprite(anim):
    assert show(running(), anim).sprite() == ANIM_SPRITES[anim] == anim


def test_scanning_or_disconnected_is_greyed():
    assert running(connected=False).sprite() == "grey"          # scanning
    v = show(running(), "thinking")
    v.apply("connection", {"connected": False, "transport": "ble"})
    assert v.sprite() == "grey"                                 # the link dropped
    assert ViewState().sprite() == "grey"                       # engine not started
    v = ViewState()
    v.apply("status", {"status": "starting"})
    assert v.sprite() == "grey"


@pytest.mark.parametrize("status", ["conflict", "error"])
def test_port_in_use_and_a_dead_engine_are_the_error_sprite(status):
    v = show(running(), "thinking")
    v.apply("status", {"status": status, "message": "x"})
    assert v.sprite() == "error"
    assert not v.bar_connected  # nothing is connected through a dead engine


def test_without_ble_the_icon_follows_claude_not_the_bar():
    v = ViewState(ble_enabled=False)
    v.apply("status", {"status": "running"})
    assert v.sprite() == "sleeping"
    assert show(v, "building").sprite() == "building"
    assert v.bar_text() == "Bluetooth off (test mode)"


def test_several_sessions_show_the_most_important_animation():
    assert pick_anim(["idle", "thinking", "typing"]) == "typing"
    assert pick_anim(["thinking", "alert", "building"]) == "alert"
    assert pick_anim(["confused", "building"]) == "confused"
    assert pick_anim(["idle", "dizzy"]) == "dizzy"
    assert pick_anim([]) == "idle" and pick_anim(["nonsense"]) == "idle"
    assert show(running(), "idle", "thinking").sprite() == "thinking"
    assert set(ANIM_PRIORITY) == set(ANIM_SPRITES)


def test_compact_plays_sweeping_for_a_few_seconds_then_falls_back():
    v = show(running(), "thinking")
    v.apply("oneshot", {"anim": "sweeping"}, now=100.0)
    assert v.sprite(now=100.5) == "sweeping" and v.oneshot_active(100.5)
    assert v.sprite(now=100.0 + ONESHOT_SECONDS + 0.1) == "thinking"
    assert not v.oneshot_active(100.0 + ONESHOT_SECONDS + 0.1)
    v.apply("oneshot", {"anim": "not-an-animation"}, now=200.0)
    assert not v.oneshot_active(200.1)


def test_every_animation_the_daemon_can_report_has_a_sprite(tmp_path):
    """Drive the real daemon's display logic through every session state."""
    d = ClawdDaemon(headless=False, sessions_path=tmp_path / "s.json")
    seen = set()
    cases = [
        {"state": "waiting"}, {"state": "thinking"}, {"state": "confused"},
        {"state": "error"}, {"state": "idle"}, {"state": "registered"},
        {"state": "working", "subagents": {"a"}},
    ] + [{"state": "working", "tool_name": t} for t in list(TOOL_ANIMATION_MAP) + ["mcp__x__y", "Whatever"]]
    for i, case in enumerate(cases):
        d._session_states = {"s": dict(case)}
        d._session_order = [("s", i)]
        seen.update(d._compute_display_state()["anims"])
    assert seen <= set(ANIM_SPRITES), seen - set(ANIM_SPRITES)
    assert {"building", "typing", "debugger", "conducting", "wizard", "beacon", "thinking",
            "confused", "alert", "dizzy", "idle"} <= seen
    assert set(ANIM_SPRITES.values()) <= set(SPRITES)


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


def test_tooltip_fits_windows_limit_and_says_something_useful():
    v = running()
    assert "connected" in v.tooltip().lower()
    show(v, "thinking")
    assert "thinking" in v.tooltip().lower()
    v.apply("status", {"status": "conflict", "message": CONFLICT_MESSAGE})
    assert "another clawd tank daemon" in v.tooltip().lower()
    for state in (v, ViewState(), running(connected=False)):
        assert len(state.tooltip()) <= 127


def test_unknown_events_are_ignored():
    v = running()
    before = v.sprite()
    v.apply("who-knows", {"x": 1})
    v.apply("notifications", {"count": 3})
    assert v.sprite() == before and v.engine == EngineStatus.RUNNING
