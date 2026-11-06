"""Keep T: mapped after the Time Capsule's file sharing drops.

The sign-in mapper runs once. This watcher lives in the gateway (no scheduled
task, no window) and runs the same mapper again when sharing is back and T:
is missing. A share that stays down for 15 minutes raises one GM one-tap and
withdraws it by itself when the box answers.

Tests inject the probe and the mapper. The real share is never contacted
from a test run.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

INTERVAL_SECONDS = 300
DOWN_ASK_SECONDS = 15 * 60
_CT = ZoneInfo("America/Chicago")

# The card's launch line. %LOCALAPPDATA% is cmd syntax; the launcher expands
# it in Python and still uses Start-Process explorer.exe so the map lands in
# the interactive session.
MAPPER_LAUNCH_COMMAND = (
    "Start-Process explorer.exe '\"%LOCALAPPDATA%\\hermes\\map-timecaps-T.vbs\"'"
)

POWER_CYCLE_ASK = {
    "question": "Power-cycle the Time Capsule?",
    "context": (
        "File sharing has been down for 15 minutes. "
        "T: cannot come back until the box is power-cycled."
    ),
    "options": [
        {
            "key": "yes",
            "label": "I power-cycled it",
            "effect": "Watcher maps T: again once sharing answers",
        },
        {
            "key": "no",
            "label": "Not now",
            "effect": "T: stays unmapped until sharing answers on its own",
        },
    ],
    "pick": "yes",
}

RECOVERY_COMMENT = (
    "Time Capsule is answering again. T: is being mapped. No action needed."
)

_DRIVE_TOKEN = __import__("re").compile(r"(?i)(?<![A-Za-z0-9])T:")
_BOX_NAME = __import__("re").compile(r"(?i)time\s*capsule|timecaps")


@dataclass
class WatchState:
    drop_started_at: float | None = None
    ask_raised: bool = False
    ask_task_id: str | None = None
    commented_ids: list[str] = field(default_factory=list)


@dataclass
class TickResult:
    state: WatchState
    log_lines: list[str] = field(default_factory=list)


def blocked_on_timecaps(title: str, body: str, reason: str) -> bool:
    """True when a blocked card is waiting on this share, not on some other colon."""
    text = f"{title or ''}\n{body or ''}\n{reason or ''}"
    return bool(_DRIVE_TOKEN.search(text) or _BOX_NAME.search(text))


def format_drop_line(start: float, end: float) -> str:
    """One line: start, end, and length. Times are Central."""
    return (
        f"drop start={_ct_stamp(start)} end={_ct_stamp(end)} "
        f"length={_length_label(end - start)}"
    )


def tick(
    state: WatchState,
    *,
    now: float,
    port_open: bool,
    drive_present: bool,
    launch_mapper: Callable[[], str],
    raise_ask: Callable[[dict], str],
    withdraw_ask: Callable[[str], bool],
    list_blocked: Callable[[], list[dict]],
    comment: Callable[[str, str], None],
) -> TickResult:
    """One pass. Mutates ``state`` and returns it.

    A closed share starts a drop and never launches the mapper (that can hang).
    At 15 minutes, one ask, never a second. When the share answers, the ask is
    withdrawn, T: is mapped, and each blocked card is commented once.
    """
    log_lines: list[str] = []
    recovering = state.drop_started_at is not None and port_open
    if recovering:
        start = float(state.drop_started_at)
        log_lines.append(format_drop_line(start, now))
        ask_id = state.ask_task_id
        if ask_id:
            withdraw_ask(ask_id)
        for card in list_blocked() or []:
            card_id = str(card.get("id") or "")
            if not card_id or card_id == ask_id or card_id in state.commented_ids:
                continue
            if not blocked_on_timecaps(
                str(card.get("title") or ""),
                str(card.get("body") or ""),
                str(card.get("reason") or ""),
            ):
                continue
            comment(card_id, RECOVERY_COMMENT)
            state.commented_ids.append(card_id)
        log_lines.append(f"remap {launch_mapper()}")
        state.drop_started_at = None
        state.ask_raised = False
        state.ask_task_id = None
        state.commented_ids = []
        return TickResult(state=state, log_lines=log_lines)

    if not port_open:
        started = state.drop_started_at if state.drop_started_at is not None else now
        state.drop_started_at = started
        elapsed = now - started
        if elapsed >= DOWN_ASK_SECONDS and not state.ask_raised:
            task_id = str(raise_ask(POWER_CYCLE_ASK) or "")
            if task_id:
                state.ask_raised = True
                state.ask_task_id = task_id
                log_lines.append(f"ask {task_id}")
        return TickResult(state=state, log_lines=log_lines)

    if not drive_present:
        log_lines.append(f"remap {launch_mapper()}")
    return TickResult(state=state, log_lines=log_lines)


def run_timecaps_watch_once(
    *,
    now: Optional[float] = None,
    port_open: Optional[bool] = None,
    drive_present: Optional[bool] = None,
    state_path: Optional[Path] = None,
    log_path: Optional[Path] = None,
    launch_mapper: Optional[Callable[[], str]] = None,
    raise_ask: Optional[Callable[[dict], str]] = None,
    withdraw_ask: Optional[Callable[[str], bool]] = None,
    list_blocked: Optional[Callable[[], list[dict]]] = None,
    comment: Optional[Callable[[str, str], None]] = None,
) -> str:
    """One gateway pass. Remembers the drop on disk so a later pass does not ask twice.

    Calling this with no seams talks to the real share. A test run refuses that.
    """
    needs_real = any(
        value is None
        for value in (
            port_open,
            drive_present,
            launch_mapper,
            raise_ask,
            withdraw_ask,
            list_blocked,
            comment,
            state_path,
            log_path,
        )
    )
    if needs_real and _under_test():
        raise RuntimeError("timecaps watch refuses the real share under tests")

    # No local config: do nothing. Guessing a host, or treating "unconfigured"
    # as a drop, would raise a one-tap for a box this install does not have.
    if port_open is None and not _share_endpoint()[0]:
        return "ok"

    now = time.time() if now is None else now
    state_file = Path(state_path) if state_path is not None else _default_state_path()
    log_file = Path(log_path) if log_path is not None else _default_log_path()
    state = _load_state(state_file)
    try:
        if port_open is None:
            port_open = probe_port_open()
        if drive_present is None:
            # A closed share must not be touched. Listing it can stall for minutes.
            drive_present = probe_drive_present() if port_open else False
        result = tick(
            state,
            now=now,
            port_open=port_open,
            drive_present=drive_present,
            launch_mapper=launch_mapper or launch_mapper_in_session,
            raise_ask=raise_ask or raise_power_cycle_ask,
            withdraw_ask=withdraw_ask or withdraw_power_cycle_ask,
            list_blocked=list_blocked or list_blocked_cards,
            comment=comment or comment_on_card,
        )
    finally:
        _save_state(state_file, state)
    _append_log(log_file, result.log_lines)
    return _summary(result)


def probe_port_open(timeout: float = 3.0) -> bool:
    """True when the configured share host accepts a TCP connection.

    The host lives in the local watcher config, not in this file. No config
    means not open, and the caller skips the pass instead of guessing.
    """
    host, port = _share_endpoint()
    if not host:
        return False
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_drive_present(timeout: float = 5.0) -> bool:
    """True when T: is visible in this process's session. Times out rather than hang."""
    if os.name != "nt":
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["cmd", "/c", "if exist T:\\ exit 0"],
            timeout=timeout,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def launch_mapper_in_session() -> str:
    """Run the existing mapper via explorer.exe. No console window."""
    if os.name != "nt":
        return "not windows"
    local = os.environ.get("LOCALAPPDATA", "").strip()
    script = os.path.join(local, "hermes", "map-timecaps-T.vbs") if local else ""
    if not script or not os.path.isfile(script):
        return "mapper script missing"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command = (
        "Start-Process -FilePath explorer.exe -ArgumentList '"
        + script.replace("'", "''")
        + "'"
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                command,
            ],
            timeout=30,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"launch failed {exc}"
    if completed.returncode != 0:
        err = (completed.stderr or b"").decode("utf-8", "replace").strip()
        return f"launch failed {completed.returncode} {err[:160]}".strip()
    return "launched"


def raise_power_cycle_ask(ask: dict) -> str:
    """File the one-tap on one reused card. An already-open ask is not filed again."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_cli.kanban_db_gm_asks import get_open_gm_ask, record_gm_ask

    conn = connect()
    try:
        task_id = kb.create_task(
            conn,
            title=str(ask.get("question") or POWER_CYCLE_ASK["question"]),
            body=(
                "Raised by the Time Capsule watcher. "
                "It withdraws this by itself when sharing answers."
            ),
            created_by="timecaps-watch",
            assignee=None,
            initial_status="blocked",
            idempotency_key="timecaps-power-cycle",
            workspace_kind="scratch",
            completion_contract="local-only",
            priority=50,
        )
        if get_open_gm_ask(conn, task_id) is not None:
            return task_id
        record_gm_ask(
            conn,
            task_id,
            ask,
            asked_by="timecaps-watch",
            reason=str(ask.get("question") or ""),
            force_park=True,
        )
        return task_id
    finally:
        conn.close()


def withdraw_power_cycle_ask(task_id: str) -> bool:
    from hermes_cli.kanban_db_connect import connect
    from hermes_cli.kanban_db_gm_asks import withdraw_gm_ask

    conn = connect()
    try:
        return withdraw_gm_ask(
            conn,
            task_id,
            reason="Sharing is answering again. This one-tap is withdrawn.",
        )
    finally:
        conn.close()


def list_blocked_cards() -> list[dict]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_cli.kanban_db_gm_asks import _latest_block_reason

    conn = connect()
    try:
        cards = []
        for task in kb.list_tasks(conn, status="blocked"):
            cards.append(
                {
                    "id": task.id,
                    "title": task.title or "",
                    "body": task.body or "",
                    "reason": _latest_block_reason(conn, task.id) or "",
                }
            )
        return cards
    finally:
        conn.close()


def comment_on_card(card_id: str, text: str) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    conn = connect()
    try:
        kb.add_comment(conn, card_id, "timecaps-watch", text)
    finally:
        conn.close()


def _under_test() -> bool:
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("HERMES_TEST_ISOLATION")
    )


def _install_root() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "hermes"
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _share_endpoint() -> tuple[str, int]:
    """Host and port from the local config. Empty host means this install is not configured."""
    path = _install_root() / "timecaps-watch.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return "", 445
    if not isinstance(raw, dict):
        return "", 445
    host = str(raw.get("host") or "").strip()
    try:
        port = int(raw.get("port") or 445)
    except (TypeError, ValueError):
        port = 445
    if port < 1 or port > 65535:
        port = 445
    return host, port


def _default_state_path() -> Path:
    return _install_root() / "logs" / "timecaps-watch-state.json"


def _default_log_path() -> Path:
    return _install_root() / "logs" / "map-timecaps.log"


def _ct_stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, _CT).strftime("%Y-%m-%d %H:%M:%S CT")


def _length_label(seconds: float) -> str:
    total = max(0, round(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if secs == 0 and hours == 0:
        return f"{minutes}m"
    if secs == 0 and minutes == 0:
        return f"{hours}h"
    if secs == 0:
        return f"{hours}h{minutes}m"
    if hours == 0:
        return f"{minutes}m{secs}s"
    return f"{hours}h{minutes}m{secs}s"


def _load_state(path: Path) -> WatchState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return WatchState()
    if not isinstance(raw, dict):
        return WatchState()
    started = raw.get("drop_started_at")
    commented = raw.get("commented_ids") or []
    return WatchState(
        drop_started_at=float(started) if isinstance(started, (int, float)) else None,
        ask_raised=bool(raw.get("ask_raised")),
        ask_task_id=str(raw["ask_task_id"]) if raw.get("ask_task_id") else None,
        commented_ids=[str(item) for item in commented if item],
    )


def _save_state(path: Path, state: WatchState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "drop_started_at": state.drop_started_at,
        "ask_raised": state.ask_raised,
        "ask_task_id": state.ask_task_id,
        "commented_ids": list(state.commented_ids),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _append_log(path: Path, lines: list[str]) -> None:
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _summary(result: TickResult) -> str:
    for line in result.log_lines:
        if line.startswith("drop ") or line.startswith("ask ") or line.startswith("remap "):
            return line
    if result.state.drop_started_at is not None:
        return "down"
    return "ok"
