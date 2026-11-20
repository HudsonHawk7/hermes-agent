"""Dependency state of a deleted checkout is reclaimed; state anything live could still read, or
whose checkout this process merely cannot see, is kept (``pm/install_states.py``)."""

from __future__ import annotations

import os
import subprocess

import pm.environments
import pytest
from hermes_cli.worktree_ops import _prune_stale_worktrees
from pm.filesystem import lock_fd
from pathlib import Path

import pm.install_states
from pm.install_states import (
    collect_orphan_install_states,
    orphan_install_states,
    prune_install_states,
    retain_install_state_names,
)


def _state(installs, key, project_root):
    state = installs / key
    (state / "inputs").mkdir(parents=True)
    (state / "inputs" / ".project-root").write_text(str(project_root), encoding="utf-8")
    (state / "environments" / "gen" / "venv").mkdir(parents=True)
    return state


def test_startup_prune_reclaims_only_provably_deleted_checkouts(tmp_path, monkeypatch):
    installs = tmp_path / "home" / "installs"
    repo = tmp_path / "repo"
    (repo / ".worktrees").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    live_root = tmp_path / "home" / "hermes-agent"
    live_root.mkdir(parents=True)
    live = _state(installs, "aaaa", live_root)
    gone_scratch = _state(installs, "bbbb", tmp_path / "home" / "cache" / "scratch" / "clone")
    gone_worktree = _state(installs, "cccc", repo / ".worktrees" / "removed-by-hand")
    # Pre-record state (no .project-root) has no evidence either way: never touched.
    unknown = installs / "dddd"
    (unknown / "environments").mkdir(parents=True)
    # A data root shared across a mount boundary records checkouts this process cannot see:
    # a host install from inside a container, a container install from the host.
    host_install = _state(installs, "eeee", tmp_path / "other-host-home" / ".hermes" / "hermes-agent")
    container_install = _state(installs, "ffff", tmp_path / "opt-hermes")

    assert orphan_install_states(installs) == [gone_scratch, gone_worktree]
    monkeypatch.setattr(pm.environments, "installs_root", lambda: installs)
    # No tree under .worktrees/ is old enough to prune: the reclaim must still run.
    _prune_stale_worktrees(str(repo))
    assert not gone_scratch.exists() and not gone_worktree.exists()
    assert all(p.is_dir() for p in (live, unknown, host_install, container_install))


def test_held_orphan_is_kept(tmp_path):
    installs = tmp_path / "installs"
    locked = _state(installs, "dddd", tmp_path / "gone-a")
    leased = _state(installs, "eeee", tmp_path / "gone-b")
    runtime = _state(installs, "ffff", tmp_path / "gone-c")

    fds = [os.open(locked / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)]
    for lease_dir in (leased / "environments" / "gen" / ".leases",
                      runtime / "pm-runtime" / "generations" / "gen" / ".leases"):
        lease_dir.mkdir(parents=True)
        fds.append(os.open(lease_dir / "reader", os.O_CREAT | os.O_RDWR, 0o600))
    assert all(lock_fd(fd, wait=False) for fd in fds)
    try:
        assert collect_orphan_install_states(installs) == []
        assert locked.is_dir() and leased.is_dir() and runtime.is_dir()
    finally:
        for fd in fds:
            os.close(fd)
    # Locks released (owner exited): all are reclaimed on the next pass.
    assert sorted(p.name for p in collect_orphan_install_states(installs)) == ["dddd", "eeee", "ffff"]


@pytest.mark.platforms("posix")
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root ignores directory modes")
def test_partly_removed_orphan_keeps_its_record_until_retried(tmp_path):
    installs = tmp_path / "installs"
    state = _state(installs, "aaaa", tmp_path / "gone")
    stuck = state / "environments" / "gen" / "venv"
    (stuck / "pyvenv.cfg").write_text("", encoding="utf-8")
    stuck.chmod(0o500)  # its entries cannot be unlinked: the removal stops part-way
    try:
        assert collect_orphan_install_states(installs) == []
        assert orphan_install_states(installs) == [state]
    finally:
        stuck.chmod(0o700)
    assert collect_orphan_install_states(installs) == [state] and not state.exists()


def test_retention_keeps_live_and_newest_only(tmp_path, monkeypatch):
    """After prune, live install survives even when older than others; only newest other kept."""
    import time

    home = tmp_path / "home"
    installs = home / "installs"
    live_root = home / "hermes-agent"
    live_root.mkdir(parents=True)
    (live_root / "hermes_cli").mkdir()
    # Live install is intentionally oldest so retention must protect it by identity, not mtime.
    live = _state(installs, "livekey00000001", live_root)
    old = _state(installs, "oldkey000000002", tmp_path / "wt-old")
    mid = _state(installs, "midkey000000003", tmp_path / "wt-mid")
    new = _state(installs, "newkey000000004", tmp_path / "wt-new")
    base = time.time() - 400
    for state, offset in ((live, 0), (old, 100), (mid, 200), (new, 300)):
        os.utime(state, (base + offset, base + offset))

    keep = retain_install_state_names(installs, keep_newest=1, live_names={"livekey00000001"})
    assert keep == {"livekey00000001", "newkey000000004"}

    # live_install_state_names uses install_key(live_root); pin it to our live key.
    monkeypatch.setattr(
        pm.environments,
        "install_key",
        lambda root: "livekey00000001" if Path(root).resolve() == live_root.resolve() else "x",
    )
    removed = prune_install_states(installs, keep_newest=1)
    assert live.is_dir(), "prune must never delete the live install"
    assert new.is_dir(), "newest non-live must be kept"
    assert not old.exists() and not mid.exists()
    assert sorted(p.name for p in removed) == ["midkey000000003", "oldkey000000002"]


def test_retention_never_deletes_held_non_live(tmp_path, monkeypatch):
    installs = tmp_path / "home" / "installs"
    live = _state(installs, "livekey00000001", tmp_path / "home" / "hermes-agent")
    (tmp_path / "home" / "hermes-agent" / "hermes_cli").mkdir(parents=True, exist_ok=True)
    held = _state(installs, "heldkey00000002", tmp_path / "held-checkout")
    spare = _state(installs, "sparekey0000003", tmp_path / "spare-checkout")
    monkeypatch.setattr(pm.install_states, "live_install_state_names", lambda installs: {"livekey00000001"})
    fd = os.open(held / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    assert lock_fd(fd, wait=False)
    try:
        removed = prune_install_states(installs, keep_newest=0)
        assert live.is_dir() and held.is_dir()
        assert not spare.exists()
        assert [p.name for p in removed] == ["sparekey0000003"]
    finally:
        os.close(fd)


def test_retention_fail_closed_when_live_unknown(tmp_path, monkeypatch):
    """Empty live detection must not mtime-prune; all non-orphan dirs survive (AC3)."""
    import time

    home = tmp_path / "home"
    installs = home / "installs"
    # No gateway.lock, no home/hermes-agent — live_install_state_names would be empty.
    # Checkouts still exist so they are not orphans.
    a = _state(installs, "aaaold000000001", tmp_path / "checkout-a")
    b = _state(installs, "bbbmid000000002", tmp_path / "checkout-b")
    c = _state(installs, "cccnew000000003", tmp_path / "checkout-c")
    base = time.time() - 300
    for state, offset in ((a, 0), (b, 100), (c, 200)):
        os.utime(state, (base + offset, base + offset))

    monkeypatch.setattr(pm.install_states, "live_install_state_names", lambda installs: set())
    keep = retain_install_state_names(installs, keep_newest=1, live_names=set())
    assert keep == {"aaaold000000001", "bbbmid000000002", "cccnew000000003"}

    removed = prune_install_states(installs, keep_newest=1)
    assert a.is_dir() and b.is_dir() and c.is_dir(), "fail-closed: non-orphans must all survive empty live"
    assert removed == []
