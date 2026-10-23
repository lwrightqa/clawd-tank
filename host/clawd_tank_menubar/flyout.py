# host/clawd_tank_menubar/flyout.py
"""The menu bar flyout: the Windows tray flyout, drawn with AppKit.

``build_flyout_view`` draws a ``flyout_model.FlyoutContent`` (Clawd, the title and
the status dot, four status lines, a red message, menu rows). ``FlyoutController``
shows it in an NSPopover under the status item's button. Colours are the system's
dynamic ones, so light and dark mode need no code of ours. Everything runs on the
main thread; ``FlyoutController.update`` hops there by itself.

PyObjC notes. Python attributes on the NSObject subclasses below start with
``ct_`` so they can never collide with an Objective-C instance variable of the
superclass. Every helper method is a ``python_method`` (otherwise PyObjC would turn
it into a selector). Objects AppKit only holds weakly (the popover's delegate) are
kept in Python attributes of the controller.
"""

import importlib.resources
import logging
import math
import os
import time
from typing import Callable, List, Optional

import AppKit
import objc

from .flyout_model import (DOT_CONNECTED, DOT_ERROR, DOT_IDLE, ROW_LOGIN, TITLE,
                           FlyoutContent, RowSpec)

logger = logging.getLogger("clawd-tank.menubar.flyout")

# Layout, in points. The views are flipped: y grows downwards, so the layout below
# reads top to bottom.
WIDTH = 300.0
PAD_X = 14.0            # left and right of the header, the lines and the row text
TEXT_INSET = 2.0        # an NSTextField draws its text this far inside its frame
TOP_PAD = 12.0
HEADER_H = 22.0         # the header row: Clawd, the title, the dot
MARK_PT = 20.0          # Clawd in the header
DOT_PT = 10.0
HEADER_GAP = 8.0        # between Clawd and the title
DOT_GAP = 7.0           # between the title and the dot
HEADER_BELOW = 8.0      # between the header and the first line
LINE_GAP = 3.0          # between two lines
LABEL_COL = 84.0        # the column of line names (Bar, Hooks, ...)
LINE_MIN_H = 16.0
BLOCK_GAP = 6.0         # under the last line
PROBLEM_GAP = 4.0       # under the red message
ROW_H = 28.0
HIGHLIGHT_INSET = 5.0   # the hover highlight stops this far from the flyout's sides
HIGHLIGHT_RADIUS = 5.0
SEP_MARGIN = 5.0        # above and below a separator
BOTTOM_PAD = 6.0
ROW_FONT_PT = 13.0      # the rows and the title
LINE_FONT_PT = 12.0     # the status lines and the red message
CHECK = "\u2713"          # the check mark, as text

# A click on the status button while the popover is open closes it (it is transient)
# and then reaches the button as a click of its own. A close this recent means that
# click: do not open it again.
CLICK_GUARD_S = 0.4

_EMPTY = FlyoutContent(TITLE, DOT_IDLE, (), "", ())


def icon_path(name: str) -> Optional[str]:
    """The path of ``icons/<name>.png`` in this package, or None if it is not there."""
    candidates = []
    try:
        candidates.append(os.fspath(importlib.resources.files("clawd_tank_menubar") / "icons"))
    except Exception:  # noqa: BLE001 - a resource that is not a plain folder
        pass
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons"))
    for folder in candidates:
        path = os.path.join(folder, f"{name}.png")
        if os.path.isfile(path):
            return path
    return None


# --- small helpers ----------------------------------------------------------------------------

def _label(text: str, size: float, *, bold: bool = False, wrap: bool = False, color=None):
    """A read-only text field with no border or background."""
    if wrap:
        field = AppKit.NSTextField.wrappingLabelWithString_(text)
        field.setSelectable_(False)          # wrappingLabel is selectable by default
    else:
        field = AppKit.NSTextField.labelWithString_(text)
    font = AppKit.NSFont.boldSystemFontOfSize_(size) if bold else AppKit.NSFont.systemFontOfSize_(size)
    field.setFont_(font)
    field.setTextColor_(color if color is not None else AppKit.NSColor.labelColor())
    return field


def _fit(field, width: Optional[float] = None):
    """(width, height) in whole points that the field's text needs. With ``width`` the
    text wraps at that frame width; without, it is one line. The cell does the
    measuring, so the field laid out at this size wraps exactly the same way."""
    try:
        cell = field.cell()
        if width is None:
            size = cell.cellSize()
        else:
            # A very tall rect: the wrapped height is what comes back.
            size = cell.cellSizeForBounds_(AppKit.NSMakeRect(0.0, 0.0, width, 10000.0))
    except Exception:  # noqa: BLE001 - fall back to the view's own idea of its size
        size = field.fittingSize()
    return float(math.ceil(size.width)), float(math.ceil(size.height))


def _center(outer: float, inner: float) -> float:
    """The offset that centres ``inner`` in ``outer``, on a whole point."""
    return float(round((outer - inner) / 2.0))


def _load_image(name: str, points: float):
    """The named icon as an NSImage ``points`` wide, with its @2x file as a second
    representation, or None if it cannot be loaded."""
    try:
        size = AppKit.NSMakeSize(points, points)
        image = AppKit.NSImage.alloc().initWithSize_(size)
        added = 0
        for path in (icon_path(name), icon_path(name + "@2x")):
            if path is None:
                continue
            rep = AppKit.NSImageRep.imageRepWithContentsOfFile_(path)
            if rep is None:
                continue
            # A 40 px file declared 20 x 20 pt is the Retina picture: AppKit chooses
            # between the representations by the screen's scale.
            rep.setSize_(size)
            image.addRepresentation_(rep)
            added += 1
        return image if added else None
    except Exception:  # noqa: BLE001 - a missing picture must not stop the flyout
        logger.debug("could not load %s", name, exc_info=True)
        return None


def _dot_color(state: str):
    if state == DOT_CONNECTED:
        return AppKit.NSColor.systemGreenColor()
    if state == DOT_ERROR:
        return AppKit.NSColor.systemRedColor()
    return AppKit.NSColor.systemGrayColor()


# --- views ------------------------------------------------------------------------------------

class ClawdFlyoutView(AppKit.NSView):
    """A plain view whose y axis points down."""

    def isFlipped(self):
        return True


class ClawdFlyoutDot(AppKit.NSView):
    """The round status dot. ``ct_state`` is one of the flyout_model DOT_* values."""

    def drawRect_(self, rect):
        _dot_color(getattr(self, "ct_state", DOT_IDLE)).setFill()
        AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            AppKit.NSInsetRect(self.bounds(), 0.5, 0.5)).fill()


class ClawdFlyoutRow(AppKit.NSView):
    """One menu row: a full-width strip with the label and, when checked, a check mark
    at the right edge. The pointer highlights it; releasing the button over it runs
    ``activate``. Not a button: no border, like a menu item."""

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def hitTest_(self, point):
        # The whole strip is the target: the label inside it must not take the click.
        # ``point`` is in the superview's coordinates.
        if AppKit.NSPointInRect(point, self.frame()):
            return self
        return None

    def updateTrackingAreas(self):
        objc.super(ClawdFlyoutRow, self).updateTrackingAreas()
        old = getattr(self, "ct_tracking", None)
        if old is not None:
            self.removeTrackingArea_(old)
        options = (AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways
                   | AppKit.NSTrackingInVisibleRect)
        area = AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None)
        self.addTrackingArea_(area)
        self.ct_tracking = area

    def mouseEntered_(self, event):
        self.ct_hover = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.ct_hover = False
        self.setNeedsDisplay_(True)

    def mouseDown_(self, event):
        self.ct_pressed = True
        self.setNeedsDisplay_(True)

    def mouseUp_(self, event):
        self.ct_pressed = False
        self.setNeedsDisplay_(True)
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if AppKit.NSPointInRect(point, self.bounds()):
            self.activate()

    def drawRect_(self, rect):
        pressed = getattr(self, "ct_pressed", False)
        if not (pressed or getattr(self, "ct_hover", False)):
            return
        # A tint of the text colour: a grey that suits the popover in light and dark.
        AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.14 if pressed else 0.08).setFill()
        area = AppKit.NSInsetRect(self.bounds(), HIGHLIGHT_INSET, 1.0)
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            area, HIGHLIGHT_RADIUS, HIGHLIGHT_RADIUS).fill()

    @objc.python_method
    def activate(self):
        """Run the row's action (what a click does)."""
        handler = getattr(self, "ct_on_activate", None)
        if handler is not None:
            handler(self.ct_spec.id)

    @objc.python_method
    def clear_hover(self):
        self.ct_hover = False
        self.ct_pressed = False
        self.setNeedsDisplay_(True)

    @objc.python_method
    def refresh_hover(self):
        """Highlight the row if the pointer is over it now (after the view was rebuilt
        under a pointer that has not moved, no enter event comes)."""
        self.ct_pressed = False
        self.ct_hover = False
        try:
            window = self.window()
            if window is not None:
                point = self.convertPoint_fromView_(window.mouseLocationOutsideOfEventStream(), None)
                self.ct_hover = bool(AppKit.NSPointInRect(point, self.bounds()))
        except Exception:  # noqa: BLE001 - only a cosmetic highlight
            logger.debug("could not read the pointer position", exc_info=True)
        self.setNeedsDisplay_(True)


def _make_row(spec: RowSpec, y: float, width: float, on_row: Callable[[str], None]):
    row = ClawdFlyoutRow.alloc().initWithFrame_(AppKit.NSMakeRect(0.0, y, width, ROW_H))
    row.ct_spec = spec
    row.ct_on_activate = on_row
    row.ct_hover = False
    row.ct_pressed = False
    row.ct_tracking = None
    row.ct_check = None
    label = _label(spec.label, ROW_FONT_PT)
    lw, lh = _fit(label)
    label.setFrame_(AppKit.NSMakeRect(PAD_X - TEXT_INSET, _center(ROW_H, lh), lw, lh))
    row.addSubview_(label)
    if spec.checked:
        check = _label(CHECK, ROW_FONT_PT)
        cw, ch = _fit(check)
        check.setFrame_(AppKit.NSMakeRect(width - PAD_X + TEXT_INSET - cw, _center(ROW_H, ch), cw, ch))
        row.addSubview_(check)
        row.ct_check = check
    try:                                        # VoiceOver: one button per row
        row.setAccessibilityElement_(True)
        row.setAccessibilityRole_(AppKit.NSAccessibilityButtonRole)
        row.setAccessibilityLabel_(spec.label)
    except Exception:  # noqa: BLE001
        logger.debug("could not set the accessibility of a row", exc_info=True)
    return row


def build_flyout_view(content: FlyoutContent, on_row: Callable[[str], None], width: float = WIDTH):
    """The flyout for ``content`` as one view, ``width`` wide and as tall as it needs.
    ``on_row(row_id)`` runs on the main thread when a row is clicked. The row views
    are ``row_views(view)``, in order."""
    AppKit.NSApplication.sharedApplication()    # AppKit wants it before any view is made
    width = float(width)
    root = ClawdFlyoutView.alloc().initWithFrame_(AppKit.NSMakeRect(0.0, 0.0, width, 100.0))
    rows: List[ClawdFlyoutRow] = []
    y = TOP_PAD

    # Header: Clawd, the title, the status dot.
    x = PAD_X
    mark = _load_image("clawd-header", MARK_PT)
    if mark is not None:
        image_view = AppKit.NSImageView.alloc().initWithFrame_(
            AppKit.NSMakeRect(x, y + _center(HEADER_H, MARK_PT), MARK_PT, MARK_PT))
        image_view.setImage_(mark)
        image_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        image_view.setEditable_(False)
        root.addSubview_(image_view)
        x += MARK_PT + HEADER_GAP
    title = _label(content.title, ROW_FONT_PT, bold=True)
    tw, th = _fit(title)
    title.setFrame_(AppKit.NSMakeRect(x - TEXT_INSET, y + _center(HEADER_H, th), tw, th))
    root.addSubview_(title)
    dot = ClawdFlyoutDot.alloc().initWithFrame_(AppKit.NSMakeRect(
        x + tw - 2 * TEXT_INSET + DOT_GAP, y + _center(HEADER_H, DOT_PT), DOT_PT, DOT_PT))
    dot.ct_state = content.dot
    root.addSubview_(dot)
    y += HEADER_H + HEADER_BELOW

    # The status lines: a grey name, then the value (which wraps).
    value_x = PAD_X + LABEL_COL
    value_w = width - PAD_X - value_x + 2 * TEXT_INSET     # the text ends PAD_X from the edge
    for line in content.lines:
        name = _label(line.label, LINE_FONT_PT, color=AppKit.NSColor.secondaryLabelColor())
        value = _label(line.value, LINE_FONT_PT, wrap=True)
        nw, nh = _fit(name)
        vw, vh = _fit(value, value_w)
        name.setFrame_(AppKit.NSMakeRect(PAD_X - TEXT_INSET, y, min(nw, LABEL_COL), nh))
        value.setFrame_(AppKit.NSMakeRect(value_x - TEXT_INSET, y, value_w, vh))
        root.addSubview_(name)
        root.addSubview_(value)
        y += max(nh, vh, LINE_MIN_H) + LINE_GAP
    if content.lines:
        y += BLOCK_GAP - LINE_GAP

    # The red message, only when something is wrong.
    if content.problem:
        problem = _label(content.problem, LINE_FONT_PT, wrap=True, color=AppKit.NSColor.systemRedColor())
        pw = width - 2 * PAD_X + 2 * TEXT_INSET
        ph = _fit(problem, pw)[1]
        problem.setFrame_(AppKit.NSMakeRect(PAD_X - TEXT_INSET, y, pw, ph))
        root.addSubview_(problem)
        y += ph + PROBLEM_GAP

    # The menu rows, with a separator before the ones that ask for it.
    for spec in content.rows:
        if spec.separator_before:
            y += SEP_MARGIN
            line_box = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(PAD_X, y, width - 2 * PAD_X, 1.0))
            line_box.setBoxType_(AppKit.NSBoxSeparator)
            root.addSubview_(line_box)
            y += 1.0 + SEP_MARGIN
        row = _make_row(spec, y, width, on_row)
        root.addSubview_(row)
        rows.append(row)
        y += ROW_H
    y += BOTTOM_PAD

    root.setFrame_(AppKit.NSMakeRect(0.0, 0.0, width, float(math.ceil(y))))
    root.ct_rows = rows
    root.ct_content = content
    return root


def row_views(view) -> list:
    """The row views of a view made by ``build_flyout_view``, top to bottom."""
    return list(getattr(view, "ct_rows", ()))


# --- the popover -------------------------------------------------------------------------------

class ClawdFlyoutPopoverDelegate(AppKit.NSObject):
    """Tells the controller when AppKit closed the popover by itself (a click elsewhere,
    Esc, another app coming to the front)."""

    @objc.typedSelector(b"v@:@")
    def popoverDidClose_(self, notification):
        callback = getattr(self, "ct_on_close", None)
        if callback is not None:
            callback()


class FlyoutController:
    """Shows the flyout in an NSPopover under the status item's button.

    ``on_row(row_id)`` runs on the main thread when a row is clicked. The popover is
    closed first, except for the Start at login row, which stays open so the change
    can be seen (the app answers it with ``update``)."""

    def __init__(self, on_row: Callable[[str], None], width: float = WIDTH, *,
                 clock: Callable[[], float] = time.monotonic):
        AppKit.NSApplication.sharedApplication()
        self._on_row = on_row
        self._width = float(width)
        self._clock = clock
        self._content: Optional[FlyoutContent] = None
        self._view = None
        self._shown = False
        self._closed_at = float("-inf")
        # The view controller's own view stays the same for the popover's whole life;
        # the built flyout is swapped inside it. It is flipped so that when the
        # popover resizes it, the content keeps its place at the top.
        self._container = ClawdFlyoutView.alloc().initWithFrame_(AppKit.NSMakeRect(0.0, 0.0, self._width, 100.0))
        self._view_controller = AppKit.NSViewController.alloc().init()
        self._view_controller.setView_(self._container)
        # NSPopover keeps its delegate weakly: hold it here.
        self._delegate = ClawdFlyoutPopoverDelegate.alloc().init()
        self._delegate.ct_on_close = self._popover_closed
        self._popover = AppKit.NSPopover.alloc().init()
        self._popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)
        self._popover.setAnimates_(True)
        self._popover.setContentViewController_(self._view_controller)
        self._popover.setDelegate_(self._delegate)

    @property
    def is_shown(self) -> bool:
        return self._shown

    @property
    def view(self):
        """The flyout view that is in the popover now, or None before the first update."""
        return self._view

    def update(self, content: FlyoutContent) -> None:
        """New content: kept for the next show, and redrawn in place if shown. May be
        called from any thread."""
        self._content = content
        if AppKit.NSThread.isMainThread():
            self._rebuild()
        else:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._rebuild)

    def toggle(self, status_button) -> None:
        """A left click on the status item: open the flyout, or close it if it is open."""
        if self._shown:
            self.close()
        elif self._clock() - self._closed_at >= CLICK_GUARD_S:
            self.show(status_button)

    def show(self, status_button) -> None:
        if self._shown or status_button is None:
            return
        if self._view is None:
            self._rebuild()
        self._clear_hover()
        # The app has no Dock icon, so it is not active when the status item is
        # clicked; a popover of an inactive app would not take clicks properly.
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._popover.showRelativeToRect_ofView_preferredEdge_(
            status_button.bounds(), status_button, AppKit.NSMinYEdge)
        self._shown = True

    def close(self) -> None:
        if not self._shown:
            return
        self._shown = False
        self._closed_at = self._clock()
        self._popover.close()

    # --- internals ---

    def _rebuild(self) -> None:
        content = self._content if self._content is not None else _EMPTY
        view = build_flyout_view(content, self._row_clicked, self._width)
        for old in list(self._container.subviews()):
            old.removeFromSuperview()
        size = view.frame().size
        self._container.setFrameSize_(size)
        self._container.addSubview_(view)
        self._view = view
        self._popover.setContentSize_(size)      # a shown popover animates to the new size
        if self._shown:
            for row in row_views(view):
                row.refresh_hover()

    def _clear_hover(self) -> None:
        for row in row_views(self._view):
            row.clear_hover()

    def _row_clicked(self, row_id: str) -> None:
        if row_id != ROW_LOGIN:
            self.close()
        try:
            self._on_row(row_id)
        except Exception:  # noqa: BLE001 - never let an action's error escape into AppKit
            logger.exception("flyout row %r failed", row_id)

    def _popover_closed(self) -> None:
        self._shown = False
        self._closed_at = self._clock()
        self._clear_hover()
