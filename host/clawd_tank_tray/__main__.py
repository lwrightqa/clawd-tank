"""``python -m clawd_tank_tray`` and the entry point of ClawdTank.exe."""

import logging
import os
import platform
import sys

from . import APP_NAME, __version__
from .logsetup import ensure_std_streams, install_excepthooks, setup_logging
from .options import Options, log_path, parse_args
from .single_instance import SingleInstance
from .winutil import ErrorBoxes, set_dpi_aware

logger = logging.getLogger("clawd-tank.tray")


def selftest() -> int:
    """Import everything the app needs and read its assets, without opening a
    window, the tray icon, or the radio. Exit code 0 only if all of it is there.
    The result goes to the log (and to the console when there is one)."""
    setup_logging(log_path())
    problems = []

    def check(label: str, fn) -> None:
        try:
            detail = fn()
            logger.info("selftest ok: %s%s", label, f" ({detail})" if detail else "")
        except Exception as e:  # noqa: BLE001
            logger.exception("selftest FAILED: %s", label)
            problems.append(f"{label}: {e}")

    def tk_check():
        import tkinter
        return tkinter.Tcl().eval("info patchlevel")

    def bleak_check():
        import bleak
        from bleak.backends.winrt.client import BleakClientWinRT  # noqa: F401
        from bleak.backends.winrt.scanner import BleakScannerWinRT  # noqa: F401
        from importlib.metadata import version
        return f"bleak {version('bleak')}"

    def winrt_check():
        from winrt.windows.devices.bluetooth import BluetoothLEDevice  # noqa: F401
        from winrt.windows.devices.bluetooth.advertisement import (  # noqa: F401
            BluetoothLEAdvertisementWatcher)
        from winrt.windows.devices.bluetooth.genericattributeprofile import (  # noqa: F401
            GattCharacteristic)
        from winrt.windows.devices.enumeration import DeviceInformation  # noqa: F401
        from winrt.windows.storage.streams import DataReader  # noqa: F401
        return "Bluetooth LE namespaces"

    def daemon_check():
        from clawd_tank_daemon.daemon import ClawdDaemon  # noqa: F401
        from clawd_tank_daemon.install_hooks import plan_merge  # noqa: F401
        from clawd_tank_menubar import hooks
        return f"hook command {hooks.HOOK_COMMAND}"

    def assets_check():
        from .icons import SIZES, IconSet
        from .state import ICON_NAMES
        icons = IconSet()
        for name in ICON_NAMES:
            for size in SIZES:
                icons.icon(name, size)
        if not icons.ico_path.is_file():
            raise FileNotFoundError(icons.ico_path)
        return f"{len(ICON_NAMES)} tray icons, {icons.ico_path.name}"

    def tray_check():
        import pystray  # noqa: F401
        from PIL import Image
        from .tray_icon import hicon_from_image, destroy_hicon
        destroy_hicon(hicon_from_image(Image.new("RGBA", (16, 16), (255, 128, 0, 255))))
        return "pystray, native icon handle"

    logger.info("selftest: %s %s, Python %s, frozen=%s", APP_NAME, __version__,
                platform.python_version(), getattr(sys, "frozen", False))
    check("tkinter", tk_check)
    check("bleak", bleak_check)
    check("winrt", winrt_check)
    check("daemon", daemon_check)
    check("assets", assets_check)
    check("tray", tray_check)
    for line in problems:
        print("selftest FAILED:", line, file=sys.stderr)
    if not problems:
        print("selftest ok")
    return 1 if problems else 0


def main(argv=None) -> int:
    ensure_std_streams()
    options: Options = parse_args(argv)
    instance = SingleInstance()

    if options.command in ("quit", "show"):
        # Talk to the running instance, if any. Touch nothing else (not the log:
        # the running instance has it open).
        return 0 if instance.signal(options.command, wait=0.0) else 1
    if options.command == "selftest":
        return selftest()

    set_dpi_aware()  # before any window or tray icon exists
    if not instance.acquire():
        instance.signal("show", wait=3.0)  # the running one comes forward
        return 0

    setup_logging(log_path(), options.debug)
    boxes = ErrorBoxes(enabled=not options.no_prompt)
    install_excepthooks(boxes)
    logger.info("%s %s, Python %s, %s, frozen=%s, argv=%s", APP_NAME, __version__,
                platform.python_version(), platform.platform(),
                getattr(sys, "frozen", False), sys.argv[1:])
    logger.info("options: %s; TCP port %s", options,
                os.environ.get("CLAWD_TANK_TCP_PORT") or options.tcp_port or "default (47421)")
    code = 1
    try:
        from .app import TrayApp
        code = TrayApp(options, instance=instance, errors=boxes).run()
    except Exception as e:  # noqa: BLE001
        logger.critical("The app crashed", exc_info=True)
        boxes.show(APP_NAME, f"Clawd Tank could not start: {e}\n\nSee the log for details.")
    finally:
        instance.release()
    return code


if __name__ == "__main__":
    sys.exit(main())
