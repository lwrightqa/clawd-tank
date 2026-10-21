"""The hook installer CLI and the merge logic it reuses from hooks.py.

Nothing here touches the real ~/.claude/settings.json: every test passes
--settings pointing into tmp_path, and the notify script's destination
(hooks.NOTIFY_SCRIPT_PATH) is redirected there too.
"""

import copy
import io
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from clawd_tank_daemon import install_hooks as cli
from clawd_tank_menubar import hooks

HOST_DIR = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 20, 23, 15, 7)
BACKUP_SUFFIX = ".bak-20261020-231507"


@pytest.fixture
def notify_path(tmp_path, monkeypatch):
    path = tmp_path / "clawd" / "clawd-tank-notify.py"
    monkeypatch.setattr(hooks, "NOTIFY_SCRIPT_PATH", path)
    return path


@pytest.fixture
def settings(tmp_path):
    return tmp_path / "claude" / "settings.json"


def run(settings_path, *extra, answer="yes", tty=True):
    """Run the CLI; returns (exit code, stdout, stderr, asked) where `asked` is the list of prompts."""
    out, err, asked = io.StringIO(), io.StringIO(), []

    def input_fn(prompt):
        asked.append(prompt)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    code = cli.run(["--settings", str(settings_path), *extra], input_fn=input_fn, is_tty=tty,
                   out=out, err=err, now=lambda: NOW)
    return code, out.getvalue(), err.getvalue(), asked


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def commands(settings_dict, event):
    return [h.get("command") for g in settings_dict.get("hooks", {}).get(event, [])
            for h in g.get("hooks", [])]


# --- typing yes ---------------------------------------------------------------------


def test_yes_installs_hooks_script_and_backup(settings, notify_path):
    settings.parent.mkdir(parents=True)
    original = {"model": "opus", "permissions": {"allow": ["Bash"]},
                "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "/my/own/hook"}]}]}}
    settings.write_text(json.dumps(original), encoding="utf-8")

    code, out, err, asked = run(settings)

    assert code == 0, err
    assert len(asked) == 1 and "yes" in asked[0]
    merged = read(settings)
    assert merged["model"] == "opus" and merged["permissions"] == {"allow": ["Bash"]}
    assert "/my/own/hook" in commands(merged, "SessionStart")
    assert hooks.HOOK_COMMAND in commands(merged, "SessionStart")
    for event in hooks.HOOKS_CONFIG:
        assert hooks.HOOK_COMMAND in commands(merged, event), event
    # the timestamped backup is the file as it was, byte for byte
    backup = settings.with_name("settings.json" + BACKUP_SUFFIX)
    assert read(backup) == original
    # the notify script it points at now exists
    assert notify_path.read_text(encoding="utf-8") == hooks.NOTIFY_SCRIPT
    assert str(backup) in out and "Restart" in out


def test_yes_is_case_and_space_insensitive(settings, notify_path):
    code, *_ = run(settings, answer="  YES \n")
    assert code == 0 and settings.exists()


@pytest.mark.parametrize("answer", ["", "y", "no", "yes please", "ok", "true", "1"])
def test_anything_but_yes_changes_nothing(settings, notify_path, answer):
    settings.parent.mkdir(parents=True)
    settings.write_text('{"model": "opus"}', encoding="utf-8")
    code, out, err, asked = run(settings, answer=answer)
    assert code == 1
    assert "Cancelled" in out
    assert settings.read_text(encoding="utf-8") == '{"model": "opus"}'
    assert not notify_path.exists()
    assert [p.name for p in settings.parent.iterdir()] == ["settings.json"]  # no backup, no temp file


@pytest.mark.parametrize("exc", [EOFError(), KeyboardInterrupt()])
def test_eof_or_ctrl_c_at_the_prompt_changes_nothing(settings, notify_path, exc):
    code, out, err, asked = run(settings, answer=exc)
    assert code == 1 and not settings.exists() and not notify_path.exists()


def test_not_a_terminal_refuses_without_asking(settings, notify_path):
    """No flag, pipe or script can approve the edit: it needs somebody at a terminal."""
    code, out, err, asked = run(settings, tty=False)
    assert code == 1
    assert asked == []
    assert "not a terminal" in err
    assert not settings.exists() and not notify_path.exists()


def test_there_is_no_flag_that_skips_the_question():
    parser_help = subprocess.run(
        [sys.executable, "-m", "clawd_tank_daemon.install_hooks", "--help"],
        capture_output=True, text=True, cwd=str(HOST_DIR), timeout=30).stdout
    assert "--yes" not in parser_help and "--force" not in parser_help
    with pytest.raises(SystemExit):
        cli.run(["--yes"], is_tty=True)


# --- print mode ----------------------------------------------------------------------------


def test_print_shows_the_block_and_writes_nothing(settings, notify_path):
    code, out, err, asked = run(settings, "--print")
    assert code == 0 and asked == []
    assert '"hooks"' in out and hooks.HOOK_COMMAND.replace('"', '\\"') in out
    for event in hooks.HOOKS_CONFIG:
        assert event in out
    assert not settings.exists() and not notify_path.exists()


def test_print_works_without_a_terminal(settings, notify_path):
    code, out, err, asked = run(settings, "--print", tty=False)
    assert code == 0 and "nothing was written" in out


# --- idempotence -----------------------------------------------------------------------------


def test_second_run_is_a_no_op_with_no_new_backup(settings, notify_path):
    assert run(settings)[0] == 0
    first = settings.read_bytes()
    code, out, err, asked = run(settings)
    assert code == 0 and asked == []
    assert "Nothing to do" in out
    assert settings.read_bytes() == first
    assert sorted(p.name for p in settings.parent.iterdir()) == ["settings.json"]  # a fresh install had nothing to back up


def test_rerun_after_a_user_edit_keeps_the_edit_and_backs_up(settings, notify_path):
    run(settings)
    edited = read(settings)
    edited["model"] = "sonnet"
    edited["hooks"]["Stop"].insert(0, {"hooks": [{"type": "command", "command": "/theirs"}]})
    del edited["hooks"]["SessionEnd"]
    settings.write_text(json.dumps(edited), encoding="utf-8")

    code, out, err, asked = run(settings)

    assert code == 0 and len(asked) == 1
    final = read(settings)
    assert final["model"] == "sonnet"
    assert "/theirs" in commands(final, "Stop")
    assert hooks.HOOK_COMMAND in commands(final, "SessionEnd")
    assert hooks.settings_have_our_hooks(final)
    assert read(settings.with_name("settings.json" + BACKUP_SUFFIX)) == edited


# --- refusing to damage a settings file ---------------------------------------------------------


@pytest.mark.parametrize("text", ["{ not json", '{"hooks": {', '// comment\n{}', "[1, 2, 3]", '"just a string"'])
def test_malformed_or_non_object_settings_are_refused_and_untouched(settings, notify_path, text):
    settings.parent.mkdir(parents=True)
    settings.write_text(text, encoding="utf-8")
    code, out, err, asked = run(settings)
    assert code == 2
    assert asked == []
    assert "nothing was changed" in err.lower()
    assert settings.read_text(encoding="utf-8") == text
    assert not notify_path.exists()
    assert [p.name for p in settings.parent.iterdir()] == ["settings.json"]


def test_utf8_bom_settings_file_is_accepted(settings, notify_path):
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"\xef\xbb\xbf" + json.dumps({"note": "caf\u00e9"}).encode("utf-8"))
    code, out, err, asked = run(settings)
    assert code == 0, err
    assert read(settings)["note"] == "caf\u00e9"  # round-trips, written back without a BOM
    assert not settings.read_bytes().startswith(b"\xef\xbb\xbf")


def test_empty_settings_file_is_treated_as_empty(settings, notify_path):
    settings.parent.mkdir(parents=True)
    settings.write_text("  \n", encoding="utf-8")
    code, *_ = run(settings)
    assert code == 0 and hooks.settings_have_our_hooks(read(settings))


def test_settings_written_with_lf_endings_and_trailing_newline(settings, notify_path):
    run(settings)
    data = settings.read_bytes()
    assert b"\r" not in data and data.endswith(b"\n")


def test_unwritable_destination_reports_an_error_and_leaves_settings(settings, notify_path, monkeypatch):
    settings.parent.mkdir(parents=True)
    settings.write_text('{"model": "opus"}', encoding="utf-8")

    def boom(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(cli, "write_settings_atomic", boom)
    code, out, err, asked = run(settings)
    assert code == 2 and "denied" in err
    assert settings.read_text(encoding="utf-8") == '{"model": "opus"}'
    assert not list(settings.parent.glob("*.tmp"))


def test_atomic_write_failure_leaves_original_and_no_temp(settings, monkeypatch):
    settings.parent.mkdir(parents=True)
    settings.write_text('{"keep": true}', encoding="utf-8")

    def fail_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(cli.os, "replace", fail_replace)
    with pytest.raises(OSError):
        cli.write_settings_atomic(settings, {"new": 1})
    assert settings.read_text(encoding="utf-8") == '{"keep": true}'
    assert [p.name for p in settings.parent.iterdir()] == ["settings.json"]


def test_default_settings_path_honours_claude_config_dir(tmp_path):
    assert cli.default_settings_path({"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}) == tmp_path / "cfg" / "settings.json"
    assert cli.default_settings_path({}) == Path.home() / ".claude" / "settings.json"


# --- the hook command it registers ---------------------------------------------------------------


def test_windows_hook_command_shape():
    cmd = hooks.windows_hook_command(Path("C:/Users/some one/.clawd-tank/clawd-tank-notify.py"))
    assert cmd == 'py -3 "C:/Users/some one/.clawd-tank/clawd-tank-notify.py"'
    assert not cmd.startswith("#!") and "bash" not in cmd and ".sh" not in cmd and "\\" not in cmd


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path handling")
def test_windows_hook_command_converts_backslashes():
    cmd = hooks.windows_hook_command(Path(r"C:\Users\me\.clawd-tank\clawd-tank-notify.py"))
    assert cmd == 'py -3 "C:/Users/me/.clawd-tank/clawd-tank-notify.py"'


@pytest.mark.skipif(sys.platform != "win32", reason="Windows default command")
def test_on_windows_the_registered_command_is_py_launcher_not_a_shebang_script():
    assert hooks.HOOK_COMMAND.startswith('py -3 "')
    assert hooks.HOOK_COMMAND.endswith('clawd-tank-notify.py"')
    assert hooks.NOTIFY_SCRIPT_PATH.suffix == ".py"
    for event, groups in hooks.HOOKS_CONFIG.items():
        for g in groups:
            for h in g["hooks"]:
                assert h["command"] == hooks.HOOK_COMMAND, event


def test_hook_command_is_recognised_as_ours_even_with_args():
    assert hooks._command_is_ours(hooks.HOOK_COMMAND)
    assert hooks._command_is_ours(hooks.HOOK_COMMAND + " --extra")
    assert not hooks._command_is_ours("/bin/cat " + hooks.HOOK_COMMAND)


# --- merge_hooks / plan_merge (pure) --------------------------------------------------------------------


def test_merge_hooks_is_pure_given_a_copy_and_idempotent():
    original = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/theirs"}]}]}}
    frozen = copy.deepcopy(original)
    merged = hooks.merge_hooks(copy.deepcopy(original))
    assert original == frozen  # merging a copy leaves the source alone
    assert hooks.merge_hooks(copy.deepcopy(merged)) == merged
    assert merged["model"] == "opus"
    assert commands(merged, "Stop")[0] == "/theirs"  # user's group stays first


@pytest.mark.parametrize("bad", [None, [], "x", 3, {"hooks": None}, {"hooks": []}, {"hooks": {"Stop": "oops"}}])
def test_merge_hooks_survives_malformed_input(bad):
    merged = hooks.merge_hooks(copy.deepcopy(bad))
    assert hooks.settings_have_our_hooks(merged)


def test_merge_hooks_prunes_only_our_own_stale_groups():
    stale = {"matcher": "Bash", "hooks": [{"type": "command", "command": hooks.HOOK_COMMAND}]}
    shared = {"hooks": [{"type": "command", "command": "/theirs"}, {"type": "command", "command": hooks.HOOK_COMMAND}]}
    merged = hooks.merge_hooks({"hooks": {"PostToolUse": [stale], "Stop": [shared]}})
    assert stale not in merged["hooks"]["PostToolUse"]
    assert shared in merged["hooks"]["Stop"]  # shared with the user: never rewritten
    assert hooks.settings_have_our_hooks(merged) is True


def test_plan_merge_labels_each_event():
    before = {"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": hooks.HOOK_COMMAND}]}],           # already right
        "PostToolUse": [{"hooks": [{"type": "command", "command": hooks.HOOK_COMMAND}]}],    # stale matcher
    }}
    merged, changes = cli.plan_merge(before, hooks)
    labels = dict(changes)
    assert labels["Stop"] == "ok"
    assert labels["PostToolUse"] == "update"
    assert labels["SessionStart"] == "add"
    assert hooks.settings_have_our_hooks(merged)
    assert before["hooks"]["Stop"] and "SessionStart" not in before["hooks"]  # input untouched


def test_settings_have_our_hooks_rejects_non_dicts_and_partial_installs():
    assert hooks.settings_have_our_hooks(None) is False
    assert hooks.settings_have_our_hooks([]) is False
    assert hooks.settings_have_our_hooks({"hooks": {"Stop": hooks.HOOKS_CONFIG["Stop"]}}) is False
    assert hooks.settings_have_our_hooks(hooks.merge_hooks({})) is True


def test_the_menu_bar_path_still_writes_utf8_with_lf(tmp_path, monkeypatch):
    """hooks.install_hooks() (the menu bar's entry) reads/writes UTF-8 and LF on every OS."""
    p = tmp_path / "s.json"
    monkeypatch.setattr(hooks, "CLAUDE_SETTINGS_PATH", p)
    p.write_text(json.dumps({"note": "caf\u00e9"}, ensure_ascii=False), encoding="utf-8")
    assert hooks.install_hooks() is True
    data = p.read_bytes()
    assert b"\r" not in data
    assert json.loads(data.decode("utf-8"))["note"] == "caf\u00e9"
    assert hooks.are_hooks_installed() is True


# --- the real entry point -------------------------------------------------------------------------------


def test_module_entry_point_prints_and_exits_without_writing(tmp_path):
    target = tmp_path / "settings.json"
    r = subprocess.run(
        [sys.executable, "-m", "clawd_tank_daemon.install_hooks", "--print", "--settings", str(target)],
        capture_output=True, text=True, cwd=str(HOST_DIR), timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    assert "SessionStart" in r.stdout and "--print: nothing was written." in r.stdout
    assert not target.exists()


def test_module_entry_point_will_not_install_from_a_pipe(tmp_path):
    target = tmp_path / "settings.json"
    r = subprocess.run(
        [sys.executable, "-m", "clawd_tank_daemon.install_hooks", "--settings", str(target)],
        input="yes\n", capture_output=True, text=True, cwd=str(HOST_DIR), timeout=60)
    assert r.returncode == 1
    assert "not a terminal" in r.stderr
    assert not target.exists()


def test_daemon_module_does_not_pull_in_macos_only_code():
    """The daemon must import without rumps, launchd or the menu-bar package."""
    code = (
        "import sys, clawd_tank_daemon.daemon, clawd_tank_daemon.install_hooks; "
        "bad = [m for m in sys.modules if m == 'rumps' or m.endswith('launchd') or m.startswith('AppKit')]; "
        "print(bad)"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=str(HOST_DIR), capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "[]", r.stderr
