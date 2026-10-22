"""Command line, environment switches, and where the app keeps its files."""

import json
from pathlib import Path

import pytest

from clawd_tank_tray.config import AppConfig
from clawd_tank_tray.options import (
    ENV_NO_BLE,
    ENV_NO_PROMPT,
    app_dir,
    env_flag,
    log_path,
    parse_args,
)


def test_defaults():
    o = parse_args([], {})
    assert (o.command, o.no_ble, o.no_prompt, o.minimized, o.tcp_port, o.debug) == \
        ("run", False, False, False, None, False)


def test_flags_and_environment_switches():
    assert parse_args(["--no-ble"], {}).no_ble
    assert parse_args([], {ENV_NO_BLE: "1"}).no_ble
    assert parse_args([], {ENV_NO_BLE: "true"}).no_ble
    assert not parse_args([], {ENV_NO_BLE: "0"}).no_ble
    assert parse_args([], {ENV_NO_PROMPT: "yes"}).no_prompt
    assert parse_args(["--no-prompt"], {}).no_prompt
    assert parse_args(["--minimized"], {}).minimized
    assert parse_args(["--hidden"], {}).minimized  # alias
    assert parse_args(["--tcp-port", "47499"], {}).tcp_port == 47499
    assert parse_args(["--debug"], {}).debug
    assert not env_flag("X", {}) and env_flag("X", {"X": " On "})


@pytest.mark.parametrize("flag,command", [("--quit", "quit"), ("--show", "show"),
                                          ("--selftest", "selftest")])
def test_control_commands(flag, command):
    assert parse_args([flag], {}).command == command


def test_unknown_flags_are_refused():
    with pytest.raises(SystemExit):
        parse_args(["--frobnicate"], {})


def test_log_lives_under_local_app_data():
    assert log_path({"LOCALAPPDATA": r"C:\L"}) == Path(r"C:\L") / "ClawdTank" / "tray.log"
    assert app_dir({}) == Path.home() / "AppData" / "Local" / "ClawdTank"


def test_config_remembers_a_no(tmp_path):
    path = tmp_path / "ClawdTank" / "state.json"
    cfg = AppConfig(path)
    assert cfg.hooks_declined is False and not path.exists()
    cfg.hooks_declined = True
    assert json.loads(path.read_text(encoding="utf-8")) == {"hooks_declined": True}
    assert AppConfig(path).hooks_declined is True
    cfg.hooks_declined = False
    assert AppConfig(path).hooks_declined is False


def test_config_survives_a_damaged_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{nope", encoding="utf-8")
    assert AppConfig(path).hooks_declined is False
    path.write_text("[1]", encoding="utf-8")
    assert AppConfig(path).hooks_declined is False
