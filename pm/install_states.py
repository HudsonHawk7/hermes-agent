"""Reclaim per-checkout dependency state that is no longer needed.

Dependency state lives under ``<home>/installs/<install_key>`` where the key hashes the
checkout path (:func:`pm.environments.install_key`). Every ``hermes -w`` worktree, review tree
or scratch clone that boots the agent therefore commits a full environment (venv, test venv,
PM runtime, hundreds of MB each). Two reclaim paths keep that tree bounded:

1. **Orphans** — the checkout is provably gone. A missing path alone does not prove that: a
   data root shared with a container (``-v ~/.hermes:/opt/data``) or an unmounted volume
   records paths this process cannot see. So a dir is an orphan only when its checkout sat
   where this process can see that it was deleted -- under the data root that owns
   ``installs/`` (scratch clones, the runtime checkout) or directly in a ``.worktrees/`` dir
   that still exists.

2. **Retention** — after a successful install, keep the install the running gateway uses
   plus the newest one other state dir, and delete the rest. Worktrees rebuild on next use.
   Held locks/leases still fail closed. When live cannot be detected, retention is skipped
   (orphans-only) so mtime keep-newest alone cannot delete a missed live install.

Removal still fails closed on anything a live process could hold: the install lock, or a
dependency / PM runtime generation lease.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# After a successful install: live gateway install + this many newest others.
DEFAULT_KEEP_NEWEST = 1


def _provably_deleted(root: Path, data_root: Path) -> bool:
    if root.exists():
        return False
    return root.is_relative_to(data_root) or (root.parent.name == ".worktrees" and root.parent.is_dir())


def orphan_install_states(installs: Path) -> list[Path]:
    """State dirs whose recorded checkout is provably deleted. Dirs without a record are left alone."""
    if not installs.is_dir():
        return []
    data_root = installs.resolve().parent
    orphans: list[Path] = []
    for state in sorted(installs.iterdir()):
        record = state / "inputs" / ".project-root"
        if not state.is_dir() or not record.is_file():
            continue
        try:
            root = record.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if root and _provably_deleted(Path(root), data_root):
            orphans.append(state)
    return orphans


def _held(state: Path) -> bool:
    """True while a process may still be using this state dir: the install lock is taken or a
    generation lease is held. Any lock we cannot even open counts as held."""
    from hermes_cli.runtime_state import leases_held
    from pm.filesystem import lock_fd

    lock = state / ".install.lock"
    if lock.exists():
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            return True
        try:
            if not lock_fd(fd, wait=False):
                return True
        finally:
            os.close(fd)
    for generations in (state / "environments", state / "pm-runtime" / "generations"):
        if generations.is_dir():
            for generation in generations.iterdir():
                if generation.is_dir() and (generation / ".leases").is_dir() and leases_held(generation):
                    return True
    return False


def _remove_state(state: Path) -> bool:
    """Remove one state dir; inputs go last so a partial failure can be retried. True when gone."""
    if not state.is_dir():
        return not state.exists()
    for child in state.iterdir():
        if child.name == "inputs":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                child.unlink()
    try:
        remaining = [child for child in state.iterdir() if child.name != "inputs"]
    except OSError:
        return False
    if remaining:
        logger.debug("install state %s only partly removed; retried next pass", state.name)
        return False
    shutil.rmtree(state, ignore_errors=True)
    return not state.exists()


def collect_orphan_install_states(installs: Path) -> list[Path]:
    """Remove every unheld orphan state dir under *installs*; returns what was removed."""
    removed: list[Path] = []
    for state in orphan_install_states(installs):
        if _held(state):
            logger.debug("orphan install state %s is still held; skipped", state.name)
            continue
        # The record goes last, and only once everything else is gone: the startup prune runs on
        # a daemon thread and a removal can fail part-way (an open file on Windows, a read-only
        # dir), and a dir that lost its record first would never be recognised again.
        if _remove_state(state):
            removed.append(state)
    return removed


def live_install_state_names(installs: Path) -> set[str]:
    """Names of install state dirs the running gateway / live checkout still needs.

    Sources, in order of preference:

    * ``gateway.lock`` argv path (the process actually serving the home)
    * ``<data-root>/hermes-agent`` when that tree exists
    * any state whose ``inputs/.project-root`` resolves to one of the above
    """
    if not installs.is_dir():
        return set()
    try:
        data_root = installs.resolve().parent
    except OSError:
        return set()

    live_roots: list[Path] = []
    lock = data_root / "gateway.lock"
    if lock.is_file():
        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError, TypeError):
            payload = json.loads(lock.read_text(encoding="utf-8-sig"))
            argv = payload.get("argv") if isinstance(payload, dict) else None
            if isinstance(argv, list) and argv:
                # argv[0] is typically .../hermes-agent/hermes_cli/main.py
                candidate = Path(str(argv[0])).resolve()
                for parent in (candidate, *candidate.parents):
                    if parent.name == "hermes-agent" and (parent / "hermes_cli").is_dir():
                        live_roots.append(parent)
                        break
    agent = data_root / "hermes-agent"
    if agent.is_dir():
        with contextlib.suppress(OSError):
            live_roots.append(agent.resolve())

    if not live_roots:
        return set()

    from pm.environments import install_key

    names = {install_key(root) for root in live_roots}
    for state in installs.iterdir():
        if not state.is_dir():
            continue
        record = state / "inputs" / ".project-root"
        if not record.is_file():
            continue
        with contextlib.suppress(OSError):
            recorded = record.read_text(encoding="utf-8-sig").strip()
            if not recorded:
                continue
            resolved = Path(recorded).resolve()
            if any(resolved == root for root in live_roots):
                names.add(state.name)
    return names


def _state_mtime(state: Path) -> float:
    try:
        return state.stat().st_mtime
    except OSError:
        return 0.0


def retain_install_state_names(
    installs: Path,
    *,
    keep_newest: int = DEFAULT_KEEP_NEWEST,
    live_names: set[str] | None = None,
) -> set[str]:
    """Names to keep under *installs*: every live name plus up to *keep_newest* other newest dirs.

    Fail closed when live is unknown: return every state dir name so mtime-only retention
    cannot delete a live install the detector missed.
    """
    if not installs.is_dir():
        return set()
    live = set(live_names) if live_names is not None else live_install_state_names(installs)
    if not live:
        return {state.name for state in installs.iterdir() if state.is_dir()}
    keep = set(live)
    others = sorted(
        (state for state in installs.iterdir() if state.is_dir() and state.name not in live),
        key=_state_mtime,
        reverse=True,
    )
    for state in others[: max(0, int(keep_newest))]:
        keep.add(state.name)
    return keep


def prune_install_states(
    installs: Path,
    *,
    keep_newest: int = DEFAULT_KEEP_NEWEST,
) -> list[Path]:
    """Orphans first, then drop unheld state dirs outside live + newest retention.

    Returns every state dir that was fully removed. Never deletes a live or held install.
    When live detection returns empty, retention is skipped (orphans-only) so a missed
    live install cannot be deleted by mtime keep-newest alone.
    """
    removed = collect_orphan_install_states(installs)
    if not installs.is_dir():
        return removed
    live = live_install_state_names(installs)
    if not live:
        logger.warning(
            "install retention skipped: live install could not be detected under %s; orphans-only",
            installs,
        )
        return removed
    keep = retain_install_state_names(installs, keep_newest=keep_newest, live_names=live)
    for state in sorted(installs.iterdir(), key=lambda p: p.name):
        if not state.is_dir() or state.name in keep:
            continue
        if _held(state):
            logger.debug("install state %s is still held; retention skipped", state.name)
            continue
        if _remove_state(state):
            removed.append(state)
            logger.info("pruned install state %s (retention: live + newest %d)", state.name, keep_newest)
    return removed


def reclaim_install_states(installs: Path | None = None) -> list[Path]:
    """Public entry after a successful full install / pm gc. Startup still uses orphans-only."""
    if installs is None:
        from pm.environments import installs_root

        installs = installs_root()
    return prune_install_states(installs)
