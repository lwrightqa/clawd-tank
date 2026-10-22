"""What the app shows, derived from what the daemon says. No GUI, no threads.

The engine thread reports daemon events; the UI thread folds them into one
ViewState, and everything on screen (tray sprite, tooltip, window lines) is a
function of that object. That keeps the mapping testable on its own.
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

# What the daemon calls an animation -> the sprite that shows it.
ANIM_SPRITES = {
    "idle": "idle", "thinking": "thinking", "typing": "typing", "debugger": "debugger",
    "building": "building", "conducting": "conducting", "wizard": "wizard",
    "beacon": "beacon", "confused": "confused", "sweeping": "sweeping",
    "happy": "happy", "alert": "alert", "dizzy": "dizzy",
}
# With several sessions the one icon shows the most important animation:
# somebody needs you, then something is wrong, then the busiest.
ANIM_PRIORITY = ("alert", "dizzy", "confused", "sweeping", "conducting", "building",
                 "typing", "debugger", "wizard", "beacon", "thinking", "happy", "idle")
ONESHOT_SECONDS = 3.0

CAPTIONS = {
    "idle": "Idle", "sleeping": "Asleep: no Claude Code session", "thinking": "Thinking",
    "typing": "Writing code", "debugger": "Reading", "building": "Running a command",
    "conducting": "Running subagents", "wizard": "Searching the web",
    "beacon": "Using a tool", "confused": "Something went wrong", "sweeping": "Compacting",
    "happy": "Done", "alert": "Needs you", "dizzy": "Error",
    "grey": "Looking for the bar", "error": "Not running",
}


def pick_anim(anims) -> str:
    """The animation the single icon shows for a list of per-session animations."""
    names = [a for a in anims if isinstance(a, str)]
    for wanted in ANIM_PRIORITY:
        if wanted in names:
            return wanted
    return "idle"


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
    oneshot: str = ""
    oneshot_until: float = 0.0
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
        elif name == "oneshot":
            anim = payload.get("anim", "")
            if anim in ANIM_SPRITES:
                self.oneshot = anim
                self.oneshot_until = now + ONESHOT_SECONDS
        elif name == "hook":
            self.last_event = describe_hook(payload.get("msg") or {})
            self.last_event_time = now

    # --- what to show -----------------------------------------------------

    @property
    def bar_ok(self) -> bool:
        """The bar is reachable. With BLE switched off (developer flag) there is no
        bar to wait for, so the icon shows what Claude is doing regardless."""
        return self.bar_connected or not self.ble_enabled

    def sprite(self, now: Optional[float] = None) -> str:
        """Which sprite the tray icon and the window show right now."""
        now = time.time() if now is None else now
        if self.engine in (EngineStatus.CONFLICT, EngineStatus.ERROR):
            return "error"
        if self.engine != EngineStatus.RUNNING or not self.bar_ok:
            return "grey"
        if self.oneshot and now < self.oneshot_until:
            return ANIM_SPRITES[self.oneshot]
        anims = self.display.get("anims")
        if anims:
            return ANIM_SPRITES.get(pick_anim(anims), "idle")
        return "sleeping"

    def oneshot_active(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.oneshot) and now < self.oneshot_until

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

    def caption(self, now: Optional[float] = None) -> str:
        return CAPTIONS.get(self.sprite(now), "")

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

    def tooltip(self, now: Optional[float] = None) -> str:
        """The tray icon's tooltip (Windows cuts it at 127 characters)."""
        if self.engine == EngineStatus.CONFLICT:
            text = f"Clawd Tank: {CONFLICT_MESSAGE}"
        elif self.engine == EngineStatus.ERROR:
            text = "Clawd Tank: the engine stopped, restarting"
        else:
            text = f"Clawd Tank: bar {self.bar_text().lower()}"
            if self.engine == EngineStatus.RUNNING and self.bar_ok:
                text += f", {self.caption(now).lower()}"
        return text[:127]
