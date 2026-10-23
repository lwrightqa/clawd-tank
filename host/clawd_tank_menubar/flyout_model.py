"""What the menu bar flyout shows, as plain data. No AppKit, no threads.

The flyout is the same panel as the Windows tray app's (host/TRAY.md): Clawd and
the connection dot on top, four status lines, then menu rows. This module turns
the app's state into a ``FlyoutContent``; ``flyout.py`` draws it with AppKit and
the tests check it without a Mac. It reuses the Windows app's ``ViewState`` (the
mapping from daemon events to text) and ``HookStatus`` (the hooks line).
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from clawd_tank_tray.hookctl import HookStatus
from clawd_tank_tray.state import ViewState

TITLE = "Clawd Tank"

# The three states of the dot next to the title, and of the menu bar icon.
DOT_CONNECTED = "connected"      # green: the bar is connected
DOT_IDLE = "idle"                # grey: looking for the bar, or the link dropped
DOT_ERROR = "error"              # red: something is wrong

# Row ids: what the flyout reports back when a row is clicked.
ROW_RETRY = "retry"
ROW_HOOKS_INSTALL = "hooks_install"
ROW_HOOKS_REMOVE = "hooks_remove"
ROW_OPEN_LOG = "open_log"
ROW_LOGIN = "login"
ROW_QUIT = "quit"


@dataclass(frozen=True)
class Line:
    label: str
    value: str


@dataclass(frozen=True)
class RowSpec:
    id: str
    label: str
    checked: bool = False        # a check mark at the right edge
    separator_before: bool = False


@dataclass(frozen=True)
class FlyoutContent:
    title: str
    dot: str
    lines: Tuple[Line, ...]
    problem: str                 # red text under the lines; "" when all is well
    rows: Tuple[RowSpec, ...]


def dot_for(view: ViewState) -> str:
    """The dot follows the tray icon's three states (state.ViewState.icon_name)."""
    return {"idle": DOT_CONNECTED, "grey": DOT_IDLE, "error": DOT_ERROR}[view.icon_name()]


def build_content(view: ViewState, hooks: Optional[HookStatus], login_enabled: bool) -> FlyoutContent:
    """The whole flyout for the current state. ``hooks`` is None while it is not known."""
    hooks_text = hooks.text if hooks is not None else "Checking..."
    lines = (
        Line("Bar", view.bar_text()),
        Line("Hooks", hooks_text),
        Line("Claude Code", view.sessions_text()),
        Line("Last event", view.last_event_text()),
    )
    problem = view.problem() or (hooks.warning if hooks is not None else "")

    rows = []
    if view.can_retry:
        rows.append(RowSpec(ROW_RETRY, "Retry"))
    # A line separates the status block (or the Retry row under a red message) from the menu rows.
    rows.append(RowSpec(ROW_HOOKS_INSTALL, "Install or repair hooks", separator_before=True))
    rows.append(RowSpec(ROW_HOOKS_REMOVE, "Remove hooks"))
    rows.append(RowSpec(ROW_OPEN_LOG, "Open log"))
    rows.append(RowSpec(ROW_LOGIN, "Start at login", checked=login_enabled))
    rows.append(RowSpec(ROW_QUIT, "Quit", separator_before=True))
    return FlyoutContent(TITLE, dot_for(view), lines, problem, tuple(rows))
