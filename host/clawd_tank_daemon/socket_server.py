"""Socket server that receives hook messages from clawd-tank-notify.

Listens on a Unix socket (macOS, Linux: upstream behaviour) or on TCP at
127.0.0.1 (Windows, or ``CLAWD_TANK_TRANSPORT=tcp``). The message handling is
the same on both: see ``hook_transport`` for how the endpoint is chosen.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import hook_transport
from .hook_transport import TCP_HOST

logger = logging.getLogger("clawd-tank.socket")

SOCKET_PATH = hook_transport.DEFAULT_SOCKET_PATH


class SocketServer:
    """Listens for JSON messages from clawd-tank-notify.

    ``transport`` is ``"unix"``, ``"tcp"`` or None (``CLAWD_TANK_TRANSPORT``,
    else auto: unix where available, tcp elsewhere). ``socket_path`` and
    ``tcp_port`` default to ``CLAWD_TANK_SOCKET`` / ``CLAWD_TANK_TCP_PORT``.
    """

    def __init__(self, on_message: Callable[[dict], Awaitable[None]],
                 socket_path: Optional[Path] = None,
                 transport: Optional[str] = None,
                 tcp_port: Optional[int] = None):
        self._on_message = on_message
        self._transport = hook_transport.resolve_transport(transport)
        self._socket_path = hook_transport.resolve_socket_path(socket_path)
        self._tcp_port = hook_transport.resolve_tcp_port(tcp_port)
        self._server: asyncio.Server | None = None

    @property
    def transport(self) -> str:
        return self._transport

    @property
    def tcp_port(self) -> int:
        return self._tcp_port

    @property
    def endpoint(self) -> str:
        return hook_transport.describe(self._transport, self._socket_path, self._tcp_port)

    async def start(self) -> None:
        if self._transport == "tcp":
            try:
                self._server = await asyncio.start_server(
                    self._handle_client, TCP_HOST, self._tcp_port
                )
            except OSError as e:
                raise OSError(
                    e.errno,
                    f"Cannot listen on {TCP_HOST}:{self._tcp_port} ({e.strerror or e}). "
                    f"Another clawd-tank daemon, or another program, holds the port: "
                    f"stop it, or set {hook_transport.ENV_TCP_PORT} (or --tcp-port) "
                    f"to a free port for both the daemon and the hooks.",
                ) from e
            logger.info("Listening on %s (TCP, localhost only)", self.endpoint)
            return

        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            self._socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        # Make socket writable by owner
        os.chmod(self._socket_path, 0o600)
        logger.info("Listening on %s", self._socket_path)

    async def _handle_client(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        # Messages are newline-delimited JSON. readline() gives a clean
        # message boundary regardless of TCP/socket buffering.
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if line:
                try:
                    msg = json.loads(line.decode("utf-8"))
                    await self._on_message(msg)
                except json.JSONDecodeError:
                    logger.error("Received malformed JSON: %r", line[:200])
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Timed out waiting for message from client")
        except Exception:
            logger.exception("Unexpected error handling socket message")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass  # peer already gone; nothing to flush

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._transport == "unix" and self._socket_path.exists():
            self._socket_path.unlink()
