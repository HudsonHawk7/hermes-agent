"""T: heals itself after a Time Capsule SMB drop.

The probe and the mapper are fakes. These tests never open the share and never start explorer.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hermes_cli.timecaps_watch import (
    DOWN_ASK_SECONDS,
    INTERVAL_SECONDS,
    POWER_CYCLE_ASK,
    WatchState,
    blocked_on_timecaps,
    format_drop_line,
    run_timecaps_watch_once,
    tick,
)

CT = ZoneInfo("America/Chicago")
T0 = datetime(2026, 11, 4, 13, 40, tzinfo=CT).timestamp()


class _Seam:
    def __init__(self, blocked=None):
        self.mapped = 0
        self.raises = []
        self.withdraws = []
        self.comments = []
        self.blocked = list(blocked or [])

    def launch_mapper(self) -> str:
        self.mapped += 1
        return "launched"

    def raise_ask(self, ask: dict) -> str:
        self.raises.append(ask)
        return "t_ask"

    def withdraw_ask(self, task_id: str) -> bool:
        self.withdraws.append(task_id)
        return True

    def list_blocked(self):
        return list(self.blocked)

    def comment(self, task_id: str, body: str) -> None:
        self.comments.append((task_id, body))


def _tick(seam, state, *, now, port_open, drive_present):
    return tick(
        state,
        now=now,
        port_open=port_open,
        drive_present=drive_present,
        launch_mapper=seam.launch_mapper,
        raise_ask=seam.raise_ask,
        withdraw_ask=seam.withdraw_ask,
        list_blocked=seam.list_blocked,
        comment=seam.comment,
    )


def test_present_does_not_remap_or_ask():
    seam = _Seam()
    result = _tick(seam, WatchState(), now=T0, port_open=True, drive_present=True)
    assert seam.mapped == 0
    assert seam.raises == []
    assert seam.withdraws == []
    assert result.log_lines == []
    assert result.state.drop_started_at is None


def test_missing_and_up_remaps_and_does_not_ask():
    seam = _Seam()
    result = _tick(seam, WatchState(), now=T0, port_open=True, drive_present=False)
    assert seam.mapped == 1
    assert seam.raises == []
    assert any("launched" in line for line in result.log_lines)
    assert not any(line.startswith("drop ") for line in result.log_lines)


def test_down_under_15_min_does_not_ask_or_remap():
    seam = _Seam()
    state = WatchState()
    result = _tick(seam, state, now=T0, port_open=False, drive_present=False)
    result = _tick(seam, result.state, now=T0 + DOWN_ASK_SECONDS - 1, port_open=False, drive_present=False)
    assert seam.raises == []
    assert seam.mapped == 0
    assert result.state.drop_started_at == T0
    assert not any(line.startswith("drop ") for line in result.log_lines)


def test_down_15_min_raises_one_ask_and_does_not_repeat():
    seam = _Seam()
    state = WatchState()
    for offset in (0, DOWN_ASK_SECONDS, DOWN_ASK_SECONDS + INTERVAL_SECONDS, DOWN_ASK_SECONDS + 2 * INTERVAL_SECONDS):
        result = _tick(seam, state, now=T0 + offset, port_open=False, drive_present=False)
        state = result.state
    assert len(seam.raises) == 1
    assert seam.raises[0]["question"] == "Power-cycle the Time Capsule?"
    assert seam.mapped == 0
    assert state.ask_raised is True
    assert state.ask_task_id == "t_ask"


def test_recovery_withdraws_ask_remaps_and_logs_one_drop():
    seam = _Seam()
    state = WatchState()
    down = _tick(seam, state, now=T0, port_open=False, drive_present=False)
    asked = _tick(seam, down.state, now=T0 + DOWN_ASK_SECONDS, port_open=False, drive_present=False)
    end = T0 + DOWN_ASK_SECONDS + INTERVAL_SECONDS
    back = _tick(seam, asked.state, now=end, port_open=True, drive_present=True)
    assert seam.withdraws == ["t_ask"]
    assert seam.mapped == 1
    drops = [line for line in back.log_lines if line.startswith("drop ")]
    assert drops == [format_drop_line(T0, end)]
    assert "2026-11-04 13:40:00 CT" in drops[0]
    assert "2026-11-04 14:00:00 CT" in drops[0]
    assert "length=20m" in drops[0]
    assert back.state.drop_started_at is None
    assert back.state.ask_raised is False


def test_recovery_comments_a_blocked_t_card_once_and_skips_the_rest():
    seam = _Seam(blocked=[
        {"id": "t_need", "title": "Copy the archive", "body": "", "reason": "T: missing"},
        {"id": "t_other", "title": "Pick a color", "body": "Waiting on a spec", "reason": "needs a decision"},
        {"id": "t_ask", "title": "Power-cycle the Time Capsule?", "body": "", "reason": "one-tap"},
    ])
    state = WatchState(drop_started_at=T0, ask_raised=True, ask_task_id="t_ask")
    first = _tick(seam, state, now=T0 + 20 * 60, port_open=True, drive_present=False)
    assert [item[0] for item in seam.comments] == ["t_need"]
    assert "answering again" in seam.comments[0][1]
    again = _tick(seam, first.state, now=T0 + 25 * 60, port_open=True, drive_present=True)
    assert len(seam.comments) == 1
    assert again.state.drop_started_at is None


def test_second_recovery_of_the_same_drop_does_not_comment_again():
    seam = _Seam(blocked=[{"id": "t_need", "title": "T: missing", "body": "", "reason": "T: missing"}])
    state = WatchState(drop_started_at=T0, ask_raised=True, ask_task_id="t_ask", commented_ids=["t_need"])
    _tick(seam, state, now=T0 + 20 * 60, port_open=True, drive_present=True)
    assert seam.comments == []


def test_unrelated_text_is_not_a_timecaps_block():
    assert blocked_on_timecaps("Pick a color", "Waiting on a spec", "needs a decision") is False
    assert blocked_on_timecaps("Note: not a drive", "at: noon", "Not: ready") is False
    assert blocked_on_timecaps("Copy", "", "T: missing") is True
    assert blocked_on_timecaps("Time Capsule share", "", "") is True


def test_ask_fits_cliff_notes():
    from hermes_cli.kanban_db_gm_asks import validate_gm_ask

    ask = validate_gm_ask(POWER_CYCLE_ASK)
    assert ask["question"] == "Power-cycle the Time Capsule?"
    assert ask["pick"] == "yes"


def test_intervals_match_the_card():
    assert INTERVAL_SECONDS == 300
    assert DOWN_ASK_SECONDS == 15 * 60


def test_mapper_command_is_the_existing_session_launch():
    from hermes_cli.timecaps_watch import MAPPER_LAUNCH_COMMAND

    assert "Start-Process" in MAPPER_LAUNCH_COMMAND
    assert "explorer.exe" in MAPPER_LAUNCH_COMMAND
    assert "map-timecaps-T.vbs" in MAPPER_LAUNCH_COMMAND
    assert "schtasks" not in MAPPER_LAUNCH_COMMAND


def test_run_once_remembers_one_ask_across_fresh_calls(tmp_path):
    seam = _Seam()
    run_timecaps_watch_once(
        now=T0,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "log.txt",
        port_open=False,
        drive_present=False,
        launch_mapper=seam.launch_mapper,
        raise_ask=seam.raise_ask,
        withdraw_ask=seam.withdraw_ask,
        list_blocked=seam.list_blocked,
        comment=seam.comment,
    )
    run_timecaps_watch_once(
        now=T0 + DOWN_ASK_SECONDS,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "log.txt",
        port_open=False,
        drive_present=False,
        launch_mapper=seam.launch_mapper,
        raise_ask=seam.raise_ask,
        withdraw_ask=seam.withdraw_ask,
        list_blocked=seam.list_blocked,
        comment=seam.comment,
    )
    run_timecaps_watch_once(
        now=T0 + DOWN_ASK_SECONDS + INTERVAL_SECONDS,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "log.txt",
        port_open=False,
        drive_present=False,
        launch_mapper=seam.launch_mapper,
        raise_ask=seam.raise_ask,
        withdraw_ask=seam.withdraw_ask,
        list_blocked=seam.list_blocked,
        comment=seam.comment,
    )
    assert len(seam.raises) == 1
    assert seam.mapped == 0


def test_run_once_refuses_the_real_share_under_pytest():
    with pytest.raises(RuntimeError, match="real share"):
        run_timecaps_watch_once()


def test_withdraw_closes_the_one_tap_and_does_not_leave_it_needing_the_gm(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_cli.kanban_db_gm_asks import get_open_gm_ask, record_gm_ask, withdraw_gm_ask

    kb.init_db()
    conn = connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Power-cycle the Time Capsule?",
            assignee=None,
            initial_status="blocked",
            workspace_kind="scratch",
            completion_contract="local-only",
        )
        record_gm_ask(
            conn,
            task_id,
            POWER_CYCLE_ASK,
            asked_by="timecaps-watch",
            force_park=True,
        )
        assert get_open_gm_ask(conn, task_id) is not None
        assert withdraw_gm_ask(conn, task_id) is True
        assert get_open_gm_ask(conn, task_id) is None
        landed = kb.get_task(conn, task_id)
        assert landed is not None
        assert landed.status == "scheduled"
        assert landed.block_kind is None
        assert landed.assignee is None
    finally:
        conn.close()


def test_module_does_not_touch_the_forbidden_surfaces():
    from pathlib import Path

    text = Path(__file__).resolve().parents[2].joinpath("hermes_cli", "timecaps_watch.py").read_text(encoding="utf-8")
    for banned in ("SMB1", "ForceCold", "schtasks", "Register-ScheduledTask", "192.168."):
        assert banned not in text


def test_watcher_is_wired_into_the_gateway():
    from pathlib import Path

    startup = Path(__file__).resolve().parents[2].joinpath("gateway", "run_startup.py").read_text(encoding="utf-8")
    assert "_timecaps_watch_watcher" in startup
