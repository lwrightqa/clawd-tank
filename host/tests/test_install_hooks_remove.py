"""install_hooks.py's inverse of the merge (plan_remove / apply_remove) and the
shared write sequence (apply_install). Temp files only."""

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from clawd_tank_daemon import install_hooks
from clawd_tank_menubar import hooks

NOW = datetime(2026, 10, 21, 12, 30, 45)

USER = {
    "model": "opus",
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}],
        "MyOwnEvent": [{"hooks": [{"type": "command", "command": "echo custom"}]}],
    },
}


@pytest.fixture(autouse=True)
def temp_script(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "NOTIFY_SCRIPT_PATH", tmp_path / "home" / "n.py")


def test_plan_remove_undoes_plan_merge_and_does_not_modify_its_input():
    merged, _ = install_hooks.plan_merge(copy.deepcopy(USER), hooks)
    snapshot = copy.deepcopy(merged)
    remaining, changed = install_hooks.plan_remove(merged, hooks)
    assert merged == snapshot
    assert remaining == USER
    assert set(changed) == set(hooks.HOOKS_CONFIG)


def test_plan_remove_edge_inputs():
    assert install_hooks.plan_remove(None, hooks) == ({}, [])
    assert install_hooks.plan_remove({}, hooks) == ({}, [])
    assert install_hooks.plan_remove({"hooks": "weird"}, hooks) == ({"hooks": "weird"}, [])
    odd = {"hooks": {"Stop": "not a list", "Other": [7, {"hooks": "no"}, {"nothooks": 1}]}}
    assert install_hooks.plan_remove(odd, hooks) == (odd, [])


def test_plan_remove_keeps_the_users_part_of_a_shared_group_and_its_matcher():
    shared = {"hooks": {"Notification": [{
        "matcher": "idle_prompt",
        "hooks": [{"type": "command", "command": "mine"},
                  {"type": "command", "command": hooks.HOOK_COMMAND}]}]}}
    remaining, changed = install_hooks.plan_remove(shared, hooks)
    assert changed == ["Notification"]
    assert remaining["hooks"]["Notification"] == [
        {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "mine"}]}]


def test_plan_remove_ignores_commands_that_merely_mention_our_script():
    lookalikes = [f"cat {hooks.HOOK_COMMAND}",
                  hooks.HOOK_COMMAND + "-old",                         # another script, same prefix
                  hooks.HOOK_COMMAND.replace("py -3", "py -2")]        # on Windows: another interpreter
    lookalike = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": c} for c in lookalikes if c != hooks.HOOK_COMMAND]}]}}
    assert install_hooks.plan_remove(lookalike, hooks) == (lookalike, [])


def test_plan_remove_drops_empty_events_and_an_emptied_hooks_object_only():
    only_ours = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": hooks.HOOK_COMMAND}]}]},
                 "model": "x"}
    assert install_hooks.plan_remove(only_ours, hooks)[0] == {"model": "x"}
    kept = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": hooks.HOOK_COMMAND}]}],
                      "Mine": [{"hooks": [{"type": "command", "command": "x"}]}]}}
    assert install_hooks.plan_remove(kept, hooks)[0] == {
        "hooks": {"Mine": [{"hooks": [{"type": "command", "command": "x"}]}]}}
    untouched_empty = {"hooks": {}}
    assert install_hooks.plan_remove(untouched_empty, hooks)[0] == {"hooks": {}}  # not ours to drop


def test_apply_remove_backs_up_then_writes_atomically(tmp_path):
    path = tmp_path / "settings.json"
    merged, _ = install_hooks.plan_merge(copy.deepcopy(USER), hooks)
    path.write_text(json.dumps(merged), encoding="utf-8")
    before = path.read_bytes()
    backup = install_hooks.backup_path_for(path, NOW)
    remaining, _ = install_hooks.plan_remove(merged, hooks)
    install_hooks.apply_remove(path, remaining, backup)
    assert backup.name == "settings.json.bak-20261021-123045" and backup.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8")) == USER
    assert not list(tmp_path.glob("*.tmp"))


def test_apply_install_writes_script_backup_then_settings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(USER), encoding="utf-8")
    merged, _ = install_hooks.plan_merge(USER, hooks)
    backup = install_hooks.backup_path_for(path, NOW)
    assert install_hooks.apply_install(hooks, path, USER, merged, backup) is True
    assert hooks.NOTIFY_SCRIPT_PATH.read_text(encoding="utf-8") == hooks.NOTIFY_SCRIPT
    assert json.loads(backup.read_text(encoding="utf-8")) == USER
    assert hooks.settings_have_our_hooks(json.loads(path.read_text(encoding="utf-8")))


def test_apply_install_with_no_existing_file_makes_no_backup(tmp_path):
    path = tmp_path / "new" / "settings.json"
    merged, _ = install_hooks.plan_merge(None, hooks)
    backup = install_hooks.backup_path_for(path, NOW)
    assert install_hooks.apply_install(hooks, path, None, merged, backup) is True
    assert path.exists() and not backup.exists()


def test_apply_install_reports_a_file_that_does_not_read_back_with_our_hooks(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    backup = install_hooks.backup_path_for(path, NOW)
    # a "merged" dict that lacks the hooks: the read-back check must say so
    assert install_hooks.apply_install(hooks, path, {}, {"model": "x"}, backup) is False


def test_the_command_line_installer_still_installs_and_backs_up(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(USER), encoding="utf-8")
    code = install_hooks.run(["--settings", str(path)], input_fn=lambda _p: "yes",
                             is_tty=True, now=lambda: NOW)
    assert code == 0
    assert hooks.settings_have_our_hooks(json.loads(path.read_text(encoding="utf-8")))
    assert path.with_name("settings.json.bak-20261021-123045").exists()


def test_the_command_line_installer_still_refuses_a_malformed_file(tmp_path, capsys):
    path = tmp_path / "settings.json"
    path.write_text("{bad", encoding="utf-8")
    code = install_hooks.run(["--settings", str(path)], input_fn=lambda _p: "yes", is_tty=True)
    assert code == 2 and path.read_text(encoding="utf-8") == "{bad"
    assert not hooks.NOTIFY_SCRIPT_PATH.exists()
