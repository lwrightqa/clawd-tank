"""The flyout's decisions, with no window and no Windows calls: where it goes (every
taskbar edge, clamping, a second monitor, DPI), when it opens and closes (the
toggle / focus-loss / Escape state machine and its guards), and which theme it uses."""

import pytest

from clawd_tank_tray import flyout as fl
from clawd_tank_tray import theme as th
from clawd_tank_tray.flyout import Rect

SIZE = (300, 400)   # a flyout


# --- flyout_position: against each taskbar edge ----------------------------------------------------


# a 1920x1080 monitor with a 48 px taskbar
BOTTOM = Rect(0, 0, 1920, 1032)      # the work area above a bottom taskbar
TOP = Rect(0, 48, 1920, 1080)
LEFT = Rect(48, 0, 1920, 1080)
RIGHT = Rect(0, 0, 1872, 1080)


def test_bottom_taskbar_puts_it_at_the_bottom_right_with_a_12px_gap():
    assert fl.flyout_position(BOTTOM, "bottom", SIZE) == (1920 - 300 - 12, 1032 - 400 - 12)


def test_top_taskbar_puts_it_under_the_taskbar_at_the_right():
    assert fl.flyout_position(TOP, "top", SIZE) == (1920 - 300 - 12, 48 + 12)


def test_left_taskbar_puts_it_beside_the_taskbar_at_the_bottom():
    assert fl.flyout_position(LEFT, "left", SIZE) == (48 + 12, 1080 - 400 - 12)


def test_right_taskbar_puts_it_beside_the_taskbar_at_the_bottom():
    assert fl.flyout_position(RIGHT, "right", SIZE) == (1872 - 300 - 12, 1080 - 400 - 12)


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
def test_the_gap_scales_with_dpi(scale):
    gap = round(12 * scale)
    w, h = round(300 * scale), round(400 * scale)
    x, y = fl.flyout_position(BOTTOM, "bottom", (w, h), scale)
    assert x == BOTTOM.right - w - gap and y == BOTTOM.bottom - h - gap
    x, y = fl.flyout_position(LEFT, "left", (w, h), scale)
    assert x == LEFT.left + gap and y == LEFT.bottom - h - gap


def test_the_gap_is_between_8_and_12_px_at_100_percent():
    assert 8 <= fl.GAP <= 12


def test_an_unknown_edge_means_bottom():
    assert fl.flyout_position(BOTTOM, "diagonal", SIZE) == fl.flyout_position(BOTTOM, "bottom", SIZE)


# --- anchored to the notification icon -----------------------------------------------------------


def test_bottom_taskbar_with_an_icon_centres_the_flyout_on_it():
    icon = Rect(1000, 1040, 1024, 1072)               # on the taskbar, mid-screen
    x, y = fl.flyout_position(BOTTOM, "bottom", SIZE, anchor=icon)
    assert x == 1012 - 150                            # centred on the icon
    assert y == 1032 - 400 - 12                       # against the taskbar


def test_an_icon_near_the_right_end_is_clamped_to_the_work_area_with_the_gap():
    icon = Rect(1880, 1040, 1904, 1072)
    x, y = fl.flyout_position(BOTTOM, "bottom", SIZE, anchor=icon)
    assert x == 1920 - 300 - 12                       # not centred: it would run off the screen


def test_an_icon_near_the_left_end_is_clamped_to_the_left_side():
    icon = Rect(4, 1040, 28, 1072)
    x, _ = fl.flyout_position(BOTTOM, "bottom", SIZE, anchor=icon)
    assert x == 12


def test_top_taskbar_with_an_icon():
    icon = Rect(900, 4, 924, 44)
    assert fl.flyout_position(TOP, "top", SIZE, anchor=icon) == (912 - 150, 48 + 12)


def test_left_taskbar_with_an_icon_is_centred_on_it_vertically():
    icon = Rect(8, 700, 40, 724)
    assert fl.flyout_position(LEFT, "left", SIZE, anchor=icon) == (48 + 12, 712 - 200)


def test_right_taskbar_with_an_icon_near_the_top_is_clamped_to_the_top():
    icon = Rect(1880, 20, 1904, 44)
    assert fl.flyout_position(RIGHT, "right", SIZE, anchor=icon) == (1872 - 300 - 12, 12)


# --- clamping ----------------------------------------------------------------------------------


@pytest.mark.parametrize("edge,work", [("bottom", BOTTOM), ("top", TOP), ("left", LEFT), ("right", RIGHT)])
@pytest.mark.parametrize("ax,ay", [(-500, -500), (0, 0), (960, 540), (1919, 1079), (5000, 5000)])
def test_the_flyout_is_always_inside_the_work_area(edge, work, ax, ay):
    anchor = Rect(ax, ay, ax + 24, ay + 24)
    for a in (anchor, None):
        x, y = fl.flyout_position(work, edge, SIZE, anchor=a)
        assert work.left <= x and x + SIZE[0] <= work.right
        assert work.top <= y and y + SIZE[1] <= work.bottom


def test_a_flyout_taller_than_the_work_area_is_pinned_to_the_top():
    small = Rect(0, 0, 1280, 300)
    x, y = fl.flyout_position(small, "bottom", (300, 500))
    assert y == 0 and x == 1280 - 300 - 12             # clipped at the bottom, never above the screen


def test_a_flyout_wider_than_the_work_area_is_pinned_to_the_left():
    narrow = Rect(0, 0, 200, 800)
    assert fl.flyout_position(narrow, "bottom", (300, 400))[0] == 0


def test_a_flyout_that_fits_with_no_room_for_the_gap_is_flush():
    tight = Rect(0, 0, 300, 400)
    assert fl.flyout_position(tight, "bottom", (300, 400)) == (0, 0)


# --- a second monitor ---------------------------------------------------------------------------

RIGHT_MONITOR = Rect(1920, 169, 3840, 1249)           # taller offset, like the real second screen
RIGHT_WORK = Rect(1920, 169, 3840, 1209)              # its own taskbar at the bottom
LEFT_MONITOR_WORK = Rect(-1920, 0, 0, 1040)           # a monitor to the left: negative coordinates
ABOVE_WORK = Rect(0, -1040, 1920, 0)                  # a monitor above the main one


def test_a_monitor_to_the_right_keeps_its_own_coordinates():
    x, y = fl.flyout_position(RIGHT_WORK, "bottom", SIZE)
    assert (x, y) == (3840 - 300 - 12, 1209 - 400 - 12)


def test_a_monitor_to_the_left_has_negative_coordinates():
    x, y = fl.flyout_position(LEFT_MONITOR_WORK, "bottom", SIZE)
    assert (x, y) == (0 - 300 - 12, 1040 - 400 - 12) and x < 0


def test_a_monitor_above_the_main_one_has_negative_y():
    x, y = fl.flyout_position(ABOVE_WORK, "bottom", SIZE)
    assert y == 0 - 400 - 12 and y < 0


def test_an_icon_on_the_second_monitor_centres_there_and_stays_there():
    icon = Rect(3000, 1217, 3024, 1249)
    x, y = fl.flyout_position(RIGHT_WORK, "bottom", SIZE, anchor=icon)
    assert x == 3012 - 150 and RIGHT_WORK.left <= x and x + 300 <= RIGHT_WORK.right
    assert RIGHT_WORK.top <= y and y + 400 <= RIGHT_WORK.bottom


def test_clamping_does_not_leak_into_the_neighbouring_monitor():
    icon = Rect(1925, 1217, 1949, 1249)                # at the far left end of the right monitor
    x, _ = fl.flyout_position(RIGHT_WORK, "bottom", SIZE, anchor=icon)
    assert x == 1920 + 12


# --- slide offset ----------------------------------------------------------------------------------


def test_the_opening_rises_out_of_the_taskbar_side():
    assert fl.slide_offset("bottom") == (0, 12)        # starts lower, moves up
    assert fl.slide_offset("top") == (0, -12)
    assert fl.slide_offset("left") == (-12, 0)
    assert fl.slide_offset("right") == (12, 0)
    assert fl.slide_offset("bottom", 2.0) == (0, 24)


# --- infer_taskbar_edge ------------------------------------------------------------------------


MONITOR = Rect(0, 0, 1920, 1080)


@pytest.mark.parametrize("work,edge", [(BOTTOM, "bottom"), (TOP, "top"), (LEFT, "left"), (RIGHT, "right")])
def test_the_edge_is_the_side_the_work_area_gave_up(work, edge):
    assert fl.infer_taskbar_edge(MONITOR, work) == edge


def test_no_edge_when_the_work_area_is_the_whole_monitor():
    assert fl.infer_taskbar_edge(MONITOR, MONITOR) is None   # an auto-hiding taskbar


# --- place_flyout: choosing the monitor, the edge and the anchor ------------------------------------


class FakeShell:
    def __init__(self, monitors, icon=None, taskbar=None, cursor=None, fallback=None):
        self.monitors = monitors            # [(monitor, work)]
        self.icon, self.taskbar_, self.cursor_ = icon, taskbar, cursor
        self.fallback = fallback or Rect(0, 0, 1280, 720)

    def icon_rect(self):
        return self.icon

    def taskbar(self):
        return self.taskbar_

    def monitor_at(self, x, y):
        for monitor, work in self.monitors:
            if monitor.contains(x, y):
                return monitor, work
        # nearest, like MONITOR_DEFAULTTONEAREST: the first one is fine for a fake
        return self.monitors[0] if self.monitors else None

    def cursor(self):
        return self.cursor_

    def fallback_work(self):
        return self.fallback


MAIN = (Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040))
SECOND = (RIGHT_MONITOR, RIGHT_WORK)
MAIN_TASKBAR = ("bottom", Rect(0, 1040, 1920, 1080))


def test_the_icon_decides_the_monitor_not_the_mouse():
    shell = FakeShell([MAIN, SECOND], icon=Rect(1800, 1048, 1824, 1072), taskbar=MAIN_TASKBAR,
                      cursor=(3000, 600))          # the mouse is on the other monitor
    p = fl.place_flyout(shell, SIZE)
    assert p.work == MAIN[1] and p.anchored and p.edge == "bottom"
    assert MAIN[1].left <= p.x and p.x + 300 <= MAIN[1].right
    assert p.y == 1040 - 400 - 12


def test_without_an_icon_the_taskbar_decides_and_it_goes_to_the_corner():
    shell = FakeShell([MAIN, SECOND], icon=None, taskbar=MAIN_TASKBAR, cursor=(3000, 600))
    p = fl.place_flyout(shell, SIZE)
    assert p.work == MAIN[1] and not p.anchored
    assert (p.x, p.y) == (1920 - 300 - 12, 1040 - 400 - 12)


def test_without_icon_and_taskbar_the_mouse_decides():
    shell = FakeShell([MAIN, SECOND], cursor=(3000, 600))
    p = fl.place_flyout(shell, SIZE)
    assert p.work == SECOND[1]
    assert p.edge == "bottom"                       # inferred: the work area gave up 40 px at the bottom
    assert (p.x, p.y) == (3840 - 300 - 12, 1209 - 400 - 12)


def test_with_nothing_known_the_fallback_work_area_is_used():
    shell = FakeShell([], fallback=Rect(0, 0, 1366, 728))
    p = fl.place_flyout(shell, SIZE)
    assert p.work == Rect(0, 0, 1366, 728) and p.edge == "bottom" and not p.anchored
    assert (p.x, p.y) == (1366 - 300 - 12, 728 - 400 - 12)


@pytest.mark.parametrize("edge,monitor_work,rect", [
    ("left", LEFT, Rect(0, 0, 48, 1080)),
    ("top", TOP, Rect(0, 0, 1920, 48)),
    ("right", RIGHT, Rect(1872, 0, 1920, 1080)),
    ("bottom", BOTTOM, Rect(0, 1032, 1920, 1080)),
])
def test_the_taskbar_edge_comes_from_the_taskbar(edge, monitor_work, rect):
    shell = FakeShell([(MONITOR, monitor_work)], taskbar=(edge, rect))
    assert fl.place_flyout(shell, SIZE).edge == edge


def test_the_edge_is_inferred_when_the_taskbar_is_on_another_monitor():
    shell = FakeShell([MAIN, SECOND], taskbar=MAIN_TASKBAR, cursor=(3000, 600),
                      icon=Rect(3000, 1217, 3024, 1249))   # the icon is on the second monitor
    p = fl.place_flyout(shell, SIZE)
    assert p.work == SECOND[1] and p.edge == "bottom" and p.anchored


def test_an_icon_that_is_not_on_the_chosen_monitor_is_not_used_as_an_anchor():
    class Odd(FakeShell):
        def monitor_at(self, x, y):                 # always the main monitor
            return MAIN
    shell = Odd([MAIN], icon=Rect(3000, 1217, 3024, 1249), taskbar=MAIN_TASKBAR)
    p = fl.place_flyout(shell, SIZE)
    assert not p.anchored and (p.x, p.y) == (1920 - 300 - 12, 1040 - 400 - 12)


def test_scale_is_passed_through():
    shell = FakeShell([MAIN], taskbar=MAIN_TASKBAR)
    p = fl.place_flyout(shell, (450, 600), 1.5)
    assert (p.x, p.y) == (1920 - 450 - 18, 1040 - 600 - 18)


# --- an auto-hiding taskbar reserves no space, but still covers the flyout when it slides out ------------------


def test_clear_of_taskbar_takes_the_taskbar_out_of_the_work_area_on_its_edge():
    whole = Rect(0, 0, 1920, 1080)
    assert fl.clear_of_taskbar(whole, "bottom", Rect(0, 1032, 1920, 1080)) == Rect(0, 0, 1920, 1032)
    assert fl.clear_of_taskbar(whole, "top", Rect(0, 0, 1920, 48)) == Rect(0, 48, 1920, 1080)
    assert fl.clear_of_taskbar(whole, "left", Rect(0, 0, 48, 1080)) == Rect(48, 0, 1920, 1080)
    assert fl.clear_of_taskbar(whole, "right", Rect(1872, 0, 1920, 1080)) == Rect(0, 0, 1872, 1080)


def test_clear_of_taskbar_changes_nothing_when_the_work_area_already_excludes_it():
    assert fl.clear_of_taskbar(BOTTOM, "bottom", Rect(0, 1032, 1920, 1080)) == BOTTOM
    assert fl.clear_of_taskbar(TOP, "top", Rect(0, 0, 1920, 48)) == TOP
    assert fl.clear_of_taskbar(LEFT, "left", Rect(0, 0, 48, 1080)) == LEFT
    assert fl.clear_of_taskbar(RIGHT, "right", Rect(1872, 0, 1920, 1080)) == RIGHT


def test_clear_of_taskbar_never_makes_the_work_area_bigger_or_empty():
    assert fl.clear_of_taskbar(BOTTOM, "bottom", Rect(0, 1070, 1920, 1080)) == BOTTOM    # a hidden sliver
    assert fl.clear_of_taskbar(BOTTOM, "bottom", None) == BOTTOM
    assert fl.clear_of_taskbar(BOTTOM, "diagonal", Rect(0, 0, 1, 1)) == BOTTOM
    assert fl.clear_of_taskbar(BOTTOM, "bottom", Rect(0, -50, 1920, -10)) == BOTTOM       # nonsense: ignored


@pytest.mark.parametrize("edge,bar,expected", [
    ("bottom", Rect(0, 1032, 1920, 1080), (1920 - 312, 1032 - 412)),
    ("top", Rect(0, 0, 1920, 48), (1920 - 312, 48 + 12)),
    ("left", Rect(0, 0, 48, 1080), (48 + 12, 1080 - 412)),
    ("right", Rect(1872, 0, 1920, 1080), (1872 - 312, 1080 - 412)),
])
def test_an_auto_hiding_taskbar_is_kept_clear_of_on_every_edge(edge, bar, expected):
    shell = FakeShell([(MONITOR, MONITOR)], taskbar=(edge, bar))      # work area == monitor
    p = fl.place_flyout(shell, SIZE)
    assert (p.x, p.y) == expected and p.edge == edge
    assert not p.work.contains(*bar.centre)                            # the taskbar is outside what it used


def test_placement_says_which_icon_rectangle_it_used():
    icon = Rect(1800, 1048, 1824, 1072)
    p = fl.place_flyout(FakeShell([MAIN], icon=icon, taskbar=MAIN_TASKBAR), SIZE)
    assert p.anchored and p.anchor == icon
    assert not fl.place_flyout(FakeShell([MAIN], taskbar=MAIN_TASKBAR), SIZE).anchored


# --- the guard: toggle, focus loss, Escape -----------------------------------------------------------


def test_a_click_opens_it_and_a_later_click_closes_it():
    g = fl.FlyoutGuard()
    assert g.tray_click(10.0) == fl.SHOW
    g.opened(10.1)
    assert g.visible
    assert g.tray_click(12.0) == fl.HIDE
    g.closed(12.0, "toggle")
    assert not g.visible and g.tray_click(12.01) == fl.SHOW      # closed by a click: a click opens again


def test_the_click_that_opened_it_does_not_close_it():
    g = fl.FlyoutGuard()
    g.opened(10.0)
    assert g.tray_click(10.0 + fl.REOPEN_GUARD / 2) == fl.IGNORE  # a bounce or a double click
    assert g.tray_click(10.0 + fl.REOPEN_GUARD + 0.01) == fl.HIDE


def test_a_click_stamped_before_it_opened_does_not_close_it():
    g = fl.FlyoutGuard()
    g.opened(10.05)                                  # shown 50 ms after the click, by the UI loop
    assert g.tray_click(10.0) == fl.IGNORE


def test_clicking_the_tray_icon_while_open_closes_it_and_the_click_does_not_reopen_it():
    """The mouse goes down on the taskbar: focus is lost and the flyout hides itself; then
    the button comes up on the icon and the tray says "click". That click must not reopen."""
    g = fl.FlyoutGuard()
    g.opened(10.0)
    assert g.focus_lost(11.00) == fl.HIDE            # mouse down on the icon
    g.closed(11.00, "focus")
    assert g.tray_click(11.12) == fl.IGNORE          # mouse up, 120 ms later
    assert g.tray_click(11.0 + fl.CLICK_GUARD + 0.05) == fl.SHOW   # a real new click, later


def test_a_click_long_after_it_closed_by_itself_opens_it():
    g = fl.FlyoutGuard()
    g.opened(10.0)
    g.focus_lost(11.0)
    g.closed(11.0, "focus")
    assert g.tray_click(20.0) == fl.SHOW


@pytest.mark.parametrize("why", ["escape", "toggle", "action", "quit", "dialog", "app"])
def test_only_a_focus_loss_makes_the_next_click_ignored(why):
    g = fl.FlyoutGuard()
    g.opened(10.0)
    g.closed(11.0, why)
    assert g.tray_click(11.05) == fl.SHOW


def test_a_focus_loss_when_it_is_not_open_is_ignored():
    g = fl.FlyoutGuard()
    assert g.focus_lost(5.0) == fl.IGNORE
    g.opened(10.0)
    g.closed(11.0, "escape")
    assert g.focus_lost(11.1) == fl.IGNORE


def test_a_focus_loss_in_the_first_moments_is_not_trusted_but_not_forgotten():
    g = fl.FlyoutGuard()
    g.opened(10.0)
    assert g.focus_lost(10.05) == fl.DEFER           # Windows is still settling the new window
    assert g.visible and g.focus_pending
    assert 0.2 < g.settle_in(10.05) < fl.OPEN_GUARD
    assert g.settle(has_focus=False) is True         # still no focus when the guard is up: close
    assert not g.focus_pending


def test_focus_that_comes_back_during_the_guard_keeps_it_open():
    g = fl.FlyoutGuard()
    g.opened(10.0)
    g.focus_lost(10.05)
    g.focus_gained()
    assert g.settle(has_focus=True) is False and g.visible


def test_settle_with_nothing_pending_or_with_focus_does_nothing():
    g = fl.FlyoutGuard()
    g.opened(10.0)
    assert g.settle(has_focus=False) is False        # nothing was lost
    g.focus_lost(10.05)
    assert g.settle(has_focus=True) is False         # lost, but has it again
    g.closed(10.5, "escape")
    g.focus_pending = True
    assert g.settle(has_focus=False) is False        # not open any more


def test_the_guard_time_is_over_after_open_guard_seconds():
    g = fl.FlyoutGuard()
    g.opened(10.0)
    assert g.settle_in(10.0 + fl.OPEN_GUARD + 1) == 0.0
    assert g.focus_lost(10.0 + fl.OPEN_GUARD + 0.01) == fl.HIDE


def test_escape_closes_only_an_open_flyout():
    g = fl.FlyoutGuard()
    assert g.escape() is False
    g.opened(1.0)
    assert g.escape() is True
    g.closed(2.0, "escape")
    assert g.escape() is False


def test_reopening_resets_the_pending_loss():
    g = fl.FlyoutGuard()
    g.opened(10.0)
    g.focus_lost(10.05)
    g.closed(10.1, "app")
    g.opened(20.0)
    assert not g.focus_pending


def test_the_guard_numbers_are_short():
    assert 0.1 <= fl.OPEN_GUARD <= 0.5 and 0.2 <= fl.CLICK_GUARD <= 0.6 and fl.REOPEN_GUARD <= 0.5


# --- theme -------------------------------------------------------------------------------------------


def test_windows_dark_mode_gives_the_dark_flyout_colours():
    t = th.current_theme(read=lambda: 0, env={})
    assert t is th.DARK
    assert (t.background, t.border, t.text, t.secondary, t.hover) == (
        "#2B2B2B", "#3F3F3F", "#FFFFFF", "#C5C5C5", "#3A3A3A")


def test_windows_light_mode_gives_the_light_flyout_colours():
    t = th.current_theme(read=lambda: 1, env={})
    assert t is th.LIGHT
    assert (t.background, t.border, t.text, t.secondary, t.hover) == (
        "#F3F3F3", "#D1D1D1", "#1A1A1A", "#5E5E5E", "#E5E5E5")


@pytest.mark.parametrize("value", [None, 2, 7])
def test_an_unreadable_or_odd_setting_means_light(value):
    assert th.current_theme(read=lambda: value, env={}) is th.LIGHT


def test_a_reader_that_fails_means_light():
    def broken():
        raise OSError("no registry")
    assert th.current_theme(read=broken, env={}) is th.LIGHT


def test_the_environment_forces_the_theme_either_way():
    assert th.current_theme(read=lambda: 1, env={th.ENV_THEME: "dark"}) is th.DARK
    assert th.current_theme(read=lambda: 0, env={th.ENV_THEME: "Light"}) is th.LIGHT
    assert th.current_theme(read=lambda: 0, env={th.ENV_THEME: "sepia"}) is th.DARK   # ignored


def test_the_theme_is_read_every_time_it_is_asked_for():
    value = [0]
    assert th.current_theme(read=lambda: value[0], env={}) is th.DARK
    value[0] = 1
    assert th.current_theme(read=lambda: value[0], env={}) is th.LIGHT


def test_the_registry_reader_returns_the_dword_or_none_on_a_real_machine():
    assert th.read_apps_use_light_theme() in (None, 0, 1)


def test_the_red_is_readable_on_its_background_in_both_themes():
    def luminance(hex_colour):
        r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    for t in (th.LIGHT, th.DARK):
        for fg in (t.critical, t.text, t.secondary):
            a, b = sorted((luminance(fg), luminance(t.background)))
            assert (b + 0.05) / (a + 0.05) >= 4.5, (t.name, fg)   # WCAG AA for normal text


# --- fonts ---------------------------------------------------------------------------------------------


def test_the_variable_font_is_used_when_installed_else_segoe_ui():
    both = ["Arial", "Segoe UI", "Segoe UI Variable Text"]
    assert th.pick_font(both, th.BODY_FONTS, "x") == "Segoe UI Variable Text"
    assert th.pick_font(["Arial", "Segoe UI"], th.BODY_FONTS, "x") == "Segoe UI"
    assert th.pick_font(["segoe ui"], th.BODY_FONTS, "x") == "Segoe UI"        # case does not matter
    assert th.pick_font(["Arial"], th.BODY_FONTS, "Arial") == "Arial"          # the fallback
    assert th.pick_font(["Segoe UI Semibold"], th.TITLE_FONTS, "z") == "Segoe UI Semibold"


def test_the_check_mark_uses_the_icon_font_of_the_windows_in_use():
    assert th.pick_check(["Segoe Fluent Icons", "Segoe MDL2 Assets"]) == ("Segoe Fluent Icons", "")
    assert th.pick_check(["Segoe MDL2 Assets"]) == ("Segoe MDL2 Assets", "")
    assert th.pick_check(["Segoe UI Symbol"])[1] == "✓"
    assert th.pick_check([])[1] == "✓"


def test_the_text_sizes_are_9_to_10_points():
    assert 9 <= th.BODY_PT <= 10 and 9 <= th.ROW_PT <= 10
