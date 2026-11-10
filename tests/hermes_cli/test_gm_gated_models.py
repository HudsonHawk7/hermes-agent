"""GM-gated models. These fail on a tree that does not enforce models.gm_gated.

The key is never defaulted. A matching card is not dispatched, and a matching
profile model is skipped, unless the GM answered Yes on a hermes kanban ask.
An agent cannot approve its own card.
"""

from __future__ import annotations

import inspect
import logging
import shutil
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
    dispatch_refuses_gated,
    model_is_gated,
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


def _other_ask():
    return {
        "question": "Ship the other change?",
        "context": "This is not the model gate.",
        "options": [
            {"key": "yes", "label": "Yes", "effect": "ships the other change"},
            {"key": "no", "label": "No", "effect": "leaves it"},
        ],
        "pick": "no",
    }


def test_yes_on_a_different_ask_does_not_unlock_the_gated_model(
    kanban_home, all_assignees_spawnable, gated,
):
    from hermes_cli.gm_gated_models import dispatch_refuses_gated

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="probe card", assignee="probe")
        _pin(conn, task_id)
        kbd.dispatch_once(conn, spawn_fn=lambda _task, _ws: 1)
        gate = gm.get_open_gm_ask(conn, task_id)
        assert gate is not None
        assert GATED in gate[1]["question"]
        gm.answer_gm_ask(conn, task_id, key="no", by="gm", via="cli")
        gm.record_gm_ask(
            conn, task_id, _other_ask(), asked_by="probe", reason="other change",
        )
        answered = gm.answer_gm_ask(conn, task_id, key="yes", by="gm", via="cli")
        assert answered["decision"]["model"] is None
        assert card_has_gm_approval(conn, task_id, GATED) is False
        assert dispatch_refuses_gated(conn, task_id, park=False) is True


def test_yes_missing_the_model_does_not_unlock(
    kanban_home, all_assignees_spawnable, gated,
):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="probe card", assignee="probe")
        _pin(conn, task_id)
        kbd.dispatch_once(conn, spawn_fn=lambda _task, _ws: 1)
        gate = gm.get_open_gm_ask(conn, task_id)
        assert gate is not None
        kb._append_event(conn, task_id, "gm_decision", {
            "key": "yes",
            "by": "gm",
            "via": "cli",
            "ask_event_id": gate[0],
        })
        assert card_has_gm_approval(conn, task_id, GATED) is False


def test_profile_gm_yes_keeps_the_model(kanban_home, gated):
    from hermes_cli.gm_gated_models import apply_profile_model_gate

    def fresh():
        return {
            "model": {"default": GATED, "provider": "copilot"},
            "fallback_providers": [
                {"provider": "xai", "model": "grok-4.7"},
            ],
            "models": {"gm_gated": PATTERNS},
        }

    first = apply_profile_model_gate(fresh())
    assert first["model"]["default"] == "grok-4.7"
    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ?",
            (f"gm-gate:profile:{GATED}",),
        ).fetchone()
        assert row is not None
        answered = gm.answer_gm_ask(conn, row["id"], key="yes", by="gm", via="cli")
        assert answered["decision"]["model"] == GATED
    second = apply_profile_model_gate(fresh())
    assert second["model"]["default"] == GATED


def test_thrown_task_read_does_not_dispatch(
    kanban_home, all_assignees_spawnable, gated, monkeypatch,
):
    from hermes_cli.gm_gated_models import dispatch_refuses_gated

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="probe card", assignee="probe")
        _pin(conn, task_id)
        real = kb.get_task

        def boom(conn, tid, *args, **kwargs):
            for frame in inspect.stack():
                if frame.function == "dispatch_refuses_gated":
                    raise RuntimeError("board read failed")
            return real(conn, tid)

        monkeypatch.setattr(kb, "get_task", boom)
        assert dispatch_refuses_gated(conn, task_id, park=False) is True
        spawned = []
        result = kbd.dispatch_once(
            conn, spawn_fn=lambda task, _ws: spawned.append(task) or 1,
        )
    assert spawned == []
    assert not any(item[0] == task_id for item in result.spawned)


def test_fallback_skip_throw_does_not_keep_the_gated_chain(gated, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("gate down")

    monkeypatch.setattr("hermes_cli.gm_gated_models.drop_gated_fallback", boom)
    chain = get_fallback_chain({
        "fallback_providers": [
            {"provider": "copilot", "model": GATED},
            {"provider": "xai", "model": "grok-4.7"},
        ],
    })
    assert all(entry.get("model") != GATED for entry in chain)


def test_moa_skip_throw_does_not_keep_the_gated_preset(gated, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("gate down")

    monkeypatch.setattr("hermes_cli.gm_gated_models.filter_moa_preset", boom)
    resolved = resolve_moa_preset({
        "default_preset": "gate-test",
        "presets": {
            "gate-test": {
                "enabled": True,
                "reference_models": [
                    {"provider": "copilot", "model": GATED, "enabled": True},
                ],
                "aggregator": {"provider": "copilot", "model": GATED},
            },
        },
    })
    models = [slot.get("model") for slot in resolved.get("reference_models") or []]
    assert GATED not in models
    assert (resolved.get("aggregator") or {}).get("model") != GATED


def test_aux_skip_throw_does_not_keep_the_model(gated, monkeypatch):
    from agent.auxiliary_task_config import (
        _get_auxiliary_task_config,
        session_auxiliary_scope,
    )

    def boom(*_args, **_kwargs):
        raise RuntimeError("gate down")

    monkeypatch.setattr("hermes_cli.gm_gated_models.drop_gated_aux_model", boom)
    with session_auxiliary_scope({"title": {"provider": "copilot", "model": GATED}}):
        loaded = _get_auxiliary_task_config("title")
    assert loaded.get("model") != GATED


def test_profile_load_throw_does_not_keep_the_gated_default(gated, monkeypatch):
    from hermes_cli.config import _apply_gm_gate

    def boom(_config):
        raise RuntimeError("gate down")

    monkeypatch.setattr(
        "hermes_cli.gm_gated_models.apply_profile_model_gate", boom,
    )
    loaded = _apply_gm_gate({
        "model": {"default": GATED, "provider": "copilot"},
        "fallback_providers": [{"provider": "copilot", "model": GATED}],
        "models": {"gm_gated": PATTERNS},
    })
    assert loaded["model"]["default"] != GATED
    assert all(
        item.get("model") != GATED
        for item in (loaded.get("fallback_providers") or [])
    )


def test_unreadable_gate_list_does_not_launch_or_keep_the_chain(
    kanban_home, all_assignees_spawnable, monkeypatch, caplog,
):
    """A thrown config load is not an empty gate list.

    On e012f81b6dd, load_config_readonly raising made model_is_gated return
    False, so dispatch launched the pin and the skip wrappers kept the chain.
    """
    def boom():
        raise OSError("config unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.gm_gated_models"):
        try:
            reported = model_is_gated(GATED)
        except Exception:
            reported = True
        assert reported is True

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="probe card", assignee="probe")
        _pin(conn, task_id)
        assert dispatch_refuses_gated(conn, task_id, park=False) is True
        spawned = []
        result = kbd.dispatch_once(
            conn, spawn_fn=lambda task, _ws: spawned.append(task) or 1,
        )
    assert spawned == []
    assert not any(item[0] == task_id for item in result.spawned)

    chain = get_fallback_chain({
        "fallback_providers": [
            {"provider": "copilot", "model": GATED},
            {"provider": "xai", "model": "grok-4.7"},
        ],
    })
    assert all(entry.get("model") != GATED for entry in chain)

    resolved = resolve_moa_preset({
        "default_preset": "gate-test",
        "presets": {
            "gate-test": {
                "enabled": True,
                "reference_models": [
                    {"provider": "copilot", "model": GATED, "enabled": True},
                ],
                "aggregator": {"provider": "copilot", "model": GATED},
            },
        },
    })
    models = [slot.get("model") for slot in resolved.get("reference_models") or []]
    assert GATED not in models
    assert (resolved.get("aggregator") or {}).get("model") != GATED

    from agent.auxiliary_task_config import (
        _get_auxiliary_task_config,
        session_auxiliary_scope,
    )

    with session_auxiliary_scope({"title": {"provider": "copilot", "model": GATED}}):
        loaded = _get_auxiliary_task_config("title")
    assert loaded.get("model") != GATED

    from hermes_cli.config import _apply_gm_gate

    profile = _apply_gm_gate({
        "model": {"default": GATED, "provider": "copilot"},
        "fallback_providers": [{"provider": "copilot", "model": GATED}],
    })
    assert profile["model"]["default"] != GATED
    assert all(
        item.get("model") != GATED
        for item in (profile.get("fallback_providers") or [])
    )
    assert SETTING in caplog.text
    assert RULE in caplog.text


def _clear_config_memory() -> None:
    from hermes_cli import config as cfg

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    raw = getattr(cfg, "_RAW_CONFIG_CACHE", None)
    if isinstance(raw, dict):
        raw.clear()


def _drop_good_backups(config_path: Path) -> None:
    backups = config_path.parent / "backups"
    if backups.exists():
        shutil.rmtree(backups)
    for leftover in config_path.parent.glob("config.yaml.good.*"):
        leftover.unlink(missing_ok=True)
    for leftover in config_path.parent.glob("config.yaml.bak*"):
        leftover.unlink(missing_ok=True)


def test_broken_config_file_is_not_an_empty_gate_list(
    kanban_home, all_assignees_spawnable, monkeypatch, caplog,
):
    """A settings file that cannot be read is not an empty gate list.

    load_config_readonly does not raise. It returns FailedConfigRead of the
    defaults when there is no last-known-good list. On 2413d2b6dc6 that
    object has no models.gm_gated, so model_is_gated returned False and
    dispatch would spawn the pin. A missing file stays list-off.
    """
    from hermes_cli.config import get_config_path, load_config_readonly
    from hermes_cli.config_read_errors import FailedConfigRead
    from hermes_cli.gm_gated_models import GateListUnreadable, patterns_from_config

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        "hermes_cli.gm_gated_models._unreadable_logged", False, raising=False,
    )
    config_path = get_config_path()
    _clear_config_memory()

    assert not config_path.exists()
    assert model_is_gated(GATED) is False

    config_path.write_text(
        "models:\n  gm_gated:\n    - claude-fable-*\n    - '*/claude-fable-*'\n",
        encoding="utf-8",
    )
    _clear_config_memory()
    assert model_is_gated(GATED) is True

    _clear_config_memory()
    _drop_good_backups(config_path)
    config_path.write_text("models: [\n", encoding="utf-8")
    _clear_config_memory()

    loaded = load_config_readonly()
    assert type(loaded) is FailedConfigRead
    assert patterns_from_config(loaded) == []

    with caplog.at_level(logging.WARNING, logger="hermes_cli.gm_gated_models"):
        try:
            reported = model_is_gated(GATED)
        except GateListUnreadable:
            reported = True
        assert reported is True

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="probe card", assignee="probe")
        _pin(conn, task_id)
        assert dispatch_refuses_gated(conn, task_id, park=False) is True
        spawned = []
        result = kbd.dispatch_once(
            conn, spawn_fn=lambda task, _ws: spawned.append(task) or 1,
        )
    assert spawned == []
    assert not any(item[0] == task_id for item in result.spawned)

    chain = get_fallback_chain({
        "fallback_providers": [
            {"provider": "copilot", "model": GATED},
            {"provider": "xai", "model": "grok-4.7"},
        ],
    })
    assert all(entry.get("model") != GATED for entry in chain)

    resolved = resolve_moa_preset({
        "default_preset": "gate-test",
        "presets": {
            "gate-test": {
                "enabled": True,
                "reference_models": [
                    {"provider": "copilot", "model": GATED, "enabled": True},
                ],
                "aggregator": {"provider": "copilot", "model": GATED},
            },
        },
    })
    models = [slot.get("model") for slot in resolved.get("reference_models") or []]
    assert GATED not in models
    assert (resolved.get("aggregator") or {}).get("model") != GATED

    from agent.auxiliary_task_config import (
        _get_auxiliary_task_config,
        session_auxiliary_scope,
    )

    with session_auxiliary_scope({"title": {"provider": "copilot", "model": GATED}}):
        loaded_aux = _get_auxiliary_task_config("title")
    assert loaded_aux.get("model") != GATED

    from hermes_cli.config import _apply_gm_gate

    profile = _apply_gm_gate({
        "model": {"default": GATED, "provider": "copilot"},
        "fallback_providers": [{"provider": "copilot", "model": GATED}],
    })
    assert profile["model"]["default"] != GATED
    assert all(
        item.get("model") != GATED
        for item in (profile.get("fallback_providers") or [])
    )
    assert SETTING in caplog.text
    assert RULE in caplog.text


_WITH_LIST = """\
model:
  default: claude-fable-5-1
  provider: copilot
fallback_providers:
  - provider: copilot
    model: claude-fable-5-1
  - provider: xai
    model: grok-4.7
moa:
  default_preset: gate-test
  presets:
    gate-test:
      enabled: true
      reference_models:
        - provider: copilot
          model: claude-fable-5-1
          enabled: true
      aggregator:
        provider: copilot
        model: claude-fable-5-1
auxiliary:
  title:
    provider: copilot
    model: claude-fable-5-1
models:
  gm_gated:
    - claude-fable-*
    - '*/claude-fable-*'
"""

_NO_LIST = """\
model:
  default: claude-fable-5-1
  provider: copilot
fallback_providers:
  - provider: copilot
    model: claude-fable-5-1
moa:
  default_preset: gate-test
  presets:
    gate-test:
      enabled: true
      reference_models:
        - provider: copilot
          model: claude-fable-5-1
          enabled: true
      aggregator:
        provider: copilot
        model: claude-fable-5-1
auxiliary:
  title:
    provider: copilot
    model: claude-fable-5-1
"""


def _served_gated(loaded) -> list[str]:
    """Names of the gated model still sitting in a loaded config."""
    found: list[str] = []
    model = loaded.get("model") if isinstance(loaded, dict) else None
    if isinstance(model, dict) and model.get("default") == GATED:
        found.append("model.default")
    for item in loaded.get("fallback_providers") or []:
        if isinstance(item, dict) and item.get("model") == GATED:
            found.append("fallback_providers")
    presets = ((loaded.get("moa") or {}).get("presets") or {})
    for preset in presets.values():
        if not isinstance(preset, dict):
            continue
        for slot in preset.get("reference_models") or []:
            if isinstance(slot, dict) and slot.get("model") == GATED:
                found.append("moa.reference_models")
        agg = preset.get("aggregator") or {}
        if isinstance(agg, dict) and agg.get("model") == GATED:
            found.append("moa.aggregator")
    for block in (loaded.get("auxiliary") or {}).values():
        if isinstance(block, dict) and block.get("model") == GATED:
            found.append("auxiliary")
    return found


def _good_backup(config_path: Path):
    from hermes_cli.config_backups import list_config_backups

    copies = list_config_backups(config_path, "good")
    assert copies, "the good backup must still be on disk"
    return copies[0]


def test_broken_file_with_good_backup_does_not_serve_gated_model(
    kanban_home, monkeypatch,
):
    """A good backup is the raw file. The gate does not rewrite it.

    On 7cf41a5aad5 a broken file returned that backup without filtering, and
    the cache served the same copy. This test keeps the backup. It does not
    delete it, and it does not make the loader raise.
    """
    from hermes_cli.config import get_config_path, load_config, load_config_readonly
    from hermes_cli.config_read_errors import FailedConfigRead

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    config_path = get_config_path()
    _clear_config_memory()
    config_path.write_text(_WITH_LIST, encoding="utf-8")

    readable = load_config_readonly()
    assert readable.get("model", {}).get("default") != GATED
    backup = _good_backup(config_path)
    raw = backup.read_bytes()
    assert GATED.encode() in raw

    _clear_config_memory()
    config_path.write_text("models: [\n", encoding="utf-8")
    _clear_config_memory()

    loaded = load_config_readonly()
    assert type(loaded) is FailedConfigRead
    assert backup.read_bytes() == raw
    assert _served_gated(loaded) == [], (
        "broken file served the gated model from the good backup"
    )
    cached = load_config_readonly()
    copied = load_config()
    assert _served_gated(cached) == []
    assert _served_gated(copied) == []
    assert model_is_gated(GATED) is True


def test_backup_without_gate_list_does_not_keep_gated_chain(
    kanban_home, monkeypatch, caplog,
):
    """A backup that has the model and no list cannot prove the model is allowed.

    On 7cf41a5aad5 the real loader raised GateListUnreadable and still
    returned claude-fable-5-1. This test keeps that backup.
    """
    from hermes_cli.config import get_config_path, load_config_readonly
    from hermes_cli.config_read_errors import FailedConfigRead
    from hermes_cli.gm_gated_models import GateListUnreadable

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        "hermes_cli.gm_gated_models._unreadable_logged", False, raising=False,
    )
    config_path = get_config_path()
    _clear_config_memory()
    config_path.write_text(_NO_LIST, encoding="utf-8")

    readable = load_config_readonly()
    assert readable.get("model", {}).get("default") == GATED
    backup = _good_backup(config_path)
    raw = backup.read_bytes()
    assert GATED.encode() in raw
    assert b"gm_gated" not in raw

    _clear_config_memory()
    config_path.write_text("models: [\n", encoding="utf-8")
    _clear_config_memory()

    with caplog.at_level(logging.WARNING):
        loaded = load_config_readonly()
    assert type(loaded) is FailedConfigRead
    assert backup.read_bytes() == raw
    assert _served_gated(loaded) == [], (
        "backup with no list still kept the gated model"
    )
    assert _served_gated(load_config_readonly()) == []
    with caplog.at_level(logging.WARNING, logger="hermes_cli.gm_gated_models"):
        with pytest.raises(GateListUnreadable):
            model_is_gated(GATED)
    assert SETTING in caplog.text
    assert RULE in caplog.text


def test_plugin_save_then_broken_file_does_not_serve_gated_model(
    kanban_home, monkeypatch,
):
    """A plugin settings save must not put the raw gated model back in memory.

    save_config stores the merged dict before the gate runs. On a2b1e1ab500
    the fallback used that copy whenever it was already in memory, and a
    broken file served claude-fable-5-1. This test uses the real loader and
    save_plugin_setting. It does not clear the in-memory cache, and it does
    not delete the good backup.
    """
    from hermes_cli import config as cfg
    from hermes_cli.config import get_config_path, load_config, load_config_readonly
    from hermes_cli.config_read_errors import FailedConfigRead
    from hermes_cli.plugins_state import save_plugin_setting

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    config_path = get_config_path()
    _clear_config_memory()
    config_path.write_text(_WITH_LIST, encoding="utf-8")

    readable = load_config_readonly()
    assert readable.get("model", {}).get("default") != GATED
    backup = _good_backup(config_path)
    raw = backup.read_bytes()
    assert GATED.encode() in raw

    save_plugin_setting("probe-plugin", ("note",), "x")
    path_key = str(config_path)
    assert path_key in cfg._LAST_EXPANDED_CONFIG_BY_PATH, (
        "the in-memory last-known-good must still be set"
    )

    config_path.write_text("models: [\n", encoding="utf-8")
    assert path_key in cfg._LAST_EXPANDED_CONFIG_BY_PATH, (
        "breaking the file must not clear the in-memory cache"
    )
    assert backup.read_bytes() == raw

    loaded = load_config_readonly()
    assert type(loaded) is FailedConfigRead
    assert _served_gated(loaded) == [], (
        "plugin save then a broken file served the gated model from memory"
    )
    assert _served_gated(load_config_readonly()) == []
    assert _served_gated(load_config()) == []


def _clear_effective_memory() -> None:
    from hermes_cli import config_effective

    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()


def test_gateway_loader_does_not_serve_gated_model(kanban_home, caplog, monkeypatch):
    """The load that starts a profile must skip a gated model.default.

    load_config already skipped it. This uses the real gateway loader, not
    load_config.
    """
    from gateway.run import _load_gateway_config, _resolve_gateway_model
    from hermes_cli.config import get_config_path
    from hermes_cli.config_effective import load_user_config_effective

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    config_path = get_config_path()
    _clear_config_memory()
    _clear_effective_memory()
    config_path.write_text(_WITH_LIST, encoding="utf-8")

    with caplog.at_level("WARNING"):
        effective = load_user_config_effective(config_path)
        gateway = _load_gateway_config(config_path)
    assert effective["model"]["default"] == "grok-4.7", (
        "load_user_config_effective served the gated model"
    )
    assert _served_gated(effective) == []
    assert _served_gated(gateway) == []
    assert _resolve_gateway_model(gateway) == "grok-4.7"
    raw = {
        "model": {"default": GATED, "provider": "copilot"},
        "fallback_providers": [{"provider": "xai", "model": "grok-4.7"}],
        "models": {"gm_gated": list(PATTERNS)},
    }
    assert _resolve_gateway_model(raw) == "grok-4.7", (
        "_resolve_gateway_model returned the gated profile model"
    )
    assert SETTING in caplog.text
    assert RULE in caplog.text
    assert GATED in caplog.text


def test_gateway_loader_broken_file_does_not_serve_gated_model(kanban_home, monkeypatch):
    """A backup or in-memory last-known-good on the gateway loader must not put the gated model back."""
    from hermes_cli import config as cfg
    from hermes_cli import config_effective
    from hermes_cli.config import get_config_path
    from hermes_cli.config_effective import load_user_config_effective

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    config_path = get_config_path()
    _clear_config_memory()
    _clear_effective_memory()
    config_path.write_text(_WITH_LIST, encoding="utf-8")

    first = load_user_config_effective(config_path)
    assert first["model"]["default"] == "grok-4.7"
    backup = _good_backup(config_path)
    raw = backup.read_bytes()
    assert GATED.encode() in raw
    path_key = str(config_path)
    assert path_key in config_effective._LAST_GOOD_USER_RAW

    config_path.write_text("models: [\n", encoding="utf-8")
    assert path_key in config_effective._LAST_GOOD_USER_RAW
    assert backup.read_bytes() == raw

    broken = load_user_config_effective(config_path)
    assert _served_gated(broken) == [], (
        "gateway loader served the gated model from in-memory last-known-good"
    )

    config_effective._LAST_GOOD_USER_RAW.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    cfg._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    fresh = load_user_config_effective(config_path)
    assert _served_gated(fresh) == [], (
        "gateway loader served the gated model from the disk backup"
    )


def test_gateway_loader_gm_yes_keeps_the_model(kanban_home, monkeypatch):
    """A GM Yes on the profile ask keeps the model on the gateway loader."""
    from hermes_cli.config import get_config_path
    from hermes_cli.config_effective import load_user_config_effective

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    config_path = get_config_path()
    _clear_config_memory()
    _clear_effective_memory()
    config_path.write_text(_WITH_LIST, encoding="utf-8")

    first = load_user_config_effective(config_path)
    assert first["model"]["default"] == "grok-4.7"
    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ?",
            (f"gm-gate:profile:{GATED}",),
        ).fetchone()
        assert row is not None
        answered = gm.answer_gm_ask(conn, row["id"], key="yes", by="gm", via="cli")
        assert answered["decision"]["model"] == GATED
    second = load_user_config_effective(config_path)
    assert second["model"]["default"] == GATED


def test_cli_start_load_does_not_serve_gated_model(kanban_home, caplog, monkeypatch):
    """The CLI start load must skip a gated model.default unless the GM answered Yes."""
    import cli
    from hermes_cli.cli_config_load import load_cli_config
    from hermes_cli.config import get_config_path

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(cli, "_hermes_home", kanban_home)
    config_path = get_config_path()
    _clear_config_memory()
    _clear_effective_memory()
    config_path.write_text(_WITH_LIST, encoding="utf-8")

    with caplog.at_level("WARNING"):
        loaded = load_cli_config()
    assert loaded["model"]["default"] == "grok-4.7", (
        "CLI start load served the gated model"
    )
    assert _served_gated(loaded) == []
    assert SETTING in caplog.text
    assert RULE in caplog.text
