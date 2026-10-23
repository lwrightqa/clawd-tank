"""Give the keyboard back to the app the user was in.

The menu bar app has no Dock icon and no window, so a click on its status item does
not make it the active app, and its popover and its dialogs would not take clicks
properly: it activates itself first. Nothing undoes that. When the flyout then closes
(Escape, a second click on the icon, a row), Clawd Tank is still the active app with
no window, and the terminal the user was typing in stays greyed out and beeps at
every key until it is clicked. ``FocusKeeper`` remembers the app that was in front
when it activated and activates that one again when the flyout or a dialog is done.

AppKit is imported only by the default arguments, so the logic runs (and is tested)
anywhere with stand-ins for the three AppKit objects.
"""

import logging
from typing import Optional

logger = logging.getLogger("clawd-tank.menubar")


class FocusKeeper:
    def __init__(self, app=None, workspace=None, running_app_class=None):
        self._app_obj = app
        self._workspace_obj = workspace
        self._running_cls = running_app_class
        self._previous = None            # the NSRunningApplication that was in front

    # NSApplication, NSWorkspace and NSRunningApplication, looked up when first needed.

    def _app(self):
        if self._app_obj is None:
            import AppKit
            self._app_obj = AppKit.NSApplication.sharedApplication()
        return self._app_obj

    def _workspace(self):
        if self._workspace_obj is None:
            import AppKit
            self._workspace_obj = AppKit.NSWorkspace.sharedWorkspace()
        return self._workspace_obj

    def _running(self):
        if self._running_cls is None:
            import AppKit
            self._running_cls = AppKit.NSRunningApplication
        return self._running_cls

    @property
    def remembered(self):
        return self._previous

    def activate(self) -> None:
        """Make this app the active one, remembering which app it takes over from. The
        first app remembered is kept while this app stays active (a dialog that opens
        as the flyout closes must not remember Clawd Tank itself)."""
        app = self._app()
        if not app.isActive():
            self._previous = self._other_frontmost_app()
        app.activateIgnoringOtherApps_(True)

    def give_back(self) -> None:
        """Activate the remembered app again, unless that would be wrong:

        * the user already clicked into another app: this app is no longer active,
          and there is nothing to give back;
        * a modal dialog is up: it gives the focus back itself when it ends, and what
          is remembered stays for that.

        With no remembered app (it quit, or was never known) the app hides, which
        makes macOS activate the next app in line."""
        app = self._app()
        if not app.isActive():
            self._previous = None
            return
        if app.modalWindow() is not None:
            return
        previous, self._previous = self._previous, None
        try:
            if previous is not None and not previous.isTerminated():
                previous.activateWithOptions_(0)    # 0: no options; the ignore-others flag is deprecated
                return
            app.hide_(None)
        except Exception:                           # noqa: BLE001 - focus is a nicety; never break the app
            logger.debug("could not give the focus back", exc_info=True)

    def _other_frontmost_app(self) -> Optional[object]:
        try:
            front = self._workspace().frontmostApplication()
            me = self._running().currentApplication()
            if front is None or me is None or front.processIdentifier() == me.processIdentifier():
                return None
            return front
        except Exception:                           # noqa: BLE001
            logger.debug("could not tell which app is in front", exc_info=True)
            return None


# The one the flyout and the dialogs share.
keeper = FocusKeeper()
