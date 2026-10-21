"""Process helpers that behave the same on POSIX and Windows.

Two things upstream does with ``os.kill`` and ``ps`` have no Windows
equivalent:

* ``os.kill(pid, 0)`` as a liveness probe. On Windows ``os.kill`` with signal
  0 does not probe at all: 0 is ``CTRL_C_EVENT``, so it calls
  ``GenerateConsoleCtrlEvent`` and can send Ctrl+C to every process sharing the
  daemon's console. ``pid_alive`` opens the process handle instead.
* ``ps -o ppid=`` to walk the process tree. ``windows_process_table`` reads
  the whole table once through the Toolhelp32 snapshot API (ctypes, stdlib).
"""

import ctypes
import os
import sys
from typing import Dict, Optional, Tuple

IS_WINDOWS = sys.platform == "win32"

# Windows constants.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def pid_alive(pid: Optional[int]) -> bool:
    """True if a process with this PID exists (or exists but is not ours).

    POSIX: ``os.kill(pid, 0)`` as upstream does; PermissionError means the
    process exists under another user, so it counts as alive.
    Windows: open the process with query-limited rights and check that it has
    not exited. Access denied means it exists, so alive.
    """
    if pid is None or pid <= 0:
        return False
    if IS_WINDOWS:
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_uint32(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def windows_process_table() -> Dict[int, Tuple[int, str]]:
    """``{pid: (parent_pid, exe_basename)}`` for every process, from one
    Toolhelp32 snapshot. Empty dict on any failure (never raises)."""
    if not IS_WINDOWS:
        return {}
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.CreateToolhelp32Snapshot.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
        kernel32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W))
        kernel32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W))
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
            return {}
        table: Dict[int, Tuple[int, str]] = {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                table[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID), entry.szExeFile,
                )
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return table
    except Exception:
        return {}


def stdin_is_terminal() -> bool:
    """True only if stdin is an interactive terminal.

    On Windows ``isatty()`` is also true for the NUL device (``< NUL``), so
    ask the console API instead: GetConsoleMode succeeds only on a real console
    handle, and fails for NUL, pipes and files. A pipe such as
    ``echo yes | ...`` therefore never counts as somebody typing.
    """
    stdin = sys.stdin
    if stdin is None:
        return False
    if not IS_WINDOWS:
        return stdin.isatty()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetStdHandle.argtypes = (ctypes.c_int32,)
        kernel32.GetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        if not handle or handle == _INVALID_HANDLE_VALUE:
            return False
        mode = ctypes.c_uint32(0)
        return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except Exception:
        return False
