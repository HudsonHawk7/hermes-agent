"""Chat comes first: due agent jobs wait while this profile is in a live chat or call."""
from __future__ import annotations

from datetime import timedelta
import sqlite3
from types import SimpleNamespace

import pytest

import cron.jobs as J
import cron.scheduler as S
import cron.scheduler_interactive_hold as hold
from cron import scheduler_authority
from cron.scheduler_interactive_hold import WAITING_CHAT_DETAIL


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(J, "HERMES_DIR", home)
    monkeypatch.setattr(J, "CRON_DIR", home / "cron")
    monkeypatch.setattr(J, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(J, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(S, "_hermes_home", home)
    monkeypatch.setattr(S, "_sweep_mcp_orphans", lambda: None)
    S._running_job_ids.clear()
    return home


def _make_due(job_id: str) -> str:
    stored = J.load_jobs()
    slot = (J._hermes_now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    next(row for row in stored if row["id"] == job_id)["next_run_at"] = slot
    J.save_jobs(stored)
    return slot


def _stub_process(monkeypatch, fired):
    monkeypatch.setattr(
        scheduler_authority, "reconcile_pending",
        lambda *, allow_connect=True: None,
    )
    # Headless ticks also run the gateway gate; pass jobs through so this
    # file measures the chatting hold, not "no gateway".
    monkeypatch.setattr(
        "cron.scheduler_gateway_gate.refuse_agent_jobs_without_gateway",
        lambda jobs: jobs,
    )
    monkeypatch.setattr(
        S, "_process_due_job",
        lambda job, *a, **k: fired.append(job["id"]) or True,
    )


def _write_state(path, *, sessions=(), messages=(), leases=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            last_activity_at REAL,
            profile_name TEXT,
            archived INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            timestamp REAL
        );
        CREATE TABLE session_turn_leases (
            conversation_id TEXT PRIMARY KEY,
            owner_pid INTEGER,
            created_monotonic REAL,
            heartbeat_monotonic REAL
        );
        """
    )
    conn.executemany(
        "INSERT INTO sessions (id, source, last_activity_at, profile_name, archived) "
        "VALUES (?, ?, ?, ?, ?)",
        sessions,
    )
    conn.executemany(
        "INSERT INTO messages (id, session_id, role, timestamp) VALUES (?, ?, ?, ?)",
        messages,
    )
    conn.executemany(
        "INSERT INTO session_turn_leases (conversation_id, owner_pid, created_monotonic, heartbeat_monotonic) "
        "VALUES (?, ?, ?, ?)",
        leases,
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        (True, None),
        (False, None),
        ("nope", None),
        (-1, None),
        (0, 0),
        ("0", 0),
        (5, 5),
        ("15", 15),
        (15.9, 15),
    ],
)
def test_parse_pause_minutes_null_is_off(raw, expected):
    assert hold.parse_pause_minutes(raw) == expected


def test_schema_names_the_setting_and_null_means_off():
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.web_server_config import CONFIG_SCHEMA

    assert DEFAULT_CONFIG["cron"]["pause_while_chatting_minutes"] is None
    field = CONFIG_SCHEMA["cron.pause_while_chatting_minutes"]
    assert field["type"] == "number"
    assert field.get("nullMeansOff") is True
    blob = (field.get("description") or "").lower()
    assert "pause while chatting" in blob
    assert "null" in blob and "off" in blob


def test_null_setting_never_holds_even_when_chat_is_live(cron_home, monkeypatch):
    fired = []
    _stub_process(monkeypatch, fired)
    monkeypatch.setattr(hold, "pause_while_chatting_minutes", lambda: None)
    monkeypatch.setattr(hold, "profile_is_live_interactive", lambda **k: True)
    job = J.create_job(prompt="hello", schedule="every 1h", name="agent", deliver="local")
    _make_due(job["id"])

    ran = S.tick(verbose=False, headless=True)

    assert ran == 1
    assert fired == [job["id"]]
    stored = next(row for row in J.load_jobs() if row["id"] == job["id"])
    assert stored.get("last_fire_error") in (None, {})


def test_tick_holds_due_agent_job_while_live_then_fires_after_quiet(cron_home, monkeypatch, caplog):
    fired = []
    _stub_process(monkeypatch, fired)
    monkeypatch.setattr(hold, "pause_while_chatting_minutes", lambda: 5)
    live = {"on": True}
    monkeypatch.setattr(hold, "profile_is_live_interactive", lambda **k: live["on"])
    job = J.create_job(prompt="hello", schedule="every 1h", name="agent", deliver="local")
    due_at = _make_due(job["id"])

    with caplog.at_level("WARNING", logger="cron.scheduler_interactive_hold"):
        ran = S.tick(verbose=False, headless=True)

    assert ran == 0 and fired == []
    stored = next(row for row in J.load_jobs() if row["id"] == job["id"])
    assert stored["last_run_at"] is None
    assert stored["next_run_at"] == due_at
    assert stored["last_fire_error"]["detail"] == WAITING_CHAT_DETAIL
    assert "pending_slot" not in stored
    holds = [r for r in caplog.records if WAITING_CHAT_DETAIL in r.getMessage()]
    assert len(holds) == 1

    live["on"] = False
    ran = S.tick(verbose=False, headless=True)
    assert ran == 1
    assert fired == [job["id"]]


def test_no_agent_jobs_still_fire_while_chatting(cron_home, monkeypatch):
    fired = []
    _stub_process(monkeypatch, fired)
    monkeypatch.setattr(hold, "pause_while_chatting_minutes", lambda: 5)
    monkeypatch.setattr(hold, "profile_is_live_interactive", lambda **k: True)
    script = cron_home / "scripts" / "fire.sh"
    script.write_text("#!/bin/sh\necho fired\n", encoding="utf-8")
    job = J.create_job(
        prompt=None, schedule="every 1h", name="script", script="fire.sh",
        no_agent=True, deliver="local",
    )
    _make_due(job["id"])

    assert S.tick(verbose=False, headless=True) == 1
    assert fired == [job["id"]]


def test_in_process_turn_is_live_for_this_profile_only(monkeypatch):
    class _Runner:
        def __init__(self, sessions):
            self._sessions = sessions

        def _sessions_map(self):
            return self._sessions

    live_state = SimpleNamespace(turn=SimpleNamespace(agent=object()))
    idle_state = SimpleNamespace(turn=SimpleNamespace(agent=None))
    monkeypatch.setattr(hold, "_voice_turn_open", lambda: False)
    monkeypatch.setattr(hold, "_durable_turn_lease", lambda: False)
    monkeypatch.setattr(hold, "_recent_user_message", lambda *a: False)

    monkeypatch.setattr(hold, "_current_profile_name", lambda: "solara")
    monkeypatch.setattr(
        hold, "_lookup_gateway_runner",
        lambda: _Runner({"agent:solara:telegram:dm:1": live_state}),
    )
    assert hold.profile_is_live_interactive(quiet_minutes=0) is True

    monkeypatch.setattr(
        hold, "_lookup_gateway_runner",
        lambda: _Runner({"agent:main:telegram:dm:1": live_state}),
    )
    assert hold.profile_is_live_interactive(quiet_minutes=0) is False

    monkeypatch.setattr(
        hold, "_lookup_gateway_runner",
        lambda: _Runner({"agent:solara:cron:job:1": live_state}),
    )
    assert hold.profile_is_live_interactive(quiet_minutes=0) is False

    monkeypatch.setattr(
        hold, "_lookup_gateway_runner",
        lambda: _Runner({"agent:solara:telegram:dm:1": idle_state}),
    )
    assert hold.profile_is_live_interactive(quiet_minutes=0) is False


def test_quiet_window_uses_recent_user_message_not_cron(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    now = 1_700_000_000.0
    _write_state(
        db,
        sessions=[
            ("chat-1", "telegram", now, "probe", 0),
            ("cron-1", "cron", now, "probe", 0),
        ],
        messages=[
            (1, "chat-1", "user", now - 60),
            (2, "cron-1", "user", now - 10),
        ],
    )
    monkeypatch.setattr(hold, "_state_db_path", lambda: db)
    monkeypatch.setattr(hold, "_lookup_gateway_runner", lambda: None)
    monkeypatch.setattr(hold, "_voice_turn_open", lambda: False)

    assert hold.profile_is_live_interactive(quiet_minutes=5, now=now) is True
    assert hold.profile_is_live_interactive(quiet_minutes=0, now=now) is False
    assert hold.profile_is_live_interactive(quiet_minutes=5, now=now + 400) is False


def test_durable_lease_holds_interactive_not_cron(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _write_state(
        db,
        sessions=[("chat-1", "local", 1.0, "probe", 0), ("cron-1", "cron", 1.0, "probe", 0)],
        leases=[("cron-1", 1, 1.0, 1.0)],
    )
    monkeypatch.setattr(hold, "_state_db_path", lambda: db)
    monkeypatch.setattr(hold, "_lookup_gateway_runner", lambda: None)
    monkeypatch.setattr(hold, "_voice_turn_open", lambda: False)
    assert hold.profile_is_live_interactive(quiet_minutes=0) is False

    _write_state(
        tmp_path / "state2.db",
        sessions=[("chat-1", "local", 1.0, "probe", 0)],
        leases=[("chat-1", 1, 1.0, 1.0)],
    )
    monkeypatch.setattr(hold, "_state_db_path", lambda: tmp_path / "state2.db")
    assert hold.profile_is_live_interactive(quiet_minutes=0) is True
