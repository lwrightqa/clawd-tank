"""BLE client behaviour that differs by platform: the WinRT client arguments, a failing
scan, and a late disconnect callback from a connection that has already been replaced."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clawd_tank_daemon import ble_client as bc
from clawd_tank_daemon.ble_client import ClawdBleClient


def test_winrt_client_asks_windows_to_rediscover_services():
    """The bar is reflashed during development; a cached service table would hide new characteristics."""
    with patch.object(bc, "sys", SimpleNamespace(platform="win32")):
        assert bc._platform_client_kwargs() == {"winrt": {"use_cached_services": False}}


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_other_platforms_pass_nothing_extra(platform):
    with patch.object(bc, "sys", SimpleNamespace(platform=platform)):
        assert bc._platform_client_kwargs() == {}


def test_mtu_text_never_raises():
    ok = MagicMock(mtu_size=256)
    assert bc._mtu_text(ok) == "256"
    broken = MagicMock()
    type(broken).mtu_size = property(lambda self: (_ for _ in ()).throw(AssertionError("no session")))
    assert bc._mtu_text(broken) == "unknown"


def _scanner_returning(*results):
    """A BleakScanner stand-in whose find_device_by_name yields each result in turn
    (an Exception instance is raised)."""
    it = iter(results)

    async def find(name, timeout):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    return SimpleNamespace(find_device_by_name=find)


@pytest.mark.asyncio
async def test_connect_survives_a_failing_scan_and_then_connects(monkeypatch, caplog):
    """Bluetooth turned off (or the service not up) makes the scan raise; the client must
    keep retrying so that turning it on is enough."""
    monkeypatch.setattr(bc, "SCAN_INTERVAL_SECS", 0.01)
    device = SimpleNamespace(name="Clawd Tank", address="80:45:6B:35:D1:1E")
    monkeypatch.setattr(bc, "BleakScanner", _scanner_returning(OSError("radio is off"), None, device))

    created = []

    class FakeClient:
        mtu_size = 256
        is_connected = True

        def __init__(self, dev, disconnected_callback=None, **kwargs):
            created.append((dev, kwargs))

        async def connect(self):
            return None

    monkeypatch.setattr(bc, "BleakClient", FakeClient)
    connects = []
    client = ClawdBleClient(on_connect_cb=lambda: connects.append(1))
    with caplog.at_level(logging.INFO, logger="clawd-tank.ble"):
        await asyncio.wait_for(client.connect(), timeout=5)

    assert connects == [1] and client.is_connected
    assert created[0][0] is device
    text = caplog.text
    assert "Bluetooth scan failed: radio is off" in text and "Is Bluetooth turned on" in text
    assert "Found Clawd Tank: Clawd Tank (80:45:6B:35:D1:1E)" in text
    assert "Connected to Clawd Tank (MTU: 256)" in text


@pytest.mark.asyncio
async def test_connect_passes_platform_kwargs_to_bleak(monkeypatch):
    monkeypatch.setattr(bc, "SCAN_INTERVAL_SECS", 0.01)
    monkeypatch.setattr(bc, "BleakScanner", _scanner_returning(SimpleNamespace(name="Clawd Tank", address="a")))
    seen = {}

    class FakeClient:
        mtu_size = 23
        is_connected = True

        def __init__(self, dev, disconnected_callback=None, **kwargs):
            seen.update(kwargs)

        async def connect(self):
            return None

    monkeypatch.setattr(bc, "BleakClient", FakeClient)
    with patch.object(bc, "sys", SimpleNamespace(platform="win32")):
        await ClawdBleClient().connect()
    assert seen == {"winrt": {"use_cached_services": False}}


@pytest.mark.asyncio
async def test_a_connect_that_succeeds_is_not_failed_by_an_mtu_read_error(monkeypatch):
    monkeypatch.setattr(bc, "SCAN_INTERVAL_SECS", 0.01)
    monkeypatch.setattr(bc, "BleakScanner", _scanner_returning(SimpleNamespace(name="Clawd Tank", address="a")))

    class FakeClient:
        is_connected = True

        @property
        def mtu_size(self):
            raise AssertionError("session not ready")

        def __init__(self, dev, disconnected_callback=None, **kwargs):
            pass

        async def connect(self):
            return None

    monkeypatch.setattr(bc, "BleakClient", FakeClient)
    connects = []
    client = ClawdBleClient(on_connect_cb=lambda: connects.append(1))
    await asyncio.wait_for(client.connect(), timeout=5)
    assert connects == [1] and client.is_connected


@pytest.mark.asyncio
async def test_still_scanning_is_reported_once_a_minute_not_every_scan(monkeypatch, caplog):
    monkeypatch.setattr(bc, "SCAN_INTERVAL_SECS", 0.001)
    monkeypatch.setattr(bc, "SCAN_PROGRESS_EVERY", 5)
    device = SimpleNamespace(name="Clawd Tank", address="a")
    monkeypatch.setattr(bc, "BleakScanner", _scanner_returning(*([None] * 11), device))

    class FakeClient:
        mtu_size = 23
        is_connected = True

        def __init__(self, *a, **k):
            pass

        async def connect(self):
            return None

    monkeypatch.setattr(bc, "BleakClient", FakeClient)
    with caplog.at_level(logging.INFO, logger="clawd-tank.ble"):
        await asyncio.wait_for(ClawdBleClient().connect(), timeout=5)
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert sum(m.startswith("Scanning for Clawd Tank") for m in infos) == 1
    assert sum(m.startswith("Still scanning") for m in infos) == 2  # after 5 and 10 empty scans


@pytest.mark.asyncio
async def test_late_callback_from_an_old_connection_does_not_drop_the_new_one():
    """WinRT can fire the disconnected callback for a client that was already replaced."""
    client = ClawdBleClient()
    client._loop = asyncio.get_running_loop()
    old, new = MagicMock(name="old"), MagicMock(name="new")
    client._client = new
    client._on_disconnect(old)
    await asyncio.sleep(0.05)
    assert client._client is new


@pytest.mark.asyncio
async def test_callback_from_the_current_connection_does_drop_it():
    client = ClawdBleClient()
    client._loop = asyncio.get_running_loop()
    current = MagicMock()
    client._client = current
    seen = []
    client._on_disconnect_cb = lambda: seen.append(1)
    client._on_disconnect(current)
    await asyncio.sleep(0.05)
    assert client._client is None and seen == [1]
