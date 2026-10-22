"""Command line and environment of the tray app, and where its files live."""

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional

# Developer and test switches. None of them is needed in normal use.
ENV_NO_BLE = "CLAWD_TANK_NO_BLE"          # 1: run the daemon without the BLE transport
ENV_NO_PROMPT = "CLAWD_TANK_NO_PROMPT"    # 1: never open a dialog on its own
ENV_CLAUDE_DIR = "CLAWD_TANK_CLAUDE_DIR"  # folder of settings.json (beats CLAUDE_CONFIG_DIR)
ENV_INSTANCE = "CLAWD_TANK_INSTANCE"      # suffix of the mutex/event names
ENV_RUN_KEY = "CLAWD_TANK_RUN_KEY"        # HKCU subkey used instead of ...\Windows\CurrentVersion\Run

# Read by the daemon itself (hook_transport.py): CLAWD_TANK_TCP_PORT,
# CLAWD_TANK_TRANSPORT.

COMMANDS = ("run", "quit", "show", "selftest")


def env_flag(name: str, env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Options:
    command: str = "run"
    no_ble: bool = False
    no_prompt: bool = False
    minimized: bool = False
    tcp_port: Optional[int] = None
    debug: bool = False


def parse_args(argv: Optional[List[str]] = None,
               env: Optional[Mapping[str, str]] = None) -> Options:
    """Options from the command line and the environment. Raises SystemExit
    (argparse) on an unknown flag, like any command."""
    parser = argparse.ArgumentParser(
        prog="ClawdTank", description="Clawd Tank for Windows: the tray app.")
    parser.add_argument("--minimized", "--hidden", dest="minimized", action="store_true",
                        help="start in the tray, without showing the window "
                             "(what Start with Windows uses)")
    parser.add_argument("--quit", dest="command", action="store_const", const="quit",
                        help="ask the running instance to quit, then exit")
    parser.add_argument("--show", dest="command", action="store_const", const="show",
                        help="ask the running instance to show its window, then exit")
    parser.add_argument("--selftest", dest="command", action="store_const", const="selftest",
                        help="import everything the app needs, touch nothing "
                             "(no window, no Bluetooth), exit 0 if all is there")
    parser.add_argument("--no-ble", action="store_true",
                        help=f"run without the Bluetooth transport (also ${ENV_NO_BLE}=1)")
    parser.add_argument("--no-prompt", action="store_true",
                        help="never open a dialog by itself: no first-run hooks question, "
                             f"no crash message box (also ${ENV_NO_PROMPT}=1)")
    parser.add_argument("--tcp-port", type=int, default=None,
                        help="TCP port of the hook listener (default: $CLAWD_TANK_TCP_PORT or 47421)")
    parser.add_argument("--debug", action="store_true", help="log at DEBUG level")
    args = parser.parse_args(argv)
    return Options(
        command=args.command or "run",
        no_ble=args.no_ble or env_flag(ENV_NO_BLE, env),
        no_prompt=args.no_prompt or env_flag(ENV_NO_PROMPT, env),
        minimized=args.minimized,
        tcp_port=args.tcp_port,
        debug=args.debug,
    )


def app_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """%LOCALAPPDATA%\\ClawdTank: the log and the app's own small state file."""
    env = os.environ if env is None else env
    base = env.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "ClawdTank"


def log_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return app_dir(env) / "tray.log"


def settings_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """The Claude Code settings.json the hooks go into: $CLAWD_TANK_CLAUDE_DIR,
    else $CLAUDE_CONFIG_DIR (which Claude Code itself honours), else
    %USERPROFILE%\\.claude."""
    env = os.environ if env is None else env
    override = env.get(ENV_CLAUDE_DIR)
    if override:
        return Path(override) / "settings.json"
    from clawd_tank_daemon.install_hooks import default_settings_path
    return default_settings_path(env)
