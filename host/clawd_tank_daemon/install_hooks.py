"""Install the Clawd Tank hooks into Claude Code's settings.json, without the menu-bar app.

    py -3 -m clawd_tank_daemon.install_hooks            (Windows)
    python3 -m clawd_tank_daemon.install_hooks          (macOS, Linux)

What it does, in order:

  1. Prints the "hooks" block it would register and what would change.
  2. Waits for you to type ``yes`` at a terminal. Anything else, end of input,
     or a stdin that is not a terminal changes nothing. There is deliberately
     no ``--yes`` flag: a script cannot approve an edit of your settings.
  3. Only then: copies the standalone notify script to
     ``~/.clawd-tank/clawd-tank-notify.py`` (``clawd-tank-notify`` on
     macOS/Linux), copies settings.json to a timestamped backup next to it,
     and writes the merged settings.json (via a temp file, then a rename).

The merge is hooks.py's: your own hooks, every other settings key and every
event this does not manage are kept; running it twice changes nothing the
second time; a malformed settings.json is refused, never overwritten.

``--print`` shows steps 1 only (no prompt, no writes).

Settings file: ``$CLAUDE_CONFIG_DIR/settings.json`` if that variable is set,
else ``~/.claude/settings.json`` (``%USERPROFILE%\\.claude\\settings.json``
on Windows). Override with ``--settings PATH``.
"""

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Optional, TextIO

from .procutil import stdin_is_terminal


class SettingsError(Exception):
    """settings.json exists but cannot be merged into safely."""


def default_settings_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    return base / "settings.json"


def load_settings(path: Path) -> Optional[dict]:
    """The parsed settings, or None if the file does not exist.

    Raises SettingsError if it exists but is unreadable, not JSON, or not a JSON
    object: the caller must then leave it alone rather than replace it.
    """
    if not path.exists():
        return None
    try:
        # utf-8-sig: Notepad and some editors save a BOM; json rejects it.
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise SettingsError(f"cannot read {path}: {e}") from e
    if not text.strip():
        return {}
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as e:
        raise SettingsError(
            f"{path} is not valid JSON ({e}). Fix it by hand and run this again; "
            f"nothing was changed."
        ) from e
    if not isinstance(settings, dict):
        raise SettingsError(f"{path} does not hold a JSON object; nothing was changed.")
    return settings


def plan_merge(settings: Optional[dict], hooks_module) -> tuple:
    """(merged settings, per-event change list). Pure; does not touch `settings`.

    Each change is ``(event, "add" | "update" | "ok")``: add = the event had no
    Clawd Tank hook, update = it had a different/stale one, ok = already right.
    """
    before = copy.deepcopy(settings) if settings is not None else {}
    merged = hooks_module.merge_hooks(copy.deepcopy(before))
    old_hooks = before.get("hooks") if isinstance(before.get("hooks"), dict) else {}
    new_hooks = merged["hooks"]
    changes = []
    for event in hooks_module.HOOKS_CONFIG:
        if old_hooks.get(event) == new_hooks.get(event):
            changes.append((event, "ok"))
        elif any(
            hooks_module._group_runs_our_command(g)
            for g in (old_hooks.get(event) if isinstance(old_hooks.get(event), list) else [])
        ):
            changes.append((event, "update"))
        else:
            changes.append((event, "add"))
    return merged, changes


def backup_path_for(path: Path, now: datetime) -> Path:
    return path.with_name(f"{path.name}.bak-{now.strftime('%Y%m%d-%H%M%S')}")


def write_settings_atomic(path: Path, settings: dict) -> None:
    """Write via a temp file in the same folder, then rename over the target, so
    a crash or a full disk never leaves a half-written settings.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(settings, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(
    argv: Optional[list] = None,
    *,
    input_fn: Callable[[str], str] = input,
    is_tty: Optional[bool] = None,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
    now: Optional[Callable[[], datetime]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """Run the installer. Returns the exit code: 0 done / nothing to do / --print,
    1 declined or refused (nothing changed), 2 an error."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    now = datetime.now if now is None else now
    if is_tty is None:
        is_tty = stdin_is_terminal()

    parser = argparse.ArgumentParser(
        prog="python -m clawd_tank_daemon.install_hooks",
        description="Add the Clawd Tank hooks to Claude Code's settings.json "
                    "(asks you to type 'yes' first).",
    )
    parser.add_argument("--settings", type=Path, default=None,
                        help="settings.json to edit (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="show the hooks block and the changes; write nothing, ask nothing")
    args = parser.parse_args(argv)

    # The hook logic lives with the menu-bar code but imports no rumps/launchd.
    from clawd_tank_menubar import hooks

    settings_path = args.settings or default_settings_path(env)

    def say(text: str = "") -> None:
        print(text, file=out)

    try:
        settings = load_settings(settings_path)
    except SettingsError as e:
        print(f"Error: {e}", file=err)
        return 2

    merged, changes = plan_merge(settings, hooks)
    already = settings is not None and all(kind == "ok" for _, kind in changes) \
        and hooks.settings_have_our_hooks(settings)

    say(f"Settings file : {settings_path}"
        + ("" if settings is not None else "  (does not exist yet; it would be created)"))
    say(f"Notify script : {hooks.NOTIFY_SCRIPT_PATH}")
    say(f"Hook command  : {hooks.HOOK_COMMAND}")
    say()
    say('The "hooks" block that would be registered:')
    say(json.dumps({"hooks": hooks.HOOKS_CONFIG}, indent=2))
    say()
    say("Changes to the settings file (your other settings and hooks are kept):")
    width = max(len(event) for event, _ in changes)
    labels = {"add": "add", "update": "replace the older Clawd Tank hook", "ok": "already in place"}
    for event, kind in changes:
        say(f"  {event:<{width}}  {labels[kind]}")
    say()

    if already:
        say("The hooks are already installed exactly as above. Nothing to do.")
        return 0

    if args.print_only:
        say("--print: nothing was written.")
        return 0

    if not is_tty:
        print("Error: stdin is not a terminal, so there is nobody to type 'yes'. "
              "Run this command in a terminal window. Nothing was changed.", file=err)
        return 1

    backup = backup_path_for(settings_path, now())
    say("If you type yes, this will:")
    say(f"  1. write the notify script to {hooks.NOTIFY_SCRIPT_PATH}")
    if settings is not None:
        say(f"  2. copy {settings_path.name} to {backup}")
        say(f"  3. rewrite {settings_path} with the hooks merged in")
    else:
        say(f"  2. create {settings_path}")
    say()
    try:
        answer = input_fn("Type yes to install the hooks, anything else to cancel: ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() != "yes":
        say("Cancelled. Nothing was changed.")
        return 1

    try:
        hooks.write_notify_script(hooks.NOTIFY_SCRIPT_PATH)
        if settings is not None:
            shutil.copy2(settings_path, backup)
        write_settings_atomic(settings_path, merged)
    except OSError as e:
        print(f"Error: {e}", file=err)
        return 2

    verify = load_settings(settings_path)
    if not hooks.settings_have_our_hooks(verify):
        print("Error: the settings file was written but does not read back with the "
              "hooks in place; restore the backup if you made one.", file=err)
        return 2

    say("Done.")
    say(f"  notify script : {hooks.NOTIFY_SCRIPT_PATH}")
    if settings is not None:
        say(f"  backup        : {backup}")
    say(f"  settings      : {settings_path}")
    say()
    say("Restart any running Claude Code session: hooks are read when it starts.")
    say("The daemon must be running for the hooks to do anything:")
    say("  python -m clawd_tank_daemon")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
