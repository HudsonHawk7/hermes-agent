"""``kanban unblock --author``: a script that revives a card signs the comment
it writes, instead of the comment carrying whatever profile the script happened
to run under."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban as kanban_cli


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _parked(conn, title="parked"):
    tid = kb.create_task(conn, title=title, assignee="hera")
    kb.schedule_task(conn, tid, reason="waiting on a time window")
    return tid


def test_unblock_author_names_the_comment_author(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _parked(conn)
        args = argparse.Namespace(
            task_ids=[tid], ids=None,
            reason="idle lane: nothing in ready/running/review, rule=idle_lane",
            author="darkeyes-sweep",
        )
        assert kanban_cli._cmd_unblock(args) == 0
        comments = kb.list_comments(conn, tid)
        assert comments[-1].author == "darkeyes-sweep"
        assert comments[-1].body.startswith("UNBLOCK: idle lane")
        assert kb.get_task(conn, tid).status == "ready"


def test_unblock_without_author_keeps_the_profile_author(kanban_home, monkeypatch):
    """Omitted --author keeps today's behaviour exactly."""
    monkeypatch.setattr(kanban_cli, "_profile_author", lambda: "the-profile")
    with kbc.connect_closing() as conn:
        tid = _parked(conn)
        args = argparse.Namespace(task_ids=[tid], ids=None, reason="manual nudge", author=None)
        assert kanban_cli._cmd_unblock(args) == 0
        assert kb.list_comments(conn, tid)[-1].author == "the-profile"


def test_unblock_author_is_a_documented_flag():
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "unblock", "t_abc", "--reason", "why", "--author", "who"])
    assert args.author == "who"
    assert parser.parse_args(["kanban", "unblock", "t_abc"]).author is None
