"""Pre-heartbeat stall rule: never hold a claim on pid_alive alone.

Workers that hang after worker_bound with no agent_init never write a heartbeat.
The old path extended their claim forever on pid_alive. GM rule (10/23): warn at
5 minutes, reclaim + kill the worker tree at 15, with a loud stalled event that
names the last worker-log line.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db import (
    claim_task,
    create_task,
    get_task,
    list_events,
    release_stale_claims,
)
from hermes_cli.kanban_db_dispatch import (
    DEFAULT_PRE_HEARTBEAT_STALL_KILL_SECONDS,
    DEFAULT_PRE_HEARTBEAT_STALL_WARN_SECONDS,
    detect_pre_heartbeat_stalls,
)


class _NoopKill:
    def __call__(self, pid, sig):
        return None


class _DieOnKill:
    """Signal hook that marks the pid dead so reclaim can finish."""

    def __init__(self, alive: dict[int, bool]):
        self.alive = alive

    def __call__(self, pid, sig):
        self.alive[int(pid)] = False


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    c = kbc.connect(tmp_path / "stall.db")
    yield c
    c.close()


def _host_claim(name: str = "stall-worker") -> str:
    return f"{kb._host_prefix()}{name}"


def _seed_running(
    conn,
    *,
    pid: int = 4242,
    started_ago: int = 0,
    heartbeat_ago=None,
    fingerprint: str | None = "epoch|100",
):
    from hermes_cli import kanban_db_dispatch as kbd

    tid = create_task(conn, title="stall probe", assignee="voss")
    claim = _host_claim()
    claimed = claim_task(conn, tid, claimer=claim)
    assert claimed is not None
    kbd._set_worker_pid(conn, tid, pid)
    now = int(time.time())
    started = now - int(started_ago)
    hb = None if heartbeat_ago is None else now - int(heartbeat_ago)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = ?, "
            "claim_expires = ?, worker_started_at = ? WHERE id = ?",
            (started, hb, now + 3600, fingerprint, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ?, last_heartbeat_at = ?, "
            "worker_started_at = ?, claim_expires = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (started, hb, fingerprint, now + 3600, tid),
        )
    return tid, claim


def test_constants_match_gm_rule():
    assert DEFAULT_PRE_HEARTBEAT_STALL_WARN_SECONDS == 5 * 60
    assert DEFAULT_PRE_HEARTBEAT_STALL_KILL_SECONDS == 15 * 60


def test_release_stale_does_not_extend_on_pid_alive_without_heartbeat(conn, monkeypatch):
    """Expired claim + live pid + null heartbeat must reclaim, not extend forever."""
    tid, _ = _seed_running(conn, started_ago=20 * 60, heartbeat_ago=None)
    # Force the claim past expiry so release_stale_claims considers it.
    conn.execute(
        "UPDATE tasks SET claim_expires = ? WHERE id = ?",
        (int(time.time()) - 60, tid),
    )
    conn.execute(
        "UPDATE task_runs SET claim_expires = ? WHERE id = "
        "(SELECT current_run_id FROM tasks WHERE id = ?)",
        (int(time.time()) - 60, tid),
    )
    conn.commit()

    alive = {4242: True}
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: bool(alive.get(int(pid), False)))
    with patch(
        "hermes_cli.kanban_db_dispatch._process_fingerprint",
        return_value="epoch|100",
    ):
        killed = _DieOnKill(alive)
        n = release_stale_claims(conn, signal_fn=killed)

    assert n == 1
    task = get_task(conn, tid)
    assert task is not None
    assert task.status == "ready"
    kinds = [e.kind for e in list_events(conn, tid)]
    assert "claim_extended" not in kinds
    assert "reclaimed" in kinds


def test_release_stale_still_extends_with_fresh_heartbeat(conn, monkeypatch):
    tid, _ = _seed_running(conn, started_ago=20 * 60, heartbeat_ago=30)
    conn.execute(
        "UPDATE tasks SET claim_expires = ? WHERE id = ?",
        (int(time.time()) - 60, tid),
    )
    conn.execute(
        "UPDATE task_runs SET claim_expires = ? WHERE id = "
        "(SELECT current_run_id FROM tasks WHERE id = ?)",
        (int(time.time()) - 60, tid),
    )
    conn.commit()

    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == 4242)
    with patch(
        "hermes_cli.kanban_db_dispatch._process_fingerprint",
        return_value="epoch|100",
    ):
        n = release_stale_claims(conn, signal_fn=_NoopKill())

    assert n == 0
    task = get_task(conn, tid)
    assert task.status == "running"
    assert any(e.kind == "claim_extended" for e in list_events(conn, tid))


def test_detect_pre_heartbeat_stall_warns_once_at_five_minutes(conn, monkeypatch):
    tid, _ = _seed_running(conn, started_ago=6 * 60, heartbeat_ago=None)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    first = detect_pre_heartbeat_stalls(
        conn,
        warn_seconds=5 * 60,
        kill_seconds=15 * 60,
        signal_fn=_NoopKill(),
    )
    second = detect_pre_heartbeat_stalls(
        conn,
        warn_seconds=5 * 60,
        kill_seconds=15 * 60,
        signal_fn=_NoopKill(),
    )

    assert first == []
    assert second == []
    assert get_task(conn, tid).status == "running"
    warned = [e for e in list_events(conn, tid) if e.kind == "stall_warned"]
    assert len(warned) == 1
    assert warned[0].payload is not None
    assert warned[0].payload.get("warn_after_seconds") == 5 * 60


def test_detect_pre_heartbeat_stall_kills_at_fifteen_with_last_log_line(
    conn, monkeypatch,
):
    tid, _ = _seed_running(conn, started_ago=16 * 60, heartbeat_ago=None)
    alive = {4242: True}
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: bool(alive.get(int(pid), False)))

    # Plant a worker log so the stalled event can name the last line.
    log_path = kb.worker_log_path(tid)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "ws accepted\nws accepted\n# still waiting before agent_init\n",
        encoding="utf-8",
    )

    with patch(
        "hermes_cli.kanban_db_dispatch._process_fingerprint",
        return_value="epoch|100",
    ):
        stalled = detect_pre_heartbeat_stalls(
            conn,
            warn_seconds=5 * 60,
            kill_seconds=15 * 60,
            signal_fn=_DieOnKill(alive),
        )

    assert stalled == [tid]
    task = get_task(conn, tid)
    assert task.status == "ready"
    stalled_events = [e for e in list_events(conn, tid) if e.kind == "stalled"]
    assert len(stalled_events) == 1
    payload = stalled_events[0].payload or {}
    assert payload.get("reason") == "no_heartbeat"
    assert payload.get("last_log_line") == "# still waiting before agent_init"


def test_detect_pre_heartbeat_stall_skips_when_agent_init_in_log(
    conn, monkeypatch,
):
    """Past agent_init the hang class is different; leave the 4h/hb paths alone."""
    tid, _ = _seed_running(conn, started_ago=20 * 60, heartbeat_ago=None)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    log_path = kb.worker_log_path(tid)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "ws accepted\nrun_agent: OpenAI client created (agent_init ok)\n",
        encoding="utf-8",
    )

    stalled = detect_pre_heartbeat_stalls(
        conn,
        warn_seconds=5 * 60,
        kill_seconds=15 * 60,
        signal_fn=_NoopKill(),
    )
    assert stalled == []
    assert get_task(conn, tid).status == "running"
    assert not any(e.kind in ("stalled", "stall_warned") for e in list_events(conn, tid))


def test_detect_pre_heartbeat_stall_noop_before_warn(conn, monkeypatch):
    tid, _ = _seed_running(conn, started_ago=60, heartbeat_ago=None)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    assert detect_pre_heartbeat_stalls(
        conn, warn_seconds=300, kill_seconds=900, signal_fn=_NoopKill(),
    ) == []
    assert get_task(conn, tid).status == "running"
    assert not any(e.kind.startswith("stall") for e in list_events(conn, tid))


def test_stall_warn_queues_critical_thinking_unstick_pointer(conn, monkeypatch, tmp_path):
    """Warn tick queues a named stall_unstick_pointer and leaves the task running.

    Drives the real detect_pre_heartbeat_stalls path (no probe script). Skill
    file is present under a fake profile path so the pointer event fires.
    """
    from hermes_cli.kanban_db_dispatch import (
        STALL_UNSTICK_POINTER_EVENT,
        STALL_UNSTICK_POINTER_TEXT,
        STALL_UNSTICK_SKILL_NAME,
        pending_stall_unstick_pointer,
    )

    skill = tmp_path / "skills" / "thinking" / "critical-thinking-unstick" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# critical-thinking-unstick\n", encoding="utf-8")

    tid, _ = _seed_running(conn, started_ago=6 * 60, heartbeat_ago=None)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        "hermes_cli.kanban_db_dispatch.stall_unstick_skill_path",
        lambda assignee: skill,
    )

    stalled = detect_pre_heartbeat_stalls(
        conn, warn_seconds=5 * 60, kill_seconds=15 * 60, signal_fn=_NoopKill(),
    )
    assert stalled == []
    task = get_task(conn, tid)
    assert task is not None
    assert task.status == "running", "warn must stay advisory — no block/complete/kill"

    kinds = [e.kind for e in list_events(conn, tid)]
    assert "stall_warned" in kinds
    assert STALL_UNSTICK_POINTER_EVENT in kinds
    assert "stalled" not in kinds
    assert "done" not in kinds
    assert "blocked" not in kinds

    pointer_events = [e for e in list_events(conn, tid) if e.kind == STALL_UNSTICK_POINTER_EVENT]
    assert len(pointer_events) == 1
    payload = pointer_events[0].payload or {}
    assert payload.get("skill") == STALL_UNSTICK_SKILL_NAME
    assert payload.get("message") == STALL_UNSTICK_POINTER_TEXT
    assert "critical-thinking-unstick" in (payload.get("message") or "")

    pending = pending_stall_unstick_pointer(conn, tid)
    assert pending is not None
    assert pending.get("skill") == STALL_UNSTICK_SKILL_NAME

    ctx = kb.build_worker_context(conn, tid)
    assert "## Stall unstick pointer" in ctx
    assert STALL_UNSTICK_SKILL_NAME in ctx
    assert STALL_UNSTICK_POINTER_TEXT in ctx


def test_stall_warn_missing_skill_logs_named_event_and_continues(conn, monkeypatch, tmp_path, caplog):
    """Missing skill file → stall_unstick_skill_missing event; tick still completes."""
    import logging

    from hermes_cli.kanban_db_dispatch import (
        STALL_UNSTICK_MISSING_EVENT,
        STALL_UNSTICK_POINTER_EVENT,
        STALL_UNSTICK_SKILL_NAME,
    )

    missing = tmp_path / "no-such-profile" / "skills" / "thinking" / "critical-thinking-unstick" / "SKILL.md"
    tid, _ = _seed_running(conn, started_ago=6 * 60, heartbeat_ago=None)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        "hermes_cli.kanban_db_dispatch.stall_unstick_skill_path",
        lambda assignee: missing,
    )

    with caplog.at_level(logging.WARNING):
        stalled = detect_pre_heartbeat_stalls(
            conn, warn_seconds=5 * 60, kill_seconds=15 * 60, signal_fn=_NoopKill(),
        )

    assert stalled == []
    assert get_task(conn, tid).status == "running"
    kinds = [e.kind for e in list_events(conn, tid)]
    assert "stall_warned" in kinds
    assert STALL_UNSTICK_MISSING_EVENT in kinds
    assert STALL_UNSTICK_POINTER_EVENT not in kinds

    missing_events = [e for e in list_events(conn, tid) if e.kind == STALL_UNSTICK_MISSING_EVENT]
    assert len(missing_events) == 1
    assert missing_events[0].payload.get("skill") == STALL_UNSTICK_SKILL_NAME
    assert missing_events[0].payload.get("reason") == "skill_file_missing"

    assert any(
        "stall unstick" in r.message.lower() and "missing" in r.message.lower()
        for r in caplog.records
    )

    # No pointer section when skill was missing.
    ctx = kb.build_worker_context(conn, tid)
    assert "## Stall unstick pointer" not in ctx


def test_stall_warn_recover_still_running_after_pointer(conn, monkeypatch, tmp_path):
    """After the pointer, a recovering task is not blocked or completed by the stall rule."""
    from hermes_cli.kanban_db_dispatch import STALL_UNSTICK_POINTER_EVENT

    skill = tmp_path / "SKILL.md"
    skill.write_text("# unstick\n", encoding="utf-8")
    tid, _ = _seed_running(conn, started_ago=6 * 60, heartbeat_ago=None)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        "hermes_cli.kanban_db_dispatch.stall_unstick_skill_path",
        lambda assignee: skill,
    )

    detect_pre_heartbeat_stalls(
        conn, warn_seconds=5 * 60, kill_seconds=15 * 60, signal_fn=_NoopKill(),
    )
    assert any(e.kind == STALL_UNSTICK_POINTER_EVENT for e in list_events(conn, tid))
    assert get_task(conn, tid).status == "running"

    # Simulate recovery: first heartbeat lands. Further stall ticks must leave it alone.
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (now, tid),
        )
        conn.execute(
            "UPDATE task_runs SET last_heartbeat_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (now, tid),
        )

    stalled = detect_pre_heartbeat_stalls(
        conn, warn_seconds=5 * 60, kill_seconds=15 * 60, signal_fn=_NoopKill(),
    )
    assert stalled == []
    task = get_task(conn, tid)
    assert task.status == "running"
    assert task.last_heartbeat_at is not None
    # Still exactly one pointer; warn path did not re-fire or escalate.
    assert sum(1 for e in list_events(conn, tid) if e.kind == STALL_UNSTICK_POINTER_EVENT) == 1
    assert not any(e.kind == "stalled" for e in list_events(conn, tid))
