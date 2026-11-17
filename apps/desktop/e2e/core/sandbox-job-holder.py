"""Hold a kill-on-close job on a Playwright worker so its sandbox gateway dies with it.

The desktop e2e gateway used to break away from the worker. A hard kill of that
worker (no exit handler, no ``cleanup()``) left ``gateway run`` behind. This
process assigns the worker to a nested job that kills its members when the last
handle closes, then waits until the worker is gone and exits — which closes
the handle.

Spawned before the assignment, so this holder is not itself a member. A
``taskkill /T`` of the worker kills this holder too; the kernel then closes
the handle. A ``taskkill /F`` without ``/T`` leaves this holder alive to exit
on its own.

Usage: python sandbox-job-holder.py <owner-pid> <ready-file>
Writes ``ok`` or ``fail <winerror>`` to the ready file.
"""
from __future__ import annotations

import sys
import time


def _write_ready(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    synchronize = 0x00100000
    handle = kernel.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    kernel.CloseHandle(handle)
    return True


def _hold(owner_pid: int, ready_file: str) -> int:
    import ctypes
    from ctypes import wintypes

    kill_on_close = 0x00002000
    extended_limit = 9
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    class _IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _Basic),
            ("IoInfo", _IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        _write_ready(ready_file, f"fail {ctypes.get_last_error()}")
        return 1
    info = _Extended()
    info.BasicLimitInformation.LimitFlags = kill_on_close
    if not kernel.SetInformationJobObject(
        job, extended_limit, ctypes.byref(info), ctypes.sizeof(info)
    ):
        _write_ready(ready_file, f"fail {ctypes.get_last_error()}")
        kernel.CloseHandle(job)
        return 1
    # PROCESS_SET_QUOTA | PROCESS_TERMINATE is enough to assign. 0x1F0FFF is
    # PROCESS_ALL_ACCESS and fails on a protected parent; the worker is ours.
    owner = kernel.OpenProcess(0x0100 | 0x0001, False, owner_pid)
    if not owner:
        _write_ready(ready_file, f"fail {ctypes.get_last_error()}")
        kernel.CloseHandle(job)
        return 1
    if not kernel.AssignProcessToJobObject(job, owner):
        err = ctypes.get_last_error()
        kernel.CloseHandle(owner)
        kernel.CloseHandle(job)
        _write_ready(ready_file, f"fail {err}")
        return 1
    kernel.CloseHandle(owner)
    _write_ready(ready_file, "ok")
    while _pid_alive(owner_pid):
        time.sleep(0.2)
    # Dropping the last handle is the kill. Do not CloseHandle early.
    return 0


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    if sys.platform != "win32":
        _write_ready(sys.argv[2], "skip")
        return 0
    try:
        owner_pid = int(sys.argv[1])
    except ValueError:
        _write_ready(sys.argv[2], "fail pid")
        return 2
    return _hold(owner_pid, sys.argv[2])


if __name__ == "__main__":
    raise SystemExit(main())
