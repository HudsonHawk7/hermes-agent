"""One wake pass over every kanban board.

The profile sweep calls this after install. The rules live in hermes_cli.kanban_wake.
A dry run prints what would move and writes nothing. --apply backfills, archives
merged Gate PR cards, and promotes cards whose waits are met.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake as kw

SHARED_OWNERS = {"_kanban", "workspaces", "work", "wt", "scratch"}
ANCHORS = ("forceagents", "sandboxes")


def board_dirs() -> list[Path]:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "kanban" / "boards"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if (path / "kanban.db").is_file())


def _ro(folder: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{folder / 'kanban.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def lane_load() -> dict[str, int]:
    counts: dict[str, int] = {}
    for folder in board_dirs():
        conn = _ro(folder)
        try:
            rows = conn.execute(
                "SELECT assignee, COUNT(*) FROM tasks "
                "WHERE status IN ('ready', 'running', 'review') "
                "AND assignee IS NOT NULL GROUP BY assignee"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            counts[row[0]] = counts.get(row[0], 0) + int(row[1])
    return counts


def parent_status_map() -> dict[str, str]:
    found: dict[str, str] = {}
    for folder in board_dirs():
        conn = _ro(folder)
        try:
            for row in conn.execute("SELECT id, status FROM tasks"):
                found[row[0]] = row[1]
        finally:
            conn.close()
    return found


def workspace_owner(path: str) -> str:
    parts = str(path or "").replace("\\", "/").lower().strip("/").split("/")
    for anchor in ANCHORS:
        if anchor in parts:
            index = parts.index(anchor) + 1
            return parts[index] if index < len(parts) else ""
    return ""


def foreign_workspace(path: str, assignee: str) -> bool:
    owner = workspace_owner(path)
    if not owner or owner in SHARED_OWNERS:
        return False
    return owner != (assignee or "").lower()


def latest_note(conn, task_id: str) -> str:
    rows = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 12",
        (task_id,),
    ).fetchall()
    kept = []
    for row in rows:
        if (row[0] or "") == "park-wake":
            continue
        kept.append(row[1] or "")
        if len(kept) >= 3:
            break
    return "\n".join(kept)


def dry_board(slug: str, folder: Path, *, now: float, active: dict[str, int], parents: dict[str, str]) -> list[str]:
    """Say what --apply would do. Does not write."""
    lines: list[str] = []
    conn = _ro(folder)
    try:
        cards = conn.execute(
            "SELECT id, title, assignee, workspace_path FROM tasks WHERE status='scheduled'"
        ).fetchall()
        for card in cards:
            note = latest_note(conn, card["id"])
            if kw.hold_reason(card["title"] or "", note) or kw.GM_WAIT_RE.search(note or ""):
                continue
            if foreign_workspace(card["workspace_path"] or "", card["assignee"] or ""):
                continue
            parsed = kw.parse_park_note(note, own_id=card["id"])
            if not _parsed_met(parsed, now, parents):
                continue
            assignee = (card["assignee"] or "").strip()
            if not assignee:
                continue
            if active.get(assignee, 0) >= 1:
                lines.append(f"{slug} would-hold {card['id']} lane full")
                continue
            lines.append(f"{slug} would-promote {card['id']} {parsed['summary']}")
            active[assignee] = active.get(assignee, 0) + 1
    finally:
        conn.close()
    return lines


def _parsed_met(parsed: dict, now: float, parents: dict[str, str]) -> bool:
    real = bool(parsed["wait_on"] or parsed["until_prs"] or parsed["until_ts"] is not None)
    if not real:
        return False
    if any(parents.get(parent) not in kw.MET_PARENT for parent in parsed["wait_on"]):
        return False
    if parsed["until_ts"] is not None and int(parsed["until_ts"]) > int(now):
        return False
    for repo, number in parsed["until_prs"]:
        if (kw.github_pr_status(repo, number) or "") not in kw.MET_PR:
            return False
    return True


def apply_board(slug: str, *, now: float, active: dict[str, int], parents: dict[str, str], daily: bool) -> list[str]:
    lines: list[str] = []
    with kbc.connect(board=slug) as conn:
        for line in kw.backfill_scheduled(conn):
            lines.append(f"{slug} backfill {line}")
        archived = kw.archive_merged_gates(conn, pr_status=kw.github_pr_status)
        promoted = kw.promote_ready(
            conn,
            now=now,
            pr_status=kw.github_pr_status,
            parent_status=parents.get,
            lane_active=active,
            lane_cap=1,
            skip_task=lambda task_id, conn=conn: _foreign_id(conn, task_id),
        )
        for task_id in promoted["promoted"]:
            assignee = _assignee(conn, task_id)
            active[assignee] = active.get(assignee, 0) + 1
            lines.append(f"{slug} promoted {task_id}")
        for task_id in promoted["held"]:
            lines.append(f"{slug} held {task_id} lane full")
        for task_id in archived["archived"]:
            lines.append(f"{slug} archived {task_id} superseded")
        if daily:
            for line in kw.daily_lines(conn, now=now):
                lines.append(f"{slug} daily {line}")
    return lines


def _foreign_id(conn, task_id: str) -> bool:
    row = conn.execute(
        "SELECT workspace_path, assignee FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    return foreign_workspace(row["workspace_path"] or "", row["assignee"] or "")


def _assignee(conn, task_id: str) -> str:
    row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
    return (row["assignee"] if row else "") or ""


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    daily = "--daily" in argv
    now = time.time()
    active = lane_load()
    parents = parent_status_map()
    printed = 0
    folders = board_dirs()
    for folder in folders:
        lines = (
            apply_board(folder.name, now=now, active=active, parents=parents, daily=daily)
            if apply
            else dry_board(folder.name, folder, now=now, active=active, parents=parents)
        )
        for line in lines:
            print(line)
            printed += 1
    print(f"wake-pass: boards={len(folders)} lines={printed} apply={int(apply)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
