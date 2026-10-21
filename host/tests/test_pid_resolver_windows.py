"""Windows PID resolution: CLAUDE_PID, else the claude.exe ancestor, else None.

The table is injected, so these run on any OS.
"""

from clawd_tank_daemon.pid_resolver import find_claude_pid_windows

# pid -> (parent pid, exe name): claude.exe 52312 -> claude.exe 64612 -> bash x3 -> python
TABLE = {
    2708: (2924, "sihost.exe"),
    52312: (2708, "claude.exe"),
    64612: (52312, "claude.exe"),
    92468: (64612, "bash.exe"),
    34724: (92468, "bash.exe"),
    57416: (34724, "bash.exe"),
    60496: (57416, "py.exe"),
    105216: (60496, "python.exe"),
}


def test_claude_pid_environment_variable_wins():
    assert find_claude_pid_windows(table=TABLE, environ={"CLAUDE_PID": "777"}, start_pid=60496) == 777


def test_ignores_a_garbage_claude_pid_and_walks_instead():
    for bad in ("", "abc", "0", "-3"):
        assert find_claude_pid_windows(table=TABLE, environ={"CLAUDE_PID": bad}, start_pid=60496) == 64612


def test_walk_finds_the_nearest_claude_exe_not_the_bash_in_between():
    """The hook's parent is a short-lived bash.exe; the answer must be Claude."""
    assert find_claude_pid_windows(table=TABLE, environ={}, start_pid=57416) == 64612


def test_walk_matches_exe_name_case_insensitively():
    table = {10: (5, "Claude.EXE"), 11: (10, "bash.exe")}
    assert find_claude_pid_windows(table=table, environ={}, start_pid=11) == 10


def test_no_claude_ancestor_gives_none_rather_than_the_bash_pid():
    table = {20: (1, "explorer.exe"), 21: (20, "cmd.exe"), 22: (21, "bash.exe")}
    assert find_claude_pid_windows(table=table, environ={}, start_pid=22) is None


def test_empty_table_gives_none():
    assert find_claude_pid_windows(table={}, environ={}, start_pid=123) is None


def test_a_parent_cycle_terminates():
    table = {30: (31, "a.exe"), 31: (30, "b.exe")}
    assert find_claude_pid_windows(table=table, environ={}, start_pid=30) is None
