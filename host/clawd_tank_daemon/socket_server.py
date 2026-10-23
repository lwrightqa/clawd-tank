"""Unix socket server that receives hook messages from clawd-tank-notify."""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable, Awaitable

logger = logging.getLogger("clawd-tank.socket")

SOCKET_PATH = Path.home() / ".clawd-tank" / "sock"


class SocketServer:
    """Listens on a Unix socket for JSON messages from clawd-tank-notify."""

    def __init__(self, on_message: Callable[[dict], Awaitable[None]],
                 socket_path: Path = SOCKET_PATH):
        self._on_message = on_message
        self._socket_path = socket_path
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
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
        except TimeoutError:
            logger.warning("Timed out waiting for message from client")
        except Exception:
            logger.exception("Unexpected error handling socket message")
        finally:
            writer.close()
            await writer.wait_closed()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._socket_path.exists():
            self._socket_path.unlink()
