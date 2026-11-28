"""Wake-pass gates: a stored none is not final, and the daily list is once a day."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.kanban_wake_pass import dry_board

CT = ZoneInfo("America/Chicago")


def _board(tmp_path, *, note: str, assignee: str = "hera"):
    folder = tmp_path / "default"
    folder.mkdir()
    db = sqlite3.connect(folder / "kanban.db")
    db.execute(
        "CREATE TABLE tasks ("
        "id TEXT, title TEXT, assignee TEXT, workspace_path TEXT, status TEXT, "
        "priority INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute(
        "CREATE TABLE task_comments ("
        "id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT)"
    )
    db.execute(
        "CREATE TABLE task_wakes ("
        "id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, "
        "parent_id TEXT, repo TEXT, pr_number INTEGER, until_ts INTEGER)"
    )
    db.execute(
        "INSERT INTO tasks (id, title, assignee, workspace_path, status) "
        "VALUES ('t_child000', 'waiting', ?, '', 'scheduled')",
        (assignee,),
    )
    db.execute(
        "INSERT INTO task_comments (task_id, author, body) VALUES "
        "('t_child000', 'hera', ?)",
        (note,),
    )
    db.execute(
        "INSERT INTO task_comments (task_id, author, body) VALUES "
        "('t_child000', 'park-wake', 'backfill: wake=none')"
    )
    db.execute("INSERT INTO task_wakes (task_id, kind) VALUES ('t_child000', 'none')")
    db.commit()
    db.close()
    return folder


def test_dry_run_rereads_a_stored_none(tmp_path):
    folder = _board(tmp_path, note="waiting on t_ecc82493")
    lines = dry_board(
        "default",
        folder,
        now=1,
        active={},
        parents={"t_ecc82493": "done"},
        profiles={"hera"},
    )
    assert any("would-promote t_child000" in line for line in lines)


def test_dry_run_ignores_a_task_id_in_an_older_comment(tmp_path):
    root = _board(tmp_path, note="placeholder", assignee="probe")
    db = sqlite3.connect(root / "kanban.db")
    db.execute("DELETE FROM task_comments")
    db.execute(
        "INSERT INTO task_comments (task_id, author, body) "
        "VALUES ('t_child000', 'hera', 'triage reassign of t_aaaaaaaa')"
    )
    db.execute(
        "INSERT INTO task_comments (task_id, author, body) VALUES "
        "('t_child000', 'darkeyes', "
        "'still parked: design backlog, not next in a runnable lane.')"
    )
    db.execute(
        "INSERT INTO task_comments (task_id, author, body) "
        "VALUES ('t_child000', 'park-wake', 'backfill: wake=none')"
    )
    db.commit()
    db.close()
    lines = dry_board(
        "default",
        root,
        now=1,
        active={},
        parents={"t_aaaaaaaa": "done"},
        profiles={"probe"},
    )
    assert lines == []


def test_dry_run_walks_priority_then_created_time(tmp_path):
    root = _board(tmp_path, note="placeholder", assignee="hera")
    db = sqlite3.connect(root / "kanban.db")
    db.execute("DELETE FROM tasks")
    db.execute("DELETE FROM task_comments")
    db.execute("DELETE FROM task_wakes")
    db.execute(
        "INSERT INTO tasks "
        "(id, title, assignee, workspace_path, status, priority, created_at) "
        "VALUES ('t_lowpri00', 'low', 'hera', '', 'scheduled', 0, 1)"
    )
    db.execute(
        "INSERT INTO tasks "
        "(id, title, assignee, workspace_path, status, priority, created_at) "
        "VALUES ('t_aaaaaaa1', 'late high', 'hera', '', 'scheduled', 5, 30)"
    )
    db.execute(
        "INSERT INTO tasks "
        "(id, title, assignee, workspace_path, status, priority, created_at) "
        "VALUES ('t_zzzzzzz2', 'early high', 'hera', '', 'scheduled', 5, 10)"
    )
    for task_id in ("t_lowpri00", "t_aaaaaaa1", "t_zzzzzzz2"):
        db.execute(
            "INSERT INTO task_comments (task_id, author, body) VALUES (?, 'darkeyes', ?)",
            (task_id, "waiting on t_aaaaaaaa"),
        )
    db.commit()
    db.close()
    lines = dry_board(
        "default",
        root,
        now=1,
        active={},
        parents={"t_aaaaaaaa": "done"},
        profiles={"hera"},
    )
    assert lines[0].startswith("default would-promote t_zzzzzzz2")
    assert any(line.startswith("default would-hold t_aaaaaaa1") for line in lines)
    assert any(line.startswith("default would-hold t_lowpri00") for line in lines)


def test_dry_run_hold_stays_parked(tmp_path):
    folder = _board(tmp_path, note="waiting on t_ecc82493. Do not promote.")
    lines = dry_board(
        "default",
        folder,
        now=1,
        active={},
        parents={"t_ecc82493": "done"},
        profiles={"hera"},
    )
    assert not any("would-promote" in line for line in lines)


def test_daily_list_is_the_first_pass_at_or_after_7_am_ct():
    from scripts.kanban_wake_pass import pass_plan, should_run_daily

    before = datetime(2026, 11, 13, 6, 59, tzinfo=CT).timestamp()
    at_seven = datetime(2026, 11, 13, 7, 0, tzinfo=CT).timestamp()
    later = datetime(2026, 11, 13, 15, 0, tzinfo=CT).timestamp()
    next_morning = datetime(2026, 11, 14, 7, 0, tzinfo=CT).timestamp()
    assert should_run_daily({}, before) is False
    assert should_run_daily({}, at_seven) is True
    assert should_run_daily({"last_daily_date": "2026-11-13"}, later) is False
    assert should_run_daily({"last_daily_date": "2026-11-13"}, next_morning) is True
    assert pass_plan([], {}, before) == {"daily": False, "stamp": False}
    assert pass_plan([], {}, at_seven)["daily"] is True
    assert pass_plan(["--daily"], {}, before)["daily"] is True
    assert pass_plan(["--apply"], {}, at_seven)["stamp"] is True
    assert pass_plan(["--apply", "--daily"], {}, before)["stamp"] is False
