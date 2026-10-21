"""BLE GATT client for communicating with the Clawd Tank ESP32 device."""

import asyncio
import json
import logging
import sys
from bleak import BleakClient, BleakScanner

logger = logging.getLogger("clawd-tank.ble")

SERVICE_UUID = "aecbefd9-98a2-4773-9fed-bb2166daa49a"
NOTIFICATION_CHR_UUID = "71ffb137-8b7a-47c9-9a7a-4b1b16662d9a"
CONFIG_CHR_UUID = "e9f6e626-5fca-4201-b80c-4d2b51c40f51"
VERSION_CHR_UUID = "b6dc9a5b-5041-4b32-9f8d-34321df8637c"
SCAN_INTERVAL_SECS = 5
# While the device is away, say so at INFO once a minute, not every scan.
SCAN_PROGRESS_EVERY = 12
# Upper bound for a single GATT read. bleak's CoreBluetooth backend has its own
# (much longer, ~20s) read timeout, but on a stale-connected dead link that long
# hold blocks every other GATT op behind _lock and delays reconnect. Fail fast.
# On Windows (bleak's WinRT backend) a read on a link Windows already knows is
# down raises "Not connected" immediately; one it has not noticed yet is bounded
# by this timeout the same way.
GATT_READ_TIMEOUT_SECS = 5.0


def _platform_client_kwargs() -> dict:
    """Extra BleakClient arguments for the platform; empty everywhere but Windows.

    WinRT caches the GATT service table of a device it has seen before. The bar
    is reflashed during development, and a stale cache makes characteristics
    "disappear" after a firmware change, so ask Windows to rediscover the
    services on every connect (costs about a second).
    """
    if sys.platform == "win32":
        return {"winrt": {"use_cached_services": False}}
    return {}


def _mtu_text(client: BleakClient) -> str:
    """The negotiated MTU for the log line; never lets a logging detail turn a
    good connection into a failed one."""
    try:
        return str(client.mtu_size)
    except Exception:
        return "unknown"


class ClawdBleClient:
    """Manages BLE connection to the Clawd Tank ESP32 device."""

    def __init__(self, on_disconnect_cb=None, on_connect_cb=None):
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_disconnect_cb = on_disconnect_cb
        self._on_connect_cb = on_connect_cb
        # Collapses the proactive drop (_handle_disconnect) and bleak's own
        # disconnected_callback into a single notification per connection.
        self._disconnect_notified = False

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        """Scan for and connect to the Clawd Tank device. Retries until found."""
        self._loop = asyncio.get_running_loop()
        if self._client is not None:
            await self.disconnect()
        attempts = 0
        while True:
            if attempts == 0:
                logger.info("Scanning for Clawd Tank device...")
            elif attempts % SCAN_PROGRESS_EVERY == 0:
                logger.info("Still scanning for Clawd Tank (%ds). Is the bar powered and "
                            "advertising?", attempts * SCAN_INTERVAL_SECS)
            else:
                logger.debug("Scanning for Clawd Tank device...")
            attempts += 1
            try:
                device = await BleakScanner.find_device_by_name(
                    "Clawd Tank", timeout=SCAN_INTERVAL_SECS
                )
            except Exception as e:
                # Radio off, no adapter, or the OS Bluetooth service down. Keep
                # retrying so turning Bluetooth on is enough to recover.
                logger.warning("Bluetooth scan failed: %s. Is Bluetooth turned on? "
                               "Retrying in %ds...", e, SCAN_INTERVAL_SECS)
                await asyncio.sleep(SCAN_INTERVAL_SECS)
                continue
            if device is None:
                logger.debug("Clawd Tank not found, retrying...")
                continue

            logger.info("Found Clawd Tank: %s (%s)", device.name, device.address)
            try:
                client = BleakClient(
                    device,
                    disconnected_callback=self._on_disconnect,
                    **_platform_client_kwargs(),
                )
                await client.connect()
                self._client = client
                self._disconnect_notified = False  # re-arm for this connection
                logger.info("Connected to Clawd Tank (MTU: %s)", _mtu_text(client))
                if self._on_connect_cb:
                    self._on_connect_cb()
                return
            except Exception as e:
                logger.warning("Connection failed: %s, retrying...", e)
                await asyncio.sleep(SCAN_INTERVAL_SECS)

    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle disconnect — may be called from a non-event-loop thread.

        bleak fires this for a lost link and, on the WinRT backend, also
        synthesises it from a manual ``disconnect()``; the one-notification
        guard in ``_notify_disconnect`` and the identity check in
        ``_clear_client`` make both cases safe.
        """
        logger.warning("Disconnected from Clawd Tank")
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._clear_client, client)
            self._loop.call_soon_threadsafe(self._notify_disconnect)
        else:
            self._clear_client(client)
            self._notify_disconnect()

    def _clear_client(self, client: BleakClient | None = None) -> None:
        """Drop the stored client, but never a newer one: a late callback from
        a previous connection must not clear the connection that replaced it."""
        if client is None or self._client is client:
            self._client = None

    def _notify_disconnect(self) -> None:
        """Fire the disconnect callback at most once per connection.

        A proactive drop (_handle_disconnect, after a write/read/probe failure)
        and bleak's own disconnected_callback (_on_disconnect) can both fire for
        the same physical disconnect. This guard collapses them so the daemon —
        and the menu bar icon — sees a single disconnect. The guard is re-armed
        on the next successful connect().
        """
        if self._disconnect_notified:
            return
        self._disconnect_notified = True
        if self._on_disconnect_cb:
            self._on_disconnect_cb()

    async def _handle_disconnect(self) -> None:
        """Force-drop the bleak client after an op failure and notify."""
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        self._notify_disconnect()

    async def ensure_connected(self) -> None:
        """Reconnect if disconnected."""
        if not self.is_connected:
            await self.connect()

    async def write_notification(self, payload: str) -> bool:
        """Write a JSON payload to the notification characteristic.

        Returns True on success, False on failure.
        """
        async with self._lock:
            if not self.is_connected:
                logger.warning("Not connected, cannot write")
                return False
            try:
                data = payload.encode("utf-8")
                await self._client.write_gatt_char(
                    NOTIFICATION_CHR_UUID, data, response=False
                )
                logger.debug("Wrote %d bytes to BLE", len(data))
                return True
            except Exception as e:
                logger.error("BLE write failed: %s", e)
                await self._handle_disconnect()
                return False

    async def read_config(self) -> dict:
        """Read full device config from the config characteristic.

        Returns empty dict if not connected or on error.
        """
        async with self._lock:
            if not self.is_connected:
                logger.warning("Not connected, cannot read config")
                return {}
            try:
                data = await self._client.read_gatt_char(CONFIG_CHR_UUID)
                return json.loads(data.decode("utf-8"))
            except Exception as e:
                logger.error("Config read failed: %s", e)
                await self._handle_disconnect()
                return {}

    async def _read_version_char(self) -> bytes:
        """Lock-guarded, timeout-bounded read of the version characteristic.

        Shared by read_version() and ping() so the version round-trip lives in
        one place. The lock serializes it with writes and with a concurrent
        read of the same characteristic (bleak keys in-flight read futures by
        characteristic handle, so overlapping reads would clobber each other).
        Raises if the link is down; on a read failure it drops the client (so
        the sender re-scans) and re-raises.
        """
        async with self._lock:
            if not self.is_connected:
                raise ConnectionError("BLE link not connected")
            try:
                return await asyncio.wait_for(
                    self._client.read_gatt_char(VERSION_CHR_UUID),
                    timeout=GATT_READ_TIMEOUT_SECS,
                )
            except Exception as e:
                logger.warning("BLE version read failed: %s", e)
                await self._handle_disconnect()
                raise

    async def read_version(self) -> int:
        """Read protocol version from firmware. Returns 1 if characteristic
        absent, unreadable, or the link is down."""
        try:
            data = await self._read_version_char()
        except Exception:
            return 1  # not connected, or read failed (client already dropped)
        try:
            return int(data.decode("utf-8").strip())
        except ValueError:
            return 1  # payload wasn't a number — firmware speaks v1

    async def ping(self) -> bool:
        """Active liveness probe: a round-trip GATT read that proves the link.

        macOS CoreBluetooth frequently fails to fire ``disconnected_callback``
        when the link is lost to range or sleep, and notification writes use
        ``response=False`` (no ACK), so a dead link never surfaces as an error.
        ``is_connected`` therefore stays stale-True and the sender's reconnect
        branch is never taken. This probe forces a round-trip; on failure it
        drops the client (clearing ``is_connected``) and fires the disconnect
        callback, which is what makes the sender re-scan and reconnect.

        On Windows the WinRT backend tracks the GATT session status itself,
        so ``is_connected`` goes False and ``disconnected_callback`` fires
        without this probe; here it is one uncached read every
        BLE_LIVENESS_INTERVAL_SECS that normally succeeds, and on a link
        Windows has not yet noticed is down it fails within
        GATT_READ_TIMEOUT_SECS and triggers the same reconnect. Harmless, and
        it keeps one code path for both platforms.

        Returns True if the link round-trips, False otherwise.
        """
        try:
            await self._read_version_char()
            return True
        except Exception:
            return False

    async def write_config(self, payload: str) -> bool:
        """Write a partial config JSON to the config characteristic.

        Returns True on success, False on failure.
        """
        async with self._lock:
            if not self.is_connected:
                logger.warning("Not connected, cannot write config")
                return False
            try:
                data = payload.encode("utf-8")
                await self._client.write_gatt_char(
                    CONFIG_CHR_UUID, data, response=False
                )
                logger.debug("Config write: %s", payload)
                return True
            except Exception as e:
                logger.error("Config write failed: %s", e)
                await self._handle_disconnect()
                return False

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._client = None
