"""The Claude Code hooks, as the tray app handles them.

This is the GUI's side of ``clawd_tank_daemon.install_hooks``. It reuses that
module's merge (``plan_merge``), backup, atomic write and ``SettingsError``, and
the hook definitions of ``clawd_tank_menubar.hooks``; the command-line installer
asks you to type ``yes`` in a terminal, this one asks with a Yes/No dialog that
the app shows (see ``consent_text``). Nothing here asks by itself: callers show
the dialog and call ``install()`` / ``remove()`` only on Yes.

The hooks are what they are in the command-line installer: each one runs
``py -3 "<home>/.clawd-tank/clawd-tank-notify.py"``.
"""

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from clawd_tank_daemon import install_hooks
from clawd_tank_daemon.install_hooks import SettingsError
from clawd_tank_menubar import hooks


@dataclass
class HookStatus:
    settings_path: Path
    script_path: Path
    settings_error: str = ""     # settings.json exists but cannot be merged into safely
    hooks_present: bool = False  # every Clawd Tank hook is in settings.json
    script_present: bool = False
    script_current: bool = False  # the notify script is byte for byte the one this app ships
    launcher_found: bool = True  # the `py` launcher the hooks run through

    @property
    def installed(self) -> bool:
        return (not self.settings_error and self.hooks_present
                and self.script_present and self.script_current)

    @property
    def text(self) -> str:
        """The window's 'Hooks:' line."""
        if self.settings_error:
            return "settings.json is not valid JSON"
        if self.installed:
            return "Installed"
        if self.hooks_present and not self.script_present:
            return "Not working: the notify script is missing"
        if self.hooks_present:
            return "Outdated: the notify script is old"
        return "Not installed"

    @property
    def warning(self) -> str:
        """A line for the window's red message area, or ''."""
        if self.settings_error:
            return self.settings_error
        if not self.launcher_found and (self.installed or self.hooks_present):
            return ("The py launcher was not found, so the hooks cannot run. "
                    "Install Python from python.org (it includes py).")
        return ""


def should_prompt(status: HookStatus, declined: bool) -> bool:
    """Whether to ask the user at start-up whether to install or repair the hooks.

    * a settings.json that cannot be merged: no (the Install button explains why);
    * hooks in place and the script fine: no;
    * hooks in place but the script missing or old: yes, that is a repair;
    * no hooks: yes on first run, but not again once the user said No or removed
      them (``declined``), so a refusal is not asked on every launch.
    """
    if status.settings_error or status.installed:
        return False
    if status.hooks_present:
        return True
    return not declined


@dataclass
class HookResult:
    ok: bool
    message: str
    backup: Optional[Path] = None


class HookManager:
    """Reads, installs and removes the hooks for one settings.json."""

    def __init__(self, settings_path: Path,
                 now: Callable[[], datetime] = datetime.now,
                 which: Callable[[str], Optional[str]] = shutil.which):
        self.settings_path = Path(settings_path)
        self._now = now
        self._which = which

    @property
    def script_path(self) -> Path:
        """Where the notify script lives: ``hooks.NOTIFY_SCRIPT_PATH``, which is
        also what every hook command points at (tests patch that name)."""
        return Path(hooks.NOTIFY_SCRIPT_PATH)

    # --- looking ----------------------------------------------------------

    def status(self) -> HookStatus:
        st = HookStatus(self.settings_path, self.script_path,
                        launcher_found=self._which("py") is not None)
        try:
            settings = install_hooks.load_settings(self.settings_path)
        except SettingsError as e:
            st.settings_error = str(e)
            settings = None
        else:
            st.hooks_present = settings is not None and hooks.settings_have_our_hooks(settings)
        st.script_present = self.script_path.is_file()
        if st.script_present:
            try:
                st.script_current = _same_text(
                    self.script_path.read_text(encoding="utf-8"), hooks.NOTIFY_SCRIPT)
            except (OSError, UnicodeDecodeError):
                st.script_current = False
        return st

    def consent_text(self, action: str = "install") -> str:
        """What the Yes/No dialog says. Names the file, says a backup is made
        first, and that everything else in it is kept."""
        backup = install_hooks.backup_path_for(self.settings_path, self._now())
        exists = self.settings_path.exists()
        if action == "remove":
            return (
                "Remove the Clawd Tank hooks from Claude Code?\n\n"
                "This edits:\n"
                f"    {self.settings_path}\n\n"
                f"A backup is made first ({backup.name}). Only the Clawd Tank hook entries "
                "are removed; all your other settings and hooks are kept.\n\n"
                "Clawd will stop reacting to Claude Code until you install the hooks again."
            )
        lines = [
            "Clawd Tank needs to add its hooks to Claude Code, so Clawd can see "
            "what Claude is doing.\n",
            "This edits:" if exists else "This creates:",
            f"    {self.settings_path}\n",
        ]
        if exists:
            lines.append(f"A backup is made first ({backup.name}, next to it). "
                         "All your other settings and hooks are kept.\n")
        lines.append("It also writes the small helper script the hooks run:")
        lines.append(f"    {self.script_path}\n")
        lines.append("Add the hooks?")
        return "\n".join(lines)

    # --- doing (call only after the user said Yes) ------------------------

    def install(self) -> HookResult:
        """Merge the hooks in: notify script, backup, atomic write of settings.json.
        A settings.json that is not valid JSON is refused and left untouched."""
        try:
            settings = install_hooks.load_settings(self.settings_path)
        except SettingsError as e:
            return HookResult(False, f"{e}\n\nNothing was changed.")
        merged, changes = install_hooks.plan_merge(settings, hooks)
        if (settings is not None and self.status().installed
                and all(kind == "ok" for _event, kind in changes)):
            return HookResult(True, "The hooks are already installed and up to date. "
                                    "Nothing was changed.")
        backup = install_hooks.backup_path_for(self.settings_path, self._now())
        try:
            verified = install_hooks.apply_install(
                hooks, self.settings_path, settings, merged, backup)
        except (OSError, SettingsError) as e:
            return HookResult(False, f"Could not install the hooks: {e}")
        if not verified:
            return HookResult(
                False, "The settings file was written but does not read back with the hooks "
                       f"in place. Your original is in {backup.name}.",
                backup if settings is not None else None)
        return HookResult(
            True, "The hooks are installed. Restart any running Claude Code session: "
                  "it reads its hooks when it starts.",
            backup if settings is not None else None)

    def remove(self) -> HookResult:
        """Take the Clawd Tank entries out of settings.json (backup first). Nothing
        else in the file changes. A malformed file is refused."""
        try:
            settings = install_hooks.load_settings(self.settings_path)
        except SettingsError as e:
            return HookResult(False, f"{e}\n\nNothing was changed.")
        if settings is None:
            return HookResult(True, "There is no settings.json, so there are no hooks to remove.")
        remaining, changed = install_hooks.plan_remove(settings, hooks)
        if not changed:
            return HookResult(True, "The Clawd Tank hooks were not in settings.json. "
                                    "Nothing was changed.")
        backup = install_hooks.backup_path_for(self.settings_path, self._now())
        try:
            install_hooks.apply_remove(self.settings_path, remaining, backup)
        except OSError as e:
            return HookResult(False, f"Could not remove the hooks: {e}")
        return HookResult(True, "The hooks are removed. Restart any running Claude Code "
                                "session for it to stop calling the notify script.", backup)


def _same_text(a: str, b: str) -> bool:
    return a.replace("\r\n", "\n") == b.replace("\r\n", "\n")
