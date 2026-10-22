"""One tray app at a time: the named mutex and the show/quit events."""

import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from clawd_tank_tray.options import ENV_INSTANCE
from clawd_tank_tray.single_instance import SingleInstance, names

HOST = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="named kernel objects")


@pytest.fixture
def name():
    return f"test-{uuid.uuid4().hex[:10]}"


def test_default_names_are_the_documented_ones():
    assert names("", {}) == ("Local\\ClawdTankTray", "Local\\ClawdTankTray.Show",
                             "Local\\ClawdTankTray.Quit")
    assert names(None, {ENV_INSTANCE: "dev"})[0] == "Local\\ClawdTankTray-dev"
    assert SingleInstance("").mutex_name == "Local\\ClawdTankTray"


def test_only_the_first_acquires(name):
    first, second = SingleInstance(name), SingleInstance(name)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
    finally:
        first.release()
        second.release()
    third = SingleInstance(name)
    try:
        assert third.acquire() is True  # free again once the first let go
    finally:
        third.release()


def test_a_different_instance_name_is_independent(name):
    a, b = SingleInstance(name), SingleInstance(name + "-b")
    try:
        assert a.acquire() and b.acquire()
    finally:
        a.release()
        b.release()


def test_a_second_process_cannot_acquire(name):
    first = SingleInstance(name)
    assert first.acquire()
    code = ("import sys; from clawd_tank_tray.single_instance import SingleInstance; "
            f"sys.exit(0 if SingleInstance({name!r}).acquire() else 3)")
    try:
        result = subprocess.run([sys.executable, "-c", code], cwd=str(HOST), timeout=30)
        assert result.returncode == 3, "a second process got the mutex"
    finally:
        first.release()
    result = subprocess.run([sys.executable, "-c", code], cwd=str(HOST), timeout=30)
    assert result.returncode == 0  # and once released, the next one does


def test_show_and_quit_signals_reach_the_instance(name):
    owner, other = SingleInstance(name), SingleInstance(name)
    shown, quit_ = threading.Event(), threading.Event()
    assert owner.acquire()
    owner.start_listener(shown.set, quit_.set)
    try:
        assert other.signal("show", wait=2.0) is True
        assert shown.wait(5) and not quit_.is_set()
        assert other.signal("quit", wait=2.0) is True
        assert quit_.wait(5)
        shown.clear()
        assert other.signal("show") is True and shown.wait(5)  # events re-arm
    finally:
        owner.release()


def test_signalling_when_nobody_listens_fails_quickly(name):
    t0 = time.monotonic()
    assert SingleInstance(name).signal("quit", wait=0.0) is False
    assert time.monotonic() - t0 < 1.0


def test_signal_waits_for_a_listener_that_is_still_starting_up(name):
    owner, other = SingleInstance(name), SingleInstance(name)
    shown = threading.Event()
    assert owner.acquire()
    timer = threading.Timer(0.5, lambda: owner.start_listener(shown.set, lambda: None))
    timer.start()
    try:
        assert other.signal("show", wait=5.0) is True
        assert shown.wait(5)
    finally:
        timer.join()
        owner.release()


def test_a_callback_that_raises_does_not_kill_the_listener(name):
    owner, other = SingleInstance(name), SingleInstance(name)
    calls = []

    def bad():
        calls.append(1)
        raise RuntimeError("boom")

    ok = threading.Event()
    assert owner.acquire()
    owner.start_listener(bad, ok.set)
    try:
        other.signal("show")
        other.signal("quit")
        assert ok.wait(5) and calls == [1]
    finally:
        owner.release()


def test_release_is_idempotent(name):
    inst = SingleInstance(name)
    assert inst.acquire()
    inst.release()
    inst.release()
