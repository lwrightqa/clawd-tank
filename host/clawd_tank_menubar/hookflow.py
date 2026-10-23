"""The Claude Code hooks, as the menu bar app asks for them. No AppKit.

The upstream menu bar app edited ``~/.claude/settings.json`` on every launch
without asking. This one works like the Windows tray app (host/TRAY.md): it
edits the file only after a Yes in a dialog that names the file and says a backup
is made first. ``HookFlow`` is that conversation; the dialogs themselves
(``ask``, ``show``) are passed in, so the flow is tested with fakes.

The work is ``clawd_tank_tray.hookctl.HookManager`` (status, consent text, the
merge, the backup). On a Mac the hook command is the notify script itself, with
no ``py`` launcher, so the manager is told every launcher is found.
"""

import logging
from pathlib import Path
from typing import Callable, Optional

from clawd_tank_tray.hookctl import HookManager, HookResult, HookStatus, should_prompt
from clawd_tank_tray.options import settings_path

logger = logging.getLogger("clawd-tank.menubar.hooks")

APP_NAME = "Clawd Tank"
PREF_HOOKS_DECLINED = "hooks_declined"   # the user said No, or removed the hooks

GetPref = Callable[..., object]          # get_pref(key, default=None)
SetPref = Callable[[str, object], None]  # set_pref(key, value)
Ask = Callable[[str, str], bool]         # ask(title, text) -> True for Yes
Show = Callable[[str, str], None]        # show(title, text)


def make_manager(settings: Optional[Path] = None) -> HookManager:
    """A HookManager for the user's settings.json: $CLAWD_TANK_CLAUDE_DIR (a test
    aid), else $CLAUDE_CONFIG_DIR (which Claude Code honours), else ~/.claude.
    ``which`` answers every name: the Windows-only ``py`` launcher check has no
    meaning on a Mac."""
    return HookManager(settings if settings is not None else settings_path(),
                       which=lambda name: name)


class HookFlow:
    """Ask, then install or remove. Nothing is written without a Yes."""

    def __init__(self, manager: HookManager, get_pref: GetPref, set_pref: SetPref,
                 ask: Ask, show: Show):
        self.manager = manager
        self._get_pref = get_pref
        self._set_pref = set_pref
        self._ask = ask
        self._show = show

    # --- looking ------------------------------------------------------------------

    def status(self) -> Optional[HookStatus]:
        """The manager's status, or None if it could not be read (logged)."""
        try:
            return self.manager.status()
        except Exception:
            logger.exception("could not read the hooks status")
            return None

    @property
    def declined(self) -> bool:
        return bool(self._get_pref(PREF_HOOKS_DECLINED, False))

    # --- at launch ----------------------------------------------------------------

    def first_run(self) -> bool:
        """The question at launch, if one is due (``should_prompt``): no hooks yet
        and not declined before, or hooks whose notify script is missing or old.
        Returns True if a dialog was shown."""
        status = self.status()
        if status is None:
            return False
        if not should_prompt(status, self.declined):
            logger.info("Hooks: %s", status.text)
            return False
        logger.info("Hooks need attention (%s); asking", status.text)
        self._offer(status)
        return True

    # --- what the menu item and the flyout rows do -----------------------------------

    def install(self) -> Optional[HookResult]:
        """Install or repair, after a Yes. Returns the result, or None if nothing
        was changed (already installed, a settings.json that cannot be merged, or No)."""
        return self._offer(self.status())

    def remove(self) -> Optional[HookResult]:
        """Take the Clawd Tank hooks out, after a Yes. Removing them on purpose
        also means the launch question is not asked again."""
        if not self._ask(f"{APP_NAME}: remove the hooks?", self.manager.consent_text("remove")):
            logger.info("Hooks: remove cancelled")
            return None
        result = self._run(self.manager.remove, "remove")
        if result.ok:
            self._set_pref(PREF_HOOKS_DECLINED, True)
        self._report(result)
        return result

    # --- inside ---------------------------------------------------------------------

    def _offer(self, status: Optional[HookStatus]) -> Optional[HookResult]:
        if status is not None and status.installed:
            self._show(APP_NAME, "The Clawd Tank hooks are installed and up to date.")
            return None
        if status is not None and status.settings_error:
            self._show(f"{APP_NAME}: the hooks were not changed",
                       f"{status.settings_error}\n\nNothing was changed.")
            return None
        if not self._ask(f"{APP_NAME}: add the hooks?", self.manager.consent_text("install")):
            logger.info("Hooks: the user said No")
            self._set_pref(PREF_HOOKS_DECLINED, True)
            return None
        result = self._run(self.manager.install, "install")
        if result.ok:
            self._set_pref(PREF_HOOKS_DECLINED, False)
        self._report(result)
        return result

    def _run(self, action: Callable[[], HookResult], what: str) -> HookResult:
        try:
            result = action()
        except Exception as e:  # noqa: BLE001 - a dialog must say so, not a traceback in a callback
            logger.exception("Hooks %s failed", what)
            result = HookResult(False, f"Could not {what} the hooks: {e}")
        logger.info("Hooks %s: %s %s", what, "ok" if result.ok else "failed", result.message)
        return result

    def _report(self, result: HookResult) -> None:
        title = APP_NAME if result.ok else f"{APP_NAME}: the hooks were not changed"
        self._show(title, result.message)
