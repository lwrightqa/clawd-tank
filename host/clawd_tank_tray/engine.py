"""The daemon, running inside the tray app.

``DaemonEngine`` runs clawd_tank_daemon's ``ClawdDaemon`` (the BLE transport
and the localhost hook listener) in a background thread with its own asyncio
loop. Nothing of the daemon is copied: the engine builds it, runs it, and
listens through the observer hooks the daemon offers.

* Signals stay out of the thread: the daemon is built with ``embedded=True``,
  which installs no signal handlers (only the main thread may).
* Another daemon, or another program, on the lock file or the hook port is not
  a crash: the engine goes to CONFLICT, touches nothing of the other process,
  and waits for ``retry()``.
* If the thread dies any other way the status is ERROR and ``watchdog()``
  (called by the UI every couple of seconds) starts it again, with a back-off.
* ``stop()`` runs the daemon's own shutdown, which sends the bar the "clear"
  (so the bar goes to No connection) and releases the port, the lock and the
  PID file.

Events go to the ``on_event(name, payload)`` callback, on the engine thread:
``status`` {status, message}, ``connection`` {connected, transport},
``display`` {state}, ``oneshot`` {anim}, ``hook`` {msg}, ``notifications``
{count}.
"""

import asyncio
import errno
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from clawd_tank_daemon.daemon import (
    ClawdDaemon,
    DaemonAlreadyRunning,
    _log_startup_banner,
)

from .state import CONFLICT_MESSAGE, EngineStatus

logger = logging.getLogger("clawd-tank.tray.engine")

# Winsock reports "address in use" as 10048 and "port not allowed" (another
# program holds it exclusively, or it is reserved) as 10013. Not plain EACCES:
# that would also match a file the daemon cannot write.
_PORT_TAKEN = {errno.EADDRINUSE, 10048, 10013}

STOP_TIMEOUT_SECS = 10.0
START_WAIT_SECS = 3.0
MAX_BACKOFF_SECS = 60.0
HEALTHY_AFTER_SECS = 30.0
SLOW_START_SECS = 10.0

EventCallback = Callable[[str, dict], None]


class _Observer:
    """The daemon's observer: forwards to the engine's callback. Runs on the
    engine thread, inside the daemon's event loop."""

    def __init__(self, engine: "DaemonEngine"):
        self._engine = engine

    def on_connection_change(self, connected: bool, transport: str = "") -> None:
        self._engine._emit("connection", {"connected": connected, "transport": transport})

    def on_notification_change(self, count: int) -> None:
        self._engine._emit("notifications", {"count": count})

    def on_started(self, endpoint: str) -> None:
        self._engine._on_started(endpoint)

    def on_display_state_change(self, state: dict) -> None:
        self._engine._emit("display", {"state": dict(state)})

    def on_display_oneshot(self, anim: str) -> None:
        self._engine._emit("oneshot", {"anim": anim})

    def on_hook_event(self, msg: dict) -> None:
        keep = ("event", "hook", "tool_name", "project", "session_id")
        self._engine._emit("hook", {"msg": {k: msg.get(k) for k in keep if msg.get(k)}})


class DaemonEngine:
    """Owns the daemon thread. start/stop/retry/watchdog are called from one
    thread (the UI's); the status and the events come from the engine thread."""

    def __init__(
        self,
        on_event: EventCallback,
        *,
        no_ble: bool = False,
        tcp_port: Optional[int] = None,
        transport: Optional[str] = None,
        sessions_path: Optional[Path] = None,
    ):
        self._on_event = on_event
        self.no_ble = no_ble
        self.tcp_port = tcp_port
        self.transport = transport
        self.sessions_path = sessions_path

        self._lock = threading.Lock()
        self._status = EngineStatus.STOPPED
        self._message = ""
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._daemon: Optional[ClawdDaemon] = None
        self._stopping = threading.Event()   # the user asked for a stop
        self._started = threading.Event()    # the listener of this run is up (or it failed)
        self._started_at = 0.0
        self._died_at = 0.0
        self._failures = 0
        self._slow_start_logged = False
        self.endpoint = ""

    # --- state ------------------------------------------------------------

    @property
    def status(self) -> EngineStatus:
        return self._status

    @property
    def message(self) -> str:
        return self._message

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _set_status(self, status: EngineStatus, message: str = "") -> None:
        with self._lock:
            self._status, self._message = status, message
        logger.info("Engine %s%s", status.value, f": {message}" if message else "")
        self._emit("status", {"status": status.value, "message": message})

    def _emit(self, name: str, payload: dict) -> None:
        try:
            self._on_event(name, payload)
        except Exception:
            logger.exception("Engine event callback raised for %r", name)

    # --- control (UI thread) ----------------------------------------------

    def start(self) -> bool:
        """Start the thread unless one is running. Returns True if it started one."""
        if self.is_alive:
            return False
        self._stopping.clear()
        self._started.clear()
        self._slow_start_logged = False
        self._started_at = time.monotonic()
        self._set_status(EngineStatus.STARTING)
        self._thread = threading.Thread(target=self._thread_main, name="clawd-engine",
                                        daemon=True)
        self._thread.start()
        return True

    def retry(self) -> bool:
        """The Retry button: start again after a conflict or a failure, now."""
        self._failures = 0
        return self.start()

    def stop(self, timeout: float = STOP_TIMEOUT_SECS) -> bool:
        """Shut the daemon down cleanly and join the thread. Returns True if the
        thread has ended. Safe to call when nothing is running."""
        self._stopping.set()
        thread = self._thread
        if thread is None or not thread.is_alive():
            if self._status != EngineStatus.STOPPED:
                self._set_status(EngineStatus.STOPPED)
            return True
        # A shutdown that lands while the listener is still binding would leave
        # the port open; let the start finish first (or fail) before asking.
        self._started.wait(START_WAIT_SECS)
        loop, daemon = self._loop, self._daemon
        if loop is not None and daemon is not None:
            try:
                loop.call_soon_threadsafe(daemon._request_shutdown)
            except RuntimeError:
                pass  # the loop closed in the meantime: the thread is ending anyway
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("Engine thread did not stop within %.0fs", timeout)
            return False
        self._set_status(EngineStatus.STOPPED)
        return True

    def watchdog(self, now: Optional[float] = None) -> bool:
        """Restart a dead thread. Called from the UI timer. Returns True if it
        restarted the engine. A CONFLICT is not restarted (that waits for the
        Retry button), nor is anything after stop()."""
        now = time.monotonic() if now is None else now
        if (self._status == EngineStatus.STARTING and self.is_alive and not self._slow_start_logged
                and now - self._started_at > SLOW_START_SECS):
            self._slow_start_logged = True
            logger.warning("The engine has been starting for %.0fs (the listener is not up yet)",
                           now - self._started_at)
        if self._stopping.is_set() or self._thread is None or self.is_alive:
            return False
        if self._status == EngineStatus.CONFLICT:
            return False
        delay = min(MAX_BACKOFF_SECS, 2.0 ** min(self._failures, 5))
        if now - self._died_at < delay:
            return False
        self._failures += 1
        logger.warning("Engine thread was not running; restarting (attempt %d)", self._failures)
        return self.start()

    # --- the thread ---------------------------------------------------------

    def _on_started(self, endpoint: str) -> None:
        self.endpoint = endpoint
        daemon = self._daemon
        restored = 0
        if daemon is not None:
            state = daemon.display_state()
            restored = len(state.get("anims") or []) + int(state.get("overflow", 0) or 0)
            self._emit("display", {"state": state})
        logger.info("Engine started: hooks reach the daemon at %s, Bluetooth %s, %d open session%s kept",
                    endpoint, "off (test mode)" if self.no_ble else "on",
                    restored, "" if restored == 1 else "s")
        self._set_status(EngineStatus.RUNNING)
        self._started.set()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as e:  # noqa: BLE001 - the thread must report, not vanish
            logger.exception("Engine thread crashed")
            self._set_status(EngineStatus.ERROR, f"The engine crashed: {e}")
        finally:
            self._started.set()
            self._loop = None
            self._daemon = None
            self._died_at = time.monotonic()
            if time.monotonic() - self._started_at >= HEALTHY_AFTER_SECS:
                self._failures = 0

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            daemon = ClawdDaemon(
                observer=_Observer(self),
                headless=True,
                sim_only=self.no_ble,        # sim_only with no sim port: no transport at all
                hook_transport=self.transport,
                hook_tcp_port=self.tcp_port,
                sessions_path=self.sessions_path,
                embedded=True,
                keep_live_sessions=True,     # an open but idle session stays on the bar
            )
        except ValueError as e:              # a bad CLAWD_TANK_TCP_PORT / _TRANSPORT
            self._set_status(EngineStatus.ERROR, f"Bad setting: {e}")
            return
        self._daemon = daemon
        logger.info("Engine thread: daemon built, starting it")
        _log_startup_banner(daemon)
        try:
            await daemon.run()
        except DaemonAlreadyRunning:
            logger.warning("Another daemon holds the lock file; not starting")
            self._set_status(EngineStatus.CONFLICT, CONFLICT_MESSAGE)
            return
        except OSError as e:
            if e.errno in _PORT_TAKEN:
                logger.warning("%s", e)
                port = daemon._socket.tcp_port
                self._set_status(EngineStatus.CONFLICT,
                                 f"{CONFLICT_MESSAGE} (port {port} is in use)")
            else:
                logger.exception("Engine failed to start")
                self._set_status(EngineStatus.ERROR, f"The engine failed to start: {e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("Engine failed")
            self._set_status(EngineStatus.ERROR, f"The engine failed: {e}")
            return
        if not self._stopping.is_set():
            self._set_status(EngineStatus.ERROR, "The engine stopped unexpectedly.")
