"""Tests for hook_transport: how the daemon and the notify scripts agree on an endpoint."""

import socket
import time
from pathlib import Path

import pytest

from clawd_tank_daemon import hook_transport as ht


def test_default_port_is_fixed_and_documented():
    assert ht.TCP_DEFAULT_PORT == 47421
    assert ht.TCP_HOST == "127.0.0.1"


# --- resolve_transport -------------------------------------------------------


def test_auto_is_unix_where_available(monkeypatch):
    monkeypatch.setattr(ht, "unix_sockets_available", lambda: True)
    assert ht.resolve_transport(env={}) == "unix"
    assert ht.resolve_transport("auto", env={}) == "unix"


def test_auto_is_tcp_where_unix_sockets_are_missing(monkeypatch):
    """This is the Windows case."""
    monkeypatch.setattr(ht, "unix_sockets_available", lambda: False)
    assert ht.resolve_transport(env={}) == "tcp"
    assert ht.resolve_transport("auto", env={}) == "tcp"


def test_tcp_can_be_selected_anywhere(monkeypatch):
    monkeypatch.setattr(ht, "unix_sockets_available", lambda: True)
    assert ht.resolve_transport(env={ht.ENV_TRANSPORT: "tcp"}) == "tcp"
    assert ht.resolve_transport("tcp", env={}) == "tcp"


def test_flag_beats_environment(monkeypatch):
    monkeypatch.setattr(ht, "unix_sockets_available", lambda: True)
    assert ht.resolve_transport("unix", env={ht.ENV_TRANSPORT: "tcp"}) == "unix"
    assert ht.resolve_transport("tcp", env={ht.ENV_TRANSPORT: "unix"}) == "tcp"


def test_value_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setattr(ht, "unix_sockets_available", lambda: True)
    assert ht.resolve_transport(env={ht.ENV_TRANSPORT: "  TCP "}) == "tcp"


def test_unix_requested_without_unix_sockets_is_an_error(monkeypatch):
    monkeypatch.setattr(ht, "unix_sockets_available", lambda: False)
    with pytest.raises(ValueError, match="Unix sockets are not available"):
        ht.resolve_transport("unix", env={})
    with pytest.raises(ValueError):
        ht.resolve_transport(env={ht.ENV_TRANSPORT: "unix"})


def test_unknown_transport_is_an_error():
    with pytest.raises(ValueError, match=ht.ENV_TRANSPORT):
        ht.resolve_transport(env={ht.ENV_TRANSPORT: "carrier-pigeon"})


def test_unix_sockets_available_matches_the_stdlib():
    import asyncio
    expected = hasattr(socket, "AF_UNIX") and hasattr(asyncio, "start_unix_server")
    assert ht.unix_sockets_available() is expected


# --- resolve_tcp_port ---------------------------------------------------------


def test_port_defaults():
    assert ht.resolve_tcp_port(env={}) == ht.TCP_DEFAULT_PORT
    assert ht.resolve_tcp_port(env={ht.ENV_TCP_PORT: ""}) == ht.TCP_DEFAULT_PORT


def test_port_from_environment_and_flag():
    assert ht.resolve_tcp_port(env={ht.ENV_TCP_PORT: "50123"}) == 50123
    assert ht.resolve_tcp_port(50555, env={ht.ENV_TCP_PORT: "50123"}) == 50555


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "65536", "12.5"])
def test_bad_port_is_an_error_naming_the_variable(bad):
    with pytest.raises(ValueError, match=ht.ENV_TCP_PORT):
        ht.resolve_tcp_port(env={ht.ENV_TCP_PORT: bad})


# --- resolve_socket_path ------------------------------------------------------


def test_socket_path_default_and_override(tmp_path):
    assert ht.resolve_socket_path(env={}) == ht.DEFAULT_SOCKET_PATH
    assert ht.DEFAULT_SOCKET_PATH == Path.home() / ".clawd-tank" / "sock"
    assert ht.resolve_socket_path(env={ht.ENV_SOCKET: str(tmp_path / "x")}) == tmp_path / "x"
    assert ht.resolve_socket_path(tmp_path / "y", env={ht.ENV_SOCKET: "ignored"}) == tmp_path / "y"


# --- connect (blocking client side) --------------------------------------------


def test_tcp_connect_reaches_a_listener_and_refuses_cleanly_when_down():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        client = ht.connect("tcp", Path("unused"), port, timeout=2.0)
        conn, _ = srv.accept()
        client.sendall(b"hello\n")
        client.close()
        assert conn.recv(16) == b"hello\n"
        conn.close()
    finally:
        srv.close()
    # Nobody listening any more. Windows takes ~2 s to report a refused loopback
    # connect; the short connect timeout must cut that down, because a hook runs
    # before every tool call and would otherwise stall Claude Code while the
    # daemon is down.
    start = time.monotonic()
    with pytest.raises(OSError):
        ht.connect("tcp", Path("unused"), port, timeout=2.0)
    assert time.monotonic() - start < 1.5


def test_describe():
    assert ht.describe("tcp", Path("/x"), 4242) == "tcp://127.0.0.1:4242"
    assert ht.describe("unix", Path("/x/sock"), 4242).startswith("unix:")
