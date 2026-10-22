"""Start with Windows, on a temp registry key (never the real Run key)."""

import os
import subprocess
import sys
import uuid
import winreg

import pytest

from clawd_tank_tray.autostart import (
    DEFAULT_RUN_KEY,
    VALUE_NAME,
    Autostart,
    run_key_path,
    startup_command,
)
from clawd_tank_tray.options import ENV_RUN_KEY

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="registry")


def real_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DEFAULT_RUN_KEY) as key:
            return winreg.QueryValueEx(key, VALUE_NAME)[0]
    except FileNotFoundError:
        return None


@pytest.fixture
def temp_key():
    path = rf"Software\ClawdTankTest\{uuid.uuid4().hex}\Run"
    before = real_value()
    yield path
    assert real_value() == before, "a test touched the real Run key"
    for p in (path, path.rsplit("\\", 1)[0], r"Software\ClawdTankTest"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, p)
        except OSError:
            pass


def raw(path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return None


def test_the_real_key_and_value_name():
    assert DEFAULT_RUN_KEY == r"Software\Microsoft\Windows\CurrentVersion\Run"
    assert VALUE_NAME == "ClawdTank"
    assert run_key_path({}) == DEFAULT_RUN_KEY
    assert run_key_path({ENV_RUN_KEY: r"Software\Elsewhere"}) == r"Software\Elsewhere"


def test_enable_writes_the_value_and_disable_deletes_it(temp_key):
    auto = Autostart(temp_key, command=r'"C:\Apps\ClawdTank\ClawdTank.exe" --minimized')
    assert not auto.is_enabled() and auto.current() is None
    auto.enable()
    assert auto.is_enabled()
    assert raw(temp_key) == (r'"C:\Apps\ClawdTank\ClawdTank.exe" --minimized', winreg.REG_SZ)
    auto.disable()
    assert not auto.is_enabled() and raw(temp_key) is None
    auto.disable()  # off already: no error
    Autostart(temp_key + "Never", command="x").disable()  # no such key at all: no error


def test_set_enabled_and_enable_twice(temp_key):
    auto = Autostart(temp_key, command="a")
    auto.set_enabled(True)
    auto.set_enabled(True)
    assert auto.is_enabled()
    auto.set_enabled(False)
    assert not auto.is_enabled()


def test_the_checkbox_state_is_the_registry_not_a_memory(temp_key):
    """Another process (or the user, in regedit) changes the value: the next read sees it."""
    auto = Autostart(temp_key, command="a")
    other = Autostart(temp_key, command="b")
    other.enable()
    assert auto.is_enabled() and auto.current() == "b"
    other.disable()
    assert not auto.is_enabled()


def test_repair_repoints_a_stale_entry_but_never_turns_it_on(temp_key):
    auto = Autostart(temp_key, command="new")
    assert auto.repair() is False and not auto.is_enabled()  # off stays off
    Autostart(temp_key, command="old").enable()
    assert auto.repair() is True and auto.current() == "new"
    assert auto.repair() is False  # already right


def test_frozen_command_runs_the_exe_minimised():
    assert startup_command(frozen=True) == f'"{sys.executable}" --minimized'


def test_source_command_runs_pythonw_and_the_package_from_host():
    cmd = startup_command(frozen=False)
    interpreter = cmd.split('"')[1].lower()
    assert interpreter.endswith(".exe")
    assert "clawd_tank_tray" in cmd and cmd.endswith("--minimized")
    assert "sys.path.insert(0" in cmd  # a Run entry starts elsewhere than host/
    assert cmd.count('"') == 4  # the interpreter and the -c code, nothing to escape


def test_source_command_really_starts_the_package(tmp_path):
    """Run the command's -c code (with --selftest in place of --minimized) from
    another working directory: it must import the package from host/."""
    cmd = startup_command(frozen=False)
    code = cmd.split('-c "', 1)[1].rsplit('" --minimized', 1)[0]
    result = subprocess.run(
        [sys.executable, "-c", code, "--selftest"], cwd=tmp_path, capture_output=True, text=True,
        timeout=60, env={**os.environ, "LOCALAPPDATA": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert "selftest ok" in result.stdout


def test_autostart_defaults_to_the_env_key(monkeypatch, temp_key):
    monkeypatch.setenv(ENV_RUN_KEY, temp_key)
    auto = Autostart(command="x")
    assert auto.key_path == temp_key
    auto.enable()
    assert raw(temp_key)[0] == "x"
    auto.disable()
