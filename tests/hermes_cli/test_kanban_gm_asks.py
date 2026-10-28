"""Structured GM asks + one-tap answers (Approvals 1/2).

Covers validation, ask→answer, send-back, undo gates, double-answer,
worker kanban_block requiring ask, cross-board /needs-you (incl. legacy),
and FastAPI endpoints against a scratch HERMES_HOME.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_gm_asks as gm
from tools import kanban_tools as kt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Clear any process-level board pin left by other tests.
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home: Path) -> TestClient:
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_gm_asks_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _running_task(conn, title: str = "t", assignee: str = "worker") -> str:
    tid = kb.create_task(conn, title=title, assignee=assignee)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer=assignee)
    assert claimed is not None
    return tid


def _valid_ask(**overrides) -> dict:
    base = {
        "question": "Rebase onto develop?",
        "context": "PR is behind mainline. CI will re-run after.",
        "options": [
            {"key": "yes", "label": "Yes", "effect": "BB-8 rebases it and proves it"},
            {"key": "no", "label": "No", "effect": "the card closes"},
        ],
        "pick": "yes",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Validation table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutator, match",
    [
        (lambda a: None, "requires an 'ask' object"),
        (lambda a: "not-a-dict", "must be an object"),
        (lambda a: {**a, "question": ""}, "question is required"),
        (lambda a: {**a, "context": ""}, "context is required"),
        (lambda a: {**a, "options": []}, "2-4 items"),
        (lambda a: {**a, "options": a["options"][:1]}, "2-4 items"),
        (
            lambda a: {
                **a,
                "options": a["options"] + [
                    {"key": f"k{i}", "label": f"L{i}", "effect": f"E{i}"} for i in range(3)
                ],
            },
            "2-4 items",
        ),
        (
            lambda a: {
                **a,
                "options": [
                    {"key": "yes", "label": "Yes", "effect": "ok"},
                    {"key": "yes", "label": "Dup", "effect": "no"},
                ],
            },
            "unique",
        ),
        (
            lambda a: {
                **a,
                "options": [
                    {"key": "yes", "label": "Yes", "effect": ""},
                    {"key": "no", "label": "No", "effect": "x"},
                ],
            },
            "effect is required",
        ),
        (lambda a: {**a, "pick": "missing"}, "one of the option keys"),
    ],
)
def test_validate_gm_ask_table(mutator, match: str) -> None:
    raw = mutator(_valid_ask()) if mutator is not None else None
    if mutator is None:
        raw = None
    with pytest.raises(gm.GmAskError, match=match):
        gm.validate_gm_ask(raw)


def test_validate_gm_ask_happy() -> None:
    out = gm.validate_gm_ask(_valid_ask())
    assert out["question"] == "Rebase onto develop?"
    assert out["pick"] == "yes"
    assert len(out["options"]) == 2
    assert out["options"][0]["effect"]


# ---------------------------------------------------------------------------
# Ask → answer path
# ---------------------------------------------------------------------------


def test_ask_answer_ready_one_decision_and_comment(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        result = gm.record_gm_ask(
            conn, tid, _valid_ask(), asked_by="architect", reason="need GM call",
        )
        assert result["status"] == "blocked"
        assert result["block_kind"] == "needs_input"
        open_ask = gm.get_open_gm_ask(conn, tid)
        assert open_ask is not None
        assert open_ask[0] == result["ask_event_id"]

        answered = gm.answer_gm_ask(
            conn, tid, key="yes", by="gm", via="cli", note="go",
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"
        assert answered["decision"]["key"] == "yes"
        assert answered["decision"]["ask_event_id"] == result["ask_event_id"]

        decisions = [e for e in kb.list_events(conn, tid) if e.kind == "gm_decision"]
        assert len(decisions) == 1
        comments = kb.list_comments(conn, tid)
        bodies = [c.body for c in comments]
        assert any(b.startswith("GM decision: Yes - BB-8 rebases it and proves it") for b in bodies)
        assert gm.get_open_gm_ask(conn, tid) is None


def test_send_back_with_note(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        gm.record_gm_ask(conn, tid, _valid_ask(), asked_by="hera")
        out = gm.answer_gm_ask(
            conn, tid, sent_back=True, note="need more screenshots", by="gm", via="button",
        )
        assert out["decision"]["sent_back"] is True
        assert kb.get_task(conn, tid).status == "ready"
        bodies = [c.body for c in kb.list_comments(conn, tid)]
        assert any(b == "GM sent it back: need more screenshots" for b in bodies)


def test_double_answer_refused(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        gm.record_gm_ask(conn, tid, _valid_ask(), asked_by="hera")
        gm.answer_gm_ask(conn, tid, key="no", by="gm", via="cli")
        with pytest.raises(gm.GmAskError, match="no open GM ask"):
            gm.answer_gm_ask(conn, tid, key="yes", by="gm", via="cli")
        decisions = [e for e in kb.list_events(conn, tid) if e.kind == "gm_decision"]
        assert len(decisions) == 1


def test_undo_allowed_then_refused_after_run(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, assignee="worker")
        gm.record_gm_ask(conn, tid, _valid_ask(), asked_by="hera")
        gm.answer_gm_ask(conn, tid, key="yes", by="gm", via="cli")
        # Undo while still ready — allowed.
        undone = gm.undo_gm_decision(conn, tid, by="gm")
        assert undone["status"] == "blocked"
        assert gm.get_open_gm_ask(conn, tid) is not None

        # Answer again, then start a run — undo must refuse.
        gm.answer_gm_ask(conn, tid, key="yes", by="gm", via="cli")
        claimed = kb.claim_task(conn, tid, claimer="worker")
        assert claimed is not None
        with pytest.raises(gm.GmAskError, match="already picked up by"):
            gm.undo_gm_decision(conn, tid, by="gm")


# ---------------------------------------------------------------------------
# Worker tool
# ---------------------------------------------------------------------------


def test_worker_block_needs_input_requires_ask(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        run_id = kb.get_task(conn, tid).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_PROFILE", "worker")

    # Missing ask → corrective tool error, nothing crashes.
    out = json.loads(kt._handle_block({
        "reason": "need a call",
        "kind": "needs_input",
    }))
    assert "error" in out or out.get("ok") is False or "ask" in json.dumps(out).lower()
    err_text = json.dumps(out).lower()
    assert "ask" in err_text

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        # Still running — block refused before state change when ask invalid.
        assert task.status == "running"
        assert gm.get_open_gm_ask(conn, tid) is None

    # Valid ask lands blocked + gm_ask event.
    out2 = json.loads(kt._handle_block({
        "reason": "need a call",
        "kind": "needs_input",
        "ask": _valid_ask(),
    }))
    assert out2.get("ok") is True or out2.get("status") == "blocked" or "blocked" in json.dumps(out2)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert gm.get_open_gm_ask(conn, tid) is not None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_ask_and_answer(kanban_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="cli-ask", assignee="architect")
        # todo card — force_park path
    args = argparse.Namespace(
        task_id=tid,
        question="Ship the mockup?",
        context="GM asked for one-tap. Two options only.",
        options=[
            "yes|Yes|land it on force/carry",
            "no|No|park and revisit",
        ],
        pick="yes",
        reason=None,
        asked_by="architect",
        json=False,
    )
    assert kanban_cli._cmd_ask(args) == 0
    out = capsys.readouterr().out
    assert tid in out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        assert gm.get_open_gm_ask(conn, tid) is not None

    ans = argparse.Namespace(
        task_id=tid, key="yes", send_back=False, undo=False, note=None, by="gm", json=False,
    )
    assert kanban_cli._cmd_answer(ans) == 0
    assert "GM decision" in capsys.readouterr().out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# Cross-board /needs-you + HTTP
# ---------------------------------------------------------------------------


def test_needs_you_cross_board_and_legacy(kanban_home: Path, client: TestClient) -> None:
    # Default board: structured ask
    with kbc.connect_closing() as conn:
        t1 = _running_task(conn, title="structured")
        gm.record_gm_ask(conn, t1, _valid_ask(), asked_by="hera")
        # Legacy needs_input with free-text block only
        t_legacy = _running_task(conn, title="legacy")
        assert kb.block_task(conn, t_legacy, reason="old free-text wait", kind="needs_input")

    # Second board: another structured ask
    kb.create_board("alpha", name="Alpha Board")
    with kbc.connect_closing(board="alpha") as conn:
        t2 = kb.create_task(conn, title="on-alpha", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (t2,))
        assert kb.claim_task(conn, t2, claimer="worker") is not None
        gm.record_gm_ask(
            conn, t2,
            _valid_ask(question="Alpha pick?", context="Board two."),
            asked_by="architect",
        )

    items = gm.list_needs_you()
    ids = {it["task_id"] for it in items}
    assert t1 in ids and t2 in ids and t_legacy in ids
    by_id = {it["task_id"]: it for it in items}
    assert by_id[t1]["ask"] is not None
    assert by_id[t1]["board"] == "default"
    assert by_id[t2]["board"] == "alpha"
    assert by_id[t2]["board_name"]
    assert by_id[t_legacy]["ask"] is None
    assert by_id[t_legacy]["legacy_reason"] == "old free-text wait"

    # HTTP surface
    resp = client.get("/api/plugins/kanban/needs-you")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] >= 3
    http_ids = {it["task_id"] for it in body["items"]}
    assert {t1, t2, t_legacy} <= http_ids

    # Answer via HTTP
    r2 = client.post(
        f"/api/plugins/kanban/tasks/{t1}/answer",
        json={"key": "yes", "by": "gm"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, t1).status == "ready"
        assert gm.get_open_gm_ask(conn, t1) is None

    # Double answer via HTTP → 400
    r3 = client.post(
        f"/api/plugins/kanban/tasks/{t1}/answer",
        json={"key": "no", "by": "gm"},
    )
    assert r3.status_code == 400

    # Undo via HTTP (no run started)
    r4 = client.post(f"/api/plugins/kanban/tasks/{t1}/undo")
    assert r4.status_code == 200, r4.text
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, t1).status == "blocked"
        assert gm.get_open_gm_ask(conn, t1) is not None


def test_answer_endpoint_send_back(kanban_home: Path, client: TestClient) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, title="send-back-http")
        gm.record_gm_ask(conn, tid, _valid_ask(), asked_by="hera")
    r = client.post(
        f"/api/plugins/kanban/tasks/{tid}/answer",
        json={"sent_back": True, "note": "try again with screenshots", "by": "gm"},
    )
    assert r.status_code == 200, r.text
    assert "sent it back" in r.json()["comment"].lower()
