"""Structured GM asks (one-tap approvals) on Kanban cards.

No schema migration: asks and decisions live in ``task_events`` as
``gm_ask`` / ``gm_decision`` / ``gm_decision_undone``. Workers file an ask
through ``kanban_block(kind=needs_input, ask=...)``; humans answer via CLI
or the dashboard HTTP surface. One shared answer path keeps both surfaces
honest.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as _kb
from hermes_cli import kanban_db_connect as _kbc

GM_ASK_KIND = "gm_ask"
GM_DECISION_KIND = "gm_decision"
GM_DECISION_UNDONE_KIND = "gm_decision_undone"

_OPTION_KEYS = ("key", "label", "effect")


class GmAskError(ValueError):
    """Invalid ask payload or illegal answer/undo transition."""


def validate_gm_ask(ask: Any) -> dict[str, Any]:
    """Normalise and validate a structured ask; raise :class:`GmAskError` with a
    corrective message the worker can retry against.

    Payload:
      question (one line), context (≤2 sentences), options (2–4 of
      ``{key, label, effect}`` + optional ``image``), pick (recommended key or
      null), asked_by (optional here — callers stamp it).
    """
    if ask is None:
        raise GmAskError(
            "kind=needs_input requires an 'ask' object with question, context, "
            "and 2-4 options each having key/label/effect. Example: "
            '{"question":"Rebase onto develop?","context":"PR is behind.","options":'
            '[{"key":"yes","label":"Yes","effect":"BB-8 rebases and proves it"},'
            '{"key":"no","label":"No","effect":"card closes"}],"pick":"yes"}'
        )
    if not isinstance(ask, dict):
        raise GmAskError("ask must be an object/dict with question, context, and options")

    question = _one_line(ask.get("question"), "question")
    context = _context_lines(ask.get("context"))
    options_raw = ask.get("options")
    if not isinstance(options_raw, list):
        raise GmAskError("ask.options must be a list of 2-4 option objects")
    if not (2 <= len(options_raw) <= 4):
        raise GmAskError(
            f"ask.options must have 2-4 items (got {len(options_raw)}). "
            "Each option needs key, label, and effect (one line on what happens)."
        )

    options: list[dict[str, Any]] = []
    keys: list[str] = []
    for i, raw in enumerate(options_raw):
        if not isinstance(raw, dict):
            raise GmAskError(f"ask.options[{i}] must be an object with key, label, effect")
        key = _token(raw.get("key"), f"options[{i}].key")
        label = _one_line(raw.get("label"), f"options[{i}].label")
        effect = _one_line(raw.get("effect"), f"options[{i}].effect")
        if key in keys:
            raise GmAskError(f"ask.options keys must be unique (duplicate {key!r})")
        keys.append(key)
        opt: dict[str, Any] = {"key": key, "label": label, "effect": effect}
        image = raw.get("image")
        if image is not None and str(image).strip():
            opt["image"] = str(image).strip()
        options.append(opt)

    pick_raw = ask.get("pick", None)
    if pick_raw is None or pick_raw == "":
        pick: Optional[str] = None
    else:
        pick = _token(pick_raw, "pick")
        if pick not in keys:
            raise GmAskError(
                f"ask.pick must be one of the option keys {keys} (got {pick!r})"
            )

    out: dict[str, Any] = {
        "question": question,
        "context": context,
        "options": options,
        "pick": pick,
    }
    asked_by = ask.get("asked_by")
    if asked_by is not None and str(asked_by).strip():
        out["asked_by"] = str(asked_by).strip()
    return out


def _one_line(value: Any, field: str) -> str:
    if value is None or not str(value).strip():
        raise GmAskError(f"ask.{field} is required (one non-empty line)")
    text = str(value).strip()
    first = text.splitlines()[0].strip()
    if not first:
        raise GmAskError(f"ask.{field} is required (one non-empty line)")
    return first


def _context_lines(value: Any) -> str:
    if value is None or not str(value).strip():
        raise GmAskError("ask.context is required (at most two sentences)")
    text = str(value).strip()
    # Cap at two sentences by splitting on '. ' / '? ' / '! ' boundaries, keep ≤2.
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in ".?!" and len(buf.strip()) > 1:
            parts.append(buf.strip())
            buf = ""
            if len(parts) >= 2:
                break
    if buf.strip() and len(parts) < 2:
        parts.append(buf.strip())
    if not parts:
        raise GmAskError("ask.context is required (at most two sentences)")
    return " ".join(parts[:2])


def _token(value: Any, field: str) -> str:
    if value is None or not str(value).strip():
        raise GmAskError(f"ask.{field} is required")
    token = str(value).strip()
    if any(c.isspace() for c in token):
        raise GmAskError(f"ask.{field} must be a single token without spaces (got {token!r})")
    return token


def _append_event_id(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: Optional[dict] = None, *,
    run_id: Optional[int] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, _kb._json_or_null(payload), int(time.time())),
    )
    return int(cur.lastrowid or 0)


def get_open_gm_ask(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[tuple[int, dict[str, Any], int]]:
    """Return ``(ask_event_id, payload, created_at)`` for the open ask, or None.

    Walks events in order: a ``gm_ask`` opens; a matching ``gm_decision`` closes;
    a ``gm_decision_undone`` re-opens the referenced ask.
    """
    rows = conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind IN (?, ?, ?) ORDER BY id ASC",
        (task_id, GM_ASK_KIND, GM_DECISION_KIND, GM_DECISION_UNDONE_KIND),
    ).fetchall()
    open_id: Optional[int] = None
    open_payload: Optional[dict[str, Any]] = None
    open_at: Optional[int] = None
    by_id: dict[int, tuple[dict[str, Any], int]] = {}
    for row in rows:
        eid = int(row["id"])
        kind = row["kind"]
        payload = _kb._json_dict(row["payload"])
        created = int(row["created_at"] or 0)
        if kind == GM_ASK_KIND:
            by_id[eid] = (payload, created)
            open_id, open_payload, open_at = eid, payload, created
        elif kind == GM_DECISION_KIND:
            open_id = open_payload = open_at = None
        elif kind == GM_DECISION_UNDONE_KIND:
            ask_id = payload.get("ask_event_id")
            if isinstance(ask_id, int) and ask_id in by_id:
                open_payload, open_at = by_id[ask_id]
                open_id = ask_id
            elif open_id is None and by_id:
                # Fall back to the most recent ask if the undo payload is thin.
                last = max(by_id)
                open_id = last
                open_payload, open_at = by_id[last]
    if open_id is None or open_payload is None or open_at is None:
        return None
    return open_id, open_payload, open_at


def get_latest_gm_decision(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[tuple[int, dict[str, Any], int]]:
    """Most recent ``gm_decision`` not cancelled by a later undo, or None."""
    rows = conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind IN (?, ?) ORDER BY id ASC",
        (task_id, GM_DECISION_KIND, GM_DECISION_UNDONE_KIND),
    ).fetchall()
    latest: Optional[tuple[int, dict[str, Any], int]] = None
    for row in rows:
        kind = row["kind"]
        if kind == GM_DECISION_KIND:
            latest = (int(row["id"]), _kb._json_dict(row["payload"]), int(row["created_at"] or 0))
        elif kind == GM_DECISION_UNDONE_KIND:
            undone = _kb._json_dict(row["payload"]).get("decision_event_id")
            if latest is not None and (undone is None or undone == latest[0]):
                latest = None
    return latest


def record_gm_ask(
    conn: sqlite3.Connection,
    task_id: str,
    ask: Any,
    *,
    asked_by: str,
    reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    force_park: bool = False,
) -> dict[str, Any]:
    """Validate *ask*, ensure the card is ``blocked``/``needs_input``, write
    ``gm_ask``. Returns ``{ask_event_id, ask, status}``.

    *force_park*: CLI path — park cards that are not running/ready by flipping
    them to ``blocked``/``needs_input`` directly. Worker path leaves that false
    so only the normal ``block_task`` transition is used.
    """
    payload = validate_gm_ask(ask)
    payload["asked_by"] = (asked_by or payload.get("asked_by") or "worker").strip() or "worker"
    if get_open_gm_ask(conn, task_id) is not None:
        raise GmAskError(f"{task_id} already has an open GM ask — answer or undo it first")

    task = _kb.get_task(conn, task_id)
    if task is None:
        raise GmAskError(f"unknown task {task_id}")

    block_reason = reason or payload["question"]

    if task.status in ("running", "ready"):
        ok = _kb.block_task(
            conn, task_id, reason=block_reason, kind="needs_input",
            expected_run_id=expected_run_id,
        )
        if not ok:
            raise GmAskError(f"could not block {task_id} (unknown id or not in running/ready)")
    elif task.status == "blocked" and task.block_kind == "needs_input":
        pass  # already waiting on a human; just attach the structured ask
    elif force_park:
        _force_needs_input(conn, task_id, reason=block_reason)
    else:
        raise GmAskError(
            f"cannot file a GM ask on {task_id} while status={task.status!r}; "
            "card must be running/ready (or already blocked needs_input)"
        )

    with _kbc.write_txn(conn):
        # Re-check inside the txn so a racing answer cannot leave us with two opens.
        if get_open_gm_ask(conn, task_id) is not None:
            raise GmAskError(f"{task_id} already has an open GM ask — answer or undo it first")
        event_id = _append_event_id(conn, task_id, GM_ASK_KIND, payload)

    landed = _kb.get_task(conn, task_id)
    return {
        "ask_event_id": event_id,
        "ask": payload,
        "status": landed.status if landed else None,
        "block_kind": landed.block_kind if landed else None,
    }


def _force_needs_input(conn: sqlite3.Connection, task_id: str, *, reason: str) -> None:
    """Park a non-running card as ``blocked``/``needs_input`` for CLI ``ask``."""
    with _kbc.write_txn(conn):
        row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            raise GmAskError(f"unknown task {task_id}")
        prev_kind = _kb._row_get(row, "block_kind")
        prev_rec = int(_kb._row_get(row, "block_recurrences") or 0)
        recurrences = prev_rec + 1 if prev_kind == "needs_input" else 1
        cur = conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input', "
            "block_recurrences = ?, claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, current_run_id = NULL "
            "WHERE id = ? AND status NOT IN ('done', 'archived')",
            (recurrences, task_id),
        )
        if cur.rowcount != 1:
            raise GmAskError(f"cannot park {task_id} for a GM ask (done/archived or missing)")
        _kb._append_event(
            conn, task_id, "blocked",
            {
                "reason": reason,
                "kind": "needs_input",
                "recurrences": recurrences,
                "source_status": row["status"],
                "via": "gm_ask",
            },
        )


def answer_gm_ask(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    key: Optional[str] = None,
    sent_back: bool = False,
    note: Optional[str] = None,
    by: str = "gm",
    via: str = "cli",
) -> dict[str, Any]:
    """Resolve the open ask: write ``gm_decision``, comment, unblock to ready.

    Exactly one of *key* or *sent_back* must be set. Raises :class:`GmAskError`
    when there is no open ask or the key is unknown. One transaction.
    """
    if sent_back and key is not None:
        raise GmAskError("pass either an option key or --send-back, not both")
    if not sent_back and key is None:
        raise GmAskError("pass an option key, or --send-back with a note")
    if via not in ("cli", "button"):
        raise GmAskError("via must be 'cli' or 'button'")

    by = (by or "gm").strip() or "gm"
    note_text = (note or "").strip() or None

    with _kbc.write_txn(conn):
        open_ask = get_open_gm_ask(conn, task_id)
        if open_ask is None:
            raise GmAskError(
                f"{task_id} has no open GM ask to answer "
                "(already decided, or never asked with a structured ask)"
            )
        ask_event_id, ask_payload, _ask_at = open_ask
        options = {o["key"]: o for o in ask_payload.get("options") or [] if isinstance(o, dict)}

        if sent_back:
            if not note_text:
                raise GmAskError("send-back requires a --note explaining what to change")
            decision: dict[str, Any] = {
                "sent_back": True,
                "note": note_text,
                "by": by,
                "via": via,
                "ask_event_id": ask_event_id,
            }
            comment = f"GM sent it back: {note_text}"
        else:
            assert key is not None
            if key not in options:
                raise GmAskError(
                    f"unknown option key {key!r}; valid keys: {sorted(options)}"
                )
            opt = options[key]
            decision = {
                "key": key,
                "note": note_text,
                "by": by,
                "via": via,
                "ask_event_id": ask_event_id,
                "label": opt.get("label"),
                "effect": opt.get("effect"),
            }
            label = opt.get("label") or key
            effect = opt.get("effect") or ""
            comment = f"GM decision: {label} - {effect}"
            if note_text:
                comment = f"{comment} ({note_text})"

        decision_id = _append_event_id(conn, task_id, GM_DECISION_KIND, decision)
        _kb._insert_comment(conn, task_id, by, comment, int(time.time()))
        _kb._append_event(conn, task_id, "commented", {"author": by, "len": len(comment)})
        new_status = _unblock_under_txn(conn, task_id)

    return {
        "decision_event_id": decision_id,
        "ask_event_id": ask_event_id,
        "decision": decision,
        "comment": comment,
        "status": new_status,
    }


def _unblock_under_txn(conn: sqlite3.Connection, task_id: str) -> str:
    """``blocked``/``scheduled`` → ready/todo/review inside the caller's txn."""
    now = int(time.time())
    status = _kb._task_status(conn, task_id)
    resume_status = (
        _kb._resume_status_from_events(conn, task_id) if status == "blocked" else "ready"
    )
    _kb._reclaim_dangling_run(
        conn, task_id, statuses=("blocked", "scheduled"), now=now,
        note="invariant recovery on gm decision",
    )
    landing = _kb._landing_status_after_parents(conn, task_id)
    new_status = (
        "review" if landing == "ready" and resume_status == "review" else landing
    )
    cur = conn.execute(
        "UPDATE tasks SET status = ?, current_run_id = NULL, "
        "consecutive_failures = 0, last_failure_error = NULL "
        "WHERE id = ? AND status IN ('blocked', 'scheduled')",
        (new_status, task_id),
    )
    if cur.rowcount != 1:
        # Card may already have been unblocked manually; decision still stands.
        landed = _kb.get_task(conn, task_id)
        return landed.status if landed else new_status
    payload = None
    if new_status != "ready" or resume_status != "ready":
        payload = {"status": new_status, "resume_status": resume_status, "via": "gm_decision"}
    _kb._append_event(conn, task_id, "unblocked", payload)
    return new_status


def undo_gm_decision(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    by: str = "gm",
) -> dict[str, Any]:
    """Re-open the last decision's ask and re-block, only if no run started since."""
    by = (by or "gm").strip() or "gm"
    with _kbc.write_txn(conn):
        if get_open_gm_ask(conn, task_id) is not None:
            raise GmAskError(f"{task_id} already has an open GM ask — nothing to undo")
        latest = get_latest_gm_decision(conn, task_id)
        if latest is None:
            raise GmAskError(f"{task_id} has no GM decision to undo")
        decision_id, decision, decided_at = latest

        # Refuse if a run started after the decision (worker picked the card up).
        # Strict ``>`` (not ``>=``): ask→answer→undo can share one wall-clock
        # second with the prior run's started_at, and that must still undo.
        run = conn.execute(
            "SELECT id, profile, started_at FROM task_runs "
            "WHERE task_id = ? AND started_at > ? ORDER BY id DESC LIMIT 1",
            (task_id, decided_at),
        ).fetchone()
        task = _kb.get_task(conn, task_id)
        if task is not None and task.status == "running":
            who = task.assignee or (run["profile"] if run else "worker")
            raise GmAskError(f"already picked up by {who}")
        if run is not None:
            who = run["profile"] or (task.assignee if task else None) or "worker"
            raise GmAskError(f"already picked up by {who}")

        ask_event_id = decision.get("ask_event_id")
        if not isinstance(ask_event_id, int):
            raise GmAskError(f"{task_id} decision is missing ask_event_id; cannot undo")

        _append_event_id(
            conn, task_id, GM_DECISION_UNDONE_KIND,
            {
                "decision_event_id": decision_id,
                "ask_event_id": ask_event_id,
                "by": by,
            },
        )
        # Re-block needs_input. Card is typically ready after a decision.
        row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise GmAskError(f"unknown task {task_id}")
        prev_kind = _kb._row_get(row, "block_kind")
        prev_rec = int(_kb._row_get(row, "block_recurrences") or 0)
        recurrences = prev_rec + 1 if prev_kind == "needs_input" else 1
        if row["status"] in ("ready", "todo", "scheduled", "review", "blocked"):
            conn.execute(
                "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input', "
                "block_recurrences = ?, claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, current_run_id = NULL "
                "WHERE id = ?",
                (recurrences, task_id),
            )
            _kb._append_event(
                conn, task_id, "blocked",
                {
                    "reason": "gm decision undone",
                    "kind": "needs_input",
                    "recurrences": recurrences,
                    "source_status": row["status"],
                    "via": "gm_decision_undone",
                },
            )
        comment = "GM undid the last decision — ask is open again"
        _kb._insert_comment(conn, task_id, by, comment, int(time.time()))
        _kb._append_event(conn, task_id, "commented", {"author": by, "len": len(comment)})

    open_ask = get_open_gm_ask(conn, task_id)
    landed = _kb.get_task(conn, task_id)
    return {
        "undone_decision_event_id": decision_id,
        "ask_event_id": ask_event_id,
        "ask": open_ask[1] if open_ask else None,
        "status": landed.status if landed else None,
    }


def list_needs_you(
    *,
    attachment_url: Optional[Callable[[str, str, str], Optional[str]]] = None,
) -> list[dict[str, Any]]:
    """Open needs-you items across every non-archived board.

    Structured asks first; legacy ``needs_input`` blocks with no ``gm_ask`` are
    included with ``ask: null`` and the latest block reason so nothing waiting
    on the GM is hidden.
    """
    items: list[dict[str, Any]] = []
    for meta in _kb.list_boards(include_archived=False):
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        name = meta.get("name") or slug
        try:
            _kb.init_db(board=slug)
        except Exception:
            continue
        with _kbc.connect_closing(board=slug) as conn:
            tasks = conn.execute(
                "SELECT id, title, assignee, block_kind, status FROM tasks "
                "WHERE status = 'blocked' AND block_kind = 'needs_input'"
            ).fetchall()
            for row in tasks:
                tid = row["id"]
                open_ask = get_open_gm_ask(conn, tid)
                item: dict[str, Any] = {
                    "board": slug,
                    "board_name": name,
                    "task_id": tid,
                    "title": row["title"],
                    "assignee": row["assignee"],
                    "status": row["status"],
                    "block_kind": row["block_kind"],
                }
                if open_ask is not None:
                    ask_id, ask_payload, asked_at = open_ask
                    ask_view = dict(ask_payload)
                    # Rewrite option images to dashboard URLs when a resolver is given.
                    if attachment_url is not None:
                        rewritten = []
                        for opt in ask_view.get("options") or []:
                            if not isinstance(opt, dict):
                                continue
                            opt2 = dict(opt)
                            image = opt2.get("image")
                            if image:
                                url = attachment_url(slug, tid, str(image))
                                if url:
                                    opt2["image_url"] = url
                            rewritten.append(opt2)
                        ask_view["options"] = rewritten
                    item["ask"] = ask_view
                    item["ask_event_id"] = ask_id
                    item["asked_by"] = ask_payload.get("asked_by")
                    item["asked_at"] = asked_at
                else:
                    item["ask"] = None
                    item["ask_event_id"] = None
                    item["asked_by"] = None
                    item["asked_at"] = None
                    item["legacy_reason"] = _latest_block_reason(conn, tid)
                items.append(item)
    # Newest asks first; legacy (no asked_at) sink after structured ones of equal age.
    items.sort(key=lambda it: it.get("asked_at") or 0, reverse=True)
    return items


def _latest_block_reason(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'block_loop_detected') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    payload = _kb._json_dict(row["payload"])
    reason = payload.get("reason")
    return str(reason) if reason else None


def parse_option_flag(raw: str) -> dict[str, str]:
    """CLI ``--option 'key|Label|effect text'`` → option dict."""
    parts = raw.split("|", 2)
    if len(parts) != 3:
        raise GmAskError(
            f"option must be 'key|Label|effect' (got {raw!r}). "
            "Example: --option 'yes|Yes|BB-8 rebases it and proves it'"
        )
    key, label, effect = (p.strip() for p in parts)
    return {"key": key, "label": label, "effect": effect}


def resolve_attachment_image_url(
    board: str, task_id: str, image: str, *, conn: sqlite3.Connection,
) -> Optional[str]:
    """Map an option ``image`` (filename or stored path leaf) to a download URL path."""
    leaf = image.replace("\\", "/").rstrip("/").split("/")[-1]
    if not leaf:
        return None
    for att in _kb.list_attachments(conn, task_id):
        if att.filename == leaf or att.filename == image:
            return f"/api/plugins/kanban/attachments/{att.id}?board={board}"
        stored_leaf = str(att.stored_path or "").replace("\\", "/").split("/")[-1]
        if stored_leaf == leaf:
            return f"/api/plugins/kanban/attachments/{att.id}?board={board}"
    return None
