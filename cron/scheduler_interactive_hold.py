"""Hold a profile's agent cron jobs while that profile is in a live chat or call.

NULL ``cron.pause_while_chatting_minutes`` means the feature is off (no auto-default).
When the setting is 0 or more, due *agent* jobs wait (not failed, due instant unmoved)
while any of these is true on this profile:

- a Desktop/gateway chat turn is in flight (``SessionState.turn.agent``, including the
  pending sentinel, or an unexpired ``session_turn_leases`` row)
- a voice/call turn has the mic Hub paused (``state()`` is idle with
  ``stop_reason == "paused"``; ``snapshot()`` still names the owner)
- (when N > 0) a user message landed in the last N minutes on an interactive session

The job list shows ``waiting: you're chatting with her``. no_agent scripts still fire.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

WAITING_CHAT_DETAIL = "waiting: you're chatting with her"

# Sessions that are this profile working, not the GM chatting or calling.
_NON_INTERACTIVE_SOURCES = frozenset({"cron", "kanban"})


def parse_pause_minutes(raw: Any) -> Optional[int]:
    """None/blank/invalid → off. 0 = live turn/call only. >0 = also a quiet window."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        logger.warning(
            "cron.pause_while_chatting_minutes=%r is not a minute count; treating as off",
            raw,
        )
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "cron.pause_while_chatting_minutes=%r is invalid; treating as off",
            raw,
        )
        return None
    if minutes < 0:
        logger.warning(
            "cron.pause_while_chatting_minutes=%r is negative; treating as off",
            raw,
        )
        return None
    return minutes


def pause_while_chatting_minutes() -> Optional[int]:
    """This profile's setting. None = off. Read from the ticked home's config."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        cron_cfg = cfg.get("cron") if isinstance(cfg, dict) else None
        raw = None if not isinstance(cron_cfg, dict) else cron_cfg.get("pause_while_chatting_minutes")
    except Exception:
        return None
    return parse_pause_minutes(raw)


def hold_agent_jobs_while_chatting(due_jobs: list) -> list:
    """Return the due jobs this tick may dispatch; agent jobs wait while chat/call is live."""
    minutes = pause_while_chatting_minutes()
    if minutes is None:
        return due_jobs
    agent_jobs = [job for job in due_jobs if not job.get("no_agent")]
    if not agent_jobs:
        return due_jobs
    if not profile_is_live_interactive(quiet_minutes=minutes):
        return due_jobs
    from cron.scheduler_gateway_gate import _note_fire_refused
    from hermes_constants import get_hermes_home

    ids = [str(job["id"]) for job in agent_jobs]
    _note_fire_refused(ids, WAITING_CHAT_DETAIL)
    home = get_hermes_home()
    logger.warning(
        "Cron tick for %s skipped %d agent job(s) (%s): %s",
        home, len(ids), ", ".join(ids), WAITING_CHAT_DETAIL,
    )
    return [job for job in due_jobs if job.get("no_agent")]


def profile_is_live_interactive(*, quiet_minutes: int, now: Optional[float] = None) -> bool:
    """True when this profile has a live turn, an open voice/call, or (N>0) a recent user message."""
    if _in_process_interactive_turn():
        return True
    if _voice_turn_open():
        return True
    if _durable_turn_lease():
        return True
    if quiet_minutes > 0 and _recent_user_message(quiet_minutes, now):
        return True
    return False


def _session_key_namespace(profile: Optional[str]) -> str:
    """Same rule as ``gateway.session._session_key_namespace`` (kept local to avoid importing gateway)."""
    if not profile or profile == "default":
        return "agent:main"
    return "agent:main~" if profile == "main" else f"agent:{profile}"


def _session_key_is_interactive(session_key: str) -> bool:
    parts = session_key.split(":")
    if len(parts) < 3:
        return True
    return parts[2].lower() not in _NON_INTERACTIVE_SOURCES


def _current_profile_name() -> Optional[str]:
    try:
        from hermes_cli.profiles import current_profile_name

        return current_profile_name()
    except Exception:
        return None


def _lookup_gateway_runner():
    """In-process GatewayRunner, or None. Lazy so a headless tick never imports gateway.run."""
    try:
        from gateway.run import _gateway_runner_ref

        return _gateway_runner_ref()
    except Exception:
        return None


def _in_process_interactive_turn() -> bool:
    runner = _lookup_gateway_runner()
    if runner is None:
        return False
    try:
        sessions = runner._sessions_map()
    except Exception:
        return False
    profile = _current_profile_name()
    ns = _session_key_namespace(None if profile in (None, "custom") else profile)
    for key, state in sessions.items():
        if not isinstance(key, str) or not key.startswith(ns + ":"):
            continue
        if not _session_key_is_interactive(key):
            continue
        try:
            # Real SessionState has no .agent; the running turn lives on turn.agent
            # (None = idle, object or _AGENT_PENDING_SENTINEL = in flight).
            agent = getattr(getattr(state, "turn", None), "agent", None)
        except Exception:
            continue
        if agent is not None:
            return True
    return False


def _voice_turn_open() -> bool:
    """Mic Hub paused for a voice/call turn, not mere wake-word listening.

    Do not call ``hub()`` — that would create a Hub in processes that never listened.
    Published ``state()`` is only armed/idle/stopped; a voice turn pauses to idle
    with ``stop_reason == "paused"`` and ``snapshot()`` still names the owner.
    """
    try:
        from tui_gateway import voice_hub as vh

        existing = getattr(vh, "_hub", None)
        if existing is None:
            return False
        published = existing.state()
        owner, _surface = existing.snapshot()
    except Exception:
        return False
    return owner is not None and published.get("stop_reason") == "paused"


def _state_db_path() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home

        path = get_hermes_home() / "state.db"
    except Exception:
        return None
    return path if path.is_file() else None


def _connect_state_ro(path: Path) -> Optional[sqlite3.Connection]:
    uri = path.resolve().as_posix()
    if uri.startswith("/") and not uri.startswith("//") and len(uri) > 1 and uri[1] != ":":
        uri_path = uri
    else:
        # Windows ``C:/...`` needs three slashes in a file URI.
        uri_path = uri if uri.startswith("/") else f"/{uri}"
    try:
        conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=0.2)
        conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(str(path), timeout=0.2)
            conn.execute("PRAGMA query_only = ON")
            return conn
        except sqlite3.Error:
            return None


def _durable_turn_lease() -> bool:
    path = _state_db_path()
    if path is None:
        return False
    conn = _connect_state_ro(path)
    if conn is None:
        return False
    now = time.time()
    try:
        rows = conn.execute(
            "SELECT l.conversation_id, s.source "
            "FROM session_turn_leases l "
            "LEFT JOIN sessions s ON s.id = l.conversation_id "
            "WHERE l.expires_at > ?",
            (now,),
        ).fetchall()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    for _cid, source in rows:
        if source is not None and str(source).strip().lower() in _NON_INTERACTIVE_SOURCES:
            continue
        return True
    return False


def _recent_user_message(quiet_minutes: int, now: Optional[float]) -> bool:
    path = _state_db_path()
    if path is None:
        return False
    conn = _connect_state_ro(path)
    if conn is None:
        return False
    cutoff = (time.time() if now is None else now) - (quiet_minutes * 60)
    placeholders = ",".join("?" for _ in _NON_INTERACTIVE_SOURCES)
    try:
        row = conn.execute(
            f"SELECT MAX(m.timestamp) FROM messages m "
            f"JOIN sessions s ON s.id = m.session_id "
            f"WHERE m.role = 'user' AND IFNULL(s.archived, 0) = 0 "
            f"AND lower(IFNULL(s.source, '')) NOT IN ({placeholders})",
            tuple(_NON_INTERACTIVE_SOURCES),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    latest = row[0] if row else None
    try:
        return latest is not None and float(latest) >= cutoff
    except (TypeError, ValueError):
        return False
