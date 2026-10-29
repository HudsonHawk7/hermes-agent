"""Work watch service — poll loop orchestration (local judge, local store)."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from plugins.work_watch import auth, judge, reader, store
from plugins.work_watch.constants import (
    ACTIVE_HOUR_END_CT,
    ACTIVE_HOUR_START_CT,
    CLOUD_JUDGE_ENV,
    POLL_ACTIVE_MINUTES,
    POLL_OFFHOURS_MINUTES,
    STATUS_NEEDS_SIGN_IN,
    STATUS_OFF,
)

logger = logging.getLogger(__name__)
CT = ZoneInfo("America/Chicago")

# Process-local reader state
_reader_stop_reason: Optional[str] = None
_scanning: bool = False


def cloud_judge_enabled() -> bool:
    import os

    raw = (os.environ.get(CLOUD_JUDGE_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def poll_interval_minutes(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(CT)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CT)
    else:
        now = now.astimezone(CT)
    if ACTIVE_HOUR_START_CT <= now.hour < ACTIVE_HOUR_END_CT:
        return POLL_ACTIVE_MINUTES
    return POLL_OFFHOURS_MINUTES


def is_scanning() -> bool:
    return _scanning


def reader_stop_reason() -> Optional[str]:
    return _reader_stop_reason


def clear_reader_stop() -> None:
    global _reader_stop_reason
    _reader_stop_reason = None


def run_once(
    *,
    db_path: Optional[str] = None,
    http_get=None,
    token_provider=None,
) -> dict[str, Any]:
    """One poll pass. AC-N1: zero Graph when unsigned. AC-N9: stop on auth fail."""
    global _scanning, _reader_stop_reason

    if not auth.reader_may_call_graph():
        return {
            "ok": True,
            "status": STATUS_OFF,
            "graph_calls": 0,
            "flags_added": 0,
            "dropped": 0,
            "candidates": 0,
            "skipped": True,
            "reason": "no_refresh_token",
            "scanning": False,
        }

    if _reader_stop_reason:
        return {
            "ok": False,
            "status": STATUS_NEEDS_SIGN_IN,
            "graph_calls": 0,
            "error": _reader_stop_reason,
            "error_plain": "Sign in again — the reader stopped after a refused refresh.",
            "scanning": False,
        }

    _scanning = True
    try:
        with store.open_store(db_path) as conn:
            delta = store.get_meta(conn, "mail_delta_link")
            try:
                result = reader.fetch_candidates(
                    http_get=http_get,
                    token_provider=token_provider,
                    mail_delta_link=delta,
                )
            except reader.ReaderStopped as exc:
                _reader_stop_reason = str(exc)
                auth.set_access_token_memory(None)
                return {
                    "ok": False,
                    "status": STATUS_NEEDS_SIGN_IN,
                    "graph_calls": 0,
                    "error": _reader_stop_reason,
                    "error_plain": "Sign in again — the reader stopped after a refused refresh.",
                    "scanning": False,
                }

            if result.get("skipped"):
                return {
                    "ok": True,
                    "status": STATUS_OFF,
                    "graph_calls": 0,
                    "skipped": True,
                    "scanning": False,
                }

            if result.get("mail_delta_link"):
                store.set_meta(conn, "mail_delta_link", str(result["mail_delta_link"]))

            flags_added = 0
            dropped = 0
            cloud = cloud_judge_enabled()
            for cand in result.get("candidates") or []:
                j = judge.classify(cand, cloud_enabled=cloud)
                out = judge.apply_to_store(conn, cand, j)
                if out is None:
                    dropped += 1
                else:
                    flags_added += 1

            n = len(result.get("candidates") or [])
            store.record_poll(conn, item_count=n)
            summary = store.board_summary(conn)
            return {
                "ok": True,
                "status": "signed_in",
                "graph_calls": int(result.get("graph_calls") or 0),
                "candidates": n,
                "flags_added": flags_added,
                "dropped": dropped,
                "board_summary": summary,  # counts only
                "scanning": False,
                "last_check": store.last_check(conn),
            }
    finally:
        _scanning = False


def settings_snapshot(db_path: Optional[str] = None) -> dict[str, Any]:
    """Compose Settings + last-check for the UI (no tokens, no message text)."""
    identity = None
    try:
        import os

        identity = (os.environ.get("GRAPH_WATCH_IDENTITY_MASKED") or "").strip() or None
    except Exception:
        identity = None

    err_code = err_plain = None
    if _reader_stop_reason:
        err_code = "AUTH"
        err_plain = "Sign in again — the reader stopped after a refused refresh."

    base = auth.current_status(identity_hint=identity, last_error_code=err_code, last_error_plain=err_plain)
    last = {"last_check_at": None, "last_check_count": 0, "message_text": None}
    try:
        with store.open_store(db_path) as conn:
            last = store.last_check(conn)
            summary = store.board_summary(conn)
    except Exception:
        summary = {"total": 0, "tier_1": 0, "tier_2": 0, "summary": "quiet"}

    base["last_check"] = last
    base["board_summary"] = summary
    base["scanning"] = _scanning
    base["reader_stopped"] = _reader_stop_reason
    base["poll_interval_minutes"] = poll_interval_minutes()
    return base
