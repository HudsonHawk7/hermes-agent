"""Machine wakes for parked kanban cards.

A park can wait on a parent task, a pull request, or a time. A park with no
wake is marked ``none`` and is not promoted. The sweep calls this module; the
profile script is not the source of truth.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

AUTHOR = "park-wake"
LANE_CAP = 1
MAX_PER_PASS = 3
IDLE_SECONDS = 3 * 24 * 3600
MET_PARENT = {"done", "archived"}
MET_PR = {"merged", "closed"}

TASK_ID_RE = re.compile(r"\b(t_[0-9a-f]{8})\b", re.IGNORECASE)
PRS_LIST_RE = re.compile(
    r"\bPRs?\b\s*#?\s*((?:\d+\b\s*,?\s*(?:and\s+)?)+)",
    re.IGNORECASE,
)
UNTIL_RE = re.compile(
    r"SCHEDULED_UNTIL=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)"
)
GATE_TITLE_RE = re.compile(r"\bGate\s+PR\s*#?\s*(\d+)\b", re.IGNORECASE)
ALREADY_RE = re.compile(
    r"already merged|already running the pr|already in live head|already live",
    re.IGNORECASE,
)
# Sentence end, not a character window. An 80-character window lets
# "already in live HEAD" in the previous sentence cancel the next PR wait.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
HOLD_RE = re.compile(
    r"do not promote|do not build|parked on purpose|on purpose|leave (?:it )?parked|on hold",
    re.IGNORECASE,
)
GM_WAIT_RE = re.compile(
    r"\bwait(?:ing)? (?:a |on )?(?:the )?gm\b|\bneeds? (?:the )?gm\b|\bgm (?:yes|ok)\b",
    re.IGNORECASE,
)

PrStatus = Callable[[str, int], Optional[str]]
ParentStatus = Callable[[str], Optional[str]]


def github_pr_status(repo: str, number: int, cache: Optional[dict] = None) -> Optional[str]:
    """merged, closed, open, or None when GitHub cannot be read. Never prints a token."""
    import json
    import subprocess
    key = (repo, int(number))
    if cache is not None and key in cache:
        return cache[key]
    proc = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", "state"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    state = None
    if proc.returncode == 0:
        raw = str((json.loads(proc.stdout or "{}")).get("state") or "").lower()
        state = raw if raw in ("merged", "closed", "open") else None
    if cache is not None:
        cache[key] = state
    return state


def ensure_schema(conn) -> None:
    """Create the wake table. Safe to call on every board, new or old.

    A board that already has the table is left alone, so a read-only pass
    can list it without writing.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_wakes'"
    ).fetchone()
    if row:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_wakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            parent_id TEXT,
            repo TEXT,
            pr_number INTEGER,
            until_ts INTEGER,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_wakes_task ON task_wakes(task_id)"
    )


def parse_until(spec: str) -> int:
    """ISO time to a unix second. A bare time is read as Central."""
    text = spec.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_central())
    return int(parsed.timestamp())


def parse_until_pr(spec: str) -> tuple[str, int]:
    """``owner/repo#number`` to a repo and a PR number."""
    if "#" not in spec:
        raise ValueError("until-pr must look like owner/repo#number")
    repo, number = spec.rsplit("#", 1)
    repo = repo.strip()
    if repo.count("/") != 1 or not number.strip().isdigit():
        raise ValueError("until-pr must look like owner/repo#number")
    return repo, int(number)


def record_wakes(
    conn,
    task_id: str,
    *,
    wait_on: tuple[str, ...] | list[str] = (),
    until_prs: tuple[tuple[str, int], ...] | list[tuple[str, int]] = (),
    until_ts: Optional[int] = None,
    link_parents: bool = True,
) -> list[str]:
    """Replace this card's wakes. No waits means ``wake=none``."""
    ensure_schema(conn)
    rows = _wake_rows(wait_on, until_prs, until_ts)
    _replace_rows(conn, task_id, rows)
    if link_parents:
        _link_parents(conn, task_id, wait_on)
    return [kind for kind, *_rest in rows]


def wake_kinds(conn, task_id: str) -> list[str]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT kind FROM task_wakes WHERE task_id=? ORDER BY id",
        (task_id,),
    ).fetchall()
    return [row["kind"] for row in rows]


def waits_met(
    conn,
    task_id: str,
    *,
    now: float,
    pr_status: PrStatus,
    parent_status: Optional[ParentStatus] = None,
) -> bool:
    """True only when every non-none wake is already met."""
    real = [row for row in _load_rows(conn, task_id) if row["kind"] != "none"]
    if not real:
        return False
    return all(_one_met(conn, row, now, pr_status, parent_status) for row in real)


def promote_ready(
    conn,
    *,
    now: float,
    pr_status: PrStatus,
    parent_status: Optional[ParentStatus] = None,
    lane_active: Optional[dict[str, int]] = None,
    lane_cap: int = LANE_CAP,
    max_per_pass: int = MAX_PER_PASS,
    skip_task: Optional[Callable[[str], bool]] = None,
    commit: bool = True,
    dispatch_profiles: Optional[set[str]] = None,
) -> dict[str, list[str]]:
    """Move scheduled cards to ready when their waits are met and the lane has room."""
    ensure_schema(conn)
    active = _lane_counts(conn, lane_active)
    allowed = None if dispatch_profiles is None else {name.lower() for name in dispatch_profiles}
    promoted: list[str] = []
    held: list[str] = []
    for card in _scheduled(conn):
        if len(promoted) >= max_per_pass:
            break
        if skip_task is not None and skip_task(card["id"]):
            continue
        outcome = _promote_one(
            conn, card, now, pr_status, parent_status, active, lane_cap, commit, allowed,
        )
        if outcome == "promoted":
            promoted.append(card["id"])
            active[card["assignee"]] = active.get(card["assignee"], 0) + 1
        elif outcome == "held":
            held.append(card["id"])
    return {"promoted": promoted, "held": held}


def archive_merged_gates(
    conn,
    *,
    pr_status: PrStatus,
    default_repo: str = "HudsonHawk7/Force_2.1",
) -> dict[str, list[str]]:
    """Archive a ``Gate PR N`` card once that PR is merged or closed."""
    ensure_schema(conn)
    archived: list[str] = []
    for card in _scheduled(conn):
        number = _gate_number(card["title"] or "")
        if number is None:
            continue
        repo = _gate_repo(conn, card["id"], default_repo)
        state = (pr_status(repo, number) or "").lower()
        if state not in MET_PR:
            continue
        _archive_gate(conn, card["id"], number, state)
        archived.append(card["id"])
    return {"archived": archived}


def backfill_task(conn, task_id: str, note: str) -> str:
    """Turn a park note into wakes and leave one comment. Returns the summary."""
    task = kb.get_task(conn, task_id)
    own_id = task.id if task else task_id
    parsed = parse_park_note(note or "", own_id=own_id)
    record_wakes(
        conn,
        task_id,
        wait_on=parsed["wait_on"],
        until_prs=parsed["until_prs"],
        until_ts=parsed["until_ts"],
    )
    summary = parsed["summary"]
    kb.add_comment(conn, task_id, AUTHOR, f"backfill: {summary}")
    return summary


def human_park_note(conn, task_id: str) -> str:
    """The park note promote, backfill, and the dry-run all read."""
    return _recent_human_note(conn, task_id)


def _backfill_note(conn, task_id: str, notes: Optional[dict[str, str]]) -> str:
    if notes and task_id in notes:
        return notes[task_id]
    return human_park_note(conn, task_id)


def _note_has_wait(task_id: str, note: str) -> bool:
    parsed = parse_park_note(note or "", own_id=task_id)
    return bool(parsed["wait_on"] or parsed["until_prs"] or parsed["until_ts"] is not None)


def backfill_scheduled(conn, notes: Optional[dict[str, str]] = None) -> list[str]:
    """Backfill scheduled cards from the human park note.

    A stored wake=none is not final. When that note has a real wait, replace
    the none row. A real stored wait is left alone.
    """
    ensure_schema(conn)
    done: list[str] = []
    for card in _scheduled(conn):
        kinds = wake_kinds(conn, card["id"])
        note = _backfill_note(conn, card["id"], notes)
        replace_none = kinds == ["none"] and _note_has_wait(card["id"], note)
        if kinds and not replace_none:
            continue
        summary = backfill_task(conn, card["id"], note)
        done.append(f"{card['id']} {summary}")
    return done


def daily_lines(
    conn,
    *,
    now: float,
    notes: Optional[dict[str, str]] = None,
    idle_seconds: int = IDLE_SECONDS,
) -> list[str]:
    """One line per card parked over 3 days with wake=none, or waiting on the GM.

    This lists. It does not ask.
    """
    ensure_schema(conn)
    lines: list[str] = []
    for card in _scheduled(conn):
        note = _daily_note(conn, card["id"], notes)
        if not _on_daily_list(conn, card, note, now, idle_seconds):
            continue
        title = (card["title"] or "").strip()
        waits = _wait_phrase(conn, card["id"], note)
        lines.append(f"{card['id']} {title} — {waits}")
    return lines


def _daily_note(conn, card_id: str, notes: Optional[dict[str, str]]) -> str:
    if notes and card_id in notes:
        return notes[card_id]
    return _recent_human_note(conn, card_id)


def parse_park_note(note: str, *, own_id: str) -> dict:
    """Read task ids, PR numbers and SCHEDULED_UNTIL out of a park note."""
    wait_on = []
    for found in TASK_ID_RE.findall(note or ""):
        if found.lower() != own_id.lower() and found not in wait_on:
            wait_on.append(found)
    until_prs = _prs_that_are_waits(note or "")
    until_ts = _until_from_note(note or "")
    return {
        "wait_on": wait_on,
        "until_prs": until_prs,
        "until_ts": until_ts,
        "summary": _summary(wait_on, until_prs, until_ts),
    }


def hold_reason(title: str, note: str) -> str:
    text = f"{title}\n{note}"
    if re.search(r"not for (?:agent )?dispatch|read-only mirror", text, re.IGNORECASE):
        return "not_for_dispatch"
    if HOLD_RE.search(text):
        return "deliberate_hold"
    return ""


def _central():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Chicago")
    except Exception:
        return timezone(timedelta(hours=-6))


def _wake_rows(wait_on, until_prs, until_ts) -> list[tuple]:
    now = int(time.time())
    rows: list[tuple] = [("parent", parent, None, None, None, now) for parent in wait_on]
    rows += [("pr", None, repo, int(number), None, now) for repo, number in until_prs]
    if until_ts is not None:
        rows.append(("time", None, None, None, int(until_ts), now))
    if not rows:
        rows.append(("none", None, None, None, None, now))
    return rows


def _replace_rows(conn, task_id: str, rows: list[tuple]) -> None:
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM task_wakes WHERE task_id=?", (task_id,))
        conn.executemany(
            "INSERT INTO task_wakes "
            "(task_id, kind, parent_id, repo, pr_number, until_ts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(task_id, *row) for row in rows],
        )


def _link_parents(conn, task_id: str, wait_on) -> None:
    for parent in wait_on:
        try:
            kb.link_tasks(conn, parent, task_id)
        except ValueError:
            continue


def _load_rows(conn, task_id: str) -> list:
    ensure_schema(conn)
    return list(conn.execute(
        "SELECT kind, parent_id, repo, pr_number, until_ts FROM task_wakes "
        "WHERE task_id=? ORDER BY id",
        (task_id,),
    ).fetchall())


def _one_met(conn, row, now, pr_status, parent_status) -> bool:
    kind = row["kind"]
    if kind == "parent":
        return _parent_state(conn, row["parent_id"], parent_status) in MET_PARENT
    if kind == "pr":
        state = (pr_status(row["repo"], int(row["pr_number"])) or "").lower()
        return state in MET_PR
    if kind == "time":
        return int(row["until_ts"]) <= int(now)
    return False


def _parent_state(conn, parent_id: str, parent_status: Optional[ParentStatus]) -> Optional[str]:
    if parent_status is not None:
        found = parent_status(parent_id)
        if found:
            return found
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (parent_id,)).fetchone()
    return row["status"] if row else None


def _scheduled(conn) -> list:
    return list(conn.execute(
        "SELECT id, title, assignee, priority, created_at FROM tasks "
        "WHERE status='scheduled' ORDER BY priority DESC, created_at ASC, id ASC"
    ).fetchall())


def _lane_counts(conn, given: Optional[dict[str, int]]) -> dict[str, int]:
    if given is not None:
        return dict(given)
    counts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT assignee, COUNT(*) AS n FROM tasks "
        "WHERE status IN ('ready', 'running', 'review') AND assignee IS NOT NULL "
        "GROUP BY assignee"
    ).fetchall()
    for row in rows:
        counts[row["assignee"]] = int(row["n"])
    return counts


def _promote_one(conn, card, now, pr_status, parent_status, active, lane_cap, commit, allowed) -> str:
    assignee = (card["assignee"] or "").strip()
    if not assignee:
        return ""
    if allowed is not None and assignee.lower() not in allowed:
        return ""
    note = _recent_human_note(conn, card["id"])
    if hold_reason(card["title"] or "", note) or GM_WAIT_RE.search(note or ""):
        return ""
    if not waits_met(conn, card["id"], now=now, pr_status=pr_status, parent_status=parent_status):
        return ""
    if active.get(assignee, 0) >= lane_cap:
        return "held"
    if not commit:
        return "promoted"
    reason = _describe(_load_rows(conn, card["id"]))
    kb.add_comment(conn, card["id"], AUTHOR, f"wake met: {reason}. Promoted to ready.")
    if not kb.unblock_task(conn, card["id"]):
        return ""
    return "promoted"


def _gate_number(title: str) -> Optional[int]:
    match = GATE_TITLE_RE.search(title or "")
    return int(match.group(1)) if match else None


def _gate_repo(conn, task_id: str, default_repo: str) -> str:
    for row in _load_rows(conn, task_id):
        if row["kind"] == "pr" and row["repo"]:
            return row["repo"]
    return default_repo


def _archive_gate(conn, task_id: str, number: int, state: str) -> None:
    kb.add_comment(conn, task_id, AUTHOR, f"superseded: Gate PR {number} is {state}")
    kb.archive_task(conn, task_id)


def _recent_human_note(conn, task_id: str) -> str:
    """Park notes from people, not our own backfill line. The newest backfill
    comment must not hide a 'do not promote' note underneath it."""
    rows = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 12",
        (task_id,),
    ).fetchall()
    kept: list[str] = []
    for row in rows:
        if (row["author"] or "") == AUTHOR:
            continue
        kept.append(row["body"] or "")
        if len(kept) >= 3:
            break
    return "\n".join(kept)


def _prs_that_are_waits(note: str) -> list[tuple[str, int]]:
    repo = _repo_for_note(note)
    found: list[tuple[str, int]] = []
    for sentence in _sentences(note or ""):
        # A landed phrase or a hold cancels only the PRs in this sentence.
        if ALREADY_RE.search(sentence) or re.search(r"do not promote", sentence, re.IGNORECASE):
            continue
        for match in PRS_LIST_RE.finditer(sentence):
            for number in re.findall(r"\d+", match.group(1)):
                item = (repo, int(number))
                if item not in found:
                    found.append(item)
    return found


def _sentences(note: str) -> list[str]:
    return [part for part in SENTENCE_SPLIT_RE.split(note) if part.strip()]


def _repo_for_note(note: str) -> str:
    low = note.lower()
    if "nousresearch" in low:
        return "NousResearch/hermes-agent"
    if "hermes-agent" in low or "hermes agent" in low:
        return "HudsonHawk7/hermes-agent"
    return "HudsonHawk7/Force_2.1"


def _until_from_note(note: str) -> Optional[int]:
    match = UNTIL_RE.search(note)
    if not match:
        return None
    return parse_until(match.group(1))


def _summary(wait_on, until_prs, until_ts) -> str:
    parts = [f"wait-on {item}" for item in wait_on]
    parts += [f"until-pr {repo}#{number}" for repo, number in until_prs]
    if until_ts is not None:
        parts.append(f"until {until_ts}")
    return ", ".join(parts) if parts else "wake=none"


def _describe(rows) -> str:
    return _summary(
        [row["parent_id"] for row in rows if row["kind"] == "parent"],
        [(row["repo"], row["pr_number"]) for row in rows if row["kind"] == "pr"],
        next((row["until_ts"] for row in rows if row["kind"] == "time"), None),
    )


def _parked_since(conn, card) -> int:
    row = conn.execute(
        "SELECT MAX(created_at) AS ts FROM task_events "
        "WHERE task_id=? AND kind='scheduled'",
        (card["id"],),
    ).fetchone()
    if row and row["ts"]:
        return int(row["ts"])
    return int(card["created_at"] or 0)


def _on_daily_list(conn, card, note: str, now: float, idle_seconds: int) -> bool:
    if int(now) - _parked_since(conn, card) < idle_seconds:
        return False
    kinds = wake_kinds(conn, card["id"])
    if kinds == ["none"] or not kinds:
        return True
    return bool(GM_WAIT_RE.search(note or ""))


def _wait_phrase(conn, task_id: str, note: str) -> str:
    kinds = wake_kinds(conn, task_id)
    if kinds == ["none"] or not kinds:
        if GM_WAIT_RE.search(note or ""):
            return "waiting on the GM"
        return "wake=none"
    return _describe(_load_rows(conn, task_id))
