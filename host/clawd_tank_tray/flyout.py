"""Where the flyout goes and when it closes: pure logic, no GUI, no ctypes.

Two small pieces that the window (window.py) and the Windows queries (winutil.py)
are wired to, so that both can be tested without a display:

* ``flyout_position`` and ``place_flyout``: the top-left corner of the flyout,
  next to the notification area, on any taskbar edge and any monitor;
* ``FlyoutGuard``: a state machine for "open, toggle with a click on the tray icon,
  close when focus is lost or Escape is pressed", with the short guards that keep
  the click that closed it from reopening it and a stray focus event from closing
  it the moment it opens.

All lengths are physical pixels in the process's own (system-DPI) coordinate space,
the one tkinter and the Windows queries use.
"""

from typing import NamedTuple, Optional, Tuple

EDGES = ("left", "top", "right", "bottom")

GAP = 12              # px at 100 %: between the flyout and the taskbar (and the screen sides)
SLIDE = 12            # px at 100 %: how far the opening animation travels

# --- the guards (seconds) ---------------------------------------------------------------------

# A focus loss this soon after the flyout opened is not trusted (Windows moves the
# activation around while it settles the new window); if it is still true when the
# time is up, the flyout closes then.
OPEN_GUARD = 0.30
# A click on the tray icon this soon after the flyout closed because focus was lost
# is the very click that closed it (the mouse went down on the taskbar, focus left,
# and the button comes up on the icon): it must not open the flyout again.
CLICK_GUARD = 0.40
# A click on the tray icon this soon after the flyout opened does not close it.
REOPEN_GUARD = 0.25


class Rect(NamedTuple):
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def centre(self) -> Tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


# --- placement ----------------------------------------------------------------------------------


def _clamp_axis(pos: int, size: int, lo: int, hi: int, gap: int) -> int:
    """`pos` kept inside [lo, hi): with `gap` to spare at both ends when there is
    room for it, else flush with the start (a flyout bigger than the work area is
    pinned to its top-left corner and clipped, never moved off-screen)."""
    lo_g, hi_g = lo + gap, hi - size - gap
    if hi_g >= lo_g:
        return max(lo_g, min(pos, hi_g))
    hi_f = hi - size
    return max(lo, min(pos, hi_f)) if hi_f >= lo else lo


def flyout_position(work: Rect, edge: str, size: Tuple[int, int], scale: float = 1.0,
                    anchor: Optional[Rect] = None) -> Tuple[int, int]:
    """The flyout's top-left corner.

    `work` is the work area of the monitor (what the taskbar leaves free), `edge`
    the side of the screen the taskbar is on, `size` the flyout's (width, height)
    and `scale` the DPI factor (1.0 at 100 %). The flyout sits against the taskbar
    side with a gap of 12 px * scale; along the taskbar it is centred on `anchor`
    (the notification icon's rectangle) when there is one, else it goes to the
    corner of the work area where the notification area is: the right end of a top
    or bottom taskbar, the bottom end of a left or right one. Always inside `work`.
    """
    if edge not in EDGES:
        edge = "bottom"
    w, h = size
    gap = round(GAP * scale)
    cx, cy = anchor.centre if anchor is not None else (None, None)

    if edge in ("bottom", "top"):
        y = work.bottom - h - gap if edge == "bottom" else work.top + gap
        x = work.right - w - gap if cx is None else cx - w // 2
    else:
        x = work.left + gap if edge == "left" else work.right - w - gap
        y = work.bottom - h - gap if cy is None else cy - h // 2

    return (_clamp_axis(x, w, work.left, work.right, gap),
            _clamp_axis(y, h, work.top, work.bottom, gap))


def slide_offset(edge: str, scale: float = 1.0) -> Tuple[int, int]:
    """(dx, dy) the opening animation starts from: the flyout rises out of the
    taskbar side, so it starts a little nearer to that side."""
    d = round(SLIDE * scale)
    return {"bottom": (0, d), "top": (0, -d), "left": (-d, 0), "right": (d, 0)}.get(edge, (0, d))


def infer_taskbar_edge(monitor: Rect, work: Rect) -> Optional[str]:
    """Which side of `monitor` the taskbar takes, from what it leaves out of the
    work area (the widest strip wins); None when the work area is the whole
    monitor (an auto-hiding taskbar, or none)."""
    strips = {"left": work.left - monitor.left, "top": work.top - monitor.top,
              "right": monitor.right - work.right, "bottom": monitor.bottom - work.bottom}
    edge, widest = max(strips.items(), key=lambda kv: kv[1])
    return edge if widest > 0 else None


def clear_of_taskbar(work: Rect, edge: str, bar: Optional[Rect]) -> Rect:
    """`work` with the taskbar's own rectangle taken out on its `edge`. Usually that
    changes nothing (the work area already excludes the taskbar), but an auto-hiding
    taskbar reserves no space, and while it is slid out (it comes out when the mouse
    reaches its edge, such as for the click on the tray icon) it would cover the
    flyout's lowest rows. A rectangle that makes no sense is ignored."""
    if bar is None or edge not in EDGES:
        return work
    if edge == "bottom":
        out = Rect(work.left, work.top, work.right, min(work.bottom, bar.top))
    elif edge == "top":
        out = Rect(work.left, max(work.top, bar.bottom), work.right, work.bottom)
    elif edge == "left":
        out = Rect(max(work.left, bar.right), work.top, work.right, work.bottom)
    else:
        out = Rect(work.left, work.top, min(work.right, bar.left), work.bottom)
    return out if out.width > 0 and out.height > 0 else work


class Placement(NamedTuple):
    x: int
    y: int
    work: Rect                     # the work area it was fitted into (the taskbar's strip excluded)
    edge: str
    anchor: Optional[Rect] = None  # the notification icon's rectangle, if it was used

    @property
    def anchored(self) -> bool:
        return self.anchor is not None


def place_flyout(shell, size: Tuple[int, int], scale: float = 1.0) -> Placement:
    """Decide where to put the flyout, from what `shell` can tell.

    `shell` answers (any of them may say None, which means "unknown"):
        icon_rect()          Rect of Clawd's notification icon, on screen
        taskbar()            (edge, Rect) of the taskbar
        monitor_at(x, y)     (monitor Rect, work-area Rect) of the monitor holding the point
        cursor()             (x, y) of the mouse
        fallback_work()      Rect to use when no monitor can be found

    The monitor is the one with the icon, else the one with the taskbar, else the
    one under the mouse. The taskbar's edge comes from the taskbar itself when it
    is on that monitor, else from what it takes out of the work area, else bottom.
    """
    anchor = shell.icon_rect()
    taskbar = shell.taskbar()
    if anchor is not None:
        point = anchor.centre
    elif taskbar is not None:
        point = taskbar[1].centre
    else:
        point = shell.cursor()

    found = shell.monitor_at(*point) if point is not None else None
    if found is None:
        work = shell.fallback_work()
        monitor = work
    else:
        monitor, work = found

    edge = None
    if taskbar is not None and monitor.contains(*taskbar[1].centre):
        edge = taskbar[0]
    if edge not in EDGES:
        edge = infer_taskbar_edge(monitor, work)
    if edge is None:
        edge = "bottom"
    if anchor is not None and not monitor.contains(*anchor.centre):
        anchor = None
    if taskbar is not None and monitor.contains(*taskbar[1].centre):
        work = clear_of_taskbar(work, edge, taskbar[1])
    x, y = flyout_position(work, edge, size, scale, anchor)
    return Placement(x, y, work, edge, anchor)


# --- open / toggle / close ---------------------------------------------------------------------

SHOW, HIDE, IGNORE, DEFER = "show", "hide", "ignore", "defer"


class FlyoutGuard:
    """Whether the flyout is open, and what a click on the tray icon, a focus loss
    or Escape should do about it. Times are seconds on any one clock (the caller
    passes ``time.monotonic()``); nothing here reads a clock or a window.

    * ``tray_click(t)``: SHOW or HIDE, or IGNORE for the click that closed it a
      moment ago by taking the focus away, and for a click right after it opened.
    * ``focus_lost(t)``: HIDE, IGNORE (not open), or DEFER (just opened: look again
      with ``settle`` when ``OPEN_GUARD`` is up).
    * ``escape()``: True if it should close.
    """

    def __init__(self) -> None:
        self.visible = False
        self.opened_at = float("-inf")
        self.closed_at = float("-inf")
        self.closed_by = ""          # "focus", "escape", "toggle", "action", ...
        self.focus_pending = False   # focus was lost inside OPEN_GUARD

    def opened(self, t: float) -> None:
        self.visible = True
        self.opened_at = t
        self.focus_pending = False

    def closed(self, t: float, why: str) -> None:
        if self.visible:
            self.closed_at, self.closed_by = t, why
        self.visible = False
        self.focus_pending = False

    def tray_click(self, t: float) -> str:
        if self.visible:
            return IGNORE if t - self.opened_at < REOPEN_GUARD else HIDE
        if self.closed_by == "focus" and t - self.closed_at < CLICK_GUARD:
            return IGNORE
        return SHOW

    def focus_lost(self, t: float) -> str:
        if not self.visible:
            return IGNORE
        if t - self.opened_at < OPEN_GUARD:
            self.focus_pending = True
            return DEFER
        return HIDE

    def focus_gained(self) -> None:
        self.focus_pending = False

    def settle_in(self, t: float) -> float:
        """Seconds until the open guard is over (>= 0)."""
        return max(0.0, self.opened_at + OPEN_GUARD - t)

    def settle(self, has_focus: bool) -> bool:
        """The open guard is over: True if focus was lost during it and has not come
        back, so the flyout should close now."""
        pending, self.focus_pending = self.focus_pending, False
        return self.visible and pending and not has_focus

    def escape(self) -> bool:
        return self.visible
