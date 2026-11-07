"""Stuck warning must not cry when the only ready cards cannot be spawned.

A capped lane, a seat that is not a Hermes profile, and an unassigned card
are expected holds. They must be named when a warning does fire, and they
must not by themselves advance the stuck counter. A dispatchable, uncapped
card whose spawn actually fails still counts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
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


def _cap_of_one(monkeypatch) -> None:
    monkeypatch.setattr(
        kbd, "configured_max_in_progress_per_profile", lambda: 1,
    )


def test_describe_suppression_names_cap_and_not_dispatchable():
    """The held-back clause names the cap and the seats that cannot spawn."""
    res = kbd.DispatchResult(
        skipped_per_profile_capped=[("t_4f9b7046", "jocastanu", 1)],
        skipped_nonspawnable=["t_796ac752", "t_b282feb2"],
        skipped_unassigned=["t_f55029ac"],
    )
    text = kbd.describe_suppression([res])
    assert "per_profile_cap=t_4f9b7046(jocastanu)" in text
    assert "not_dispatchable=t_796ac752,t_b282feb2" in text
    assert "unassigned=t_f55029ac" in text


def test_capped_ready_card_does_not_count_as_stuck(kanban_home, all_assignees_spawnable, monkeypatch):
    """A lane already at its cap is not a failed spawn."""
    _cap_of_one(monkeypatch)
    with kbc.connect() as conn:
        holder = kb.create_task(conn, title="running review", assignee="jocastanu")
        assert kb.claim_task(conn, holder, claimer="jocastanu") is not None
        waiting = kb.create_task(conn, title="held review", assignee="jocastanu")
        assert kbd.has_spawnable_ready(conn) is False
        assert kbd.tick_is_stuck(spawned=0, spawnable_uncapped=False) is False
        res = kbd.dispatch_once(
            conn, max_in_progress_per_profile=1, spawn_fn=lambda _t, _w: 1,
        )
    assert waiting in [tid for tid, _assignee, _n in res.skipped_per_profile_capped]
    assert res.spawned == []
    assert "per_profile_cap=" in kbd.describe_suppression([res])


def test_capped_review_card_does_not_count_as_stuck(kanban_home, all_assignees_spawnable, monkeypatch):
    """Review lane uses the same cap. A full lane is not stuck work."""
    _cap_of_one(monkeypatch)
    with kbc.connect() as conn:
        holder = kb.create_task(conn, title="running", assignee="jocastanu")
        assert kb.claim_task(conn, holder, claimer="jocastanu") is not None
        review = kb.create_task(conn, title="review PR", assignee="jocastanu")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
        conn.commit()
        assert kbd.has_spawnable_review(conn) is False


def test_outside_allowlist_is_named_and_not_stuck(kanban_home, all_assignees_spawnable):
    """A Claude Code seat is not a Hermes profile this home may spawn."""
    (Path(kanban_home) / "config.yaml").write_text(
        "kanban:\n  dispatch_profiles:\n    - jocastanu\n",
        encoding="utf-8",
    )
    with kbc.connect() as conn:
        foreign = kb.create_task(
            conn, title="architect seat", assignee="architect_claudecode",
        )
        other = kb.create_task(
            conn, title="second seat", assignee="architect_claudecode",
        )
        assert kbd.has_spawnable_ready(conn) is False
        res = kbd.dispatch_once(conn, dry_run=True)
    assert set(res.skipped_nonspawnable) == {foreign, other}
    text = kbd.describe_suppression([res])
    assert text.startswith("not_dispatchable=")
    assert foreign in text and other in text
    assert kbd.tick_is_stuck(spawned=0, spawnable_uncapped=False) is False


def test_unassigned_alone_is_not_stuck(kanban_home, all_assignees_spawnable):
    """An unassigned card must not advance the stuck counter."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="no owner")
        assert kbd.has_spawnable_ready(conn) is False
        res = kbd.dispatch_once(conn, dry_run=True)
    assert res.skipped_unassigned == [tid]
    assert "unassigned=" in kbd.describe_suppression([res])
    assert kbd.tick_is_stuck(spawned=0, spawnable_uncapped=False) is False


def test_real_failed_spawn_still_counts_as_stuck(kanban_home, all_assignees_spawnable, monkeypatch):
    """A dispatchable, uncapped card whose spawn raises is still stuck work."""
    _cap_of_one(monkeypatch)

    def _boom(_task, _workspace):
        raise RuntimeError("spawn failed")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="should spawn", assignee="hera")
        res = kbd.dispatch_once(
            conn,
            max_in_progress_per_profile=1,
            spawn_fn=_boom,
            failure_limit=5,
        )
        task = kb.get_task(conn, tid)
        still_waiting = kbd.has_spawnable_ready(conn)
    assert res.spawned == []
    assert task.status == "ready"
    assert still_waiting is True
    assert kbd.tick_is_stuck(spawned=0, spawnable_uncapped=True) is True
