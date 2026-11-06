"""Windows: a test-spawned gateway must not outlive the runner that started it.

Every test here spawns real processes and reads their real environment — no mocks. Each one fails
if the protection is removed: drop the kill-on-close job, the environment census, or the tree kill
and the survivor stays alive long enough for the assertion to catch it. A test that cannot go red
proves nothing about a leak.

The stand-in children are ``python -c "time.sleep(...)"``: they open no port, read no config and
never touch the home named in their environment. They exist only to be found (or not found) and
killed (or not killed) by the sweep under test.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tests.process_custody import kill_tree, processes_with_hermes_home_under, reap_pids, spawn

pytestmark = [pytest.mark.platforms("windows"), pytest.mark.spawns_gateway_lookalike]

_REPO_ROOT = Path(__file__).resolve().parents[1]

# A stand-in runner: start one child, write its pid, then die with no cleanup at all — the shape of
# a pytest worker killed by a dispatcher timeout or taskkill /F.
_LAUNCHER = r"""
import json, os, subprocess, sys
mode, pid_file, home = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.environ["HERMES_TEST_REPO_ROOT"])
env = dict(os.environ)
if home != "-":
    env["HERMES_HOME"] = home
command = [sys.executable, "-c", "import time; time.sleep(300)"]
if mode == "contained":
    from tests.process_custody import spawn
    child = spawn(command, env=env, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
else:
    child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
with open(pid_file, "w", encoding="utf-8") as handle:
    json.dump({"pid": child.pid}, handle)
os._exit(0)
"""

# A parent of one grandchild, for the tree-kill test: it keeps running so the test owns the kill.
_TREE_PARENT = r"""
import json, subprocess, sys
pid_file = sys.argv[1]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
with open(pid_file, "w", encoding="utf-8") as handle:
    json.dump({"pid": child.pid}, handle)
import time
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _wait_until(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def _launch(tmp_path: Path, mode: str, home) -> int:
    """Run the stand-in runner; it exits immediately, leaving its child behind unless contained."""
    pid_file = tmp_path / f"child-{mode}.pid"
    env = dict(os.environ, HERMES_TEST_REPO_ROOT=str(_REPO_ROOT))
    completed = subprocess.run(
        [sys.executable, "-c", _LAUNCHER, mode, str(pid_file), str(home)],
        env=env, capture_output=True, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    return _pid_from(pid_file)


def _pid_from(pid_file: Path) -> int:
    import json

    return int(json.loads(pid_file.read_text(encoding="utf-8"))["pid"])


def test_contained_child_dies_with_the_runner_that_spawned_it(tmp_path):
    """AC4: kill-on-close — the runner dies hard, the kernel takes the child with it."""
    home = tmp_path / "sandbox-home"
    pid = _launch(tmp_path, "contained", home)
    assert _wait_until(lambda: not _alive(pid), timeout=30.0), (
        f"contained child {pid} outlived its runner: the job object did not reap it"
    )


def test_tree_kill_takes_the_grandchild_not_just_the_parent(tmp_path):
    """AC3: ``kill_tree`` is ``taskkill /T /F`` — a re-exec'd child cannot survive it."""
    pid_file = tmp_path / "grandchild.pid"
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "sandbox-home"))
    parent = spawn([sys.executable, "-c", _TREE_PARENT, str(pid_file)], env=env,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_until(lambda: pid_file.exists() and pid_file.stat().st_size > 0, timeout=60.0), \
            "stand-in parent never started its grandchild"
        grandchild = _pid_from(pid_file)
        assert _alive(grandchild)
        kill_tree(parent)
        parent.wait(timeout=30)
        assert _wait_until(lambda: not _alive(grandchild), timeout=30.0), (
            f"grandchild {grandchild} survived the tree kill — only the direct child was signalled"
        )
    finally:
        if parent.poll() is None:
            kill_tree(parent)


def test_session_sweep_reaps_a_sandbox_gateway_whose_parent_is_dead(tmp_path):
    """AC1: the environment census finds the orphan, and the sweep kills its whole tree."""
    scan_root = tmp_path / "hermes-pytest"
    home = scan_root / "sweep0" / "state"
    pid = _launch(tmp_path, "orphan", home)
    try:
        assert _alive(pid)
        found = processes_with_hermes_home_under([scan_root])
        assert pid in found, f"census missed orphan {pid} whose HERMES_HOME is under {scan_root}"
        reap_pids(found)
        assert _wait_until(lambda: not _alive(pid), timeout=30.0), f"orphan {pid} survived the sweep"
    finally:
        if _alive(pid):
            reap_pids([pid])


def test_sweep_never_signals_the_live_root_or_a_real_profile_home(tmp_path):
    """AC2: dead parent is not enough — a home outside the swept root is never touched."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        pytest.skip("LOCALAPPDATA is not set on this host")
    live_root = Path(local_app_data) / "hermes"
    # A real profile home: ``profiles\<name>``, deliberately NOT under ``cache\scratch``.
    profile_home = live_root / "profiles" / "custody-probe"

    live_pid = _launch(tmp_path, "orphan", live_root)
    profile_pid = _launch(tmp_path, "orphan", profile_home)
    try:
        found = processes_with_hermes_home_under([tmp_path / "hermes-pytest"])
        assert live_pid not in found, "the live Hermes root was swept: that is the live gateway's home"
        assert profile_pid not in found, "a real profile home was swept: only sandbox homes may be reaped"
        reap_pids(found)
        assert _alive(live_pid), "the sweep killed a process rooted at the live Hermes home"
        assert _alive(profile_pid), "the sweep killed a process rooted at a real profile home"
    finally:
        reap_pids([live_pid, profile_pid])
