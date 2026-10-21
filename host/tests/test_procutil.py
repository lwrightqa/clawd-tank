"""Tests for procutil: PID liveness and the Windows process table, on both families of OS."""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from clawd_tank_daemon import procutil
from clawd_tank_daemon.procutil import pid_alive

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only API")


# --- POSIX semantics (os.kill mocked, so these run on any OS) --------------------


@pytest.fixture
def posix():
    with patch.object(procutil, "IS_WINDOWS", False):
        yield


def test_posix_probe_alive(posix):
    with patch("clawd_tank_daemon.procutil.os.kill", return_value=None) as kill:
        assert pid_alive(4242) is True
    kill.assert_called_once_with(4242, 0)


def test_posix_probe_dead(posix):
    with patch("clawd_tank_daemon.procutil.os.kill", side_effect=ProcessLookupError):
        assert pid_alive(4242) is False


def test_posix_probe_other_users_process_counts_as_alive(posix):
    with patch("clawd_tank_daemon.procutil.os.kill", side_effect=PermissionError):
        assert pid_alive(4242) is True


@pytest.mark.parametrize("bad", [None, 0, -1])
def test_nonsense_pids_are_not_alive_and_never_reach_os_kill(bad, posix):
    with patch("clawd_tank_daemon.procutil.os.kill") as kill:
        assert pid_alive(bad) is False
    kill.assert_not_called()


# --- real processes (any OS) ---------------------------------------------------


def test_own_pid_is_alive():
    assert pid_alive(os.getpid()) is True


def test_running_child_is_alive_and_finished_child_is_not():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(child.pid) is True
    finally:
        child.kill()
        child.wait()
    # The Popen object still holds the (Windows) process handle, so the PID
    # still resolves to an exited process: it must read as not alive.
    assert pid_alive(child.pid) is False


@windows_only
def test_windows_does_not_use_os_kill_for_the_probe():
    """os.kill(pid, 0) on Windows is GenerateConsoleCtrlEvent(CTRL_C_EVENT); the
    probe must never go through it."""
    with patch("os.kill", side_effect=AssertionError("os.kill must not be used on Windows")):
        assert pid_alive(os.getpid()) is True


# --- Windows process table ---------------------------------------------------------


@windows_only
def test_windows_process_table_lists_this_process_with_its_parent():
    table = procutil.windows_process_table()
    assert os.getpid() in table
    parent, exe = table[os.getpid()]
    assert parent == os.getppid()
    assert exe.lower().startswith("python")


def test_process_table_is_empty_off_windows():
    if sys.platform == "win32":
        pytest.skip("this checks the non-Windows answer")
    assert procutil.windows_process_table() == {}


# --- stdin_is_terminal --------------------------------------------------------------


def test_no_stdin_is_not_a_terminal():
    with patch.object(sys, "stdin", None):
        assert procutil.stdin_is_terminal() is False


def test_a_pipe_is_not_a_terminal():
    """A child whose stdin is a pipe (like `echo yes | ...`) is not a terminal."""
    code = "from clawd_tank_daemon import procutil; print(procutil.stdin_is_terminal())"
    r = subprocess.run([sys.executable, "-c", code], input=b"yes\n", capture_output=True,
                       cwd=os.path.dirname(os.path.dirname(__file__)))
    assert r.stdout.strip() == b"False", r.stderr.decode()


def test_the_null_device_is_not_a_terminal():
    """isatty() is True for NUL on Windows; stdin_is_terminal must not be fooled."""
    code = "from clawd_tank_daemon import procutil; print(procutil.stdin_is_terminal())"
    r = subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL, capture_output=True,
                       cwd=os.path.dirname(os.path.dirname(__file__)))
    assert r.stdout.strip() == b"False", r.stderr.decode()
