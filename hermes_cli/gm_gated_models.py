"""GM-gated models. ``models.gm_gated`` is named and never defaulted.

A model that matches a pattern in that list is not dispatched, and is skipped
when a profile loads it, unless the GM answered Yes on that gate ask for that
exact model. A later Yes on a different ask does not count. The answer is
recorded with the answering identity. An agent setting a flag, or answering
its own card, does not count. If the check cannot prove the model is allowed,
it is not launched and the gated chain is not kept.

The list is off when the key is missing, null, or not a list. This module
never invents a pattern. An unreadable list is not off: the model is not
launched, and a gated chain is not kept.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from fnmatch import fnmatchcase
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A nested config load (approval lookup opens the board) must not re-enter the gate.
_apply_depth = 0

SETTING = "models.gm_gated"
GATE_PURPOSE = "gm_gated_model"


def profile_gate_key(model: str) -> str:
    """One idempotency key for a profile approval of *model*.

    Filing and lookup must use this same key. A per-setting key never matches
    the ask that was filed, so a GM Yes would not keep the model.
    """
    return f"gm-gate:profile:{model}"


RULE = (
    "A gated model is not used unless the GM answered Yes on a hermes kanban "
    "ask for that model. An agent cannot approve its own card."
)
_GM_IDENTITIES = frozenset({"gm", "user", "default"})
_ASK_CONTEXT = (
    "models.gm_gated blocks this until the GM answers Yes. "
    "An agent cannot approve its own card."
)


def patterns_from_config(config: Any) -> list[str]:
    """Return ``models.gm_gated`` or an empty list. Never a built-in default."""
    if not isinstance(config, dict):
        return []
    models = config.get("models")
    if not isinstance(models, dict) or "gm_gated" not in models:
        return []
    raw = models.get("gm_gated")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


class GateListUnreadable(Exception):
    """The gate list could not be read. That is not an empty list."""


_inside_config_load = 0
_unreadable_logged = False


def _config_names_gate(config: Any) -> bool:
    return (
        isinstance(config, dict)
        and isinstance(config.get("models"), dict)
        and "gm_gated" in config["models"]
    )


def _warn_unreadable() -> None:
    """One loud line. An unreadable list is not unset."""
    global _unreadable_logged
    if _unreadable_logged:
        return
    _unreadable_logged = True
    logger.warning(
        "%s: could not read the gate list; not treating it as unset. %s",
        SETTING,
        RULE,
        exc_info=True,
    )


def _failed_read_without_list(config: Any) -> bool:
    """A failed read with no last-known-good list is not an empty list.

    A missing file is a normal dict. A file that is there and cannot be read
    comes back as FailedConfigRead of the defaults. That object has no
    models.gm_gated. A last-known-good that actually contains the list is
    usable and is not this case.
    """
    try:
        from hermes_cli.config_read_errors import FailedConfigRead
    except Exception:
        return False
    if not isinstance(config, FailedConfigRead):
        return False
    return not patterns_from_config(config)


def current_patterns(config: Any = None) -> list[str]:
    """Patterns from *config*, or from the loaded config when *config* is omitted.

    A thrown root read, or a returned FailedConfigRead with no last-known-good
    list, raises :class:`GateListUnreadable`. That is not an empty list, and
    callers must not treat it as "not gated". A missing file stays list-off.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except GateListUnreadable:
            raise
        except Exception as exc:
            _warn_unreadable()
            raise GateListUnreadable("could not read models.gm_gated") from exc
    if _failed_read_without_list(config):
        _warn_unreadable()
        raise GateListUnreadable(
            "could not read models.gm_gated; no last-known-good list"
        )
    return patterns_from_config(config)


def require_readable_list(config: Any = None) -> None:
    """Raise when the gate list cannot be read.

    A dict that names ``models.gm_gated`` is the list. A missing key is unset
    only when a root read succeeds, or when this call is already inside a
    config load (that dict is the load result; do not re-enter).
    """
    if _config_names_gate(config) or _apply_depth or _inside_config_load:
        return
    current_patterns(None)


def model_is_gated(
    model: Any,
    *,
    provider: Any = None,
    patterns: Optional[list[str]] = None,
    config: Any = None,
) -> bool:
    """True when *model* matches a configured gate pattern.

    Matches the full name, the leaf after ``/``, and ``provider/model`` when
    a provider is given. An empty pattern list (the unset key) matches nothing.
    """
    name = str(model or "").strip()
    if not name:
        return False
    pats = list(patterns) if patterns is not None else current_patterns(config)
    if not pats:
        return False
    candidates = [name]
    if "/" in name:
        candidates.append(name.rsplit("/", 1)[-1])
    prov = str(provider or "").strip()
    if prov and "/" not in name:
        candidates.append(f"{prov}/{name}")
    for pattern in pats:
        folded = str(pattern).strip().lower()
        if not folded:
            continue
        for candidate in candidates:
            if fnmatchcase(candidate.lower(), folded):
                return True
    return False


def emit_skip(where: str, models: list[str]) -> None:
    """One warning naming the setting, the skipped models, and the rule."""
    shown = [str(item).strip() for item in models if str(item).strip()]
    if not shown:
        return
    logger.warning(
        "%s: skipped %s %s. %s",
        SETTING,
        where,
        ", ".join(repr(item) for item in shown),
        RULE,
    )


def is_gm_identity(by: Any, assignee: Any = None) -> bool:
    """True only for a GM identity that is not the card's own assignee."""
    name = str(by or "").strip()
    if not name or name.lower() not in _GM_IDENTITIES:
        return False
    owner = str(assignee or "").strip()
    if owner and name.lower() == owner.lower():
        return False
    return True


def stamp_answering_identity(supplied: Any, *, via: str = "cli", gate: bool = False) -> str:
    """Keep a supplied GM identity unless a worker or agent CLI is claiming it.

    Dashboard answers (``via='button'``) keep the supplied identity so the GM's
    one-tap still records ``gm``. A worker process, or an agent profile on the
    CLI claiming ``gm``, is recorded as that profile instead.
    """
    name = str(supplied or "").strip() or "gm"
    if not gate:
        return name
    profile = (os.environ.get("HERMES_PROFILE") or "").strip()
    worker = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    agent_profile = bool(profile) and profile.lower() not in _GM_IDENTITIES
    claiming_gm = name.lower() in _GM_IDENTITIES
    if worker and (agent_profile or claiming_gm):
        return profile or "agent"
    if via == "cli" and agent_profile and claiming_gm:
        return profile
    return name


def _json_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def card_has_gm_approval(
    conn: sqlite3.Connection,
    task_id: str,
    model: str,
    assignee: Any = None,
) -> bool:
    """True when the gate ask for *model* has a GM Yes on that same ask.

    The decision must name this ask's event id and this exact model. A later
    Yes on a different ask does not count. A missing model does not count.
    A metadata flag, a self-answer, or a Yes from the assignee does not count.
    An undone decision does not count.
    """
    try:
        rows = conn.execute(
            "SELECT id, kind, payload FROM task_events "
            "WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
    except sqlite3.Error:
        logger.debug("gm_gated: could not read events for %s", task_id, exc_info=True)
        return False
    ask_id = None
    for row in rows:
        if row["kind"] != "gm_ask":
            continue
        payload = _json_payload(row["payload"])
        if payload.get("purpose") != GATE_PURPOSE:
            continue
        if str(payload.get("model") or "") != model:
            continue
        ask_id = row["id"]
    if ask_id is None:
        return False
    decision = None
    decision_id = None
    for row in rows:
        if row["kind"] != "gm_decision":
            continue
        payload = _json_payload(row["payload"])
        if str(payload.get("ask_event_id")) != str(ask_id):
            continue
        decision = payload
        decision_id = row["id"]
    if not decision or decision.get("undone") or decision.get("sent_back"):
        return False
    if str(decision.get("key") or "").strip().lower() != "yes":
        return False
    decided = decision.get("model")
    if not isinstance(decided, str) or decided != model:
        return False
    for row in rows:
        if decision_id is not None and row["id"] > decision_id and row["kind"] == "gm_decision_undone":
            return False
    return is_gm_identity(decision.get("by"), assignee)


def gate_question(model: str, *, scope: str = "card") -> str:
    """One-tap question. Stays inside the 90-character ask cap."""
    target = "this profile" if scope == "profile" else "this card"
    question = f"Use {model} for {target}?"
    if len(question) <= 90:
        return question
    room = 90 - len(f"Use  for {target}?")
    tail = model[-room:] if room > 0 else model[:40]
    return f"Use {tail} for {target}?"


def gate_ask(model: str, *, scope: str = "card", setting: str = "") -> dict[str, Any]:
    """Structured ask body. Extra fields are merged after validation."""
    return {
        "question": gate_question(model, scope=scope),
        "context": _ASK_CONTEXT,
        "options": [
            {"key": "yes", "label": "Yes", "effect": "Use this model."},
            {"key": "no", "label": "No", "effect": "Do not use this model."},
        ],
        "pick": None,
        "_extra": {
            "purpose": GATE_PURPOSE,
            "model": model,
            "scope": scope,
            "setting": setting,
        },
    }


def park_if_gated(
    conn: sqlite3.Connection,
    task_id: str,
    model: Any,
    *,
    assignee: Any = None,
    provider: Any = None,
    scope: str = "card",
    setting: str = "",
) -> bool:
    """File the one-tap ask and park the card when *model* is gated.

    Returns True when the card must not run on *model*. A ready card with no
    GM Yes is parked ``needs_input``. An already-open ask is left in place.
    A todo card is left for dispatch (parents still gate it). A running card
    is left alone; the next dispatch refuses the pin.
    """
    name = str(model or "").strip()
    if not name:
        return False
    try:
        gated = model_is_gated(name, provider=provider)
    except GateListUnreadable:
        gated = True
    if not gated:
        return False
    if card_has_gm_approval(conn, task_id, name, assignee):
        return False
    try:
        from hermes_cli import kanban_db as kb

        task = kb.get_task(conn, task_id)
    except Exception:
        logger.warning("%s: could not read %s to park %r", SETTING, task_id, name)
        return True
    if task is None or task.status in ("done", "archived"):
        return False
    try:
        from hermes_cli import kanban_db_gm_asks as gm

        ask = gate_ask(name, scope=scope, setting=setting)
        extra = ask.pop("_extra")
        gm.record_gm_ask(
            conn,
            task_id,
            ask,
            asked_by="gm-gate",
            reason=f"{SETTING}: {RULE} Model: {name}",
            force_park=True,
            payload_extra=extra,
        )
    except Exception as exc:
        message = str(exc)
        if "already has an open GM ask" in message:
            return True
        logger.warning("%s: could not file the ask for %s (%s)", SETTING, task_id, message)
        return True
    return True


def dispatch_refuses_gated(conn: sqlite3.Connection, task_id: str, *, park: bool = True) -> bool:
    """True when this card's model override is gated and not GM-approved."""
    try:
        from hermes_cli import kanban_db as kb

        task = kb.get_task(conn, task_id)
    except Exception:
        logger.warning(
            "%s: could not read %s at dispatch; not spawning", SETTING, task_id,
        )
        return True
    if task is None:
        return False
    model = str(getattr(task, "model_override", None) or "").strip()
    if not model:
        return False
    provider = getattr(task, "provider_override", None)
    try:
        gated = model_is_gated(model, provider=provider)
    except GateListUnreadable:
        gated = True
    if not gated:
        return False
    assignee = getattr(task, "assignee", None)
    if card_has_gm_approval(conn, task_id, model, assignee):
        return False
    if park:
        park_if_gated(
            conn, task_id, model, assignee=assignee, provider=provider,
        )
        emit_skip("kanban dispatch", [model])
    return True


def reject_non_gm_gate_answer(conn: sqlite3.Connection, task_id: str, by: str) -> None:
    """Raise when *by* is not a GM identity for this card's gate ask."""
    from hermes_cli.kanban_db_gm_asks import GmAskError
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    assignee = getattr(task, "assignee", None) if task else None
    if is_gm_identity(by, assignee):
        return
    who = str(by or "").strip() or "agent"
    raise GmAskError(
        f"{who} cannot approve a gated model. {SETTING}: {RULE}"
    )


def _entry_model(entry: Any) -> tuple[str, str]:
    if not isinstance(entry, dict):
        return "", ""
    return str(entry.get("model") or "").strip(), str(entry.get("provider") or "").strip()


def drop_gated_fallback(
    chain: list[dict[str, Any]],
    *,
    config: Any = None,
    log: bool = True,
) -> list[dict[str, Any]]:
    """Drop gated fallback entries. The next allowed entry stays in order."""
    require_readable_list(config)
    kept: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in chain:
        model, provider = _entry_model(entry)
        if model and model_is_gated(model, provider=provider, config=config):
            if not _profile_keeps(config, "fallback_providers", model):
                skipped.append(model)
                continue
        kept.append(entry)
    if log and skipped:
        emit_skip("fallback_providers", skipped)
    return kept


def filter_moa_preset(preset: dict[str, Any], *, config: Any = None, log: bool = True) -> dict[str, Any]:
    """Drop gated reference slots and a gated aggregator. Other slots stay."""
    if not isinstance(preset, dict):
        return preset
    require_readable_list(config)
    skipped: list[str] = []
    refs = preset.get("reference_models")
    if isinstance(refs, list):
        kept = []
        for slot in refs:
            model, provider = _entry_model(slot)
            if (
                model
                and model_is_gated(model, provider=provider, config=config)
                and not _profile_keeps(config, "moa.reference_models", model)
            ):
                skipped.append(model)
                continue
            kept.append(slot)
        preset["reference_models"] = kept
    aggregator = preset.get("aggregator")
    model, provider = _entry_model(aggregator)
    if (
        model
        and model_is_gated(model, provider=provider, config=config)
        and not _profile_keeps(config, "moa.aggregator", model)
    ):
        skipped.append(model)
        replacement: dict[str, Any] = {}
        for slot in preset.get("reference_models") or []:
            slot_model, slot_provider = _entry_model(slot)
            if slot_model and not model_is_gated(slot_model, provider=slot_provider, config=config):
                replacement = {"provider": slot_provider, "model": slot_model}
                break
        preset["aggregator"] = replacement
    if log and skipped:
        emit_skip("moa.reference_models", skipped)
    return preset


def drop_gated_aux_model(
    task: str,
    config: dict[str, Any],
    *,
    log: bool = True,
    root: Any = None,
) -> dict[str, Any]:
    """Clear a gated auxiliary model so the next allowed route is used.

    *config* is the task block. *root* is the loaded config when the caller
    already has it, so a backup filter does not re-read a broken file.
    """
    if not isinstance(config, dict):
        return config
    require_readable_list(root)
    model, provider = _entry_model(config)
    if not model or not model_is_gated(model, provider=provider, config=root):
        return config
    if _profile_keeps(None, f"auxiliary.{task}", model):
        return config
    if log:
        emit_skip(f"auxiliary.{task}", [model])
    cleared = dict(config)
    cleared["model"] = ""
    return cleared


def _profile_keeps(config: Any, setting: str, model: str) -> bool:
    """True when a GM Yes is already on file for this model.

    Filing and lookup share :func:`profile_gate_key`. A per-setting key never
    matches the ask that was filed. Fail closed: a missing board, a flag, or
    a re-entrant load does not keep the model.
    """
    del config, setting
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        key = profile_gate_key(model)
        with kbc.connect_closing() as conn:
            row = conn.execute(
                "SELECT id, assignee FROM tasks WHERE idempotency_key = ? "
                "AND status NOT IN ('archived') ORDER BY created_at DESC LIMIT 1",
                (key,),
            ).fetchone()
            if row is None:
                return False
            return card_has_gm_approval(conn, row["id"], model, row["assignee"])
    except Exception:
        logger.debug("gm_gated: profile approval lookup failed", exc_info=True)
        return False


def _next_allowed(config: dict[str, Any]) -> tuple[str, str]:
    chain = drop_gated_fallback(
        _fallback_entries(config), config=config, log=False,
    )
    if not chain:
        return "", ""
    model, provider = _entry_model(chain[0])
    return provider, model


def _fallback_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("fallback_providers") if isinstance(config, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def filter_served_backup(config: dict[str, Any]) -> dict[str, Any]:
    """Filter a raw-file backup before it is returned or cached.

    The backup is written before the gate runs, so it still names gated models.
    A backup that has the list is filtered with that list. A backup with no
    list cannot prove a model is allowed, so the profile, fallback, moa, and
    aux chains are not kept.
    """
    if not isinstance(config, dict):
        return config
    from hermes_cli.config import _drop_unproven_profile_models

    if not patterns_from_config(config):
        logger.warning(
            "%s: last-known-good backup has no gate list; not keeping an unproven model. %s",
            SETTING,
            RULE,
        )
        return _drop_unproven_profile_models(config)
    try:
        return apply_profile_model_gate(config)
    except Exception:
        logger.warning(
            "%s: could not filter a last-known-good backup; not keeping an unproven model. %s",
            SETTING,
            RULE,
            exc_info=True,
        )
        return _drop_unproven_profile_models(config)


def filter_cached_last_known_good(config: dict[str, Any]) -> dict[str, Any]:
    """Filter an in-memory last-known-good that names the gate list.

    ``save_config`` stores the canonicalized dict before the gate runs, so
    that copy can still name a gated model. Filter it before it is stored,
    returned, or cached again. A copy with no list is list-off and is left
    unchanged, so a YAML typo still serves the last loaded model.
    """
    if not isinstance(config, dict) or not patterns_from_config(config):
        return config
    return filter_served_backup(config)


def apply_profile_model_gate(config: dict[str, Any]) -> dict[str, Any]:
    """Skip gated profile models in a loaded config. The file on disk is not rewritten.

    One warning names every setting skipped in this load. The next allowed
    model in the fallback chain replaces a gated ``model.default``. A missing
    key is a no-op only when the list can be read. An unreadable list raises
    so the caller does not keep the gated model.
    """
    global _apply_depth
    if _apply_depth or not isinstance(config, dict):
        return config
    if not patterns_from_config(config):
        require_readable_list(config)
        return config
    _apply_depth += 1
    try:
        return _apply_profile_model_gate(config)
    finally:
        _apply_depth -= 1


def _apply_profile_model_gate(config: dict[str, Any]) -> dict[str, Any]:
    skipped: list[str] = []
    model_section = config.get("model")
    if isinstance(model_section, dict):
        default = str(model_section.get("default") or "").strip()
        provider = str(model_section.get("provider") or "").strip()
        if default and model_is_gated(default, provider=provider, config=config):
            if not _profile_keeps(config, "model.default", default):
                skipped.append(default)
                next_provider, next_model = _next_allowed(config)
                model_section["default"] = next_model
                if next_model and next_provider:
                    model_section["provider"] = next_provider
    raw_fallbacks = config.get("fallback_providers")
    if isinstance(raw_fallbacks, list):
        config["fallback_providers"] = drop_gated_fallback(
            [item for item in raw_fallbacks if isinstance(item, dict)],
            config=config,
            log=False,
        )
        for item in raw_fallbacks:
            model, provider = _entry_model(item)
            if model and model_is_gated(model, provider=provider, config=config):
                if model not in skipped and not _profile_keeps(config, "fallback_providers", model):
                    skipped.append(model)
    moa = config.get("moa")
    if isinstance(moa, dict):
        presets = moa.get("presets")
        if isinstance(presets, dict):
            for preset in presets.values():
                if not isinstance(preset, dict):
                    continue
                before = [
                    _entry_model(slot)[0]
                    for slot in (preset.get("reference_models") or [])
                ]
                filter_moa_preset(preset, config=config, log=False)
                after = {
                    _entry_model(slot)[0]
                    for slot in (preset.get("reference_models") or [])
                }
                for model in before:
                    if model and model not in after and model not in skipped:
                        skipped.append(model)
    auxiliary = config.get("auxiliary")
    if isinstance(auxiliary, dict):
        for task_name, block in list(auxiliary.items()):
            if not isinstance(block, dict):
                continue
            model = str(block.get("model") or "").strip()
            filtered = drop_gated_aux_model(
                str(task_name), block, log=False, root=config,
            )
            if model and not str(filtered.get("model") or "").strip():
                skipped.append(model)
                auxiliary[task_name] = filtered
    if skipped:
        emit_skip("profile load", skipped)
        _file_profile_asks(skipped)
    return config


def _file_profile_asks(models: list[str]) -> None:
    """Best-effort one-tap asks so a profile skip can be approved the same way."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        return
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        kb.init_db()
        with kbc.connect_closing() as conn:
            for model in models:
                key = profile_gate_key(model)
                existing = conn.execute(
                    "SELECT id FROM tasks WHERE idempotency_key = ? "
                    "AND status NOT IN ('archived') LIMIT 1",
                    (key,),
                ).fetchone()
                if existing is not None:
                    park_if_gated(
                        conn, existing["id"], model, scope="profile", setting="profile",
                    )
                    continue
                task_id = kb.create_task(
                    conn,
                    title=f"GM gate: {model}"[:120],
                    body=(
                        f"{SETTING} skipped {model} at profile load. "
                        f"{RULE}"
                    ),
                    assignee=None,
                    created_by="gm-gate",
                    idempotency_key=key,
                )
                park_if_gated(
                    conn, task_id, model, scope="profile", setting="profile",
                )
    except Exception:
        logger.debug("gm_gated: could not file a profile ask", exc_info=True)
