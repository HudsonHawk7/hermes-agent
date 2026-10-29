"""Local SQLite store for Work watch flags + classification receipts.

Lives under ``<hermes home>/plugin-data/work_watch/`` (not Force SQL Server).
Message body/subject/sender never leave this local store by default.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from plugins.work_watch.constants import PLUGIN_NAME, TIER_1, TIER_2, TIER_3

SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,              -- mail | chat
    tier INTEGER NOT NULL,
    why TEXT NOT NULL,
    title_local TEXT,                 -- local-only; never exported by default
    preview_local TEXT,               -- local-only
    sender_local TEXT,                -- local-only
    updated_at REAL NOT NULL,
    created_at REAL NOT NULL,
    last_item_id TEXT,
    seen_count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                -- tier3_drop | poll | error | revoke
    conversation_id TEXT,
    tier INTEGER,
    why TEXT,
    detail_json TEXT,                 -- never raw tokens; no body by default
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flags_tier ON flags(tier);
CREATE INDEX IF NOT EXISTS idx_flags_updated ON flags(updated_at DESC);
"""


@dataclass
class FlagRow:
    id: int
    conversation_id: str
    source: str
    tier: int
    why: str
    updated_at: float
    created_at: float
    seen_count: int
    # Local-only fields omitted from board exports unless explicitly requested.
    title_local: Optional[str] = None
    preview_local: Optional[str] = None
    sender_local: Optional[str] = None


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    if db_path:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
    else:
        from plugins.plugin_storage import plugin_db

        conn = plugin_db(PLUGIN_NAME)
        conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def open_store(db_path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def record_poll(conn: sqlite3.Connection, *, item_count: int, when: Optional[float] = None) -> None:
    ts = when if when is not None else time.time()
    set_meta(conn, "last_check_at", str(ts))
    set_meta(conn, "last_check_count", str(int(item_count)))
    conn.execute(
        "INSERT INTO receipts(kind, tier, why, detail_json, created_at) VALUES(?,?,?,?,?)",
        ("poll", None, "poll", json.dumps({"item_count": int(item_count)}), ts),
    )


def last_check(conn: sqlite3.Connection) -> dict[str, Any]:
    at = get_meta(conn, "last_check_at")
    count = get_meta(conn, "last_check_count", "0")
    return {
        "last_check_at": float(at) if at else None,
        "last_check_count": int(count or 0),
        # AC-P3: never message text
        "message_text": None,
    }


def upsert_flag(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    source: str,
    tier: int,
    why: str,
    title_local: Optional[str] = None,
    preview_local: Optional[str] = None,
    sender_local: Optional[str] = None,
    item_id: Optional[str] = None,
    when: Optional[float] = None,
) -> FlagRow:
    """Dedupe by conversation_id — long reply chains update one row (AC-N17)."""
    if not why or not str(why).strip():
        raise ValueError("flag why is required (AC-N15)")
    if tier == TIER_3:
        raise ValueError("tier-3 must be dropped, not stored as a flag (AC-N14)")
    ts = when if when is not None else time.time()
    existing = conn.execute(
        "SELECT id, seen_count, created_at FROM flags WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE flags SET source=?, tier=?, why=?, title_local=?, preview_local=?,
               sender_local=?, updated_at=?, last_item_id=?, seen_count=seen_count+1
               WHERE conversation_id=?""",
            (
                source,
                int(tier),
                str(why).strip(),
                title_local,
                preview_local,
                sender_local,
                ts,
                item_id,
                conversation_id,
            ),
        )
        row_id = int(existing["id"])
        created = float(existing["created_at"])
        seen = int(existing["seen_count"]) + 1
    else:
        cur = conn.execute(
            """INSERT INTO flags(conversation_id, source, tier, why, title_local, preview_local,
               sender_local, updated_at, created_at, last_item_id, seen_count)
               VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
            (
                conversation_id,
                source,
                int(tier),
                str(why).strip(),
                title_local,
                preview_local,
                sender_local,
                ts,
                ts,
                item_id,
            ),
        )
        raw_id = cur.lastrowid
        if raw_id is None:
            raise RuntimeError("flag insert returned no row id")
        row_id = int(raw_id)
        created = ts
        seen = 1
    return FlagRow(
        id=row_id,
        conversation_id=conversation_id,
        source=source,
        tier=int(tier),
        why=str(why).strip(),
        updated_at=ts,
        created_at=created,
        seen_count=seen,
        title_local=title_local,
        preview_local=preview_local,
        sender_local=sender_local,
    )


def drop_tier3(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    why: str,
    when: Optional[float] = None,
) -> None:
    """Record a Tier-3 drop receipt without putting content on the board (AC-N14, AC-X2)."""
    ts = when if when is not None else time.time()
    conn.execute(
        "INSERT INTO receipts(kind, conversation_id, tier, why, detail_json, created_at) VALUES(?,?,?,?,?,?)",
        (
            "tier3_drop",
            conversation_id,
            TIER_3,
            why,
            json.dumps({"dropped": True}),  # no body/subject/sender
            ts,
        ),
    )


def list_flags(
    conn: sqlite3.Connection,
    *,
    include_local_content: bool = False,
    tiers: Optional[tuple[int, ...]] = None,
) -> list[dict[str, Any]]:
    """Board-safe list by default: counts + why + tier, no message content."""
    allowed = tiers or (TIER_1, TIER_2)
    q = f"SELECT * FROM flags WHERE tier IN ({','.join('?' * len(allowed))}) ORDER BY updated_at DESC"
    rows = conn.execute(q, tuple(allowed)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        item = {
            "id": int(r["id"]),
            "conversation_id": r["conversation_id"],
            "source": r["source"],
            "tier": int(r["tier"]),
            "why": r["why"],
            "updated_at": float(r["updated_at"]),
            "created_at": float(r["created_at"]),
            "seen_count": int(r["seen_count"]),
        }
        if include_local_content:
            item["title_local"] = r["title_local"]
            item["preview_local"] = r["preview_local"]
            item["sender_local"] = r["sender_local"]
        out.append(item)
    return out


def board_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts only — safe for bus/cards (AC-N12)."""
    rows = conn.execute(
        "SELECT tier, COUNT(*) AS n FROM flags WHERE tier IN (?, ?) GROUP BY tier",
        (TIER_1, TIER_2),
    ).fetchall()
    by_tier = {int(r["tier"]): int(r["n"]) for r in rows}
    total = sum(by_tier.values())
    return {
        "total": total,
        "tier_1": by_tier.get(TIER_1, 0),
        "tier_2": by_tier.get(TIER_2, 0),
        "summary": f"{total} items" if total else "quiet",
    }


def list_tier3_receipts(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, conversation_id, why, created_at FROM receipts WHERE kind='tier3_drop' ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "conversation_id": r["conversation_id"],
            "why": r["why"],
            "created_at": float(r["created_at"]),
            # no content
        }
        for r in rows
    ]
