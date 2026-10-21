# host/clawd_tank_menubar/hooks.py
"""Install the Claude Code hook script and configure hooks in settings."""

import copy
import json
import logging
import os
import stat
import sys
import textwrap
from pathlib import Path

from clawd_tank_daemon.protocol import ASK_USER_QUESTION_TOOL

logger = logging.getLogger("clawd-tank.hooks")

IS_WINDOWS = sys.platform == "win32"

CLAWD_DIR = Path.home() / ".clawd-tank"
# Windows has no shebang handling and no executable bit: the script is a .py
# file run through the `py` launcher (see windows_hook_command()).
NOTIFY_SCRIPT_PATH = CLAWD_DIR / ("clawd-tank-notify.py" if IS_WINDOWS else "clawd-tank-notify")
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Standalone hook script — uses only Python stdlib, no external imports.
NOTIFY_SCRIPT = textwrap.dedent('''\
    #!/usr/bin/env python3
    """clawd-tank-notify - Claude Code hook handler for Clawd Tank.

    Reads hook payload from stdin, converts it to a daemon message,
    and forwards it to the daemon: over a Unix socket (macOS, Linux) or over TCP
    on 127.0.0.1 (Windows, or CLAWD_TANK_TRANSPORT=tcp). No external dependencies.
    """
    # NOTIFY_SCRIPT_VERSION: 2026-10-20-windows-tcp

    import json
    import os
    import re
    import socket
    import subprocess
    import sys
    from pathlib import Path

    IS_WINDOWS = sys.platform == "win32"

    # Where the daemon listens. Same rules as clawd_tank_daemon/hook_transport.py
    # (keep in sync): CLAWD_TANK_TRANSPORT = unix | tcp | auto (unix where
    # AF_UNIX exists, tcp elsewhere), CLAWD_TANK_TCP_PORT, CLAWD_TANK_SOCKET.
    SOCKET_PATH = os.environ.get(
        "CLAWD_TANK_SOCKET",
        str(Path.home() / ".clawd-tank" / "sock"),
    )
    TCP_HOST = "127.0.0.1"
    TCP_DEFAULT_PORT = 47421
    # Short on purpose: Windows takes ~2 s to report a refused loopback connect,
    # which would delay every tool call while the daemon is down. A live
    # listener accepts in well under a millisecond. Keep in sync with
    # hook_transport.TCP_CONNECT_TIMEOUT_SECS.
    TCP_CONNECT_TIMEOUT = 0.3

    _CLAUDE_ARGV_RE = re.compile(r"(^|/)claude($|\\s)")


    def _use_tcp():
        pref = (os.environ.get("CLAWD_TANK_TRANSPORT") or "auto").strip().lower()
        return pref == "tcp" or not hasattr(socket, "AF_UNIX")


    def _tcp_port():
        try:
            port = int(os.environ.get("CLAWD_TANK_TCP_PORT") or TCP_DEFAULT_PORT)
        except ValueError:
            return TCP_DEFAULT_PORT
        return port if 1 <= port <= 65535 else TCP_DEFAULT_PORT


    def _connect():
        if _use_tcp():
            sock = socket.create_connection((TCP_HOST, _tcp_port()), timeout=TCP_CONNECT_TIMEOUT)
            sock.settimeout(3.0)
            return sock
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(3.0)
            sock.connect(SOCKET_PATH)
        except BaseException:
            sock.close()
            raise
        return sock


    def _ps(field, pid):
        """Run `ps -o <field>= -p <pid>`, return trimmed stdout. Empty on error."""
        try:
            r = subprocess.run(
                ["ps", "-o", field + "=", "-p", str(pid)],
                capture_output=True, text=True, timeout=1.0,
            )
            return r.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return ""


    def _find_claude_pid_windows():
        """Windows has no `ps`, and Claude Code runs hooks through Git Bash, so
        os.getppid() is a short-lived bash.exe. Use CLAUDE_PID (Claude Code
        exports its own PID), else the first claude.exe ancestor from one
        Toolhelp32 snapshot, else None: the daemon then skips PID dedup and
        liveness for the session instead of trusting a PID that dies at once.
        """
        try:
            pid = int(os.environ.get("CLAUDE_PID", ""))
            if pid > 0:
                return pid
        except ValueError:
            pass
        try:
            import ctypes

            class ENTRY(ctypes.Structure):
                _fields_ = [
                    ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                    ("th32ProcessID", ctypes.c_uint32), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
                    ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260),
                ]

            k32 = ctypes.WinDLL("kernel32")
            k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
            k32.CreateToolhelp32Snapshot.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
            k32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ENTRY))
            k32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ENTRY))
            k32.CloseHandle.argtypes = (ctypes.c_void_p,)
            snap = k32.CreateToolhelp32Snapshot(0x2, 0)
            if not snap or snap == ctypes.c_void_p(-1).value:
                return None
            table = {}
            try:
                e = ENTRY()
                e.dwSize = ctypes.sizeof(ENTRY)
                ok = k32.Process32FirstW(snap, ctypes.byref(e))
                while ok:
                    table[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile.lower())
                    ok = k32.Process32NextW(snap, ctypes.byref(e))
            finally:
                k32.CloseHandle(snap)
        except Exception:
            return None
        pid, seen = os.getppid(), set()
        while pid in table and pid not in seen:
            seen.add(pid)
            parent, exe = table[pid]
            if exe in ("claude.exe", "claude"):
                return pid
            pid = parent
        return None


    def _find_claude_pid():
        """Walk from os.getppid() up the process tree to find the long-lived
        Claude Code PID. Falls back to os.getppid() if no `claude` ancestor.
        On Windows see _find_claude_pid_windows (it may return None).

        Mirrors clawd_tank_daemon/pid_resolver.py — keep in sync.
        """
        if IS_WINDOWS:
            return _find_claude_pid_windows()
        start = os.getppid()
        pid = start
        while pid > 1:
            if _ps("comm", pid) == "claude":
                return pid
            if _CLAUDE_ARGV_RE.search(_ps("command", pid)):
                return pid
            ppid_str = _ps("ppid", pid)
            try:
                pid = int(ppid_str)
            except ValueError:
                break
        return start


    def hook_to_message(hook):
        """Convert a Claude Code hook payload to a daemon message."""
        event_name = hook.get("hook_event_name", "")
        session_id = hook.get("session_id", "")
        cwd = hook.get("cwd", "")
        project = Path(cwd).name if cwd else ""
        pid = _find_claude_pid()

        if event_name == "SessionStart":
            msg = {"event": "session_start", "session_id": session_id, "project": project, "pid": pid}
            source = hook.get("source")
            if source is not None:
                msg["source"] = source
            return msg

        if event_name == "PreToolUse":
            return {"event": "tool_use", "session_id": session_id, "tool_name": hook.get("tool_name", ""), "project": project, "pid": pid}

        if event_name == "PostToolUse":
            return {"event": "tool_done", "session_id": session_id, "tool_name": hook.get("tool_name", ""), "project": project, "pid": pid}

        if event_name == "PermissionRequest":
            return {"event": "permission", "session_id": session_id, "tool_name": hook.get("tool_name", ""), "project": project, "pid": pid}

        if event_name == "PostToolUseFailure":
            return {"event": "tool_failed", "session_id": session_id, "tool_name": hook.get("tool_name", ""), "project": project, "pid": pid}

        if event_name == "PreCompact":
            return {"event": "compact", "session_id": session_id, "pid": pid}

        if event_name == "Stop":
            return {
                "event": "add",
                "hook": "Stop",
                "session_id": session_id,
                "project": project or "unknown",
                "message": "Waiting for input",
                "pid": pid,
            }

        if event_name == "StopFailure":
            message = hook.get("error", "") or hook.get("stop_reason", "") or "API error"
            return {
                "event": "add",
                "hook": "StopFailure",
                "session_id": session_id,
                "project": project or "unknown",
                "message": message,
                "pid": pid,
            }

        if event_name == "Notification":
            if hook.get("notification_type") != "idle_prompt":
                return None
            return {
                "event": "add",
                "hook": "Notification",
                "session_id": session_id,
                "project": project or "unknown",
                "message": hook.get("message", "Waiting for input"),
                "pid": pid,
            }

        if event_name == "UserPromptSubmit":
            return {"event": "dismiss", "hook": "UserPromptSubmit", "session_id": session_id, "pid": pid}

        if event_name == "SessionEnd":
            msg = {"event": "dismiss", "hook": "SessionEnd", "session_id": session_id, "pid": pid}
            reason = hook.get("reason")
            if reason is not None:
                msg["reason"] = reason
            return msg

        if event_name == "SubagentStart":
            return {
                "event": "subagent_start",
                "session_id": session_id,
                "agent_id": hook.get("agent_id", ""),
                "pid": pid,
            }

        if event_name == "SubagentStop":
            return {
                "event": "subagent_stop",
                "session_id": session_id,
                "agent_id": hook.get("agent_id", ""),
                "pid": pid,
            }

        return None


    def main():
        try:
            # Bytes, decoded as UTF-8: Windows' default text encoding (cp1252)
            # would garble a non-ASCII project folder, or fail outright.
            raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            if not raw.strip():
                sys.exit(0)
            payload = json.loads(raw)
        except json.JSONDecodeError:
            sys.exit(1)

        msg = hook_to_message(payload)
        if msg is None:
            sys.exit(0)

        sock = None
        try:
            sock = _connect()
            sock.sendall(json.dumps(msg).encode("utf-8") + b"\\n")
        except OSError:
            # Daemon not running (refused, no socket file, timeout): best-effort.
            sys.exit(0)
        finally:
            if sock is not None:
                sock.close()


    if __name__ == "__main__":
        main()
''')


def windows_hook_command(script_path: Path, launcher: str = "py -3") -> str:
    """The hook `command` string for Windows: ``py -3 "C:/Users/me/.clawd-tank/clawd-tank-notify.py"``.

    Claude Code on Windows hands the command to Git Bash (or PowerShell/cmd,
    depending on version and settings). Three things make one string work in
    all of them: it starts with a plain word (``py``), not a quoted path; the
    script path is double-quoted, so a profile folder with a space survives;
    and it uses forward slashes, because bash eats the backslashes of an
    unquoted Windows path and a quoted one is only safe by luck. Windows
    itself accepts forward slashes in file paths. There is no shebang and no
    ``.sh`` anywhere: ``py`` runs the file by explicit path.
    """
    return f'{launcher} "{Path(script_path).as_posix()}"'


HOOK_COMMAND = windows_hook_command(NOTIFY_SCRIPT_PATH) if IS_WINDOWS else str(NOTIFY_SCRIPT_PATH)

HOOKS_CONFIG = {
    "SessionStart": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
    "Stop": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
    "StopFailure": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
    "Notification": [
        {
            "matcher": "idle_prompt",
            "hooks": [{"type": "command", "command": HOOK_COMMAND}],
        }
    ],
    "UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
    "PreToolUse": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
    # Scoped to AskUserQuestion only: the sole purpose is clearing the "waiting
    # for input" alert when the user answers. Registering PostToolUse for every
    # tool would double the device's event/BLE traffic for no added value.
    "PostToolUse": [
        {
            "matcher": ASK_USER_QUESTION_TOOL,
            "hooks": [{"type": "command", "command": HOOK_COMMAND}],
        }
    ],
    # Claude is blocked waiting for the user to approve a tool → waiting/alert.
    "PermissionRequest": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
    # A tool genuinely errored (not a non-zero shell exit) → confused.
    "PostToolUseFailure": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
    "PreCompact": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
    "SessionEnd": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
    "SubagentStart": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
    "SubagentStop": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    ],
}


def install_notify_script() -> None:
    """Write the standalone notify script to ~/.clawd-tank/clawd-tank-notify."""
    write_notify_script(NOTIFY_SCRIPT_PATH)
    logger.info("Installed hook script: %s", NOTIFY_SCRIPT_PATH)


def write_notify_script(path: Path) -> None:
    """Write NOTIFY_SCRIPT to `path`: UTF-8 with LF endings on every platform
    (Windows' text mode would otherwise write CRLF), executable where that
    means something."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(NOTIFY_SCRIPT)
    if not IS_WINDOWS:
        path.chmod(0o755)


def _matcher_of(entry: dict):
    """Normalised matcher of a hook group. Absent/empty matcher → None.

    Claude Code treats a group with no matcher (or "") as matching everything,
    so they are equivalent for the purpose of locating "our" group.
    """
    if not isinstance(entry, dict):
        return None
    matcher = entry.get("matcher")
    return matcher if matcher else None


def _command_is_ours(command) -> bool:
    """True only if a hook command actually invokes our notify script — the exact
    path, or the path followed by args. A command that merely CONTAINS the path as a
    substring (a wrapper, or `cat <path>`) is not ours."""
    if not isinstance(command, str):
        return False
    return command == HOOK_COMMAND or command.startswith(HOOK_COMMAND + " ")


def _group_runs_our_command(entry: dict) -> bool:
    """True if a hook group contains a hook that runs the Clawd Tank notify script."""
    if not isinstance(entry, dict):
        return False
    hooks_list = entry.get("hooks")
    if not isinstance(hooks_list, list):
        return False
    return any(isinstance(h, dict) and _command_is_ours(h.get("command", "")) for h in hooks_list)


def _is_our_managed_group(entry: dict) -> bool:
    """True if this group was created by Clawd Tank — a non-empty hooks list where
    EVERY hook runs our notify script. Such groups are safe to prune on install;
    a group the user shares with us (mixing their command with ours) is not."""
    if not isinstance(entry, dict):
        return False
    hooks_list = entry.get("hooks")
    if not isinstance(hooks_list, list) or not hooks_list:
        return False
    return all(isinstance(h, dict) and _command_is_ours(h.get("command", "")) for h in hooks_list)


def _our_hook_present(existing_entries, our_matcher) -> bool:
    """True if some existing group with the same matcher already runs our command."""
    if not isinstance(existing_entries, list):
        return False
    for entry in existing_entries:
        if _matcher_of(entry) == our_matcher and _group_runs_our_command(entry):
            return True
    return False


def are_hooks_installed() -> bool:
    """True only if every Clawd Tank hook (event + matcher) is already registered.

    Matcher-aware: a hook whose command is present but under the wrong matcher
    (e.g. a new matcher we started requiring) counts as NOT installed, so the
    menu bar app treats it as outdated and re-runs install_hooks().
    """
    if not CLAUDE_SETTINGS_PATH.exists():
        return False
    try:
        settings = json.loads(CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    return settings_have_our_hooks(settings)


def settings_have_our_hooks(settings) -> bool:
    """True only if every Clawd Tank hook (event + matcher) is registered in this
    parsed settings dict, and no superseded Clawd Tank group lingers. The pure
    half of are_hooks_installed(); the installer CLI uses it to detect a no-op."""
    if not isinstance(settings, dict):
        return False
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    for event_name, our_entries in HOOKS_CONFIG.items():
        existing = hooks.get(event_name, [])
        expected_matchers = {_matcher_of(e) for e in our_entries}
        # (a) every expected event+matcher must be present
        for our_entry in our_entries:
            if not _our_hook_present(existing, _matcher_of(our_entry)):
                return False
        # (b) no leftover Clawd Tank group under a matcher we no longer use —
        # otherwise a superseded group would persist forever (install never re-runs).
        if isinstance(existing, list):
            for g in existing:
                if _is_our_managed_group(g) and _matcher_of(g) not in expected_matchers:
                    return False
    return True


def install_hooks() -> bool:
    """Merge Clawd Tank hooks into Claude Code settings without clobbering the user's
    own hooks. For each managed event we drop our OWN prior groups (so a changed
    matcher self-heals instead of leaving a stale duplicate), then append the current
    config only where it isn't already present. The user's groups — and any group the
    user shares with us — are never modified or removed. Idempotent. Returns True.
    """
    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

    if CLAUDE_SETTINGS_PATH.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            settings = {}
    else:
        settings = {}

    settings = merge_hooks(settings)

    with open(CLAUDE_SETTINGS_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(settings, indent=2) + "\n")
    logger.info("Installed hooks in %s", CLAUDE_SETTINGS_PATH)
    return True


def merge_hooks(settings) -> dict:
    """Return `settings` with the Clawd Tank hooks merged in. Pure: no file I/O.

    Mutates and returns the dict it is given (deep-copy first to keep the
    original). A non-dict input is treated as empty. Every other key, every
    event we do not manage, and every group of the user's own (including one
    they share with us) is left exactly as it was; see install_hooks().
    """
    if not isinstance(settings, dict):
        settings = {}

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks

    for event_name, our_entries in HOOKS_CONFIG.items():
        existing = hooks.get(event_name)
        if not isinstance(existing, list):
            existing = []
        # Prune our own prior groups (self-heal across matcher/config changes); keep
        # the user's groups and any group the user shares with us untouched.
        kept = [g for g in existing if not _is_our_managed_group(g)]
        for our_entry in our_entries:
            if not _our_hook_present(kept, _matcher_of(our_entry)):
                kept.append(copy.deepcopy(our_entry))
        hooks[event_name] = kept

    return settings
