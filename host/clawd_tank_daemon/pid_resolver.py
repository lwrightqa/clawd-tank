"""Resolve the long-lived Claude Code session PID.

POSIX: walk the process tree with ``ps``, mirroring
claude-plugins/agent-bus/lib/common.sh:_find_claude_pid.

Windows: there is no ``ps``, and Claude Code spawns hook commands through Git
Bash, so ``os.getppid()`` is a short-lived ``bash.exe`` (a PID the daemon's
liveness check would soon find dead, evicting a live session). Instead:

  1. ``CLAUDE_PID`` in the environment. Claude Code exports its own PID to
     the processes it spawns; it is exact and costs nothing.
  2. Otherwise walk the ancestors in one Toolhelp32 snapshot and return the
     first ``claude.exe``.
  3. Otherwise None: the daemon then skips PID dedup and PID liveness for
     that session (SessionEnd and the staleness timeout still clean it up),
     which is better than a wrong PID.

The embedded NOTIFY_SCRIPT in clawd_tank_menubar/hooks.py duplicates this
logic — keep them in sync.
"""

import os
import re
import subprocess
from typing import Dict, Mapping, Optional, Tuple

from . import procutil

_CLAUDE_ARGV_RE = re.compile(r"(^|/)claude($|\s)")
_WINDOWS_CLAUDE_EXES = ("claude.exe", "claude")


def _ps(field: str, pid: int) -> str:
    """Run `ps -o <field>= -p <pid>` and return trimmed stdout. Empty on error."""
    try:
        r = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True, text=True, timeout=1.0,
        )
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def find_claude_pid() -> Optional[int]:
    """The Claude Code PID for the current hook process. See the module doc.

    POSIX always returns an int (falls back to ``os.getppid()``); Windows
    returns None when no Claude Code process can be identified.
    """
    if procutil.IS_WINDOWS:
        return find_claude_pid_windows()
    return find_claude_pid_posix()


def find_claude_pid_posix() -> int:
    """Walk from os.getppid() up the process tree to find the long-lived
    Claude Code PID. Falls back to os.getppid() if no `claude` ancestor found.

    Identification (in order, per ancestor):
      1. `ps -o comm=` exactly equals "claude" (native binary)
      2. `ps -o command=` matches (^|/)claude($|\\s) (node-wrapped install)

    Loose substring matching against argv is DELIBERATELY AVOIDED — see the
    agent-bus comment block (lib/common.sh:36-47) for the rationale.
    """
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


def find_claude_pid_windows(
    table: Optional[Dict[int, Tuple[int, str]]] = None,
    environ: Optional[Mapping[str, str]] = None,
    start_pid: Optional[int] = None,
) -> Optional[int]:
    """Windows resolution: CLAUDE_PID, else the first claude.exe ancestor, else None.

    ``table``, ``environ`` and ``start_pid`` are injectable for tests; by
    default they are a fresh Toolhelp32 snapshot, ``os.environ`` and
    ``os.getppid()``.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get("CLAUDE_PID", "")
    try:
        pid = int(raw)
        if pid > 0:
            return pid
    except ValueError:
        pass

    if table is None:
        table = procutil.windows_process_table()
    pid = os.getppid() if start_pid is None else start_pid
    seen = set()
    while pid in table and pid not in seen:
        seen.add(pid)
        parent, exe = table[pid]
        if exe.lower() in _WINDOWS_CLAUDE_EXES:
            return pid
        pid = parent
    return None
