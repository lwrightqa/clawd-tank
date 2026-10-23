"""Clawd Tank daemon — bridges Claude Code hooks to ESP32 via BLE."""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from .ble_client import ClawdBleClient
from .hook_transport import ENV_TCP_PORT, ENV_TRANSPORT, TCP_DEFAULT_PORT, TRANSPORTS
from .procutil import IS_WINDOWS, pid_alive, session_process_alive
from .protocol import (
    ASK_USER_QUESTION_TOOL,
    daemon_message_to_ble_payload,
    display_state_to_ble_payload,
    display_state_to_v1_payload,
)
from .sim_client import SimClient, SIM_DEFAULT_PORT
from .socket_server import SocketServer
from .transport import TransportClient
from . import session_store
from .session_store import save_sessions, load_sessions

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl

logger = logging.getLogger("clawd-tank")

TOOL_ANIMATION_MAP = {
    "Edit": "typing",
    "Write": "typing",
    "NotebookEdit": "typing",
    "Read": "debugger",
    "Grep": "debugger",
    "Glob": "debugger",
    "Bash": "building",
    "Agent": "conducting",
    "WebSearch": "wizard",
    "WebFetch": "wizard",
    "LSP": "beacon",
}


def _tool_to_anim(tool_name: str) -> str:
    if tool_name and tool_name.startswith("mcp__"):
        return "beacon"
    return TOOL_ANIMATION_MAP.get(tool_name, "typing")


PID_PATH = Path.home() / ".clawd-tank" / "daemon.pid"
LOCK_PATH = Path.home() / ".clawd-tank" / "daemon.lock"
PID_DEDUP_FRESHNESS_SECONDS = 60.0
# How often to actively probe BLE links for liveness. macOS CoreBluetooth often
# never reports range/sleep disconnects, so without this probe a dead link is
# never detected and the daemon never re-scans. Detection lag ~= this interval.
BLE_LIVENESS_INTERVAL_SECS = 20.0


@runtime_checkable
class DaemonObserver(Protocol):
    def on_connection_change(self, connected: bool, transport: str = "") -> None: ...
    def on_notification_change(self, count: int) -> None: ...

    # Optional extras, deliberately NOT part of this Protocol (an observer that
    # lacks them is still a DaemonObserver). The daemon calls each one only if
    # the observer has it, always on the daemon's event-loop thread, and logs
    # and swallows anything it raises:
    #
    #   on_started(endpoint: str)                  hook listener is up
    #   on_display_state_change(state: dict)       what the bar shows changed: the
    #                                              dict the daemon logs as
    #                                              "Display: anims=[...]" or
    #                                              {"status": "sleeping"}
    #   on_display_oneshot(anim: str)              a one-shot animation ("sweeping"
    #                                              on /compact) played on the bar
    #   on_hook_event(msg: dict)                   a hook message was received


class DaemonAlreadyRunning(RuntimeError):
    """Another daemon holds the lock file (embedded mode raises this instead of
    exiting the process)."""


def _stop_existing_daemon() -> bool:
    """Send SIGTERM to an existing daemon and wait for it to exit. Returns True if stopped.

    On Windows ``os.kill(pid, SIGTERM)`` is TerminateProcess: abrupt, but this
    path is only taken in menu bar (takeover) mode, which Windows has not."""
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # already dead
    except PermissionError:
        return False
    except OSError:
        return not pid_alive(pid)  # Windows: no such process raises a plain OSError
    # Wait up to 3 seconds for it to release the lock
    for _ in range(30):
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    logger.warning("Existing daemon (PID %d) did not exit in time", pid)
    return False


def _try_lock(fd: int) -> None:
    """Take an exclusive, non-blocking lock on fd; raise OSError if held elsewhere.

    POSIX uses flock; Windows has no fcntl, so it locks the first byte with
    msvcrt (LK_NBLCK fails at once when another process holds it). Both are
    released by the OS when the daemon dies, so a crash never leaves a stale
    lock."""
    if IS_WINDOWS:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _acquire_lock(takeover: bool = False, raise_on_conflict: bool = False) -> int:
    """Acquire an exclusive file lock.

    If takeover is True (menu bar mode), stop the existing daemon first.
    If takeover is False (headless mode), exit if another daemon is running,
    or raise DaemonAlreadyRunning when raise_on_conflict is set (a daemon
    embedded in another program must not end that program's process).
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _try_lock(fd)
    except OSError:
        if takeover:
            logger.info("Stopping existing daemon to take over...")
            _stop_existing_daemon()
            # Retry the lock
            try:
                _try_lock(fd)
            except OSError:
                os.close(fd)
                print("Could not acquire lock after stopping existing daemon", file=sys.stderr)
                sys.exit(1)
        else:
            os.close(fd)
            if raise_on_conflict:
                raise DaemonAlreadyRunning("Another clawd-tank daemon is already running")
            print("Another clawd-tank daemon is already running", file=sys.stderr)
            sys.exit(0)
    return fd


def _shutdown_signals() -> list:
    sigs = [signal.SIGTERM, signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl+Break, and console close
        sigs.append(signal.SIGBREAK)
    return sigs


class ClawdDaemon:
    def __init__(
        self,
        observer: Optional["DaemonObserver"] = None,
        headless: bool = True,
        sim_port: int = 0,
        sim_only: bool = False,
        sessions_path: Optional[Path] = None,
        hook_transport: Optional[str] = None,
        hook_tcp_port: Optional[int] = None,
        embedded: bool = False,
        keep_live_sessions: bool = False,
    ):
        """``hook_transport`` ("unix", "tcp" or None for auto) and
        ``hook_tcp_port`` choose where clawd-tank-notify reaches this daemon;
        see hook_transport.py.

        ``embedded`` is for a daemon that runs in a thread of another program
        (the Windows tray app): run() installs no signal handlers (only the main
        thread may), and a lock held by another daemon raises DaemonAlreadyRunning
        instead of calling sys.exit().

        ``keep_live_sessions`` keeps a session for as long as its Claude Code
        process is running, however long it has been quiet. A session that is open
        but idle sends no hook events, so the 10 minute staleness timeout would drop
        it, and a bar that connects (or a tray app that starts) after that would show
        nothing until the next hook. The PID is also kept across a restart, checked
        against the process's creation time so a recycled PID does not keep a ghost.
        The PID-liveness check still drops a session whose process has gone.
        Everything else is as upstream."""
        self._transports: dict[str, TransportClient] = {}
        self._transport_queues: dict[str, asyncio.Queue] = {}

        if headless:
            # Headless (CLI) mode — create transports from args
            if not sim_only:
                ble = ClawdBleClient(
                    on_disconnect_cb=lambda: self._on_transport_disconnect("ble"),
                    on_connect_cb=lambda: self._on_transport_connect("ble"),
                )
                self._transports["ble"] = ble
                self._transport_queues["ble"] = asyncio.Queue()

            if sim_port > 0:
                sim = SimClient(
                    port=sim_port,
                    on_disconnect_cb=lambda: self._on_transport_disconnect("sim"),
                    on_connect_cb=lambda: self._on_transport_connect("sim"),
                )
                self._transports["sim"] = sim
                self._transport_queues["sim"] = asyncio.Queue()
        # Menu bar mode (headless=False): transports added later via add_transport()

        self._sender_tasks: dict[str, asyncio.Task] = {}
        self._socket = SocketServer(
            on_message=self._handle_message,
            transport=hook_transport,
            tcp_port=hook_tcp_port,
        )
        self._active_notifications: dict[str, dict] = {}
        self._running = True
        self._shutdown_event = asyncio.Event()
        self._shutdown_task: Optional[asyncio.Task] = None
        self._lock_fd: int | None = None
        self._observer = observer
        self._headless = headless
        self._embedded = embedded
        self._sessions_path = sessions_path if sessions_path is not None else session_store.SESSIONS_PATH
        self._keep_live_sessions = keep_live_sessions
        loaded_states, loaded_order, loaded_next_id = load_sessions(
            self._sessions_path, keep_pid=keep_live_sessions)

        # One-shot startup prune by wall-clock: if last_event is older than the
        # staleness timeout, the session almost certainly belongs to a dead Claude
        # Code process from before the daemon restarted. After this prune we switch
        # to monotonic time for runtime tracking (survives macOS sleep/wake).
        # With keep_live_sessions a session that has a PID is judged by its process
        # instead of by its age: kept while that same process runs, dropped if not.
        now_wall = time.time()
        stale_ids = []
        for sid, s in loaded_states.items():
            last_event = s.get("last_event", now_wall)
            if keep_live_sessions and s.get("pid") is not None:
                if not session_process_alive(s["pid"], last_event):
                    stale_ids.append(sid)
            elif now_wall - last_event > 600.0:  # default timeout
                stale_ids.append(sid)
        for sid in stale_ids:
            del loaded_states[sid]
        loaded_order = [(sid, did) for sid, did in loaded_order if sid not in stale_ids]

        now_mono = time.monotonic()
        for state in loaded_states.values():
            state["last_event_monotonic"] = now_mono
        self._session_states: dict[str, dict] = loaded_states
        self._session_order: list[tuple[str, int]] = loaded_order
        self._next_display_id: int = loaded_next_id
        self._last_display_state: dict = {"status": "sleeping"}
        self._transport_versions: dict[str, int] = {}  # transport_name → protocol version
        self._session_staleness_timeout: float = 600.0
        # _evict_stale_sessions() removed — Task 6 startup prune covers this.

    def _observe(self, name: str, *args) -> None:
        """Call an optional observer method (see DaemonObserver). A missing method
        is fine; one that raises is logged and must not stop the daemon."""
        method = getattr(self._observer, name, None) if self._observer else None
        if method is None:
            return
        try:
            method(*args)
        except Exception:
            logger.exception("Observer %s raised", name)

    async def _handle_message(self, msg: dict) -> None:
        """Handle a message from clawd-tank-notify via the socket."""
        event = msg.get("event")
        session_id = msg.get("session_id", "")
        hook = msg.get("hook", "")
        # Store project name on session if provided
        project = msg.get("project", "")
        if project and session_id and session_id in self._session_states:
            self._session_states[session_id]["project"] = project
        elif project and session_id:
            # Will be stored after _update_session_state creates the entry
            pass

        extra = ""
        if event in ("tool_use", "tool_done", "permission", "tool_failed"):
            extra = f" tool={msg.get('tool_name', '?')}"
        elif event in ("subagent_start", "subagent_stop"):
            extra = f" agent={msg.get('agent_id', '?')[:12]}"
        elif event == "add":
            extra = f" msg={msg.get('message', '')[:30]}"
        if msg.get("source"):
            extra += f" source={msg['source']}"
        if msg.get("reason"):
            extra += f" reason={msg['reason']}"
        if msg.get("pid"):
            extra += f" pid={msg['pid']}"
        session_project = ""
        if session_id:
            s = self._session_states.get(session_id)
            if s and s.get("project"):
                session_project = f" [{s['project']}]"
            elif project:
                session_project = f" [{project}]"
        logger.info("Socket msg: event=%s hook=%s session=%s%s%s",
                     event, hook, session_id[:12], session_project, extra)
        self._observe("on_hook_event", msg)

        if event == "add":
            self._active_notifications[session_id] = msg
        elif event == "dismiss":
            self._active_notifications.pop(session_id, None)

        # --- PID-based dedup: SessionStart with PID matching a recent session = /clear ---
        if event == "session_start":
            incoming_pid = msg.get("pid")
            if incoming_pid is not None:
                now_mono = time.monotonic()
                to_evict = []
                for sid, state in self._session_states.items():
                    if sid == session_id:
                        continue
                    if state.get("pid") != incoming_pid:
                        continue
                    last_mono = state.get("last_event_monotonic", now_mono)
                    if now_mono - last_mono < PID_DEDUP_FRESHNESS_SECONDS:
                        to_evict.append(sid)
                for sid in to_evict:
                    logger.info(
                        "PID dedup: evicting session %s (PID %d reused by new session %s)",
                        sid[:12], incoming_pid, session_id[:12],
                    )
                    del self._session_states[sid]
                    self._session_order = [(s, d) for s, d in self._session_order if s != sid]
                    self._active_notifications.pop(sid, None)

        changed = self._update_session_state(
            event, hook, session_id,
            msg.get("agent_id", ""), msg.get("tool_name", ""),
            pid=msg.get("pid"),
        )

        # Store project name after session state is created
        if project and session_id and session_id in self._session_states:
            self._session_states[session_id]["project"] = project

        # --- Handle compact: send sweeping oneshot ---
        if event == "compact":
            computed = self._compute_display_state()
            for name, transport in self._transports.items():
                if not transport.is_connected:
                    continue
                version = self._transport_versions.get(name, 1)
                if version >= 2 and session_id:
                    # V2: send set_sessions with the compacting session's slot as "sweeping"
                    sweeping_state = self._compute_display_state()
                    if "anims" in sweeping_state:
                        for sid, did in self._session_order:
                            if sid == session_id:
                                idx = next(
                                    (i for i, d in enumerate(sweeping_state["ids"]) if d == did),
                                    None,
                                )
                                if idx is not None:
                                    sweeping_state["anims"][idx] = "sweeping"
                                break
                    payload = display_state_to_ble_payload(sweeping_state)
                    await transport.write_notification(payload)
                    # No second fallback for v2 — firmware handles sweeping as oneshot
                else:
                    # V1: send sweeping oneshot then restore display state
                    sweeping_payload = json.dumps({"action": "set_status", "status": "sweeping"})
                    await transport.write_notification(sweeping_payload)
                    fallback_payload = display_state_to_v1_payload(computed)
                    await transport.write_notification(fallback_payload)
            self._last_display_state = computed
            self._observe("on_display_state_change", computed)
            self._observe("on_display_oneshot", "sweeping")

        for q in self._transport_queues.values():
            await q.put(msg)

        if self._observer:
            self._observer.on_notification_change(len(self._active_notifications))

        if event != "compact":
            await self._broadcast_display_state_if_changed()

        if changed:
            self._persist_sessions()

    def display_state(self) -> dict:
        """The display state the bar should show right now: ``{"status":
        "sleeping"}`` or ``{"anims": [...], "ids": [...], ...}``. Read-only; call
        it on the daemon's event-loop thread."""
        return self._compute_display_state()

    def _compute_display_state(self) -> dict:
        """Derive the display state from all active session states."""
        if not self._session_states:
            return {"status": "sleeping"}

        anims = []
        ids = []

        # Count subagents across ALL sessions, not just visible ones
        total_subagents = sum(
            len(s.get("subagents", set())) for s in self._session_states.values()
        )

        for session_id, display_id in self._session_order[:4]:
            state = self._session_states.get(session_id)
            if state is None:
                continue
            session_subagents = state.get("subagents", set())

            if state["state"] == "waiting":
                # "needs you" is the most actionable signal — outrank the subagent
                # 'conducting' indicator so a blocked session is never masked.
                anims.append("alert")
            elif session_subagents:
                anims.append("conducting")
            elif state["state"] == "working":
                anims.append(_tool_to_anim(state.get("tool_name", "")))
            elif state["state"] == "thinking":
                anims.append("thinking")
            elif state["state"] == "confused":
                anims.append("confused")
            elif state["state"] == "error":
                anims.append("dizzy")
            else:
                anims.append("idle")
            ids.append(display_id)

        if not anims:
            return {"status": "sleeping"}

        result = {"anims": anims, "ids": ids, "subagents": total_subagents}
        if len(self._session_order) > 4:
            result["overflow"] = len(self._session_order) - 4
        return result

    def _enter_state(
        self, session_id: str, state: str, tool_name: str, now: float, *, create: bool,
    ) -> None:
        """Set a session's state/tool_name/last_event. When create is False and the
        session does not exist, do nothing (avoids resurrecting an ended session)."""
        s = self._session_states.get(session_id)
        if s is None:
            if not create:
                return
            s = {"last_event": now}
            self._session_states[session_id] = s
        s["state"] = state
        s["tool_name"] = tool_name
        s["last_event"] = now

    def _update_session_state(
        self, event: str, hook: str, session_id: str,
        agent_id: str = "", tool_name: str = "", pid: Optional[int] = None,
    ) -> bool:
        """Update per-session state based on a received event.

        Returns True if session state or subagents changed structurally
        (not just last_event), indicating the change should be persisted.
        """
        if not session_id:
            return False
        now = time.time()
        now_mono = time.monotonic()
        prev = self._session_states.get(session_id)
        prev_state = prev["state"] if prev else None
        prev_subagents = prev.get("subagents", set()).copy() if prev else None

        if event == "session_start":
            self._session_states[session_id] = {"state": "registered", "last_event": now}
        elif event == "tool_use":
            # AskUserQuestion blocks on a human choice — surface it as a distinct
            # "waiting" state (alert animation), not as ordinary work. PreToolUse is a
            # session's first signal in some flows, so it may create the session.
            new_state = "waiting" if tool_name == ASK_USER_QUESTION_TOOL else "working"
            self._enter_state(session_id, new_state, tool_name, now, create=True)
        elif event == "tool_done":
            # PostToolUse (registered scoped to AskUserQuestion): the user answered,
            # so clear the waiting alert and let Claude resume thinking. Never creates
            # a session — a PostToolUse with no prior PreToolUse is ignored.
            cur_s = self._session_states.get(session_id)
            if cur_s is not None:
                if tool_name == ASK_USER_QUESTION_TOOL and cur_s["state"] == "waiting":
                    cur_s["state"] = "thinking"
                cur_s["last_event"] = now
        elif event == "permission":
            # PermissionRequest: Claude is blocked waiting for the human to approve a
            # tool. Same "needs you" semantics as AskUserQuestion → waiting/alert. No
            # "granted" hook exists, so this clears on the next tool_use/Stop/prompt.
            # PreToolUse always precedes a real permission prompt, so never create a
            # missing session — a late event must not resurrect an ended one.
            self._enter_state(session_id, "waiting", tool_name, now, create=False)
        elif event == "tool_failed":
            # PostToolUseFailure: a tool genuinely errored (not a non-zero shell exit).
            # A transient "that didn't work" snag → confused, lighter than the API-error
            # 'error' state. No notification card; never resurrects an ended session.
            self._enter_state(session_id, "confused", tool_name, now, create=False)
        elif event == "compact":
            if session_id in self._session_states:
                self._session_states[session_id]["last_event"] = now
        elif event == "add":
            self._session_states.setdefault(session_id, {"state": "idle", "last_event": now})
            if hook == "Stop":
                self._session_states[session_id]["state"] = "idle"
            elif hook == "Notification":
                self._session_states[session_id]["state"] = "confused"
            elif hook == "StopFailure":
                self._session_states[session_id]["state"] = "error"
            self._session_states[session_id]["last_event"] = now
        elif event == "dismiss":
            if hook == "SessionEnd":
                self._session_states.pop(session_id, None)
                self._session_order = [(sid, did) for sid, did in self._session_order if sid != session_id]
            elif hook == "UserPromptSubmit":
                self._session_states.setdefault(session_id, {"state": "thinking", "last_event": now})
                self._session_states[session_id]["state"] = "thinking"
                self._session_states[session_id]["last_event"] = now
            else:
                if session_id in self._session_states:
                    self._session_states[session_id]["last_event"] = now
        elif event == "subagent_start":
            if not agent_id:
                return False
            self._session_states.setdefault(session_id, {"state": "working", "last_event": now})
            self._session_states[session_id].setdefault("subagents", set())
            self._session_states[session_id]["subagents"].add(agent_id)
            self._session_states[session_id]["last_event"] = now
        elif event == "subagent_stop":
            if session_id in self._session_states:
                subagents = self._session_states[session_id].get("subagents")
                if subagents is not None:
                    subagents.discard(agent_id)
                self._session_states[session_id]["last_event"] = now

        # Track session order — append on first appearance
        cur = self._session_states.get(session_id)
        if cur is not None and session_id not in [sid for sid, _ in self._session_order]:
            self._session_order.append((session_id, self._next_display_id))
            self._next_display_id += 1

        # Stamp PID + monotonic on every event (only if session still exists —
        # SessionEnd may have just removed it).
        if cur is not None:
            if pid is not None:
                cur["pid"] = pid
            cur["last_event_monotonic"] = now_mono

        if cur is None:
            return prev is not None  # session was removed
        return cur["state"] != prev_state or cur.get("subagents", set()) != (prev_subagents or set())

    def _evict_stale_sessions(self) -> None:
        # Active subagents refresh last_event via PreToolUse on the parent session.
        # If last_event_monotonic is stale, subagents are dead too — safe to evict.
        # Uses monotonic time so macOS sleep doesn't trigger mass-eviction on wake.
        now_mono = time.monotonic()
        stale = [
            sid for sid, s in self._session_states.items()
            # 'waiting' sessions are blocked on the human and legitimately emit no
            # events; time-based eviction would drop the alert while the user is away.
            # A dead Claude process is still caught by the PID-liveness checker.
            if s.get("state") != "waiting"
            and now_mono - s.get("last_event_monotonic", now_mono) > self._session_staleness_timeout
            # An open but idle session emits nothing; its process still running is
            # the proof it is not stale. The liveness checker drops it if that dies.
            and not (self._keep_live_sessions and pid_alive(s.get("pid")))
        ]
        for sid in stale:
            logger.info("Evicting stale session: %s", sid[:12])
            del self._session_states[sid]
            self._active_notifications.pop(sid, None)
        if stale:
            self._session_order = [(sid, did) for sid, did in self._session_order if sid not in stale]
            self._persist_sessions()

    def _persist_sessions(self) -> None:
        save_sessions(
            self._session_states,
            self._sessions_path,
            order=self._session_order,
            next_id=self._next_display_id,
        )

    async def _broadcast_display_state_if_changed(self) -> None:
        """Broadcast display state to all connected transports if changed."""
        new_state = self._compute_display_state()
        if new_state == self._last_display_state:
            return
        self._last_display_state = new_state
        if "anims" in new_state:
            logger.info("Display: anims=%s subagents=%d%s",
                        new_state["anims"], new_state.get("subagents", 0),
                        f" overflow={new_state['overflow']}" if "overflow" in new_state else "")
        else:
            logger.info("Display: %s", new_state.get("status", "?"))
        self._observe("on_display_state_change", new_state)
        for name, transport in self._transports.items():
            if transport.is_connected:
                version = self._transport_versions.get(name, 1)
                if version >= 2:
                    payload = display_state_to_ble_payload(new_state)
                else:
                    payload = display_state_to_v1_payload(new_state)
                await transport.write_notification(payload)

    async def _staleness_checker(self) -> None:
        while self._running:
            await asyncio.sleep(30)
            self._evict_stale_sessions()
            await self._broadcast_display_state_if_changed()

    def _check_liveness(self) -> list[str]:
        """Synchronous half of the liveness check — separated for testability.

        Returns the list of evicted session_ids.
        """
        dead = []
        for sid, state in self._session_states.items():
            pid = state.get("pid")
            if pid is None:
                continue
            # POSIX: os.kill(pid, 0); PermissionError (another user's PID) counts
            # as alive. Windows: OpenProcess, because os.kill(pid, 0) there is
            # GenerateConsoleCtrlEvent(CTRL_C_EVENT), not a probe.
            if not pid_alive(pid):
                dead.append(sid)
        for sid in dead:
            logger.info(
                "Liveness: evicting session %s (PID %d gone)",
                sid[:12], self._session_states[sid].get("pid"),
            )
            del self._session_states[sid]
            self._session_order = [(s, d) for s, d in self._session_order if s != sid]
            self._active_notifications.pop(sid, None)
        if dead:
            self._persist_sessions()
        return dead

    async def _liveness_checker(self) -> None:
        """Async task: every 30s, evict sessions whose Claude Code PID is gone."""
        while self._running:
            await asyncio.sleep(30)
            evicted = self._check_liveness()
            if evicted:
                await self._broadcast_display_state_if_changed()

    async def _probe_ble_liveness(self) -> None:
        """One liveness sweep: round-trip probe every connected, probe-capable
        transport. A failed probe drops the client inside ping(), which flips
        is_connected to False so _transport_sender re-scans and reconnects. TCP
        transports (the simulator) detect disconnects natively and expose no
        ping(), so they are skipped via duck typing."""
        for name, transport in list(self._transports.items()):
            # A prior probe's await may have let remove_transport() run; don't
            # probe a transport that was disabled mid-sweep.
            if name not in self._transports:
                continue
            probe = getattr(transport, "ping", None)
            if probe is None or not transport.is_connected:
                continue
            try:
                alive = await probe()
                if not alive:
                    logger.warning(
                        "Transport '%s' liveness probe failed; sender will reconnect",
                        name,
                    )
                else:
                    logger.debug("Transport '%s' liveness probe ok", name)
            except Exception:
                logger.exception("Transport '%s' liveness probe raised", name)

    async def _ble_liveness_checker(self) -> None:
        """Async task: periodically probe BLE links so dead connections that
        CoreBluetooth never reported are detected and reconnected."""
        while self._running:
            await asyncio.sleep(BLE_LIVENESS_INTERVAL_SECS)
            await self._probe_ble_liveness()

    def set_session_timeout(self, seconds: int) -> None:
        self._session_staleness_timeout = float(seconds)
        logger.info("Session staleness timeout set to %ds", seconds)

    def _on_transport_connect(self, name: str) -> None:
        """Called by a transport client on successful connection."""
        logger.info("Transport '%s' connected", name)
        # Simulator always supports latest protocol; BLE defaults to v1
        if name.startswith("sim"):
            self._transport_versions[name] = 2
        if self._observer:
            self._observer.on_connection_change(True, name)

    def _on_transport_disconnect(self, name: str) -> None:
        """Called by a transport client on disconnect."""
        logger.warning("Transport '%s' disconnected", name)
        if self._observer:
            self._observer.on_connection_change(False, name)

    async def _sync_time_for(self, transport) -> None:
        """Send current host time and timezone to a transport."""
        epoch = int(time.time())
        # Build POSIX TZ string from local UTC offset
        # POSIX TZ signs are inverted: UTC+3 means 3 hours *west* of Greenwich
        utc_offset = time.localtime().tm_gmtoff  # seconds east of UTC
        sign = "-" if utc_offset >= 0 else "+"  # inverted for POSIX
        abs_offset = abs(utc_offset)
        hours, remainder = divmod(abs_offset, 3600)
        minutes = remainder // 60
        tz = f"UTC{sign}{hours}" if minutes == 0 else f"UTC{sign}{hours}:{minutes:02d}"
        payload = json.dumps({"action": "set_time", "epoch": epoch, "tz": tz})
        await transport.write_notification(payload)
        logger.info("Synced time: epoch %d, tz %s", epoch, tz)

    async def _post_connect_sync(self, transport, name: str) -> None:
        """Run time sync, version read, and replay after a (re)connection."""
        await self._sync_time_for(transport)
        if hasattr(transport, 'read_version') and callable(getattr(transport, 'read_version', None)):
            try:
                version = await transport.read_version()
                if isinstance(version, int) and version >= 1:
                    self._transport_versions[name] = version
                    logger.info("Transport '%s': protocol version %d", name, version)
            except Exception:
                self._transport_versions[name] = 1
                logger.warning("Transport '%s': version read failed, defaulting to v1", name)
        await self._replay_active_for(transport, name)

    async def _replay_active_for(self, transport, name: str = "") -> None:
        """Replay all active notifications to a transport after reconnect."""
        logger.info("Replaying %d active notifications", len(self._active_notifications))
        for msg in list(self._active_notifications.values()):
            try:
                payload = daemon_message_to_ble_payload(msg)
            except ValueError:
                continue
            if payload is None:
                continue
            await transport.write_notification(payload)
            await asyncio.sleep(0.05)

        # Send current display state
        state = self._compute_display_state()
        self._last_display_state = state
        self._observe("on_display_state_change", state)
        version = self._transport_versions.get(name, 1)
        if version >= 2:
            status_payload = display_state_to_ble_payload(state)
        else:
            status_payload = display_state_to_v1_payload(state)
        await transport.write_notification(status_payload)

    async def _transport_sender(self, name: str) -> None:
        """Process pending messages and send them over a named transport."""
        transport = self._transports[name]
        queue = self._transport_queues[name]
        # Initial connection — retries until connected
        await transport.ensure_connected()
        if transport.is_connected:
            await self._post_connect_sync(transport, name)
        while self._running:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # Proactively reconnect if transport dropped
                if not transport.is_connected:
                    await transport.ensure_connected()
                    if transport.is_connected:
                        await self._post_connect_sync(transport, name)
                continue
            try:
                payload = daemon_message_to_ble_payload(msg)
            except ValueError:
                logger.error("[%s] Skipping unknown event: %s", name, msg.get("event"))
                continue
            if payload is None:
                continue

            was_connected = transport.is_connected
            await transport.ensure_connected()
            if not was_connected and transport.is_connected:
                await self._post_connect_sync(transport, name)

            success = await transport.write_notification(payload)

            if not success:
                await transport.ensure_connected()
                if transport.is_connected:
                    await self._post_connect_sync(transport, name)

    def _write_pid(self) -> None:
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(os.getpid()))

    def _remove_pid(self) -> None:
        if PID_PATH.exists():
            PID_PATH.unlink()

    async def _shutdown(self) -> None:
        logger.info("Shutting down...")
        self._running = False
        self._shutdown_event.set()

        if hasattr(self, '_staleness_task'):
            self._staleness_task.cancel()
            try:
                await self._staleness_task
            except asyncio.CancelledError:
                pass

        if hasattr(self, '_liveness_task'):
            self._liveness_task.cancel()
            try:
                await self._liveness_task
            except asyncio.CancelledError:
                pass

        if hasattr(self, '_ble_liveness_task'):
            self._ble_liveness_task.cancel()
            try:
                await self._ble_liveness_task
            except asyncio.CancelledError:
                pass

        for task in self._sender_tasks.values():
            task.cancel()
        for task in self._sender_tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._sender_tasks.clear()

        clear_payload = daemon_message_to_ble_payload({"event": "clear"})
        for transport in self._transports.values():
            if transport.is_connected:
                await transport.write_notification(clear_payload)
            await transport.disconnect()
        await self._socket.stop()
        self._remove_pid()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    async def add_transport(self, name: str, client: TransportClient) -> None:
        """Add a transport dynamically and start its sender task."""
        client._on_connect_cb = lambda: self._on_transport_connect(name)
        client._on_disconnect_cb = lambda: self._on_transport_disconnect(name)
        self._transports[name] = client
        self._transport_queues[name] = asyncio.Queue()
        self._sender_tasks[name] = asyncio.create_task(self._transport_sender(name))

    async def remove_transport(self, name: str) -> None:
        """Stop sender task, disconnect client, and remove transport."""
        if name in self._sender_tasks:
            self._sender_tasks[name].cancel()
            try:
                await self._sender_tasks[name]
            except asyncio.CancelledError:
                pass
            del self._sender_tasks[name]
        if name in self._transports:
            client = self._transports[name]
            if client.is_connected:
                await client.disconnect()
            del self._transports[name]
        self._transport_queues.pop(name, None)
        self._on_transport_disconnect(name)

    async def read_config(self) -> dict:
        """Read config from the first connected transport."""
        for transport in self._transports.values():
            if transport.is_connected:
                return await transport.read_config()
        return {}

    async def write_config(self, payload: str) -> bool:
        """Write config to all connected transports."""
        success = False
        for transport in self._transports.values():
            if transport.is_connected:
                if await transport.write_config(payload):
                    success = True
        return success

    async def reconnect(self) -> None:
        """Force a full reconnect + post-connect sync on all transports."""
        for name, transport in self._transports.items():
            try:
                await transport.disconnect()
            except Exception:
                logger.warning("Transport '%s' disconnect failed during reconnect", name)
            try:
                await transport.ensure_connected()
                if transport.is_connected:
                    await self._post_connect_sync(transport, name)
            except Exception:
                logger.exception("Transport '%s' reconnect failed", name)

    def _request_shutdown(self) -> None:
        """Start the shutdown once; a second signal is ignored while it runs."""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.ensure_future(self._shutdown())

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """SIGTERM/SIGINT (and SIGBREAK on Windows) trigger a clean shutdown.

        asyncio's add_signal_handler is POSIX-only. On Windows the proactor
        loop still wakes up for a signal, so a plain signal.signal handler
        that posts the shutdown onto the loop does the same job: Ctrl+C in the
        daemon's console sends the "clear" payload and removes the PID file
        instead of killing the process mid-write.
        """
        def _from_signal(*_args) -> None:
            loop.call_soon_threadsafe(self._request_shutdown)

        for sig in _shutdown_signals():
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except (NotImplementedError, RuntimeError, ValueError):
                signal.signal(sig, _from_signal)

    async def run(self) -> None:
        """Main daemon loop."""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )

        self._lock_fd = _acquire_lock(
            takeover=not self._headless, raise_on_conflict=self._embedded)
        self._write_pid()

        if self._headless and not self._embedded:
            self._install_signal_handlers(asyncio.get_running_loop())

        try:
            await self._socket.start()
        except BaseException:
            # The hook port is taken (or unusable). We hold the lock and wrote
            # the PID file, so give both back: a daemon embedded in a long-lived
            # program is retried in the same process, and its own stale lock
            # would block that.
            self._remove_pid()
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            raise
        self._observe("on_started", self._socket.endpoint)

        for name in self._transports:
            # Each sender handles its own connect via ensure_connected()
            self._sender_tasks[name] = asyncio.create_task(self._transport_sender(name))

        self._staleness_task = asyncio.create_task(self._staleness_checker())
        self._liveness_task = asyncio.create_task(self._liveness_checker())
        self._ble_liveness_task = asyncio.create_task(self._ble_liveness_checker())

        await self._shutdown_event.wait()
        if self._shutdown_task is not None:
            # Let a signal-triggered shutdown finish (clear payload, disconnect,
            # PID file) before asyncio.run() tears the loop down.
            await self._shutdown_task
        logger.info("Daemon run() finished")


def _configure_logging(debug: bool, log_file: Optional[Path]) -> None:
    """Console logging, plus an appended log file when asked. Called before
    run(), whose own basicConfig is then a no-op."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
    )
    if not debug:
        # bleak's WinRT backend is chatty at DEBUG; keep it at INFO unless asked.
        logging.getLogger("bleak").setLevel(logging.INFO)


def _log_startup_banner(daemon: "ClawdDaemon") -> None:
    try:
        from importlib.metadata import version
        bleak_version = version("bleak")
    except Exception:
        bleak_version = "unknown"
    logger.info("Clawd Tank daemon starting: %s, Python %s, bleak %s",
                sys.platform, sys.version.split()[0], bleak_version)
    logger.info("Hooks reach this daemon at %s (set %s / %s to change)",
                daemon._socket.endpoint, ENV_TRANSPORT, ENV_TCP_PORT)
    logger.info("Transports: %s. Press Ctrl+C to stop.",
                ", ".join(daemon._transports) or "none")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Clawd Tank daemon")
    parser.add_argument("--sim", action="store_true",
                        help="Enable simulator transport (BLE + TCP)")
    parser.add_argument("--sim-only", action="store_true",
                        help="Simulator only (no BLE)")
    parser.add_argument("--sim-port", type=int, default=SIM_DEFAULT_PORT,
                        help=f"Simulator TCP port (default: {SIM_DEFAULT_PORT})")
    parser.add_argument("--transport", choices=TRANSPORTS, default=None,
                        help="How Claude Code hooks reach the daemon: a Unix socket, "
                             "TCP on 127.0.0.1, or auto (unix where available, tcp on "
                             f"Windows). Default: ${ENV_TRANSPORT} or auto")
    parser.add_argument("--tcp-port", type=int, default=None,
                        help=f"Port for the TCP hook listener (default: ${ENV_TCP_PORT} "
                             f"or {TCP_DEFAULT_PORT})")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="Also append log lines to this file")
    parser.add_argument("--debug", action="store_true",
                        help="Log at DEBUG level, bleak included")
    args = parser.parse_args()

    sim_port = 0
    if args.sim or args.sim_only or args.sim_port != SIM_DEFAULT_PORT:
        sim_port = args.sim_port

    _configure_logging(args.debug, args.log_file)
    try:
        daemon = ClawdDaemon(
            sim_port=sim_port, sim_only=args.sim_only,
            hook_transport=args.transport, hook_tcp_port=args.tcp_port,
        )
    except ValueError as e:
        parser.error(str(e))
    _log_startup_banner(daemon)
    try:
        asyncio.run(daemon.run())
    except OSError as e:
        # Typically the TCP hook port is taken; SocketServer.start() says what to do.
        logger.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
