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
