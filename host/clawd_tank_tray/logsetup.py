"""Logging for a windowed app: a rotating file, and uncaught exceptions that end
up there (and in one message box) instead of in a console nobody can see.

``%LOCALAPPDATA%\\ClawdTank\\tray.log``, 1 MB per file, 3 old files kept. The
daemon's own loggers (``clawd-tank``, ``clawd-tank.ble``, ...) propagate to the
root logger, so its lines land in the same file.
"""

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .winutil import ErrorBoxes

FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
MAX_BYTES = 1_000_000
BACKUPS = 3

logger = logging.getLogger("clawd-tank.tray")


def setup_logging(path: Path, debug: bool = False) -> RotatingFileHandler:
    """Log to `path` (created with its folder). Replaces a handler this function
    installed earlier, so calling it twice does not duplicate lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(FORMAT))
    handler._clawd_tray = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, "_clawd_tray", False)]:
        root.removeHandler(old)
        old.close()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    if not debug:
        # bleak's WinRT backend is chatty at DEBUG, and Pillow logs every PNG chunk.
        logging.getLogger("bleak").setLevel(logging.INFO)
        logging.getLogger("PIL").setLevel(logging.WARNING)
    return handler


def ensure_std_streams() -> None:
    """A windowed exe has no stdout/stderr (they are None). Give anything that
    prints, argparse or a library, somewhere harmless to write."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def install_excepthooks(boxes: ErrorBoxes) -> None:
    """Send uncaught exceptions, in any thread, to the log, and show one message
    box. KeyboardInterrupt and SystemExit are left alone."""

    def report(where: str, exc_type, exc, tb) -> None:
        logger.critical("Uncaught exception in %s", where, exc_info=(exc_type, exc, tb))
        boxes.show("Clawd Tank",
                   f"Clawd Tank hit an unexpected error ({exc_type.__name__}: {exc}).\n\n"
                   "Details are in the log (Open log in the window or the tray menu).")

    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return
        report("the main thread", exc_type, exc, tb)

    def thread_hook(args: "threading.ExceptHookArgs") -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "a thread"
        report(name, args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = hook
    threading.excepthook = thread_hook


def tk_exception_handler(boxes: ErrorBoxes):
    """For ``root.report_callback_exception``: tkinter swallows exceptions of its
    callbacks and prints them to stderr, which a windowed app does not have."""

    def handler(exc_type, exc, tb) -> None:
        logger.critical("Uncaught exception in a window callback",
                        exc_info=(exc_type, exc, tb))
        boxes.show("Clawd Tank",
                   f"Clawd Tank hit an unexpected error ({exc_type.__name__}: {exc}).\n\n"
                   "Details are in the log.")

    return handler
