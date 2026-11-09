"""GM-gated models. These fail on a tree that does not enforce models.gm_gated.

The key is never defaulted. A matching card is not dispatched, and a matching
profile model is skipped, unless the GM answered Yes on a hermes kanban ask.
An agent cannot approve its own card.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_gm_asks as gm
from hermes_cli.fallback_config import get_fallback_chain
from hermes_cli.gm_gated_models import (
    RULE,
    SETTING,
    card_has_gm_approval,
    current_patterns,
)
from hermes_cli.moa_config import resolve_moa_preset

GATED = "claude-fable-5-1"
PATTERNS = ["claude-fable-*", "*/claude-fable-*"]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.gm_gated_models.current_patterns",
        lambda config=None: PATTERNS,
    )
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)


def _pin(conn, task_id: str) -> None:
    conn.execute(
        "UPDATE tasks SET model_override = ?, provider_override = ? WHERE id = ?",
        (GATED, "copilot", task_id),
    )
    conn.commit()


def test_unset_key_is_not_a_gate():
    """A missing or non-list key matches nothing. This card never invents a pattern."""
    assert current_patterns({}) == []
    assert current_patterns({"models": {"gm_gated": None}}) == []
    assert current_patterns({"models": {"gm_gated": "claude-fable-*"}}) == []


def test_gated_card_is_not_dispatched_until_the_gm_answers(
    kanban_home, all_assignees_spawnable, gated,
):
    spawned = []
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="probe card", assignee="probe")
        _pin(conn, task_id)
        first = kbd.dispatch_once(conn, spawn_fn=lambda _task, _ws: spawned.append(_task) or 1)
        task = kb.get_task(conn, task_id)
        open_ask = gm.get_open_gm_ask(conn, task_id)
        assert not any(item[0] == task_id for item in first.spawned)
        assert spawned == []
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert open_ask is not None
        payload = open_ask[1]
        assert payload["question"] == f"Use {GATED} for this card?"
        assert {opt["key"] for opt in payload["options"]} == {"yes", "no"}
        assert SETTING in payload["context"]
        reason = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert reason is not None and RULE in (reason["payload"] or "")
        assert card_has_gm_approval(conn, task_id, GATED) is False

        answered = gm.answer_gm_ask(conn, task_id, key="yes", by="gm", via="cli")
        assert answered["decision"]["by"] == "gm"
        assert answered["decision"]["model"] == GATED
        assert card_has_gm_approval(conn, task_id, GATED) is True
        second = kbd.dispatch_once(conn, spawn_fn=lambda _task, _ws: spawned.append(_task) or 1)

    assert any(item[0] == task_id for item in second.spawned)
    assert [item.id for item in spawned] == [task_id]
    assert spawned[0].model_override == GATED


def test_ungated_card_still_dispatches(kanban_home, all_assignees_spawnable, gated):
    spawned = []
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="plain card", assignee="probe")
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, _ws: spawned.append(task) or 1)
    assert any(item[0] == task_id for item in result.spawned)
    assert [item.id for item in spawned] == [task_id]


def test_profile_load_skips_gated_models(kanban_home, gated, caplog):
    from hermes_cli.gm_gated_models import apply_profile_model_gate

    config = {
        "model": {"default": GATED, "provider": "copilot"},
        "fallback_providers": [
            {"provider": "copilot", "model": GATED},
            {"provider": "xai", "model": "grok-4.7"},
        ],
        "auxiliary": {"title": {"provider": "copilot", "model": GATED}},
        "models": {"gm_gated": PATTERNS},
    }
    with caplog.at_level(logging.WARNING, logger="hermes_cli.gm_gated_models"):
        loaded = apply_profile_model_gate(config)
    assert loaded["model"]["default"] == "grok-4.7"
    assert loaded["model"]["provider"] == "xai"
    assert loaded["auxiliary"]["title"]["model"] == ""
    assert SETTING in caplog.text
    assert RULE in caplog.text
    with kbc.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ?",
            (f"gm-gate:profile:{GATED}",),
        ).fetchall()
    assert rows


def test_agent_cannot_approve_its_own_card(kanban_home, all_assignees_spawnable, gated, monkeypatch):
    spawned = []
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="probe wants fable", assignee="probe")
        assert kb.set_model_override(conn, task_id, GATED, provider="copilot")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert card_has_gm_approval(conn, task_id, GATED) is False

        with pytest.raises(gm.GmAskError, match="cannot approve its own card"):
            gm.answer_gm_ask(conn, task_id, key="yes", by="probe", via="cli")

        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_PROFILE", "probe")
        with pytest.raises(gm.GmAskError, match="cannot approve its own card"):
            gm.answer_gm_ask(conn, task_id, key="yes", by="gm", via="cli")

        kb._append_event(conn, task_id, "metadata", {"allow_gated_model": True, "by": "probe"})
        assert card_has_gm_approval(conn, task_id, GATED) is False
        result = kbd.dispatch_once(conn, spawn_fn=lambda _task, _ws: spawned.append(_task) or 1)

    assert spawned == []
    assert not any(item[0] == task_id for item in result.spawned)


def test_gated_moa_reference_is_skipped_with_the_log_line(gated, caplog):
    cfg = {
        "default_preset": "gate-test",
        "presets": {
            "gate-test": {
                "enabled": True,
                "reference_models": [
                    {"provider": "copilot", "model": GATED, "enabled": True},
                    {"provider": "xai", "model": "grok-4.7", "enabled": True},
                ],
                "aggregator": {"provider": "copilot", "model": GATED},
            }
        },
    }
    with caplog.at_level(logging.WARNING, logger="hermes_cli.gm_gated_models"):
        resolved = resolve_moa_preset(cfg, "gate-test")
    models = [slot["model"] for slot in resolved["reference_models"]]
    assert GATED not in models
    assert "grok-4.7" in models
    assert resolved["aggregator"]["model"] == "grok-4.7"
    text = caplog.text
    assert SETTING in text
    assert GATED in text
    assert RULE in text


def test_gated_fallback_entry_is_skipped(gated, caplog):
    config = {
        "fallback_providers": [
            {"provider": "copilot", "model": GATED},
            {"provider": "xai", "model": "grok-4.7"},
        ]
    }
    with caplog.at_level(logging.WARNING, logger="hermes_cli.gm_gated_models"):
        chain = get_fallback_chain(config)
    models = [entry["model"] for entry in chain]
    assert GATED not in models
    assert models == ["grok-4.7"]
    text = caplog.text
    assert SETTING in text
    assert "fallback_providers" in text
    assert RULE in text
