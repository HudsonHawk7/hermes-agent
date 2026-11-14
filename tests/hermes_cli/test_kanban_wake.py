"""Parks wake themselves: unmet stays parked, met promotes, a merged gate archives, caps hold."""

import argparse
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


def test_still_parked_and_do_not_restart_are_not_holds():
    assert kw.hold_reason("x", "still parked. waiting on t_aaaaaaaa") == ""
    assert kw.hold_reason("x", "Do not restart the gateway.") == ""
    assert kw.hold_reason("x", "Do not promote.") == "deliberate_hold"
    assert kw.hold_reason("x", "do not build") == "deliberate_hold"
    assert kw.hold_reason("x", "parked on purpose") == "deliberate_hold"
    assert kw.hold_reason("x", "leave parked") == "deliberate_hold"
    assert kw.hold_reason("x", "on hold") == "deliberate_hold"


def test_still_parked_note_does_not_block_a_met_parent(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="done parent", assignee="aurora")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        child = _park(conn, "normal park", assignee="architect")
        kb.add_comment(
            conn,
            child,
            "probe",
            "still parked. waiting on the parent. Do not restart the gateway.",
        )
        kw.record_wakes(conn, child, wait_on=[parent])
        result = kw.promote_ready(conn, now=time.time(), pr_status=lambda repo, number: "open")
        assert child in result["promoted"]
        assert _status(conn, child) == "ready"


def test_already_in_live_head_is_not_a_wait():
    short = "PR 19 is already in live HEAD ... Do not promote."
    longer = (
        "Parked: member-media PR 19 is already in live HEAD d6270f17. "
        "Not next in a runnable lane. Do not promote."
    )
    landed_only = "PR 19 is already in live HEAD."
    assert kw.parse_park_note(short, own_id="t_00000000")["until_prs"] == []
    assert kw.parse_park_note(longer, own_id="t_00000000")["until_prs"] == []
    assert kw.parse_park_note(landed_only, own_id="t_00000000")["until_prs"] == []


def test_landed_phrase_does_not_cancel_the_next_pr_wait():
    mixed = "PR 19 is already in live HEAD. Waiting on PR 20."
    parsed = kw.parse_park_note(mixed, own_id="t_00000000")
    assert parsed["until_prs"] == [("HudsonHawk7/Force_2.1", 20)]


def test_pr_list_captures_every_number():
    parsed = kw.parse_park_note(
        "Parked until PRs 233, 234, 235 and 236 merge.",
        own_id="t_00000000",
    )
    numbers = [number for _repo, number in parsed["until_prs"]]
    assert numbers == [233, 234, 235, 236]


def test_daily_list_reads_the_human_note_not_the_backfill(kanban_home):
    with kbc.connect() as conn:
        task_id = _park(conn, "needs a ruling")
        kb.add_comment(conn, task_id, "darkeyes", "Parked: waiting on the GM.")
        kw.record_wakes(conn, task_id)
        kb.add_comment(conn, task_id, "park-wake", "backfill: wake=none")
        conn.execute("UPDATE tasks SET created_at=1 WHERE id=?", (task_id,))
        conn.execute(
            "UPDATE task_events SET created_at=1 WHERE task_id=? AND kind='scheduled'",
            (task_id,),
        )
        lines = kw.daily_lines(conn, now=1 + 4 * 24 * 3600)
        assert any("waiting on the GM" in line for line in lines)


def test_non_dispatch_assignee_stays_parked(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="done parent", assignee="aurora")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        child = _park(conn, "outside the dispatcher", assignee="architect_claudecode")
        kw.record_wakes(conn, child, wait_on=[parent])
        result = kw.promote_ready(
            conn,
            now=time.time(),
            pr_status=lambda repo, number: "open",
            dispatch_profiles={"probe", "hera"},
        )
        assert result["promoted"] == []
        assert _status(conn, child) == "scheduled"


def _done_parent(conn) -> str:
    parent = kb.create_task(conn, title="done parent", assignee="aurora")
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    conn.commit()
    return parent


def test_stored_none_does_not_hide_a_met_human_wait(kanban_home):
    with kbc.connect() as conn:
        parent = _done_parent(conn)
        child = _park(conn, "waiting on a done parent", assignee="probe")
        kb.add_comment(conn, child, "hera", f"still parked. waiting on {parent}")
        kw.record_wakes(conn, child)
        kb.add_comment(conn, child, "park-wake", "backfill: wake=none")
        kw.backfill_scheduled(conn)
        assert "parent" in kw.wake_kinds(conn, child)
        result = kw.promote_ready(
            conn, now=time.time(), pr_status=lambda repo, number: "open"
        )
        assert child in result["promoted"]
        assert _status(conn, child) == "ready"


def test_stored_none_with_a_hold_stays_parked(kanban_home):
    with kbc.connect() as conn:
        parent = _done_parent(conn)
        child = _park(conn, "held on purpose", assignee="probe")
        kb.add_comment(conn, child, "hera", f"waiting on {parent}. Do not promote.")
        kw.record_wakes(conn, child)
        kb.add_comment(conn, child, "park-wake", "backfill: wake=none")
        kw.backfill_scheduled(conn)
        result = kw.promote_ready(
            conn, now=time.time(), pr_status=lambda repo, number: "open"
        )
        assert child not in result["promoted"]
        assert _status(conn, child) == "scheduled"


def test_cli_wake_pass_skips_a_non_dispatch_assignee(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from hermes_cli import kanban as kanban_cli
    from hermes_cli import kanban_db_dispatch as kbd

    assert str(kanban_home) in str(kb.kanban_db_path())
    monkeypatch.setattr(
        kbd, "_dispatch_profile_allowlist", lambda _normalize: frozenset({"probe"})
    )
    with kbc.connect() as conn:
        parent = _done_parent(conn)
        outsider = _park(conn, "not dispatched", assignee="architect_claudecode")
        insider = _park(conn, "dispatched", assignee="probe")
        kw.record_wakes(conn, outsider, wait_on=[parent])
        kw.record_wakes(conn, insider, wait_on=[parent])
    args = argparse.Namespace(backfill=False, daily=False)
    assert kanban_cli._cmd_wake_pass(args) == 0
    with kbc.connect() as conn:
        assert _status(conn, outsider) == "scheduled"
        assert _status(conn, insider) == "ready"
