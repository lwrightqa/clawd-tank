"""Yes/No and message dialogs, as NSAlert. Main thread only.

The app has no Dock icon (LSUIElement), so it is never the active app: each dialog
activates it first, or the alert would open behind the window in front.
"""

import AppKit

from .activation import keeper


def _alert(title: str, text: str):
    keeper.activate()       # remembers the app in front; give_back() returns to it after the dialog
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(text)
    # NSAlertStyleInformational (1) on macOS 10.12 and later; the old name before that.
    style = getattr(AppKit, "NSAlertStyleInformational", None)
    if style is None:
        style = getattr(AppKit, "NSInformationalAlertStyle", 1)
    alert.setAlertStyle_(style)
    return alert


def ask_yes_no(title: str, text: str) -> bool:
    """A Yes/No question. Yes is the default button. True for Yes."""
    alert = _alert(title, text)
    alert.addButtonWithTitle_("Yes")     # the first button added is the default one
    alert.addButtonWithTitle_("No")
    answered_yes = alert.runModal() == AppKit.NSAlertFirstButtonReturn
    keeper.give_back()
    return answered_yes


def show_message(title: str, text: str) -> None:
    """A message with an OK button."""
    alert = _alert(title, text)
    alert.addButtonWithTitle_("OK")
    alert.runModal()
    keeper.give_back()
