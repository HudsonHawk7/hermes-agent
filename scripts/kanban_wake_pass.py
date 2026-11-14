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

from hermes_cli.kanban_db_connect import connect as board_connect
from hermes_cli import kanban_wake as kw

SHARED_OWNERS = {"_kanban", "workspaces", "work", "wt", "scratch"}
ANCHORS = ("forceagents", "sandboxes")


def board_dirs() -> list[Path]:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "kanban" / "boards"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if (path / "kanban.db").is_file())


def host_dispatch_profiles() -> set[str]:
    """Names the dispatcher will run. Empty means unreadable: promote nothing."""
    path = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "config.yaml"
    if not path.is_file():
        return set()
    in_kanban = False
    collecting = False
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("kanban:"):
            in_kanban = True
            continue
        if in_kanban and line and not line[:1].isspace():
            break
        if not in_kanban:
            continue
        stripped = line.strip()
        if collecting:
            if stripped.startswith("- "):
                names.append(stripped[2:].strip().strip("'\""))
                continue
            break
        if not stripped.startswith("dispatch_profiles:"):
            continue
        rest = stripped.split(":", 1)[1].strip()
        if rest.startswith("[") and rest.endswith("]"):
            return {part.strip().strip("'\"") for part in rest[1:-1].split(",") if part.strip()}
        collecting = True
    return {name for name in names if name}


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
        if (row[0] or "") in {"park-wake", "kanban_wake"}:
            continue
        kept.append(row[1] or "")
        if len(kept) >= 3:
            break
    return "\n".join(kept)


def dry_board(
    slug: str,
    folder: Path,
    *,
    now: float,
    active: dict[str, int],
    parents: dict[str, str],
    profiles: set[str] | None = None,
) -> list[str]:
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
            assignee = (card["assignee"] or "").strip()
            if not _assignee_runs(assignee, profiles):
                continue
            stored, summary = _stored_decision(conn, card["id"], now, parents)
            if stored == "none" or stored == "unmet":
                continue
            if stored == "absent":
                parsed = kw.parse_park_note(note, own_id=card["id"])
                if not _parsed_met(parsed, now, parents):
                    continue
                summary = parsed["summary"]
            if active.get(assignee, 0) >= 1:
                lines.append(f"{slug} would-hold {card['id']} lane full")
                continue
            lines.append(f"{slug} would-promote {card['id']} {summary}")
            active[assignee] = active.get(assignee, 0) + 1
    finally:
        conn.close()
    return lines


def _assignee_runs(assignee: str, profiles: set[str] | None) -> bool:
    if not assignee:
        return False
    if profiles is None:
        return True
    return assignee.lower() in {name.lower() for name in profiles}


def _stored_decision(conn, task_id: str, now: float, parents: dict[str, str]) -> tuple[str, str]:
    """absent, none, unmet, or met. Apply uses stored rows and does not re-parse them."""
    try:
        rows = conn.execute(
            "SELECT kind, parent_id, repo, pr_number, until_ts FROM task_wakes WHERE task_id=?",
            (task_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return "absent", ""
    if not rows:
        return "absent", ""
    real = [row for row in rows if row["kind"] != "none"]
    if not real:
        return "none", "wake=none"
    for row in real:
        if row["kind"] == "parent" and parents.get(row["parent_id"]) not in kw.MET_PARENT:
            return "unmet", ""
        if row["kind"] == "time" and int(row["until_ts"] or 0) > int(now):
            return "unmet", ""
        if row["kind"] == "pr":
            state = (kw.github_pr_status(row["repo"], int(row["pr_number"])) or "").lower()
            if state not in kw.MET_PR:
                return "unmet", ""
    return "met", _stored_summary(real)


def _stored_summary(rows) -> str:
    parts = []
    for row in rows:
        if row["kind"] == "parent":
            parts.append(f"wait-on {row['parent_id']}")
        elif row["kind"] == "pr":
            parts.append(f"until-pr {row['repo']}#{row['pr_number']}")
        elif row["kind"] == "time":
            parts.append(f"until {row['until_ts']}")
    return ", ".join(parts) or "wake=none"


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


def apply_board(
    slug: str,
    *,
    now: float,
    active: dict[str, int],
    parents: dict[str, str],
    daily: bool,
    profiles: set[str] | None = None,
) -> list[str]:
    lines: list[str] = []
    with board_connect(board=slug) as conn:
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
            dispatch_profiles=profiles,
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
    profiles = host_dispatch_profiles()
    print(f"dispatch profiles: {len(profiles)}")
    if not profiles:
        print("dispatch profiles unreadable; promoting nothing")
    printed = 0
    folders = board_dirs()
    for folder in folders:
        lines = (
            apply_board(
                folder.name, now=now, active=active, parents=parents, daily=daily, profiles=profiles,
            )
            if apply
            else dry_board(
                folder.name, folder, now=now, active=active, parents=parents, profiles=profiles,
            )
        )
        for line in lines:
            print(line)
            printed += 1
    print(f"wake-pass: boards={len(folders)} lines={printed} apply={int(apply)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
