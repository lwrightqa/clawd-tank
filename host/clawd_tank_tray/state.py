"""What the app shows, derived from what the daemon says. No GUI, no threads.

The engine thread reports daemon events; the UI thread folds them into one
ViewState, and everything on screen (tray icon, tooltip, menu line, window
lines) is a function of that object. That keeps the mapping testable on its own.

The tray icon is one still picture that depends on the connection alone: Clawd
when the bar is connected (whatever Claude Code is doing), greyed when it is not,
with a red dot when something is wrong. What Claude is doing is text, not a
picture, and does not change the icon.
"""

import enum
import time
from dataclasses import dataclass, field
from typing import Optional


class EngineStatus(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    CONFLICT = "conflict"   # another daemon (or program) holds the lock or the hook port
    ERROR = "error"         # the engine thread died; it is restarted


CONFLICT_MESSAGE = "Another Clawd Tank daemon is running; close it."

# The three pictures the tray icon can be, by asset name
# (assets/icons/<name>-<size>.png).
ICON_CONNECTED = "idle"      # the orange Clawd: the bar is connected
ICON_DISCONNECTED = "grey"   # greyed Clawd: looking for the bar, or the link dropped
ICON_ERROR = "error"         # Clawd with a red dot: not running, or the port is taken
ICON_NAMES = (ICON_CONNECTED, ICON_DISCONNECTED, ICON_ERROR)


def describe_hook(msg: dict) -> str:
    """One short phrase for a hook message: 'PreToolUse Bash in myproject'."""
    what = msg.get("hook") or msg.get("event") or "event"
    tool = msg.get("tool_name")
    text = f"{what} {tool}" if tool else str(what)
    project = msg.get("project")
    return f"{text} in {project}" if project else text


@dataclass
class ViewState:
    engine: EngineStatus = EngineStatus.STOPPED
    engine_message: str = ""
    ble_enabled: bool = True
    bar_connected: bool = False
    display: dict = field(default_factory=lambda: {"status": "sleeping"})
    last_event: str = ""
    last_event_time: Optional[float] = None

    # --- folding events in ------------------------------------------------

    def apply(self, name: str, payload: dict, now: Optional[float] = None) -> None:
        """Fold one engine event in. Unknown events are ignored."""
        now = time.time() if now is None else now
        if name == "status":
            self.engine = EngineStatus(payload["status"])
            self.engine_message = payload.get("message", "")
            if self.engine in (EngineStatus.STOPPED, EngineStatus.ERROR, EngineStatus.CONFLICT):
                self.bar_connected = False
        elif name == "connection":
            self.bar_connected = bool(payload.get("connected"))
        elif name == "display":
            self.display = dict(payload.get("state") or {"status": "sleeping"})
        elif name == "hook":
            self.last_event = describe_hook(payload.get("msg") or {})
            self.last_event_time = now

    # --- what to show -----------------------------------------------------

    @property
    def bar_ok(self) -> bool:
        """The bar is reachable. With BLE switched off (developer flag) there is no
        bar to wait for, so the icon shows the connected picture regardless."""
        return self.bar_connected or not self.ble_enabled

    def icon_name(self) -> str:
        """Which of the three tray pictures to show. Depends on the engine and the
        bar link only: the display state, the sessions and the hooks never
        change it."""
        if self.engine in (EngineStatus.CONFLICT, EngineStatus.ERROR):
            return ICON_ERROR
        if self.engine != EngineStatus.RUNNING or not self.bar_ok:
            return ICON_DISCONNECTED
        return ICON_CONNECTED

    @property
    def sessions(self) -> int:
        """Claude Code sessions the daemon is tracking (shown ones plus overflow)."""
        anims = self.display.get("anims") or []
        return len(anims) + int(self.display.get("overflow", 0) or 0)

    def bar_text(self) -> str:
        if not self.ble_enabled:
            return "Bluetooth off (test mode)"
        if self.engine in (EngineStatus.CONFLICT, EngineStatus.ERROR, EngineStatus.STOPPED):
            return "Disconnected"
        if self.bar_connected:
            return "Connected"
        return "Scanning..."

    def sessions_text(self) -> str:
        n = self.sessions
        if n == 0:
            return "no active session"
        return f"{n} active session" + ("" if n == 1 else "s")

    def last_event_text(self) -> str:
        if not self.last_event or self.last_event_time is None:
            return "no event yet"
        stamp = time.strftime("%H:%M:%S", time.localtime(self.last_event_time))
        return f"{self.last_event}, {stamp}"

    def status_text(self) -> str:
        """What is going on, in words: the tray menu's status line."""
        if self.engine == EngineStatus.CONFLICT:
            return CONFLICT_MESSAGE
        if self.engine == EngineStatus.ERROR:
            return "The engine stopped, restarting"
        if self.engine != EngineStatus.RUNNING:
            return "Starting..."
        if not self.ble_enabled:
            return "Bluetooth off (test mode)"
        if self.bar_connected:
            return "Connected to Clawd Tank"
        return "Scanning for the bar..."

    def problem(self) -> str:
        """A line for the window's red message area; empty when all is well."""
        if self.engine == EngineStatus.CONFLICT:
            return self.engine_message or CONFLICT_MESSAGE
        if self.engine == EngineStatus.ERROR:
            return self.engine_message or "The engine stopped and is being restarted."
        return ""

    @property
    def can_retry(self) -> bool:
        return self.engine in (EngineStatus.CONFLICT, EngineStatus.ERROR)

    def tooltip(self) -> str:
        """The tray icon's tooltip (Windows cuts it at 127 characters). It follows
        the icon: it changes when the connection does, not when Claude does, so
        the notification area is only touched on a real change."""
        return f"Clawd Tank: {self.status_text()}"[:127]
