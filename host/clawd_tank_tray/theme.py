"""Colours and fonts of the flyout: Windows 11's own flyout palette, light or dark.

No GUI and no ctypes here except one registry read, which is injectable, so the
choice can be tested without a display. The flyout asks again each time it opens,
so switching Windows between light and dark mode shows the next time it is used.
"""

import os
import sys
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, Tuple

if sys.platform == "win32":
    import winreg

# CLAWD_TANK_THEME=light|dark forces the theme (tests and screenshots); anything
# else, or nothing, means "what Windows says".
ENV_THEME = "CLAWD_TANK_THEME"

PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
APPS_USE_LIGHT_VALUE = "AppsUseLightTheme"


@dataclass(frozen=True)
class Theme:
    name: str
    background: str   # the flyout
    border: str       # the 1 px outline and the separators
    text: str
    secondary: str    # the line names (Bar, Hooks, ...)
    hover: str        # a row under the pointer (or chosen with the keys)
    pressed: str      # a row while the button is down
    critical: str     # the red message


LIGHT = Theme("light", background="#F3F3F3", border="#D1D1D1", text="#1A1A1A",
              secondary="#5E5E5E", hover="#E5E5E5", pressed="#DADADA", critical="#C42B1C")
DARK = Theme("dark", background="#2B2B2B", border="#3F3F3F", text="#FFFFFF",
             secondary="#C5C5C5", hover="#3A3A3A", pressed="#333333", critical="#FF99A4")
THEMES = {"light": LIGHT, "dark": DARK}

# Read the Windows setting: returns the DWORD, or None when there is no such value.
ThemeReader = Callable[[], Optional[int]]


def read_apps_use_light_theme() -> Optional[int]:
    """HKCU\\...\\Themes\\Personalize\\AppsUseLightTheme (0 = dark, 1 = light), or
    None if it cannot be read."""
    if sys.platform != "win32":
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE_KEY, 0,
                            winreg.KEY_READ) as key:
            value, kind = winreg.QueryValueEx(key, APPS_USE_LIGHT_VALUE)
    except OSError:
        return None
    return int(value) if kind == winreg.REG_DWORD else None


def choose_theme(apps_use_light: Optional[int], env: Optional[Mapping[str, str]] = None) -> Theme:
    """The environment override wins; else 0 means dark, and 1, a missing value or
    anything unreadable means light (what Windows itself does before the setting
    exists)."""
    env = os.environ if env is None else env
    forced = env.get(ENV_THEME, "").strip().lower()
    if forced in THEMES:
        return THEMES[forced]
    return DARK if apps_use_light == 0 else LIGHT


def current_theme(read: Optional[ThemeReader] = None,
                  env: Optional[Mapping[str, str]] = None) -> Theme:
    """The theme to use right now. `read` replaces the registry read (tests)."""
    try:
        value = (read or read_apps_use_light_theme)()
    except Exception:  # noqa: BLE001 - a theme must never stop the flyout from opening
        value = None
    return choose_theme(value, env)


# --- fonts --------------------------------------------------------------------------------------

# Windows 11 ships Segoe UI Variable; older Windows has Segoe UI.
BODY_FONTS = ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI")
TITLE_FONTS = ("Segoe UI Variable Text Semibold", "Segoe UI Semibold")
BODY_PT = 9          # the status lines
ROW_PT = 10          # the title and the rows
# The check mark of the Start with Windows row: the icon font of Windows 11, else
# of Windows 10, else a plain glyph.
CHECK_FONTS = (("Segoe Fluent Icons", ""), ("Segoe MDL2 Assets", ""),
               ("Segoe UI Symbol", "✓"))


def pick_font(available: Sequence[str], wanted: Sequence[str], fallback: str) -> str:
    """The first of `wanted` that is installed (names compare without case), else
    `fallback`."""
    have = {name.lower() for name in available}
    for name in wanted:
        if name.lower() in have:
            return name
    return fallback


def pick_check(available: Sequence[str]) -> Tuple[str, str]:
    """(font family, character) for the check mark."""
    have = {name.lower() for name in available}
    for family, char in CHECK_FONTS:
        if family.lower() in have:
            return family, char
    return "TkDefaultFont", "✓"
