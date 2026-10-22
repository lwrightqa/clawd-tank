"""Start with Windows: the value ``ClawdTank`` under
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``.

Per-user, no administrator rights. The value is the command Windows runs at
sign-in: the packaged exe, or, from source, ``pythonw`` (no console window).
The menu's checkbox reads the registry every time it opens, so it shows what is
really there, not what this process last did.
"""

import os
import sys
from pathlib import Path
from typing import Mapping, Optional

from .options import ENV_RUN_KEY

if sys.platform == "win32":
    import winreg

DEFAULT_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ClawdTank"


def run_key_path(env: Optional[Mapping[str, str]] = None) -> str:
    """The HKCU subkey. ``CLAWD_TANK_RUN_KEY`` replaces it (tests use a temp key,
    never the real Run key)."""
    env = os.environ if env is None else env
    return env.get(ENV_RUN_KEY) or DEFAULT_RUN_KEY


def startup_command(frozen: Optional[bool] = None) -> str:
    """The command line Windows runs at sign-in.

    Packaged: ``"C:\\...\\ClawdTank.exe" --minimized``. From source: pythonw running
    the package, with host/ put on the path first, because a Run entry starts in
    the system folder and ``-m clawd_tank_tray`` would not find the package there.
    """
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else exe
    host_dir = Path(__file__).resolve().parent.parent
    code = (f"import sys, runpy; sys.path.insert(0, r'{host_dir}'); "
            f"runpy.run_module('clawd_tank_tray', run_name='__main__')")
    return f'"{interpreter}" -c "{code}" --minimized'


class Autostart:
    def __init__(self, key_path: Optional[str] = None, value_name: str = VALUE_NAME,
                 command: Optional[str] = None):
        self.key_path = key_path or run_key_path()
        self.value_name = value_name
        self._command = command

    @property
    def command(self) -> str:
        return self._command if self._command is not None else startup_command()

    def current(self) -> Optional[str]:
        """The command stored in the registry, or None if there is no such value."""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0,
                                winreg.KEY_READ) as key:
                value, _kind = winreg.QueryValueEx(key, self.value_name)
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) else None

    def is_enabled(self) -> bool:
        return self.current() is not None

    def enable(self) -> None:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, self.command)

    def disable(self) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.value_name)
        except FileNotFoundError:
            pass  # already off

    def set_enabled(self, on: bool) -> None:
        self.enable() if on else self.disable()

    def repair(self) -> bool:
        """If Start with Windows is on but points somewhere else (the folder was
        moved, or another build wrote it), point it at this app. Returns True if
        it rewrote the value."""
        current = self.current()
        if current is None or current == self.command:
            return False
        self.enable()
        return True
