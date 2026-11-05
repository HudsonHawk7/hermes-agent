"""Contain test-spawned Hermes daemons so a hard-killed runner cannot leave them behind.

On Windows a pytest run that is killed (Ctrl-Break, ``taskkill /F``, dispatcher timeout) runs no
``finally`` and no fixture teardown: the ``Popen`` child — and the re-exec'd child it owns — keep
running detached, each holding a gateway port, a lock file and a few hundred MB. Nothing in the
run can clean up after itself because the run is gone.

A Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` makes the kernel the reaper: the moment
the runner's handles close — normal exit, hard kill, or a crash — the job closes and every process
still inside it is terminated. Handles are held in ``_JOBS`` for the life of the runner and are
deliberately never closed; closing one early would kill the daemon the test is talking to.

Assignment happens while the child is still ``CREATE_SUSPENDED``: a gateway that spawns its own
re-exec before assignment would put that grandchild outside the job, which is the exact survivor
class this module exists to prevent.

POSIX runs get plain ``subprocess.Popen``: process-group reaping and the session sweep in
``tests/conftest.py`` already cover them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

CREATE_SUSPENDED = 0x00000004

# Owner-held job handles: one per contained tree, alive until this process dies (by design).
_JOBS: list[Any] = []


def spawn(command: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen`` whose whole tree dies with this process on Windows."""
    if sys.platform != "win32":
        return subprocess.Popen(command, **kwargs)

    from hermes_cli.local_runtime.processes import _WindowsJob

    job = _WindowsJob()
    kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | CREATE_SUSPENDED
    proc = subprocess.Popen(command, **kwargs)
    try:
        job.assign(proc)
        _resume(proc)
    except BaseException:
        try:
            proc.kill()
            proc.wait(timeout=10)
        finally:
            job.close()
        raise
    _JOBS.append(job)
    return proc


def _resume(proc: subprocess.Popen) -> None:
    import psutil

    psutil.Process(proc.pid).resume()


def job_handles() -> list[Any]:
    """The job handles this process owns (kept alive by the module, never closed here)."""
    return list(_JOBS)


def kill_tree(proc: subprocess.Popen) -> None:
    """Best-effort tree kill for teardown paths; never raises.

    Windows needs ``taskkill /T /F``: ``proc.kill()`` reaches only the direct child and leaves the
    re-exec'd gateway (and any ``git``-style helper) running.
    """
    try:
        from hermes_cli._subprocess_compat import kill_process_tree

        kill_process_tree(proc)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def environ_hermes_homes() -> list[tuple[int, str]]:
    """(pid, ``HERMES_HOME``) for every process on this host whose environment is readable.

    Linux reads ``/proc/<pid>/environ``. Windows has neither ``/proc`` nor a home in the command
    line, so the environment is read through psutil, which reads the target's own process block
    (PEB); protected processes and other users' processes raise and are skipped.
    """
    if os.path.isdir("/proc/self"):
        found = []
        for entry in os.scandir("/proc"):
            pid = int(entry.name) if entry.name.isdigit() else 0
            if not pid or pid == os.getpid():
                continue
            try:
                with open(f"/proc/{entry.name}/environ", "rb") as handle:
                    environ = handle.read().split(b"\0")
            except OSError:
                continue
            home = next((item[12:] for item in environ if item.startswith(b"HERMES_HOME=")), None)
            if home:
                found.append((pid, os.fsdecode(home)))
        return found

    if sys.platform != "win32":
        return []

    import psutil

    found = []
    for proc in psutil.process_iter():
        if proc.pid == os.getpid():
            continue
        try:
            home = proc.environ().get("HERMES_HOME")
        except (psutil.Error, OSError):
            continue
        if home:
            found.append((proc.pid, home))
    return found


def processes_with_hermes_home_under(roots: Iterable) -> list[int]:
    """PIDs (never this process) whose environment names a ``HERMES_HOME`` inside one of *roots*.

    The environment — not the command line — is authoritative: ``gateway run`` argv names no home,
    so a command-line scan cannot see a survivor at all. A home outside every root is never
    returned, which is what keeps the live gateway and real profile homes out of the sweep.
    """
    resolved = []
    for root in roots:
        if not root:
            continue
        try:
            resolved.append(Path(root).resolve())
        except (OSError, ValueError):
            continue
    if not resolved:
        return []

    found = []
    for pid, home in environ_hermes_homes():
        try:
            path = Path(home).resolve()
        except (OSError, ValueError):
            continue
        if any(path == root or path.is_relative_to(root) for root in resolved):
            found.append(pid)
    return found


def reap_pids(pids: Iterable[int]) -> None:
    """Terminate these processes as trees; never raises.

    On Windows ``gateway run`` re-execs itself, so signalling the parent alone leaves the child
    holding the port and the lock: ``taskkill /T /F`` is the spelling that reaches the re-exec
    child. POSIX gets SIGTERM per pid.
    """
    if sys.platform == "win32":
        from agent.deadline import kill_process_tree

        for pid in pids:
            try:
                kill_process_tree(pid)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return

    import signal

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
