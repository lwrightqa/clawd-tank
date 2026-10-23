"""Yes/No and message dialogs, as NSAlert. Main thread only.

The app has no Dock icon (LSUIElement), so it is never the active app: each dialog
activates it first, or the alert would open behind the window in front.
"""

import AppKit


def _alert(title: str, text: str):
    # NSApp is the shared NSApplication; ``AppKit.NSApp`` is not always a plain name in PyObjC.
    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
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
    return alert.runModal() == AppKit.NSAlertFirstButtonReturn


def show_message(title: str, text: str) -> None:
    """A message with an OK button."""
    alert = _alert(title, text)
    alert.addButtonWithTitle_("OK")
    alert.runModal()
