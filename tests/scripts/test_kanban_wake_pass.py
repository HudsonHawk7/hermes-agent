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
        "id TEXT, title TEXT, assignee TEXT, workspace_path TEXT, status TEXT)"
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
        "INSERT INTO tasks VALUES ('t_child000', 'waiting', ?, '', 'scheduled')",
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
