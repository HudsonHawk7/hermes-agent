"""Work watch service — poll loop orchestration (local judge, local store)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from plugins.work_watch import auth, judge, reader, store
from plugins.work_watch.constants import (
    ACTIVE_HOUR_END_CT,
    ACTIVE_HOUR_START_CT,
    CLOUD_JUDGE_ENV,
    DIGEST_HOUR_CT,
    DIGEST_MINUTE_CT,
    POLL_ACTIVE_MINUTES,
    POLL_OFFHOURS_MINUTES,
    STATUS_NEEDS_SIGN_IN,
    STATUS_OFF,
    TIER_2,
)

logger = logging.getLogger(__name__)
CT = ZoneInfo("America/Chicago")

# Process-local reader state
_reader_stop_reason: Optional[str] = None
_scanning: bool = False

# Background poll scheduler (Probe defect 4 / r3 cadence)
_scheduler_stop = threading.Event()
_scheduler_thread: Optional[threading.Thread] = None
_scheduler_lock = threading.Lock()
# Injectable sleep + clock for tests
_sleep_fn: Callable[[float], None] = time.sleep
_now_fn: Callable[..., datetime] = datetime.now
# Last Graph poll monotonic marker (None = never polled this process)
_last_graph_poll_mono: Optional[float] = None
# Wall-clock next-due (lets tests advance CT without mono)
_next_graph_wall: Optional[datetime] = None
# Max wake so 08:07 digest is not missed by a long sleep (does NOT call Graph).
_DIGEST_WAKE_CAP_S = 60.0


def cloud_judge_enabled() -> bool:
    import os

    raw = (os.environ.get(CLOUD_JUDGE_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def poll_interval_minutes(now: Optional[datetime] = None) -> int:
    now = now or _now_ct()
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


def set_sleep_fn(fn: Optional[Callable[[float], None]]) -> None:
    """Test seam for the poll scheduler sleep."""
    global _sleep_fn
    _sleep_fn = fn or time.sleep


def set_now_fn(fn: Optional[Callable[..., datetime]]) -> None:
    """Test seam for CT clock (digest + cadence)."""
    global _now_fn
    _now_fn = fn or datetime.now


def reset_scheduler_poll_clock() -> None:
    """Clear last Graph-poll marker (tests + revoke restart)."""
    global _last_graph_poll_mono, _next_graph_wall
    _last_graph_poll_mono = None
    _next_graph_wall = None


def _now_ct() -> datetime:
    now = _now_fn(CT) if _now_fn is not datetime.now else datetime.now(CT)
    # Allow plain datetime.now() style fakes that ignore tz arg.
    if not isinstance(now, datetime):
        now = datetime.now(CT)
    if now.tzinfo is None:
        return now.replace(tzinfo=CT)
    return now.astimezone(CT)


def _mono() -> float:
    """Monotonic seconds for production cadence."""
    return time.monotonic()


def seconds_until_digest(now: Optional[datetime] = None) -> float:
    """Seconds until next 8:07 AM CT digest window (AC-P5)."""
    now = now or _now_ct()
    if now.tzinfo is None:
        now = now.replace(tzinfo=CT)
    else:
        now = now.astimezone(CT)
    target = now.replace(hour=DIGEST_HOUR_CT, minute=DIGEST_MINUTE_CT, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


def is_digest_window(now: Optional[datetime] = None, *, window_seconds: int = 60) -> bool:
    """True when within ``window_seconds`` of 8:07 CT (morning digest)."""
    now = now or _now_ct()
    if now.tzinfo is None:
        now = now.replace(tzinfo=CT)
    else:
        now = now.astimezone(CT)
    if now.hour != DIGEST_HOUR_CT or now.minute != DIGEST_MINUTE_CT:
        return False
    return now.second < window_seconds


def seconds_until_next_graph_poll(now: Optional[datetime] = None) -> float:
    """Seconds until the next Graph poll is due (AC-P4 cadence). 0 = due now."""
    global _last_graph_poll_mono, _next_graph_wall
    now = now or _now_ct()
    interval_s = float(poll_interval_minutes(now)) * 60.0
    if _last_graph_poll_mono is None and _next_graph_wall is None:
        return 0.0
    # Wall clock (tests inject now_fn and advance without mono moving).
    if _next_graph_wall is not None:
        return max(0.0, (_next_graph_wall - now).total_seconds())
    elapsed_mono = _mono() - float(_last_graph_poll_mono or 0.0)
    return max(0.0, interval_s - elapsed_mono)


def _mark_graph_polled(now: Optional[datetime] = None) -> None:
    global _last_graph_poll_mono, _next_graph_wall
    now = now or _now_ct()
    _last_graph_poll_mono = _mono()
    interval_s = float(poll_interval_minutes(now)) * 60.0
    _next_graph_wall = now + timedelta(seconds=interval_s)


def build_digest(db_path: Optional[str] = None) -> dict[str, Any]:
    """Tier-2 digest payload — counts + why tags only (no message bodies)."""
    with store.open_store(db_path) as conn:
        flags = store.list_flags(conn, include_local_content=False)
        tier2 = [f for f in flags if int(f.get("tier") or 0) == TIER_2]
        summary = store.board_summary(conn)
    return {
        "ok": True,
        "kind": "morning_digest",
        "at_ct": f"{DIGEST_HOUR_CT:02d}:{DIGEST_MINUTE_CT:02d}",
        "tier_2_count": len(tier2),
        "items": [{"why": f.get("why"), "tier": f.get("tier"), "source": f.get("source")} for f in tier2],
        "board_summary": summary,
        # Never message text
        "message_text": None,
    }


def run_once(
    *,
    db_path: Optional[str] = None,
    http_get=None,
    token_provider=None,
    force_refresh=None,
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
                    force_refresh=force_refresh,
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
                "chats_discovered": int(result.get("chats_discovered") or 0),
            }
    finally:
        _scanning = False


def _scheduler_loop(db_path: Optional[str] = None) -> None:
    """Background loop: Graph on 10 min / hourly cadence; short wakes only for digest.

    Probe r3: a 60s wake must NOT call Graph. Digest window may wake often, but
    ``run_once`` only fires when the full poll interval has elapsed (AC-P4 / AC-P5).
    """
    last_digest_day: Optional[str] = None
    first = True
    while not _scheduler_stop.is_set():
        try:
            now = _now_ct()
            due_graph = seconds_until_next_graph_poll(now)
            due_digest = seconds_until_digest(now)

            candidates = [_DIGEST_WAKE_CAP_S]
            if due_graph > 0.0:
                candidates.append(due_graph)
            else:
                # Graph due now: brief settle so start is not a busy-loop.
                candidates.append(5.0 if first else 1.0)
            if due_digest > 0.0:
                candidates.append(due_digest)
            sleep_s = max(1.0, min(candidates))

            _sleep_fn(sleep_s)
            first = False
            if _scheduler_stop.is_set():
                break

            now = _now_ct()
            # Graph only when the full cadence says so (never on every digest wake).
            if (
                auth.reader_may_call_graph()
                and not _reader_stop_reason
                and seconds_until_next_graph_poll(now) <= 0.0
            ):
                try:
                    run_once(db_path=db_path)
                    _mark_graph_polled(now)
                except Exception as exc:
                    logger.warning("work_watch scheduled poll failed: %s", type(exc).__name__)
                    # Still advance the clock so a hard error cannot spin Graph every wake.
                    _mark_graph_polled(now)

            day_key = now.strftime("%Y-%m-%d")
            if is_digest_window(now) and day_key != last_digest_day:
                try:
                    digest = build_digest(db_path=db_path)
                    store_digest_receipt(digest, db_path=db_path)
                    last_digest_day = day_key
                    logger.info(
                        "work_watch morning digest: tier_2=%s",
                        digest.get("tier_2_count"),
                    )
                except Exception as exc:
                    logger.warning("work_watch digest failed: %s", type(exc).__name__)
        except Exception as exc:
            logger.warning("work_watch scheduler tick error: %s", type(exc).__name__)
            _sleep_fn(5.0)


def store_digest_receipt(digest: dict[str, Any], *, db_path: Optional[str] = None) -> None:
    """Persist a local digest receipt (counts only)."""
    try:
        with store.open_store(db_path) as conn:
            store.set_meta(conn, "last_digest_at", _now_ct().isoformat())
            store.set_meta(conn, "last_digest_tier2", str(int(digest.get("tier_2_count") or 0)))
    except Exception as exc:
        logger.debug("digest receipt skipped: %s", type(exc).__name__)


def start_poll_scheduler(*, db_path: Optional[str] = None) -> bool:
    """Start the background poll loop via spawn_context_thread (Probe defect 4 / r3).

    ``spawn_context_thread`` returns an unstarted Thread — always ``.start()`` it.
    No bare-thread fallback (drops secret_scope). Returns True when a new thread
    was started.
    """
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        _scheduler_stop.clear()

        def _run() -> None:
            _scheduler_loop(db_path=db_path)

        from agent.memory_provider import spawn_context_thread

        t = spawn_context_thread(
            _run,
            name="work-watch-poll",
            daemon=True,
        )
        t.start()
        _scheduler_thread = t
        return True


def stop_poll_scheduler(timeout: float = 2.0) -> None:
    """Stop the background poll loop (tests + revoke path)."""
    global _scheduler_thread
    _scheduler_stop.set()
    t = _scheduler_thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    _scheduler_thread = None


def scheduler_running() -> bool:
    t = _scheduler_thread
    return t is not None and t.is_alive()


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
            digest_at = store.get_meta(conn, "last_digest_at")
    except Exception:
        summary = {"total": 0, "tier_1": 0, "tier_2": 0, "summary": "quiet"}
        digest_at = None

    base["last_check"] = last
    base["board_summary"] = summary
    base["scanning"] = _scanning
    base["reader_stopped"] = _reader_stop_reason
    base["poll_interval_minutes"] = poll_interval_minutes()
    base["scheduler_running"] = scheduler_running()
    base["digest_at_ct"] = f"{DIGEST_HOUR_CT:02d}:{DIGEST_MINUTE_CT:02d}"
    base["last_digest_at"] = digest_at
    return base
