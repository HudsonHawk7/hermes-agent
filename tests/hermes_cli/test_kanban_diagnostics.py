"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_running_with_open_parents_fires_only_while_running():
    """A running card whose parent is not terminal is flagged; the same graph
    on a ready/todo card (the gate is holding it) and a done parent are not."""
    graph = {"parents": [{"id": "t_parent", "title": "p", "status": "todo"}], "children": []}
    diags = kd.compute_task_diagnostics(_task(status="running", started_at=100), [], [], graph=graph)
    assert [d.kind for d in diags] == ["running_with_open_parents"]
    assert diags[0].data["open_parents"] == [{"id": "t_parent", "status": "todo"}]
    assert "hermes kanban unlink t_parent t_demo00" in diags[0].actions[0].payload["command"]
    assert kd.compute_task_diagnostics(_task(status="todo"), [], [], graph=graph) == []
    done_graph = {"parents": [{"id": "t_parent", "title": "p", "status": "done"}], "children": []}
    assert kd.compute_task_diagnostics(_task(status="running"), [], [], graph=done_graph) == []


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48








# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True

# ---------------------------------------------------------------------------
# stranded_in_ready vs the per-profile cap: queued is not stranded
#
# The dispatcher serialises a profile (kanban.max_in_progress_per_profile), so a
# ``ready`` card whose own lane is already at its cap is waiting its turn, not
# stranded. The lane block rides in on the graph context every diagnostics
# caller already builds (``kb.task_graph_contexts``), so no signature changed.
# ---------------------------------------------------------------------------


def _lane(cap, running, assignee="demo", running_ids=None):
    """Graph context carrying the ``lane`` block ``task_graph_contexts`` emits."""
    return {
        "parents": [],
        "children": [],
        "lane": {
            "assignee": assignee,
            "cap": cap,
            "running": running,
            "running_ids": (
                running_ids if running_ids is not None
                else [f"t_run{i}" for i in range(running)]
            ),
        },
    }


def test_stranded_in_ready_silent_when_lane_is_at_its_cap():
    """Cap 1 with one running card: this card is queued behind its own lane, so
    age alone (4h here, critical band) must not make it an error."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 4 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now, graph=_lane(1, 1))
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_fires_when_lane_is_below_its_cap():
    """Cap 2 with one running card leaves a free slot: the age rule still fires."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now, graph=_lane(2, 1))
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"


def test_stranded_in_ready_fires_when_cap_is_unset():
    """No configured cap means no queue: behaviour is exactly as it was."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now, graph=_lane(None, 1))
    assert [d.kind for d in diags if d.kind == "stranded_in_ready"] == ["stranded_in_ready"]


def test_stranded_in_ready_fires_without_a_lane_block():
    """Older callers pass a graph with no ``lane`` key at all — unchanged."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 45 * 60)]
    no_lane = {"parents": [], "children": []}
    for graph in (no_lane, None):
        diags = kd.compute_task_diagnostics(task, events, [], now=now, graph=graph)
        assert [d.kind for d in diags if d.kind == "stranded_in_ready"] == [
            "stranded_in_ready"
        ]


def test_stranded_in_ready_ignores_a_lane_block_for_another_assignee():
    """A lane block that describes somebody else's lane must never silence this
    card — the block is keyed on the card's own assignee, and a mismatch is a
    caller bug, not a queue."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 45 * 60)]
    graph = _lane(1, 1, assignee="someone-else")
    diags = kd.compute_task_diagnostics(task, events, [], now=now, graph=graph)
    assert [d.kind for d in diags if d.kind == "stranded_in_ready"] == ["stranded_in_ready"]


def test_task_graph_contexts_reports_the_lane_cap_and_running_count(kanban_home):
    """``task_graph_contexts`` counts running rows per assignee — the same rows
    the dispatcher's per-profile cap counts — and puts the configured cap beside
    them."""
    with kbc.connect_closing() as conn:
        busy = kb.create_task(conn, title="busy", assignee="hera")
        queued = kb.create_task(conn, title="queued", assignee="hera")
        other = kb.create_task(conn, title="other-lane", assignee="sable")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running' WHERE id IN (?, ?)", (busy, other)
            )
        graphs = kb.task_graph_contexts(conn, [queued, other], per_profile_cap=1)
        assert graphs[queued]["lane"] == {
            "assignee": "hera", "cap": 1, "running": 1, "running_ids": [busy],
        }
        # A busy hera lane says nothing about sable's lane.
        assert graphs[other]["lane"]["running"] == 1
        assert graphs[other]["lane"]["running_ids"] == [other]


def test_at_cap_queue_still_surfaces_a_stale_owner_as_error():
    """The load-bearing half of the early return: silence at cap must never hide
    a dead lane. The queued card goes quiet, and the card holding the slot is
    still diagnosed at error+ by ``_rule_stale_running``."""
    now = 200_000
    expired = now - 3 * 3600  # 3x the 1h grace -> error band
    running = _task(
        id="t_run0", status="running", assignee="demo",
        claim_lock=f"{kb._host_prefix()}abc",
        claim_expires=expired, last_heartbeat_at=None,
        worker_pid=None, current_run_id=None,
    )
    queued = _task(id="t_queued", status="ready", assignee="demo", claim_lock=None)
    graph = _lane(1, 1, running_ids=["t_run0"])
    queued_events = [_event("created", ts=now - 4 * 3600)]

    queued_diags = kd.compute_task_diagnostics(queued, queued_events, [], now=now, graph=graph)
    assert [d for d in queued_diags if d.kind == "stranded_in_ready"] == []

    owner_diags = [d for d in kd.compute_task_diagnostics(running, [], [], now=now, graph=graph)
                   if d.kind == "stale_running"]
    assert len(owner_diags) == 1
    assert owner_diags[0].severity in ("error", "critical")


def test_lane_cap_falls_back_to_the_root_config(kanban_home, monkeypatch):
    """The cap is host-level: it lives in the root (pre-profile) config.yaml. A
    CLI running under a named profile sees no ``kanban`` block in its own config,
    so the reader returns None — the lane block must still carry the real cap,
    or the sweep's CLI gate would disagree with the gateway."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import config_effective
    import hermes_constants

    monkeypatch.setattr(kbd, "configured_max_in_progress_per_profile", lambda: None)
    monkeypatch.setattr(
        config_effective, "load_user_config_effective",
        lambda *a, **kw: {"kanban": {"max_in_progress_per_profile": 1}},
    )
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: Path("/nope"))
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="solo", assignee="hera")
        assert kb.task_graph_contexts(conn, [tid])[tid]["lane"]["cap"] == 1


def test_lane_cap_prefers_the_active_config_value(kanban_home, monkeypatch):
    """The dispatcher's own reader wins when it has an answer — the fallback is
    only for the profile-invoked case, never an override."""
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "configured_max_in_progress_per_profile", lambda: 3)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="solo", assignee="hera")
        assert kb.task_graph_contexts(conn, [tid])[tid]["lane"]["cap"] == 3
