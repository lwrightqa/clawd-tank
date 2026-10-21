"""Tests for SocketServer: concurrent connections, malformed JSON, clean shutdown.

Every test runs over each hook transport the platform has: a Unix socket
(macOS, Linux; skipped on Windows, which has none) and TCP on 127.0.0.1.
"""

import asyncio
import json
import socket
import tempfile
from pathlib import Path

import pytest
from clawd_tank_daemon import hook_transport
from clawd_tank_daemon.socket_server import SocketServer

HAS_UNIX = hook_transport.unix_sockets_available()


def free_port() -> int:
    """A currently unused localhost TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Endpoint:
    """A server endpoint plus a way to send it one raw line."""

    def __init__(self, transport: str, sock_path: Path, port: int):
        self.transport = transport
        self.sock_path = sock_path
        self.port = port

    def server(self, on_message) -> SocketServer:
        return SocketServer(
            on_message=on_message,
            socket_path=self.sock_path,
            transport=self.transport,
            tcp_port=self.port,
        )

    async def send_raw(self, data: bytes) -> None:
        """Send data to the server, appending a newline delimiter."""
        if self.transport == "unix":
            reader, writer = await asyncio.open_unix_connection(str(self.sock_path))
        else:
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(data + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    def is_listening(self) -> bool:
        if self.transport == "unix":
            return self.sock_path.exists()
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
            return True
        except OSError:
            return False


@pytest.fixture(params=[
    pytest.param("unix", marks=pytest.mark.skipif(
        not HAS_UNIX, reason="Unix domain sockets are not available on this platform")),
    "tcp",
])
def endpoint(request):
    # A short path under the system temp dir: AF_UNIX paths are limited to
    # about 104 bytes on macOS, which pytest's tmp_path can exceed.
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Endpoint(request.param, Path(tmpdir) / "test.sock", free_port())


@pytest.mark.asyncio
async def test_socket_server_receives_message(endpoint):
    """Server must deliver a well-formed JSON message to the callback."""
    received: list[dict] = []

    async def on_message(msg: dict) -> None:
        received.append(msg)

    server = endpoint.server(on_message)
    await server.start()

    payload = {"event": "add", "session_id": "s1", "project": "p", "message": "m"}
    await endpoint.send_raw(json.dumps(payload).encode())
    await asyncio.sleep(0.05)  # allow handler coroutine to run

    assert len(received) == 1
    assert received[0]["event"] == "add"
    assert received[0]["session_id"] == "s1"

    await server.stop()


@pytest.mark.asyncio
async def test_socket_server_concurrent_connections(endpoint):
    """Multiple simultaneous connections must each deliver their message."""
    received: list[dict] = []

    async def on_message(msg: dict) -> None:
        received.append(msg)

    server = endpoint.server(on_message)
    await server.start()

    messages = [
        {"event": "add", "session_id": f"s{i}", "project": "p", "message": "m"}
        for i in range(5)
    ]
    await asyncio.gather(*[
        endpoint.send_raw(json.dumps(m).encode()) for m in messages
    ])
    await asyncio.sleep(0.1)

    assert len(received) == 5
    session_ids = {r["session_id"] for r in received}
    assert session_ids == {"s0", "s1", "s2", "s3", "s4"}

    await server.stop()


@pytest.mark.asyncio
async def test_socket_server_malformed_json_does_not_crash(endpoint):
    """Invalid JSON must be silently absorbed — server must keep running."""
    received: list[dict] = []

    async def on_message(msg: dict) -> None:
        received.append(msg)

    server = endpoint.server(on_message)
    await server.start()

    # Send garbage
    await endpoint.send_raw(b"not json {{{{")
    await asyncio.sleep(0.05)

    # Server must still accept a subsequent valid message
    payload = {"event": "dismiss", "session_id": "s1"}
    await endpoint.send_raw(json.dumps(payload).encode())
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0]["event"] == "dismiss"

    await server.stop()


@pytest.mark.asyncio
async def test_socket_server_stop_releases_endpoint(endpoint):
    """stop() must remove the socket file (unix) / free the port (tcp)."""
    async def on_message(msg: dict) -> None:
        pass

    server = endpoint.server(on_message)
    await server.start()
    assert endpoint.is_listening()

    await server.stop()
    assert not endpoint.is_listening()


# --- TCP-specific behaviour ---------------------------------------------------


@pytest.mark.asyncio
async def test_tcp_server_listens_on_loopback_only():
    """The hook listener must not be reachable from the network."""
    async def on_message(msg: dict) -> None:
        pass

    port = free_port()
    server = SocketServer(on_message=on_message, transport="tcp", tcp_port=port)
    await server.start()
    try:
        bound = {s.getsockname()[0] for s in server._server.sockets}
        assert bound == {"127.0.0.1"}
        assert server.transport == "tcp"
        assert server.endpoint == f"tcp://127.0.0.1:{port}"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_tcp_server_port_in_use_explains_what_to_do():
    async def on_message(msg: dict) -> None:
        pass

    first = SocketServer(on_message=on_message, transport="tcp", tcp_port=free_port())
    await first.start()
    taken = first.tcp_port
    try:
        second = SocketServer(on_message=on_message, transport="tcp", tcp_port=taken)
        with pytest.raises(OSError) as exc:
            await second.start()
        text = str(exc.value)
        assert str(taken) in text
        assert "CLAWD_TANK_TCP_PORT" in text
    finally:
        await first.stop()


@pytest.mark.asyncio
async def test_tcp_server_handles_client_that_sends_nothing():
    """A connection that never sends a line is dropped after the read timeout
    without delivering anything or taking the server down."""
    received: list[dict] = []

    async def on_message(msg: dict) -> None:
        received.append(msg)

    port = free_port()
    server = SocketServer(on_message=on_message, transport="tcp", tcp_port=port)
    await server.start()
    try:
        # Connect and close at once: readline() returns b"" -> no message.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)
        assert received == []

        # Still serving.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b'{"event": "clear"}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)
        assert received == [{"event": "clear"}]
    finally:
        await server.stop()


def test_server_defaults_follow_the_platform(monkeypatch):
    """With nothing configured: unix where available, tcp otherwise."""
    for var in (hook_transport.ENV_TRANSPORT, hook_transport.ENV_TCP_PORT,
                hook_transport.ENV_SOCKET):
        monkeypatch.delenv(var, raising=False)

    async def on_message(msg: dict) -> None:
        pass

    server = SocketServer(on_message=on_message)
    assert server.transport == ("unix" if HAS_UNIX else "tcp")
    assert server.tcp_port == hook_transport.TCP_DEFAULT_PORT
