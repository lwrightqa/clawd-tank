"""The menu bar flyout's view (flyout.py) and the snapshot tool (snapshot.py).

The first half is plain data and runs everywhere. The second half draws with AppKit
and runs on a Mac only; elsewhere the ``mac`` fixture skips it.
"""

import dataclasses
import struct
import types
from pathlib import Path

import pytest

from clawd_tank_menubar import flyout_model as fm
from clawd_tank_menubar import snapshot


def png_size(path):
    """(width, height) in pixels, read from the PNG header."""
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


# --- plain data: runs on every platform --------------------------------------------------------

def test_the_sample_contents_are_the_three_states_in_order():
    samples = snapshot.sample_contents()
    assert tuple(name for name, _ in samples) == snapshot.STATES == ("connected", "scanning", "conflict")
    assert all(isinstance(c, fm.FlyoutContent) for _, c in samples)


def test_the_connected_sample_has_two_sessions_and_a_last_event():
    c = dict(snapshot.sample_contents())["connected"]
    assert c.dot == fm.DOT_CONNECTED and c.problem == ""
    values = {ln.label: ln.value for ln in c.lines}
    assert values["Bar"] == "Connected"
    assert values["Claude Code"] == "2 active sessions"
    assert values["Last event"].startswith("PreToolUse Bash in clawd-tank, 14:32:07")
    assert [r.checked for r in c.rows if r.id == fm.ROW_LOGIN] == [True]


def test_the_scanning_sample_is_grey_with_nothing_wrong():
    c = dict(snapshot.sample_contents())["scanning"]
    assert c.dot == fm.DOT_IDLE and c.problem == ""
    assert {ln.label: ln.value for ln in c.lines}["Bar"] == "Scanning..."
    assert fm.ROW_RETRY not in [r.id for r in c.rows]


def test_the_conflict_sample_has_the_red_message_and_the_retry_row():
    c = dict(snapshot.sample_contents())["conflict"]
    assert c.dot == fm.DOT_ERROR
    assert "Another Clawd Tank daemon" in c.problem
    assert c.rows[0].id == fm.ROW_RETRY
    # long enough to wrap in a 300 pt flyout
    assert len({ln.label: ln.value for ln in c.lines}["Last event"]) > 50


def test_the_picture_names():
    assert snapshot.png_name("connected", False) == "flyout-connected-light.png"
    assert snapshot.png_name("conflict", True) == "flyout-conflict-dark.png"
    names = {snapshot.png_name(s, d) for s in snapshot.STATES for d in (False, True)}
    assert len(names) == 6


def test_the_snapshot_command_wants_one_folder(capsys, tmp_path):
    assert snapshot.main([]) == 2
    assert snapshot.main([str(tmp_path), "extra"]) == 2
    assert "usage" in capsys.readouterr().err
    assert snapshot.main(["--help"]) == 0
    assert list(tmp_path.iterdir()) == []


# --- AppKit: a Mac only ------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mac():
    AppKit = pytest.importorskip("AppKit")
    from clawd_tank_menubar import flyout
    return types.SimpleNamespace(AppKit=AppKit, flyout=flyout)


def samples():
    return snapshot.sample_contents()


def noop(row_id):
    pass


def text_of(view, AppKit):
    """Every text field's string in a view tree."""
    found = []
    for sub in view.subviews():
        if isinstance(sub, AppKit.NSTextField):
            found.append(str(sub.stringValue()))
        found.extend(text_of(sub, AppKit))
    return found


def with_last_event(content, text):
    lines = tuple(dataclasses.replace(ln, value=text) if ln.label == "Last event" else ln
                  for ln in content.lines)
    return dataclasses.replace(content, lines=lines)


@pytest.mark.parametrize("state", snapshot.STATES)
def test_the_view_builds_300_wide_for_every_sample(mac, state):
    content = dict(samples())[state]
    view = mac.flyout.build_flyout_view(content, noop)
    size = view.frame().size
    assert size.width == 300.0
    assert size.height > 0


def test_the_width_can_be_chosen(mac):
    content = dict(samples())["connected"]
    assert mac.flyout.build_flyout_view(content, noop, width=340.0).frame().size.width == 340.0


def test_the_view_shows_the_title_the_lines_and_the_rows(mac):
    content = dict(samples())["connected"]
    texts = text_of(mac.flyout.build_flyout_view(content, noop), mac.AppKit)
    assert "Clawd Tank" in texts
    for line in content.lines:
        assert line.label in texts and line.value in texts
    for row in content.rows:
        assert row.label in texts


def test_a_long_last_event_makes_the_view_taller(mac):
    content = dict(samples())["connected"]
    short = mac.flyout.build_flyout_view(with_last_event(content, "Stop in clawd-tank"), noop)
    long = mac.flyout.build_flyout_view(with_last_event(content, "PostToolUse Edit in " + "very-long-name-" * 8), noop)
    assert long.frame().size.height > short.frame().size.height
    assert long.frame().size.width == short.frame().size.width == 300.0


def test_the_red_message_adds_height_and_text(mac):
    content = dict(samples())["conflict"]
    quiet = dataclasses.replace(content, problem="")
    with_problem = mac.flyout.build_flyout_view(content, noop)
    without = mac.flyout.build_flyout_view(quiet, noop)
    assert with_problem.frame().size.height > without.frame().size.height
    assert content.problem in text_of(with_problem, mac.AppKit)
    assert content.problem not in text_of(without, mac.AppKit)


def test_there_is_one_row_view_per_row_spec_and_a_click_reports_its_id(mac):
    for _, content in samples():
        clicked = []
        view = mac.flyout.build_flyout_view(content, clicked.append)
        rows = mac.flyout.row_views(view)
        assert len(rows) == len(content.rows)
        for row, spec in zip(rows, content.rows):
            row.activate()
        assert clicked == [spec.id for spec in content.rows]


def test_the_rows_are_full_width_and_stacked_top_to_bottom(mac):
    content = dict(samples())["conflict"]
    view = mac.flyout.build_flyout_view(content, noop)
    frames = [r.frame() for r in mac.flyout.row_views(view)]
    assert all(f.origin.x == 0.0 and f.size.width == 300.0 for f in frames)
    assert all(f.size.height == mac.flyout.ROW_H for f in frames)
    tops = [f.origin.y for f in frames]
    assert tops == sorted(tops) and len(set(tops)) == len(tops)
    assert max(f.origin.y + f.size.height for f in frames) <= view.frame().size.height
    # a separator (and its margins) lies between the Retry row and the next one
    assert tops[1] - tops[0] > mac.flyout.ROW_H


def test_only_start_at_login_has_a_check_mark_and_only_when_on(mac):
    content = dict(samples())["connected"]       # Start at login is on in this sample
    rows = mac.flyout.row_views(mac.flyout.build_flyout_view(content, noop))
    assert [r.ct_check is not None for r in rows] == [spec.checked for spec in content.rows]
    assert [spec.id for spec in content.rows if spec.checked] == [fm.ROW_LOGIN]
    off = dataclasses.replace(content, rows=tuple(dataclasses.replace(r, checked=False) for r in content.rows))
    assert all(r.ct_check is None for r in mac.flyout.row_views(mac.flyout.build_flyout_view(off, noop)))


def test_the_icon_path_finds_a_file_or_says_none(mac):
    assert mac.flyout.icon_path("no-such-icon") is None
    path = mac.flyout.icon_path("clawd-header")
    assert path is None or Path(path).name == "clawd-header.png"


# --- the controller ---

def make_controller(mac, **kw):
    calls = []
    ctl = mac.flyout.FlyoutController(calls.append, **kw)
    return ctl, calls


def test_update_before_show_does_not_raise_and_sizes_the_popover(mac):
    ctl, _ = make_controller(mac)
    assert ctl.view is None and not ctl.is_shown
    content = dict(samples())["connected"]
    ctl.update(content)
    first = ctl.view.frame().size.height
    ctl.update(dataclasses.replace(content, problem="Something went wrong and this is the long message " * 3))
    assert ctl.view.frame().size.height > first
    assert ctl._popover.contentSize().height == pytest.approx(ctl.view.frame().size.height, abs=1.0)
    assert not ctl.is_shown


def test_close_when_never_shown_does_not_raise(mac):
    ctl, calls = make_controller(mac)
    ctl.close()
    ctl.update(dict(samples())["scanning"])
    ctl.close()
    assert not ctl.is_shown and calls == []


def test_a_row_click_closes_the_popover_first_except_start_at_login(mac):
    ctl, _ = make_controller(mac)
    order = []
    ctl.close = lambda: order.append("close")
    ctl._on_row = lambda row_id: order.append(row_id)
    ctl.update(dict(samples())["conflict"])
    rows = {r.ct_spec.id: r for r in mac.flyout.row_views(ctl.view)}
    rows[fm.ROW_QUIT].activate()
    rows[fm.ROW_LOGIN].activate()
    rows[fm.ROW_RETRY].activate()
    assert order == ["close", fm.ROW_QUIT, fm.ROW_LOGIN, "close", fm.ROW_RETRY]


def test_an_action_that_raises_does_not_escape_into_appkit(mac):
    def boom(row_id):
        raise RuntimeError("boom")

    ctl = mac.flyout.FlyoutController(boom)
    ctl.update(dict(samples())["connected"])
    mac.flyout.row_views(ctl.view)[0].activate()      # logged, not raised


def test_appkit_closing_the_popover_clears_is_shown(mac):
    ctl, _ = make_controller(mac)
    ctl._shown = True
    ctl._delegate.popoverDidClose_(None)
    assert not ctl.is_shown


def test_a_click_right_after_a_close_does_not_open_it_again(mac):
    now = [100.0]
    ctl, _ = make_controller(mac, clock=lambda: now[0])
    opened = []
    ctl.show = opened.append
    ctl._delegate.popoverDidClose_(None)          # the transient popover closed on the mouse-down
    now[0] = 100.1
    ctl.toggle("button")                          # ...and the mouse-up reaches the button
    assert opened == []
    now[0] = 101.0
    ctl.toggle("button")
    assert opened == ["button"]


# --- the snapshot ---

def test_render_png_writes_a_picture_of_the_expected_size(mac, tmp_path):
    AppKit = mac.AppKit
    content = dict(samples())["connected"]
    height = mac.flyout.build_flyout_view(content, noop).frame().size.height
    path = tmp_path / "one.png"
    snapshot.render_png(content, path, dark=False, scale=2.0)
    assert png_size(path) == (600, int(round(height * 2)))
    snapshot.render_png(content, tmp_path / "one-x1.png", scale=1.0)
    assert png_size(tmp_path / "one-x1.png") == (300, int(round(height)))

    rep = AppKit.NSBitmapImageRep.imageRepWithContentsOfFile_(str(path))
    seen = set()
    for x in range(0, 600, 12):
        for y in range(0, int(rep.pixelsHigh()), 12):
            c = rep.colorAtX_y_(x, y).colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
            seen.add((round(c.redComponent() * 255), round(c.greenComponent() * 255),
                      round(c.blueComponent() * 255), round(c.alphaComponent() * 255)))
    assert len(seen) > 3        # not one flat colour


def test_light_and_dark_renders_differ(mac, tmp_path):
    AppKit = mac.AppKit
    content = dict(samples())["scanning"]
    light, dark = tmp_path / "light.png", tmp_path / "dark.png"
    snapshot.render_png(content, light, dark=False)
    snapshot.render_png(content, dark, dark=True)
    assert light.read_bytes() != dark.read_bytes()

    def backdrop_brightness(path):
        rep = AppKit.NSBitmapImageRep.imageRepWithContentsOfFile_(str(path))
        c = rep.colorAtX_y_(8, rep.pixelsHigh() // 2).colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
        return c.redComponent() + c.greenComponent() + c.blueComponent()

    assert backdrop_brightness(light) > backdrop_brightness(dark) + 1.0


def _fake_button(frame, screen=(0.0, 0.0, 1920.0, 1080.0)):
    """A status button whose window has this frame (x, y, w, h) on a screen (x, y, w, h)."""
    def rect(x, y, w, h):
        return types.SimpleNamespace(origin=types.SimpleNamespace(x=x, y=y),
                                     size=types.SimpleNamespace(width=w, height=h))
    scr = None if screen is None else types.SimpleNamespace(frame=lambda: rect(*screen))
    window = types.SimpleNamespace(frame=lambda: rect(*frame), screen=lambda: scr)
    return types.SimpleNamespace(window=lambda: window)


def test_the_anchor_is_placed_when_the_status_window_is_on_the_menu_bar(mac):
    from clawd_tank_menubar.flyout import anchor_is_placed
    assert anchor_is_placed(_fake_button((1650.0, 1056.0, 34.0, 24.0)))          # top of the main screen
    assert anchor_is_placed(_fake_button((300.0, -24.0, 34.0, 24.0),             # a screen below the main one
                                         screen=(0.0, -1080.0, 1920.0, 1080.0)))


def test_the_anchor_is_not_placed_just_after_launch(mac):
    """The frame AppKit reports for a moment after launch: below the screen's bottom edge."""
    from clawd_tank_menubar.flyout import anchor_is_placed
    assert not anchor_is_placed(_fake_button((0.0, -24.0, 34.0, 24.0)))


def test_the_anchor_is_not_placed_without_a_window_or_a_screen(mac):
    from clawd_tank_menubar.flyout import anchor_is_placed
    assert not anchor_is_placed(_fake_button((1650.0, 1056.0, 34.0, 24.0), screen=None))
    assert not anchor_is_placed(types.SimpleNamespace(window=lambda: None))
    assert not anchor_is_placed(object())


def test_the_snapshot_command_writes_six_pictures(mac, tmp_path, capsys):
    assert snapshot.main([str(tmp_path / "out")]) == 0
    printed = capsys.readouterr().out.split()
    expected = sorted(snapshot.png_name(s, d) for s in snapshot.STATES for d in (False, True))
    assert sorted(printed) == expected
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == expected
