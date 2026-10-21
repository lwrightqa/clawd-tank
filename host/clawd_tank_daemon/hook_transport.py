"""Where Claude Code hooks reach the daemon: a Unix socket, or TCP on localhost.

Upstream clawd-tank talks to the daemon over a Unix domain socket at
``~/.clawd-tank/sock``. Windows has no Unix sockets (neither ``socket.AF_UNIX``
nor ``asyncio.start_unix_server`` exist there), so on Windows the same
newline-delimited JSON goes over TCP on 127.0.0.1 instead. Three environment
variables select and configure the endpoint. The daemon, the repo's
``clawd-tank-notify`` and the standalone notify script written by the hook
installer all read them the same way, so the two sides always agree:

  CLAWD_TANK_TRANSPORT  ``unix`` | ``tcp`` | ``auto`` (default). ``auto`` is
                        ``unix`` where Unix sockets exist (macOS, Linux) and
                        ``tcp`` elsewhere (Windows).
  CLAWD_TANK_TCP_PORT   TCP port on 127.0.0.1 (default 47421).
  CLAWD_TANK_SOCKET     Unix socket path (default ``~/.clawd-tank/sock``).

The wire protocol is identical on both transports: one JSON object, one
trailing newline, then the client closes.
"""

import asyncio
import os
import socket
from pathlib import Path
from typing import Mapping, Optional

TCP_HOST = "127.0.0.1"
TCP_DEFAULT_PORT = 47421
# How long a hook waits to *connect* over TCP. A live loopback listener accepts
# in well under a millisecond (the kernel completes the handshake before the
# daemon calls accept()). Windows, however, takes about two seconds to report a
# refused loopback connection (it retries the SYN), and a hook runs before every
# tool call, so with the daemon down an unbounded wait would stall Claude Code
# by two seconds per call. Failing after this long makes "daemon not running"
# cost a fraction of a second. The standalone notify script uses the same value.
TCP_CONNECT_TIMEOUT_SECS = 0.3
DEFAULT_SOCKET_PATH = Path.home() / ".clawd-tank" / "sock"

ENV_TRANSPORT = "CLAWD_TANK_TRANSPORT"
ENV_TCP_PORT = "CLAWD_TANK_TCP_PORT"
ENV_SOCKET = "CLAWD_TANK_SOCKET"

TRANSPORTS = ("auto", "unix", "tcp")


def unix_sockets_available() -> bool:
    """True where both the socket module and asyncio support Unix sockets."""
    return hasattr(socket, "AF_UNIX") and hasattr(asyncio, "start_unix_server")


def resolve_transport(preference: Optional[str] = None,
                      env: Optional[Mapping[str, str]] = None) -> str:
    """Return ``"unix"`` or ``"tcp"``.

    ``preference`` (a CLI flag) wins over ``CLAWD_TANK_TRANSPORT``, which wins
    over ``auto``. Raises ValueError for an unknown value, or for ``unix`` on a
    platform without Unix sockets.
    """
    env = os.environ if env is None else env
    pref = (preference or env.get(ENV_TRANSPORT) or "auto").strip().lower()
    if pref not in TRANSPORTS:
        raise ValueError(
            f"{ENV_TRANSPORT} must be one of {', '.join(TRANSPORTS)}, not {pref!r}"
        )
    if pref == "auto":
        return "unix" if unix_sockets_available() else "tcp"
    if pref == "unix" and not unix_sockets_available():
        raise ValueError(
            "Unix sockets are not available on this platform; use the tcp transport"
        )
    return pref


def resolve_tcp_port(override: Optional[int] = None,
                     env: Optional[Mapping[str, str]] = None) -> int:
    """TCP port: ``override`` (CLI), else ``CLAWD_TANK_TCP_PORT``, else the default."""
    env = os.environ if env is None else env
    raw = override if override is not None else env.get(ENV_TCP_PORT)
    if raw is None or raw == "":
        return TCP_DEFAULT_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{ENV_TCP_PORT} must be a port number, not {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{ENV_TCP_PORT} must be between 1 and 65535, not {port}")
    return port


def resolve_socket_path(override: Optional[Path] = None,
                        env: Optional[Mapping[str, str]] = None) -> Path:
    """Unix socket path: ``override``, else ``CLAWD_TANK_SOCKET``, else the default."""
    env = os.environ if env is None else env
    if override is not None:
        return Path(override)
    raw = env.get(ENV_SOCKET)
    return Path(raw) if raw else DEFAULT_SOCKET_PATH


def describe(transport: str, socket_path: Path, tcp_port: int) -> str:
    """Human-readable endpoint for logs and error messages."""
    if transport == "tcp":
        return f"tcp://{TCP_HOST}:{tcp_port}"
    return f"unix:{socket_path}"


def connect(transport: str, socket_path: Path, tcp_port: int,
            timeout: float = 3.0,
            connect_timeout: float = TCP_CONNECT_TIMEOUT_SECS) -> socket.socket:
    """Open a blocking client connection to the daemon's hook endpoint.

    `timeout` bounds the send; for TCP, `connect_timeout` (much shorter, see
    TCP_CONNECT_TIMEOUT_SECS) bounds the connect itself. Raises OSError
    (ConnectionRefusedError, FileNotFoundError, socket.timeout, ...) when the
    daemon is not listening. The caller sends one JSON line and closes the socket.
    """
    if transport == "tcp":
        sock = socket.create_connection((TCP_HOST, tcp_port),
                                        timeout=min(connect_timeout, timeout))
        sock.settimeout(timeout)
        return sock
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
    except BaseException:
        sock.close()
        raise
    return sock
