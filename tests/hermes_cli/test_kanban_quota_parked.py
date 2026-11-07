"""Stale quota text must not park a review handoff, and stuck warnings must name real holds.

A rate-limited run stamps ``last_failure_error``. A later ``review_requested``
run is a success: the stamp is cleared, and the respawn guard does not treat
that text as a quota hold. The stuck warning names each held task id with a
key the secret redactor will not eat.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_review_handoff_after_rate_limit_clears_stamp_and_spawns(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """rate_limited, then review_requested, then the next tick spawns the reviewer."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    quota = "pid 1 exited rate-limited (quota wall) — requeued"
    spawned: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="handoff after quota", assignee="probe")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id
        run_id = claimed.current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=5000000 WHERE id=?",
            (run_id,),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
            (quota, tid),
        )
        conn.commit()

        again = kb.claim_task(conn, tid)
        assert again is not None and again.current_run_id
        assert kb.request_review(
            conn, tid, summary="ready for review", reviewer="probe",
            expected_run_id=again.current_run_id,
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "review"
        assert task.last_failure_error is None
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None

        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert tid in spawned
        assert any(item[0] == tid for item in res.spawned)
        assert tid not in [gid for gid, _reason in res.respawn_guarded]


def test_describe_suppression_names_task_ids_and_survives_redaction():
    """Warning text contains the task id and no ***. The old blocker_auth= key does not."""
    from agent.redact import redact_sensitive_text

    res = kbd.DispatchResult()
    res.respawn_guarded = [("t_fb73530b", "blocker_auth")]
    text = kbd.describe_suppression([res])
    assert "t_fb73530b" in text
    assert "quota_or_auth_hold=t_fb73530b" in text
    assert "blocker_auth=" not in text
    assert "***" not in text

    warning = (
        "kanban dispatcher stuck: ready queue non-empty for 6 consecutive ticks "
        f"but 0 workers spawned. Last tick held back: {text}."
    )
    redacted = redact_sensitive_text(warning, force=True)
    assert "t_fb73530b" in redacted
    assert "***" not in redacted

    eaten = redact_sensitive_text(
        "Last tick held back: blocker_auth=t_fb73530b.", force=True,
    )
    assert "t_fb73530b" not in eaten


def _close_claimed_run(conn, task_id, run_id, outcome, ended_at, *, assignee="architect"):
    """End a claimed run in SQL and hand the card back to ready.

    Skips the lifecycle helpers on purpose: those helpers clear a quota stamp,
    and the live bug is a stamp that survived an older review handoff.
    """
    conn.execute(
        "UPDATE task_runs SET outcome = ?, status = ?, ended_at = ? WHERE id = ?",
        (outcome, outcome, ended_at, run_id),
    )
    conn.execute(
        """
        UPDATE tasks
           SET status = 'ready',
               current_run_id = NULL,
               claim_lock = NULL,
               claim_expires = NULL,
               worker_pid = NULL,
               worker_started_at = NULL,
               assignee = ?
         WHERE id = ?
        """,
        (assignee, task_id),
    )


def test_changes_requested_after_rate_limit_respawns_implementer(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """rate_limited, then review_requested, then changes_requested; the next tick respawns.

    Mirrors t_fb73530b: the quota stamp predates the clear-on-handoff, and
    changes_requested is the latest run. That non-failure end must not park
    the implementer.
    """
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    quota = "pid 1 exited rate-limited (quota wall) — requeued"
    spawned: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="changes after quota", assignee="architect")
        for outcome, ended_at in (
            ("rate_limited", 5_000_000),
            ("review_requested", 5_000_100),
            ("changes_requested", 5_000_200),
        ):
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None and claimed.current_run_id
            _close_claimed_run(conn, tid, claimed.current_run_id, outcome, ended_at)
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            (quota, tid),
        )
        conn.commit()

        assert kbd.check_respawn_guard(conn, tid) is None
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert tid in spawned
        assert any(item[0] == tid for item in res.spawned)
        assert tid not in [gid for gid, _reason in res.respawn_guarded]


def test_request_changes_clears_stale_quota_stamp(kanban_home):
    """A changes_requested end clears quota text the earlier handoff never saw."""
    quota = "pid 1 exited rate-limited (quota wall) — requeued"
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="clear on changes", assignee="architect")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome = 'rate_limited', status = 'rate_limited', "
            "ended_at = 5000000 WHERE id = ?",
            (claimed.current_run_id,),
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "last_failure_error = ? WHERE id = ?",
            (quota, tid),
        )
        conn.commit()
        assert kb.request_review(conn, tid, reviewer="probe") is True
        # The live stamp survived the handoff (it was written before that clear existed).
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            (quota, tid),
        )
        conn.commit()
        review = kb.claim_review_task(conn, tid, claimer="probe")
        assert review is not None
        ok, implementer = kb.request_changes(conn, tid, reason="needs another pass")
        assert ok is True
        assert implementer == "architect"
        row = conn.execute(
            "SELECT last_failure_error, status, assignee FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        assert row["last_failure_error"] is None
        assert row["status"] == "ready"
        assert row["assignee"] == "architect"


def test_approved_run_also_supersedes_stale_quota_stamp(kanban_home):
    """approved is a non-failure end, same as changes_requested."""
    quota = "pid 1 exited rate-limited (quota wall) — requeued"
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="approved after quota", assignee="architect")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id
        _close_claimed_run(conn, tid, claimed.current_run_id, "approved", 5_000_300)
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            (quota, tid),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) is None


def test_latest_auth_failure_still_holds_after_changes_requested(kanban_home):
    """A real auth failure on the latest run still parks, even after an older hand-back."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="auth after changes", assignee="architect")
        first = kb.claim_task(conn, tid)
        assert first is not None and first.current_run_id
        _close_claimed_run(conn, tid, first.current_run_id, "changes_requested", 5_000_100)
        second = kb.claim_task(conn, tid)
        assert second is not None and second.current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome = 'gave_up', status = 'gave_up', ended_at = 5000200 "
            "WHERE id = ?",
            (second.current_run_id,),
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "last_failure_error = ? WHERE id = ?",
            ("401 unauthorized", tid),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"
