"""The log file: rotation must never cost a line.

Regression for a silent log. On Windows the standard RotatingFileHandler fails to
rotate while another program has the log open (Notepad after "Open log", an
editor, ``Get-Content -Wait``); the exception aborted every write, and the app ran
on with a log that stopped growing at just under 1 MB. The files here are in
temp folders, and the "other program" is a plain open file handle in this test."""

import logging
import sys

import pytest

from clawd_tank_tray import logsetup
from clawd_tank_tray.logsetup import SafeRotatingFileHandler, setup_logging

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the sharing rules are Windows'")

LIMIT = 4000
LINE = ("old log line " + "x" * 86 + "\n").encode()   # 100 bytes


@pytest.fixture
def small_log(tmp_path, monkeypatch):
    """A log path whose handler rotates at LIMIT bytes, and a clean root logger."""
    monkeypatch.setattr(logsetup, "MAX_BYTES", LIMIT)
    root = logging.getLogger()
    before = list(root.handlers), root.level
    path = tmp_path / "ClawdTank" / "tray.log"
    yield path
    for h in [h for h in root.handlers if h not in before[0]]:
        root.removeHandler(h)
        h.close()
    root.setLevel(before[1])


def seed(path, size=LIMIT - 150):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(LINE * (size // len(LINE)))


def text(path):
    return path.read_text(encoding="utf-8", errors="replace")


def emit(n, prefix="line"):
    log = logging.getLogger("clawd-tank.test")
    for i in range(n):
        log.info("%s %d %s", prefix, i, "y" * 60)


def test_a_full_log_that_another_program_holds_open_still_gets_every_line(small_log):
    seed(small_log)
    with open(small_log, "rb"):                       # another program has it open
        handler = setup_logging(small_log)
        emit(8)                                       # crosses the limit: rotation is refused
        handler.flush()
        got = text(small_log)
    for i in range(8):
        assert f"line {i} " in got, f"line {i} was lost"
    assert "Could not rotate" in got                  # and the log says why it is growing
    assert not small_log.with_name("tray.log.1").exists()
    assert handler.rotation_failures == 1             # one attempt, then it waits: not one per line


def test_the_first_lines_of_a_run_are_not_lost_either(small_log):
    """The case that was seen: the very first records, written when the file is
    already just under the limit, must land (the app logs its start-up at once)."""
    seed(small_log, LIMIT - 20)
    with open(small_log, "rb"):
        handler = setup_logging(small_log)
        logging.getLogger("clawd-tank.tray").info("Clawd Tank 1.0.0 starting")
        logging.getLogger("clawd-tank.tray.engine").info("Engine started")
        handler.flush()
        got = text(small_log)
    assert "Clawd Tank 1.0.0 starting" in got and "Engine started" in got


def test_rotation_is_retried_and_happens_once_the_other_program_lets_go(small_log, monkeypatch):
    monkeypatch.setattr(logsetup, "ROTATE_RETRY_SECONDS", 0.0)
    seed(small_log)
    holder = open(small_log, "rb")
    handler = setup_logging(small_log)
    try:
        emit(4, "while-held")
        assert not small_log.with_name("tray.log.1").exists()
    finally:
        holder.close()
    emit(4, "after-release")                          # the next line rotates, normally
    handler.flush()
    old, new = small_log.with_name("tray.log.1"), small_log
    assert old.exists() and handler.rotation_failures >= 1
    everything = text(old) + text(new)
    for prefix in ("while-held", "after-release"):
        for i in range(4):
            assert f"{prefix} {i} " in everything
    assert new.stat().st_size < LIMIT                  # a fresh file, below the limit again


def test_normal_rotation_is_unchanged(small_log):
    seed(small_log)
    handler = setup_logging(small_log)
    emit(60)
    handler.flush()
    names = sorted(p.name for p in small_log.parent.iterdir())
    assert "tray.log.1" in names and len(names) <= 1 + logsetup.BACKUPS
    assert handler.rotation_failures == 0
    assert "Could not rotate" not in "".join(text(small_log.parent / n) for n in names)


def test_setup_logging_installs_the_safe_handler_and_never_duplicates_it(small_log):
    first = setup_logging(small_log)
    second = setup_logging(small_log)
    ours = [h for h in logging.getLogger().handlers if isinstance(h, SafeRotatingFileHandler)]
    assert isinstance(first, SafeRotatingFileHandler) and ours == [second]
    assert second.maxBytes == LIMIT and second.backupCount == logsetup.BACKUPS
