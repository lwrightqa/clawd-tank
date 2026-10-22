"""The tray app's hook handling: consent logic, install, remove, on temp files only.

Every test points the HookManager at a settings.json in a temp folder and moves
``hooks.NOTIFY_SCRIPT_PATH`` (where the notify script is written) there too, so
the real ``%USERPROFILE%\\.claude\\settings.json`` is never read or written.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from clawd_tank_daemon import install_hooks
from clawd_tank_menubar import hooks
from clawd_tank_tray.hookctl import HookManager, HookStatus, should_prompt
from clawd_tank_tray.options import ENV_CLAUDE_DIR, settings_path

FIXED_NOW = datetime(2026, 10, 21, 12, 30, 45)
STAMP = "20261021-123045"


@pytest.fixture
def env(tmp_path, monkeypatch):
    script = tmp_path / "home" / ".clawd-tank" / "clawd-tank-notify.py"
    monkeypatch.setattr(hooks, "NOTIFY_SCRIPT_PATH", script)
    settings = tmp_path / "claude" / "settings.json"
    mgr = HookManager(settings, now=lambda: FIXED_NOW, which=lambda name: "C:/Windows/py.exe")
    return mgr, settings, script


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


USER_SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"]},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
        "MyOwnEvent": [{"hooks": [{"type": "command", "command": "echo custom"}]}],
    },
}


# --- status -------------------------------------------------------------------------


def test_status_with_nothing_installed(env):
    mgr, settings, script = env
    st = mgr.status()
    assert not st.installed and not st.hooks_present and not st.script_present
    assert st.text == "Not installed" and st.settings_error == ""
    write(settings, {"model": "x"})
    assert mgr.status().text == "Not installed"


def test_status_installed_after_install(env):
    mgr, settings, script = env
    assert mgr.install().ok
    st = mgr.status()
    assert st.installed and st.text == "Installed" and st.warning == ""


def test_status_flags_a_missing_or_outdated_notify_script(env):
    mgr, settings, script = env
    mgr.install()
    script.write_text("# an older script\n", encoding="utf-8")
    st = mgr.status()
    assert st.hooks_present and st.script_present and not st.script_current
    assert not st.installed and st.text.startswith("Outdated")
    script.unlink()
    st = mgr.status()
    assert not st.installed and "missing" in st.text


def test_a_crlf_copy_of_the_script_still_counts_as_current(env):
    mgr, settings, script = env
    mgr.install()
    script.write_bytes(script.read_bytes().replace(b"\n", b"\r\n"))
    assert mgr.status().installed


def test_status_reports_a_missing_py_launcher(env):
    mgr, settings, script = env
    mgr.install()
    no_py = HookManager(settings, now=lambda: FIXED_NOW, which=lambda name: None)
    st = no_py.status()
    assert not st.launcher_found
    assert "py launcher" in st.warning
    assert HookManager(settings, which=lambda n: "C:/py.exe").status().warning == ""
    # nothing to warn about while no hooks exist yet
    write(settings, {})
    assert no_py.status().warning == ""


# --- the consent dialog's text ---------------------------------------------------------


def test_consent_text_names_the_file_the_backup_and_what_is_kept(env):
    mgr, settings, script = env
    write(settings, {"model": "x"})
    text = mgr.consent_text("install")
    assert str(settings) in text
    assert f"settings.json.bak-{STAMP}" in text
    assert "backup" in text.lower() and "first" in text
    assert "other settings" in text and "hooks are kept" in text
    assert str(script) in text and text.rstrip().endswith("Add the hooks?")


def test_consent_text_when_there_is_no_settings_file_says_it_creates_one(env):
    mgr, settings, script = env
    text = mgr.consent_text("install")
    assert "creates" in text and str(settings) in text and "backup" not in text.lower()


def test_consent_text_names_the_real_default_file():
    """Without overrides the file is %USERPROFILE%\\.claude\\settings.json."""
    mgr = HookManager(Path.home() / ".claude" / "settings.json", now=lambda: FIXED_NOW)
    assert str(Path.home() / ".claude" / "settings.json") in mgr.consent_text()


def test_consent_text_for_removal(env):
    mgr, settings, script = env
    write(settings, {"hooks": {}})
    text = mgr.consent_text("remove")
    assert str(settings) in text and "backup" in text and "only" in text.lower()


# --- should we ask --------------------------------------------------------------------------


def status(**kw) -> HookStatus:
    base = dict(settings_path=Path("s"), script_path=Path("n"), hooks_present=False,
                script_present=False, script_current=False)
    base.update(kw)
    return HookStatus(**base)


def test_should_prompt_on_first_run():
    assert should_prompt(status(), declined=False)


def test_should_not_prompt_when_everything_is_in_place():
    assert not should_prompt(status(hooks_present=True, script_present=True, script_current=True), False)


def test_should_prompt_to_repair_even_after_a_no():
    assert should_prompt(status(hooks_present=True, script_present=True, script_current=False), False)
    assert should_prompt(status(hooks_present=True, script_present=False), declined=True)


def test_a_no_is_remembered_so_it_is_not_asked_on_every_launch():
    assert not should_prompt(status(), declined=True)


def test_should_not_prompt_for_a_settings_file_that_cannot_be_merged():
    assert not should_prompt(status(settings_error="not valid JSON"), declined=False)


# --- install -----------------------------------------------------------------------------------


def test_install_creates_settings_when_there_is_none(env):
    mgr, settings, script = env
    res = mgr.install()
    assert res.ok and res.backup is None
    assert hooks.settings_have_our_hooks(read(settings))
    assert script.read_text(encoding="utf-8") == hooks.NOTIFY_SCRIPT
    assert b"\r\n" not in script.read_bytes()
    assert list(settings.parent.glob("settings.json.bak-*")) == []  # nothing to back up


def test_install_keeps_every_other_setting_and_hook_and_backs_up_first(env):
    mgr, settings, script = env
    write(settings, USER_SETTINGS)
    original = settings.read_bytes()
    res = mgr.install()
    assert res.ok
    assert res.backup == settings.with_name(f"settings.json.bak-{STAMP}")
    assert res.backup.read_bytes() == original  # a byte-for-byte copy of what was there

    after = read(settings)
    assert after["model"] == "opus" and after["permissions"] == USER_SETTINGS["permissions"]
    assert after["hooks"]["MyOwnEvent"] == USER_SETTINGS["hooks"]["MyOwnEvent"]
    for event in ("PreToolUse", "Stop"):
        assert USER_SETTINGS["hooks"][event][0] in after["hooks"][event]  # the user's group, untouched
    assert hooks.settings_have_our_hooks(after)
    assert not list(settings.parent.glob("*.tmp"))  # the atomic write left no temp file


def test_install_twice_changes_nothing_the_second_time(env):
    mgr, settings, script = env
    write(settings, USER_SETTINGS)
    assert mgr.install().ok
    first = settings.read_bytes()
    script_bytes = script.read_bytes()
    second = mgr.install()
    assert second.ok and "already installed" in second.message
    assert settings.read_bytes() == first and script.read_bytes() == script_bytes
    assert len(list(settings.parent.glob("settings.json.bak-*"))) == 1  # no second backup


def test_install_repairs_an_outdated_script(env):
    mgr, settings, script = env
    mgr.install()
    script.write_text("# stale\n", encoding="utf-8")
    res = mgr.install()
    assert res.ok and mgr.status().installed
    assert script.read_text(encoding="utf-8") == hooks.NOTIFY_SCRIPT


def test_install_upgrades_a_stale_clawd_group_without_duplicating(env):
    mgr, settings, script = env
    stale = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": hooks.HOOK_COMMAND}]}]}}
    write(settings, stale)
    assert mgr.install().ok
    groups = read(settings)["hooks"]["PreToolUse"]
    assert sum(1 for g in groups if hooks._group_runs_our_command(g)) == 1


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"hooks": '])
def test_install_refuses_a_settings_file_it_cannot_merge_and_leaves_it_alone(env, content):
    mgr, settings, script = env
    write(settings, content)
    before = settings.read_bytes()
    res = mgr.install()
    assert not res.ok
    assert "Nothing was changed" in res.message
    assert settings.read_bytes() == before
    assert not list(settings.parent.glob("settings.json.bak-*"))
    assert not script.exists()  # not even the helper script is written
    assert mgr.status().settings_error and mgr.status().text == "settings.json is not valid JSON"


def test_the_malformed_message_explains_what_to_do(env):
    mgr, settings, script = env
    write(settings, "{oops")
    res = mgr.install()
    assert "not valid JSON" in res.message and "Fix it by hand" in res.message


def test_a_bom_in_settings_json_is_fine(env):
    mgr, settings, script = env
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"\xef\xbb\xbf" + json.dumps({"model": "x"}).encode())
    assert mgr.install().ok and read(settings)["model"] == "x"


def test_install_reports_a_disk_error_without_corrupting_settings(env, monkeypatch):
    mgr, settings, script = env
    write(settings, USER_SETTINGS)
    before = settings.read_bytes()

    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(install_hooks, "write_settings_atomic", boom)
    res = mgr.install()
    assert not res.ok and "disk full" in res.message
    assert settings.read_bytes() == before


# --- remove --------------------------------------------------------------------------------------


def test_remove_is_the_exact_inverse_of_install_on_user_settings(env):
    mgr, settings, script = env
    write(settings, USER_SETTINGS)
    mgr.install()
    res = mgr.remove()
    assert res.ok and res.backup is not None
    assert read(settings) == USER_SETTINGS
    assert not mgr.status().hooks_present


def test_remove_backs_up_first(env):
    mgr, settings, script = env
    mgr.install()
    with_hooks = settings.read_bytes()
    res = mgr.remove()
    assert res.backup == settings.with_name(f"settings.json.bak-{STAMP}")
    assert res.backup.read_bytes() == with_hooks


def test_remove_drops_the_hooks_object_when_it_was_only_ours(env):
    mgr, settings, script = env
    write(settings, {"model": "x"})
    mgr.install()
    mgr.remove()
    assert read(settings) == {"model": "x"}


def test_remove_keeps_a_group_the_user_shares_with_us(env):
    mgr, settings, script = env
    shared = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "mine --flag"},
        {"type": "command", "command": hooks.HOOK_COMMAND},
    ]}]}}
    write(settings, shared)
    res = mgr.remove()
    assert res.ok
    assert read(settings)["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "mine --flag"}]}]


def test_remove_leaves_lookalikes_alone(env):
    mgr, settings, script = env
    sneaky = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": f"cat {hooks.HOOK_COMMAND}"}]}]}}
    write(settings, sneaky)
    res = mgr.remove()
    assert res.ok and "not in settings.json" in res.message
    assert read(settings) == sneaky
    assert not list(settings.parent.glob("*.bak-*"))  # nothing changed, so no backup either


def test_remove_removes_our_entries_from_events_this_version_does_not_manage(env):
    mgr, settings, script = env
    write(settings, {"hooks": {"OldEvent": [
        {"hooks": [{"type": "command", "command": hooks.HOOK_COMMAND}]}]}})
    assert mgr.remove().ok
    assert read(settings) == {}


def test_remove_without_settings_or_hooks(env):
    mgr, settings, script = env
    res = mgr.remove()
    assert res.ok and not settings.exists()
    write(settings, {"model": "x"})
    res = mgr.remove()
    assert res.ok and read(settings) == {"model": "x"}


def test_remove_refuses_a_malformed_file(env):
    mgr, settings, script = env
    write(settings, "{broken")
    before = settings.read_bytes()
    res = mgr.remove()
    assert not res.ok and settings.read_bytes() == before
    assert not list(settings.parent.glob("*.bak-*"))


def test_remove_leaves_the_notify_script_alone(env):
    mgr, settings, script = env
    mgr.install()
    mgr.remove()
    assert script.exists()


# --- where the settings file is -----------------------------------------------------------------------


def test_settings_path_override_beats_claude_config_dir(tmp_path):
    env = {ENV_CLAUDE_DIR: str(tmp_path / "a"), "CLAUDE_CONFIG_DIR": str(tmp_path / "b")}
    assert settings_path(env) == tmp_path / "a" / "settings.json"
    assert settings_path({"CLAUDE_CONFIG_DIR": str(tmp_path / "b")}) == tmp_path / "b" / "settings.json"
    assert settings_path({}) == Path.home() / ".claude" / "settings.json"
