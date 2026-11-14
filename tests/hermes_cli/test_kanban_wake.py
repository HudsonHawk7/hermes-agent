"""Parks wake themselves: unmet stays parked, met promotes, a merged gate archives, caps hold."""

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake as kw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _park(conn, title, assignee="probe"):
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    assert kb.schedule_task(conn, task_id, reason="parked for the wake test")
    return task_id


def _status(conn, task_id):
    return kb.get_task(conn, task_id).status


def test_unmet_parent_wait_stays_parked(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="still running", assignee="aurora")
        child = _park(conn, "waits on the parent", assignee="architect")
        kw.record_wakes(conn, child, wait_on=[parent])
        result = kw.promote_ready(conn, now=time.time(), pr_status=lambda repo, number: "open")
        assert result["promoted"] == []
        assert _status(conn, child) == "scheduled"


def test_met_parent_wait_promotes(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="finished", assignee="aurora")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.commit()
        child = _park(conn, "ready once the parent is done", assignee="architect")
        kw.record_wakes(conn, child, wait_on=[parent])
        result = kw.promote_ready(conn, now=time.time(), pr_status=lambda repo, number: "open")
        assert child in result["promoted"]
        assert _status(conn, child) == "ready"


def test_merged_pr_gate_archives(kanban_home):
    with kbc.connect() as conn:
        gate = _park(conn, "Gate PR 203", assignee="darkeyes")
        kw.record_wakes(conn, gate, until_prs=[("HudsonHawk7/hermes-agent", 203)])
        result = kw.archive_merged_gates(
            conn,
            pr_status=lambda repo, number: "merged",
            default_repo="HudsonHawk7/hermes-agent",
        )
        assert gate in result["archived"]
        assert _status(conn, gate) == "archived"
        comment = conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (gate,),
        ).fetchone()
        assert "superseded" in (comment["body"] or "").lower()


def test_lane_cap_holds_a_met_wait(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="finished", assignee="aurora")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.commit()
        child = _park(conn, "met, but the lane is full", assignee="probe")
        kw.record_wakes(conn, child, wait_on=[parent])
        result = kw.promote_ready(
            conn,
            now=time.time(),
            pr_status=lambda repo, number: "open",
            lane_active={"probe": 1},
            lane_cap=1,
        )
        assert result["promoted"] == []
        assert result["held"] == [child]
        assert _status(conn, child) == "scheduled"


def test_park_with_no_wake_is_marked_none(kanban_home):
    with kbc.connect() as conn:
        task_id = _park(conn, "no machine wake")
        kw.record_wakes(conn, task_id)
        kinds = kw.wake_kinds(conn, task_id)
        assert kinds == ["none"]
        result = kw.promote_ready(conn, now=time.time(), pr_status=lambda repo, number: "merged")
        assert task_id not in result["promoted"]
        assert _status(conn, task_id) == "scheduled"


def test_passed_until_promotes(kanban_home):
    with kbc.connect() as conn:
        task_id = _park(conn, "recheck after the until", assignee="probe")
        kw.record_wakes(conn, task_id, until_ts=1_700_000_000)
        result = kw.promote_ready(conn, now=1_700_000_001, pr_status=lambda repo, number: "open")
        assert task_id in result["promoted"]
        assert _status(conn, task_id) == "ready"


def test_backfill_reads_until_and_skips_a_merged_mention(kanban_home):
    with kbc.connect() as conn:
        waiting = _park(conn, "Time Capsule", assignee="probe")
        held = _park(conn, "map design", assignee="architect")
        kw.backfill_task(
            conn,
            waiting,
            "still parked. SCHEDULED_UNTIL=2026-11-09T16:00:00-06:00. Do not restart the gateway.",
        )
        kw.backfill_task(
            conn,
            held,
            "Parked: PR 203 is already merged. Do not promote.",
        )
        assert "time" in kw.wake_kinds(conn, waiting)
        assert kw.wake_kinds(conn, held) == ["none"]


def test_backfill_comment_does_not_hide_a_hold(kanban_home):
    with kbc.connect() as conn:
        task_id = _park(conn, "already live", assignee="probe")
        kb.add_comment(conn, task_id, "darkeyes", "Parked: PR 19 is already live. Do not promote.")
        kw.record_wakes(conn, task_id, until_prs=[("HudsonHawk7/Force_2.1", 19)])
        kb.add_comment(conn, task_id, "park-wake", "backfill: until-pr HudsonHawk7/Force_2.1#19")
        result = kw.promote_ready(conn, now=time.time(), pr_status=lambda repo, number: "merged")
        assert task_id not in result["promoted"]
        assert _status(conn, task_id) == "scheduled"
