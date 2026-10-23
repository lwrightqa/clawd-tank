"""HookFlow: the Yes/No conversation before the menu bar app edits settings.json.

Mostly with a fake manager and fake dialogs. The last tests use the real HookManager
on files in tmp_path (and move hooks.NOTIFY_SCRIPT_PATH there too), so the real
~/.claude/settings.json and ~/.clawd-tank are never read or written.
"""

import ast
import json
from datetime import datetime
from pathlib import Path

import pytest

from clawd_tank_menubar import hooks
from clawd_tank_menubar.hookflow import APP_NAME, PREF_HOOKS_DECLINED, HookFlow, make_manager
from clawd_tank_tray.hookctl import HookManager, HookResult, HookStatus

MENUBAR = Path(__file__).resolve().parents[1] / "clawd_tank_menubar"


def status(installed=False, hooks_present=False, script_present=False, error=""):
    return HookStatus(Path("settings.json"), Path("notify"), settings_error=error,
                      hooks_present=hooks_present or installed,
                      script_present=script_present or installed,
                      script_current=installed)


class FakeManager:
    def __init__(self, st=None, install_result=None, remove_result=None):
        self.st = st if st is not None else status()
        self.install_result = install_result or HookResult(True, "installed it")
        self.remove_result = remove_result or HookResult(True, "removed it")
        self.calls = []

    def status(self):
        self.calls.append("status")
        if isinstance(self.st, Exception):
            raise self.st
        return self.st

    def consent_text(self, action="install"):
        return f"consent text for {action}"

    def install(self):
        self.calls.append("install")
        if isinstance(self.install_result, Exception):
            raise self.install_result
        return self.install_result

    def remove(self):
        self.calls.append("remove")
        if isinstance(self.remove_result, Exception):
            raise self.remove_result
        return self.remove_result

    def changed(self):
        return [c for c in self.calls if c != "status"]


class Rig:
    """A HookFlow with a dict for preferences and dialogs that record what they were
    asked and answer ``yes``."""

    def __init__(self, manager, declined=None, yes=True):
        self.manager = manager
        self.prefs = {} if declined is None else {PREF_HOOKS_DECLINED: declined}
        self.yes = yes
        self.asked = []
        self.shown = []
        self.flow = HookFlow(manager, self.prefs.get, self.prefs.__setitem__, self.ask, self.show)

    def ask(self, title, text):
        self.asked.append((title, text))
        return self.yes

    def show(self, title, text):
        self.shown.append((title, text))

    @property
    def declined(self):
        return self.prefs.get(PREF_HOOKS_DECLINED)


# --- the question at launch ------------------------------------------------------------------------


def test_first_run_asks_when_there_are_no_hooks_and_a_yes_installs():
    rig = Rig(FakeManager())
    assert rig.flow.first_run() is True
    assert rig.asked == [(f"{APP_NAME}: add the hooks?", "consent text for install")]
    assert rig.manager.changed() == ["install"]
    assert rig.shown == [(APP_NAME, "installed it")]
    assert rig.declined is False


def test_a_no_changes_nothing_and_is_remembered():
    rig = Rig(FakeManager(), yes=False)
    assert rig.flow.first_run() is True
    assert rig.manager.changed() == [] and rig.shown == []
    assert rig.declined is True


def test_first_run_does_not_ask_again_after_a_no():
    rig = Rig(FakeManager(), declined=True)
    assert rig.flow.first_run() is False
    assert rig.asked == [] and rig.manager.changed() == []


def test_first_run_does_not_ask_when_the_hooks_are_in_place():
    rig = Rig(FakeManager(status(installed=True)))
    assert rig.flow.first_run() is False and rig.asked == []


def test_first_run_does_not_ask_about_a_settings_file_it_cannot_merge():
    rig = Rig(FakeManager(status(error="settings.json is not valid JSON")))
    assert rig.flow.first_run() is False and rig.asked == [] and rig.shown == []


def test_first_run_asks_to_repair_an_old_notify_script_even_after_a_no():
    old = status(hooks_present=True, script_present=True)         # hooks in, script not current
    rig = Rig(FakeManager(old), declined=True)
    assert rig.flow.first_run() is True
    assert rig.manager.changed() == ["install"]


def test_first_run_with_an_unreadable_status_does_nothing():
    rig = Rig(FakeManager(RuntimeError("disk on fire")))
    assert rig.flow.first_run() is False
    assert rig.asked == [] and rig.shown == []


# --- install, from the menu item and the flyout row -------------------------------------------------


def test_install_says_so_when_the_hooks_are_already_in_place():
    rig = Rig(FakeManager(status(installed=True)))
    assert rig.flow.install() is None
    assert rig.asked == [] and rig.manager.changed() == []
    assert rig.shown == [(APP_NAME, "The Clawd Tank hooks are installed and up to date.")]


def test_install_refuses_a_settings_file_it_cannot_merge_and_says_why():
    rig = Rig(FakeManager(status(error="settings.json is not valid JSON")))
    assert rig.flow.install() is None
    assert rig.asked == [] and rig.manager.changed() == []
    title, text = rig.shown[0]
    assert "not changed" in title and "not valid JSON" in text and "Nothing was changed" in text


def test_install_asks_with_the_managers_consent_text_then_installs():
    rig = Rig(FakeManager())
    result = rig.flow.install()
    assert result.ok and rig.asked[0][1] == "consent text for install"
    assert rig.manager.changed() == ["install"]


def test_install_after_a_no_earlier_clears_the_decline_on_a_yes():
    rig = Rig(FakeManager(), declined=True)
    rig.flow.install()
    assert rig.declined is False


def test_install_with_a_no_installs_nothing():
    rig = Rig(FakeManager(), yes=False)
    assert rig.flow.install() is None
    assert rig.manager.changed() == [] and rig.declined is True


def test_a_failed_install_is_shown_as_a_problem_and_leaves_the_decline_alone():
    rig = Rig(FakeManager(install_result=HookResult(False, "could not write")), declined=True)
    result = rig.flow.install()
    assert result.ok is False
    title, text = rig.shown[0]
    assert "not changed" in title and text == "could not write"
    assert rig.declined is True


def test_an_install_that_raises_is_a_message_not_a_traceback():
    rig = Rig(FakeManager(install_result=OSError("read-only file system")))
    result = rig.flow.install()
    assert result.ok is False
    assert "read-only file system" in rig.shown[0][1] and "not changed" in rig.shown[0][0]


# --- remove ------------------------------------------------------------------------------------------------


def test_remove_asks_with_the_remove_text_then_removes_and_does_not_ask_again_later():
    rig = Rig(FakeManager(status(installed=True)))
    result = rig.flow.remove()
    assert result.ok
    assert rig.asked == [(f"{APP_NAME}: remove the hooks?", "consent text for remove")]
    assert rig.manager.changed() == ["remove"]
    assert rig.shown == [(APP_NAME, "removed it")]
    assert rig.declined is True            # removed on purpose: the launch question stays quiet


def test_remove_with_a_no_does_nothing():
    rig = Rig(FakeManager(status(installed=True)), yes=False)
    assert rig.flow.remove() is None
    assert rig.manager.changed() == [] and rig.shown == [] and rig.declined is None


def test_a_failed_remove_is_a_problem_and_does_not_set_the_decline():
    rig = Rig(FakeManager(remove_result=HookResult(False, "could not remove")))
    result = rig.flow.remove()
    assert result.ok is False and "not changed" in rig.shown[0][0]
    assert rig.declined is None


def test_a_remove_that_raises_is_a_message():
    rig = Rig(FakeManager(remove_result=PermissionError("denied")))
    assert rig.flow.remove().ok is False
    assert "denied" in rig.shown[0][1]


# --- status --------------------------------------------------------------------------------------------------


def test_status_is_the_managers():
    st = status(installed=True)
    assert Rig(FakeManager(st)).flow.status() is st


def test_status_is_none_when_it_cannot_be_read():
    assert Rig(FakeManager(RuntimeError("no"))).flow.status() is None


def test_declined_reads_the_preference():
    assert Rig(FakeManager()).flow.declined is False
    assert Rig(FakeManager(), declined=True).flow.declined is True


# --- the manager the app builds ------------------------------------------------------------------------------


def test_the_manager_follows_claude_code_for_where_settings_json_is(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAWD_TANK_CLAUDE_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert make_manager().settings_path == tmp_path / "cfg" / "settings.json"
    monkeypatch.setenv("CLAWD_TANK_CLAUDE_DIR", str(tmp_path / "test"))
    assert make_manager().settings_path == tmp_path / "test" / "settings.json"
    monkeypatch.delenv("CLAWD_TANK_CLAUDE_DIR")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert make_manager().settings_path == Path.home() / ".claude" / "settings.json"


def test_the_manager_finds_a_launcher_on_a_machine_that_has_no_py(tmp_path, monkeypatch):
    """The Windows-only ``py`` check must not report a missing launcher on a Mac."""
    monkeypatch.setattr(hooks, "NOTIFY_SCRIPT_PATH", tmp_path / "notify")
    mgr = make_manager(tmp_path / "settings.json")
    assert mgr.status().launcher_found is True
    assert HookManager(tmp_path / "settings.json", which=lambda name: None).status().launcher_found is False


# --- with the real HookManager, on temp files ------------------------------------------------------------------


@pytest.fixture
def real(tmp_path, monkeypatch):
    script = tmp_path / "home" / ".clawd-tank" / "clawd-tank-notify"
    monkeypatch.setattr(hooks, "NOTIFY_SCRIPT_PATH", script)
    settings = tmp_path / "claude" / "settings.json"
    mgr = make_manager(settings)
    mgr._now = lambda: datetime(2026, 10, 23, 9, 30, 0)
    return mgr, settings, script


def test_nothing_is_written_before_a_yes(real):
    mgr, settings, script = real
    rig = Rig(mgr, yes=False)
    assert rig.flow.first_run() is True
    assert not settings.exists() and not script.exists()
    assert "settings.json" in rig.asked[0][1]                      # the dialog names the file
    assert rig.declined is True


def test_a_yes_writes_the_hooks_with_a_backup_and_keeps_the_users_own(real):
    mgr, settings, script = real
    settings.parent.mkdir(parents=True)
    mine = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
    settings.write_text(json.dumps(mine), encoding="utf-8")
    rig = Rig(mgr)
    assert rig.flow.first_run() is True
    written = json.loads(settings.read_text(encoding="utf-8"))
    assert written["model"] == "opus"
    assert {"hooks": [{"type": "command", "command": "echo mine"}]} in written["hooks"]["Stop"]
    assert hooks.settings_have_our_hooks(written)
    assert script.is_file()
    backups = [p for p in settings.parent.iterdir() if p.name.startswith("settings.json.")]
    assert len(backups) == 1
    assert rig.flow.status().installed
    assert rig.shown and rig.shown[0][0] == APP_NAME


def test_remove_takes_only_our_hooks_out(real):
    mgr, settings, script = real
    rig = Rig(mgr)
    rig.flow.install()
    assert rig.flow.status().installed
    assert rig.flow.remove().ok
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert not hooks.settings_have_our_hooks(after)
    assert rig.declined is True
    assert rig.flow.first_run() is False                           # and it is not asked again


def test_a_settings_file_that_is_not_json_is_left_alone(real):
    mgr, settings, script = real
    settings.parent.mkdir(parents=True)
    settings.write_text("{ not json", encoding="utf-8")
    rig = Rig(mgr)
    assert rig.flow.first_run() is False
    assert rig.flow.install() is None
    assert settings.read_text(encoding="utf-8") == "{ not json" and not script.exists()


# --- the app asks only when it may -----------------------------------------------------------------------------


def test_the_launch_question_is_skipped_by_no_prompt():
    tree = ast.parse((MENUBAR / "app.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ClawdTankApp")
    schedule = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "schedule_startup")
    guards = [n for n in ast.walk(schedule) if isinstance(n, ast.If)
              and "no_prompt" in ast.dump(n.test)
              and any("_first_run" in ast.dump(c) for c in n.body)]
    assert guards, "the first-run question must sit under `if not self._opts.no_prompt`"
