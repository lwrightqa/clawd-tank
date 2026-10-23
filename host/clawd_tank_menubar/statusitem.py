"""Left and right clicks on the menu bar item.

rumps gives its status item an NSMenu, and a status item with a menu opens that
menu on every click. The app wants the left click to open the flyout and the
right click (or a control-click) to open the original menu. So this takes the menu
off the item, makes the item's button send an action on mouse up, and puts the menu
back only for the moment of a right click.

rumps keeps updating the same NSMenuItem objects, so the menu is current whenever
it is shown. Everything here runs on the main thread (AppKit calls the action there).
"""

import logging
import os
from typing import Callable, Optional

try:
    import AppKit
    import objc
except ImportError:          # not a Mac: the pure part of this module still imports
    AppKit = None
    objc = None

logger = logging.getLogger("clawd-tank.menubar.statusitem")

# Raw AppKit values (NSEventType, NSEventMask, NSEventModifierFlags): they are part
# of the ABI and do not change, and this keeps classify_click free of AppKit.
EVENT_LEFT_MOUSE_UP = 2           # NSEventTypeLeftMouseUp
EVENT_RIGHT_MOUSE_UP = 4          # NSEventTypeRightMouseUp
MASK_LEFT_MOUSE_UP = 1 << EVENT_LEFT_MOUSE_UP     # NSEventMaskLeftMouseUp
MASK_RIGHT_MOUSE_UP = 1 << EVENT_RIGHT_MOUSE_UP   # NSEventMaskRightMouseUp
FLAG_CONTROL = 1 << 18            # NSEventModifierFlagControl

ICON_POINTS = 18.0                # the menu bar picture's size in points


def classify_click(event_type: Optional[int], modifier_flags: int = 0) -> str:
    """'right' for a right button click or a control-click, else 'left'. With no
    event (the action was sent by code) it is a left click."""
    if event_type == EVENT_RIGHT_MOUSE_UP or (modifier_flags & FLAG_CONTROL):
        return "right"
    return "left"


def retina_sibling(path: str) -> str:
    """'x/clawd-error.png' -> 'x/clawd-error@2x.png'."""
    root, ext = os.path.splitext(path)
    return f"{root}@2x{ext}"


def make_status_image(path_1x: Optional[str], path_2x: Optional[str] = None,
                      points: float = ICON_POINTS):
    """An NSImage ``points`` wide and tall from a 1x and a Retina file, or None if
    neither can be read. It is not a template image: its colours are drawn as they
    are. Built by hand because rumps sizes every icon at 20 x 20 points and may not
    pick up the @2x file."""
    image = AppKit.NSImage.alloc().initWithSize_((points, points))
    added = 0
    for path in (path_1x, path_2x):
        if not path or not os.path.isfile(path):
            continue
        data = AppKit.NSData.dataWithContentsOfFile_(path)
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(data) if data is not None else None
        if rep is None:
            continue
        # The rep's size is in points: a 36 px file declared 18 x 18 pt is the Retina
        # picture, and AppKit chooses between the two by the screen's scale.
        rep.setSize_((points, points))
        image.addRepresentation_(rep)
        added += 1
    if not added:
        return None
    image.setTemplate_(False)
    return image


if AppKit is not None:

    class _ClickTarget(AppKit.NSObject):
        """The target of the status button's action. Python attributes start with
        ``ct_`` so they cannot collide with an Objective-C instance variable."""

        @objc.python_method
        def configure(self, on_click: Callable[[str, object], None]):
            self.ct_on_click = on_click
            return self

        def statusItemClicked_(self, sender):
            try:
                event = AppKit.NSApplication.sharedApplication().currentEvent()
                if event is None:
                    kind = classify_click(None)
                else:
                    kind = classify_click(int(event.type()), int(event.modifierFlags()))
                self.ct_on_click(kind, sender)
            except Exception:  # noqa: BLE001 - never let a click's error escape into AppKit
                logger.exception("status item click failed")


def _make_click_target(on_click: Callable[[str, object], None]):
    return _ClickTarget.alloc().init().configure(on_click)


class StatusItemClicks:
    """Sends a left click and a right click on one NSStatusItem to two callbacks.

    ``install`` takes the item's menu away (the item then no longer opens it by
    itself) and keeps it. ``show_menu`` is what a right click usually does."""

    def __init__(self, target_factory: Optional[Callable] = None):
        """``target_factory(on_click)`` makes the object the button's action goes
        to; the default is the AppKit one. Tests pass a fake."""
        self._target_factory = target_factory or _make_click_target
        self._item = None
        self._menu = None            # strong references: AppKit would drop both
        self._target = None
        self._on_left: Optional[Callable] = None
        self._on_right: Optional[Callable] = None
        self._showing_menu = False

    @property
    def installed(self) -> bool:
        return self._target is not None

    def button(self):
        """The status item's button (an NSStatusBarButton), or None before install."""
        return self._item.button() if self._item is not None else None

    def install(self, status_item, on_left: Callable, on_right: Optional[Callable] = None) -> None:
        """``on_left(button)`` runs for a left click without the control key,
        ``on_right(button)`` for a right click or a control-click. With no
        ``on_right`` a right click shows the original menu."""
        if self.installed:
            return
        button = status_item.button()
        if button is None:
            raise RuntimeError("the status item has no button")
        self._item = status_item
        self._menu = status_item.menu()
        self._on_left = on_left
        self._on_right = on_right
        try:
            self._target = self._target_factory(self._clicked)
            button.setTarget_(self._target)
            button.setAction_("statusItemClicked:")      # a selector name: PyObjC converts it
            # By default a status button acts on mouse down; the flyout and the menu
            # open when the button comes up, as for a push button.
            button.sendActionOn_(MASK_LEFT_MOUSE_UP | MASK_RIGHT_MOUSE_UP)
            # Last, so that if anything above failed the item keeps its menu. Without
            # a menu the item sends the button's action instead of opening anything.
            status_item.setMenu_(None)
        except Exception:
            self._item = self._menu = self._target = None    # not installed
            raise

    def set_image(self, image) -> None:
        """Show ``image`` (an NSImage) on the button."""
        if self._item is not None and image is not None:
            self._item.button().setImage_(image)

    def show_menu(self) -> None:
        """Open the original menu under the item and wait until it is dismissed."""
        if self._item is None or self._menu is None or self._showing_menu:
            return
        self._showing_menu = True
        try:
            # With a menu on it the item opens that menu when its button is
            # clicked; performClick_ is that click. The menu goes away again after.
            self._item.setMenu_(self._menu)
            self._item.button().performClick_(None)
        finally:
            self._item.setMenu_(None)
            self._showing_menu = False

    # --- internals ---

    def _clicked(self, kind: str, button) -> None:
        if self._showing_menu:
            return                               # the click performClick_ made
        if kind == "right":
            if self._on_right is not None:
                self._on_right(button)
            else:
                self.show_menu()
        elif self._on_left is not None:
            self._on_left(button)
