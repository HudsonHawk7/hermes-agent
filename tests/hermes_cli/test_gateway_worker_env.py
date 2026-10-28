"""A gateway must not inherit a worker's Kanban fence.

A gateway started from inside an agent/Kanban session (Hermes Desktop launched from a
worker's shell, ``hermes gateway run`` typed in one, ``hermes gateway ensure`` spawned
from one) inherits ``HERMES_DELEGATED_CHILD_CONTEXT`` plus the ``HERMES_KANBAN_*`` board
pins. ``kanban_path_is_fenced()`` then denies every board mutation, so every dispatcher
tick on every board dies with ``PermissionError: delegate_task child contexts cannot
mutate Kanban tasks or boards`` and the board is dead until the gateway is relaunched
from a clean shell.

These tests boot a real dispatcher tick against a real (temp) board: fenced, the tick
raises; after the gateway's boot-time strip, the same tick lands.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.gateway_worker_env import WORKER_ENV_MARKERS, strip_worker_markers


@pytest.fixture
def clean_worker_env(monkeypatch):
    """Start from no inherited worker markers (the test process may itself be a worker)."""
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def board_home(tmp_path, clean_worker_env):
    home = tmp_path / ".hermes"
    home.mkdir()
    clean_worker_env.setenv("HERMES_HOME", str(home))
    clean_worker_env.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_a_gateway_that_inherits_the_marker_cannot_dispatch_until_it_strips_it(
    board_home, all_assignees_spawnable, clean_worker_env,
):
    """The bug, then the fix, through the real dispatcher tick."""
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="t", assignee="alice")
        # The environment Desktop/CLI had: launched from inside a Kanban session.
        clean_worker_env.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(board_home))
        clean_worker_env.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
        clean_worker_env.setenv("HERMES_KANBAN_TASK", task_id)

        # Inherited fence: the tick's first write (the crash sweep) is refused.
        with pytest.raises(PermissionError, match="cannot mutate Kanban"):
            kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)

        # Gateway boot strips its own environment, then dispatches normally.
        stripped = strip_worker_markers()
        assert "HERMES_DELEGATED_CHILD_CONTEXT" in stripped
        assert not any(key in os.environ for key in WORKER_ENV_MARKERS)

        tick = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()

    assert not tick.skipped_locked
    assert tick.spawned, f"expected the stripped gateway to dispatch the card, got {tick}"
    assert task is not None and task.status == "running"


def test_the_warning_names_every_stripped_marker(caplog):
    env = {key: "inherited" for key in WORKER_ENV_MARKERS}
    env["HERMES_HOME"] = "keep-me"

    with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway_worker_env"):
        stripped = strip_worker_markers(env)

    assert set(stripped) == set(WORKER_ENV_MARKERS)
    assert env == {"HERMES_HOME": "keep-me"}
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one WARNING, got {[r.getMessage() for r in warnings]}"
    message = warnings[0].getMessage()
    for key in WORKER_ENV_MARKERS:
        assert key in message, f"the warning must name {key}: {message}"


def test_a_clean_environment_logs_nothing_and_strips_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway_worker_env"):
        assert strip_worker_markers({}) == ()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_the_spawned_daemon_strips_every_worker_marker_too():
    """Defense in depth: Desktop's ``gateway ensure`` spawn never hands these to the daemon at all."""
    from hermes_cli.gateway_runtime_start import DAEMON_STRIPPED_ENV

    missing = [key for key in WORKER_ENV_MARKERS if key not in DAEMON_STRIPPED_ENV]
    assert not missing, f"the daemon spawn env must strip {missing} as well"


def test_gateway_run_main_strips_the_markers_before_anything_else(monkeypatch, clean_worker_env, caplog):
    """``python -m gateway.run`` — the s6/container boot — strips before its first step."""
    import sys
    import types

    # ``gateway.run`` imports ``hermes_bootstrap`` for its side effects only. In THIS checkout the real
    # bootstrap settles a stale interrupted-update marker under the installed tree and trips the
    # home-I/O guard (pre-existing, not this change: tests/hermes_cli/test_gateway_run_hard_exit.py
    # fails the same way). Neutralise only that recovery step so the real modules load.
    monkeypatch.setattr("hermes_cli._early_recovery.restore_interrupted_pull", lambda root: False)
    import gateway.run as gateway_run

    clean_worker_env.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    clean_worker_env.setenv("HERMES_KANBAN_BOARD", "inherited")
    clean_worker_env.setenv("HERMES_KANBAN_CLAIM_LOCK", "hostA:1")

    seen_at_boot: list[tuple[str, ...]] = []

    def _fake_run(coro):
        seen_at_boot.append(tuple(key for key in WORKER_ENV_MARKERS if key in os.environ))
        coro.close()
        return True

    # Hermetic boot: no identity registration, no watchdog, no pm maintenance on the real install.
    monkeypatch.setattr(gateway_run, "_guard_corrupt_user_config", lambda: None)
    monkeypatch.setattr(gateway_run, "_best_effort", lambda step: None)
    monkeypatch.setattr("hermes_cli.venv_sync.check_runtime", lambda root: None)
    monkeypatch.setattr("hermes_cli.boot_bootstrap.maybe_run_boot_bootstrap", lambda root: None)
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", lambda code: None)
    monkeypatch.setattr(gateway_run.asyncio, "run", _fake_run)
    monkeypatch.setattr(sys, "argv", ["gateway"])

    with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway_worker_env"):
        gateway_run.main()

    assert seen_at_boot == [()], f"markers survived into the gateway boot: {seen_at_boot}"
    assert not any(key in os.environ for key in WORKER_ENV_MARKERS)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("HERMES_DELEGATED_CHILD_CONTEXT" in m for m in messages), messages


def test_run_gateway_strips_the_markers_before_its_first_guard(monkeypatch, clean_worker_env, caplog):
    """``hermes gateway run`` — the entry Desktop's ``gateway ensure`` spawns — strips first."""
    import sys
    import types

    import hermes_cli.gateway as gateway_cli

    # ``run_gateway`` imports ``gateway.run`` lazily at boot; importing the REAL module runs
    # ``hermes_bootstrap``, which settles an interrupted update under the installed tree and trips the
    # home-I/O guard in this checkout (pre-existing: tests/hermes_cli/test_gateway_run_hard_exit.py
    # fails the same way). Stub only those two boot hooks — the subject here is run_gateway's ordering.
    monkeypatch.setitem(sys.modules, "gateway.run", types.SimpleNamespace(
        start_gateway=lambda *args, **kwargs: None,
        _exit_after_graceful_shutdown=lambda code: None,
    ))

    clean_worker_env.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    clean_worker_env.setenv("HERMES_KANBAN_DB", "S:/pinned/kanban.db")
    clean_worker_env.setenv("HERMES_KANBAN_TASK", "t_inherited")
    clean_worker_env.setenv("HERMES_GATEWAY_EXIT_DIAG", "0")

    seen_at_first_guard: list[tuple[str, ...]] = []

    def _first_guard() -> None:
        seen_at_first_guard.append(tuple(key for key in WORKER_ENV_MARKERS if key in os.environ))

    monkeypatch.setattr(gateway_cli, "_guard_official_docker_root_gateway", _first_guard)
    monkeypatch.setattr(gateway_cli, "_guard_named_profile_under_multiplexer", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_attach_to_host_gateway_or_guard", lambda **kwargs: None)
    monkeypatch.setattr(gateway_cli, "_guard_supervised_gateway_conflict", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_guard_existing_gateway_process_conflict", lambda replace=False: None)
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)

    async def _start_gateway(*args, **kwargs):  # pragma: no cover - never awaited here
        return True

    monkeypatch.setattr(sys.modules["gateway.run"], "start_gateway", _start_gateway)
    monkeypatch.setattr(gateway_cli.asyncio, "run", lambda coro: (coro.close(), True)[1])

    with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway_worker_env"):
        gateway_cli.run_gateway()

    # The gateway's own guards never saw the inherited markers...
    assert seen_at_first_guard == [()], f"markers survived into the boot guards: {seen_at_first_guard}"
    assert not any(key in os.environ for key in WORKER_ENV_MARKERS)
    # ...and the operator gets one line naming what was dropped.
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("HERMES_DELEGATED_CHILD_CONTEXT" in m and "HERMES_KANBAN_TASK" in m for m in messages), messages
