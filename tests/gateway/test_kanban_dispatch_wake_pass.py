"""The dispatcher tick calls the wake pass.

A met wait promotes. An unmet wait stays parked. A dry tick writes nothing.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake as kw
from hermes_cli import kanban_wake_tick as wake_tick


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _park(conn, title, assignee="architect"):
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    assert kb.schedule_task(conn, task_id, reason="parked for the wake test")
    return task_id


def _status(folder: Path, task_id: str) -> str:
    conn = sqlite3.connect(folder / "kanban.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return row["status"]


@contextmanager
def _open_board(folder: Path, board=None):
    conn = sqlite3.connect(folder / "kanban.db")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _bind(monkeypatch, folder: Path):
    mod = wake_tick._pass()
    monkeypatch.setattr(mod, "board_dirs", lambda: [folder])
    monkeypatch.setattr(mod, "board_connect", lambda board=None: _open_board(folder))
    monkeypatch.setattr(mod, "host_dispatch_profiles", lambda: {"architect"})
    monkeypatch.setattr(mod, "load_daily_state", dict)
    monkeypatch.setattr(mod, "save_daily_state", lambda state: None)


def _copy_board(folder: Path) -> None:
    folder.mkdir(parents=True)
    src = Path(kb.kanban_db_path())
    (folder / "kanban.db").write_bytes(src.read_bytes())
    for suffix in ("-wal", "-shm"):
        extra = Path(str(src) + suffix)
        if extra.is_file():
            (folder / f"kanban.db{suffix}").write_bytes(extra.read_bytes())


def _dispatcher():
    settings = _DispatcherSettings(
        interval=60,
        max_spawn=0,
        max_in_progress=None,
        failure_limit=2,
        stale_timeout_seconds=0,
        reconcile_orphans=False,
        default_assignee=None,
        max_in_progress_per_profile=None,
    )
    dispatcher = _KanbanDispatcher(kb, settings)
    return dispatcher


def _tick(monkeypatch, dispatcher):
    monkeypatch.setattr(dispatcher, "tick_once_for_board", lambda slug: None)
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["chat-rescue"])
    dispatcher.tick_once()


def test_dispatch_tick_promotes_a_met_wait(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="finished", assignee="aurora")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.commit()
        child = _park(conn, "ready once the parent is done")
        kw.record_wakes(conn, child, wait_on=[parent])
    folder = tmp_path / "boards" / "chat-rescue"
    _copy_board(folder)
    _bind(monkeypatch, folder)
    _tick(monkeypatch, _dispatcher())
    assert _status(folder, child) == "ready"


def test_dispatch_tick_leaves_an_unmet_wait_parked(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="still running", assignee="aurora")
        child = _park(conn, "waits on the parent")
        kw.record_wakes(conn, child, wait_on=[parent])
    folder = tmp_path / "boards" / "chat-rescue"
    _copy_board(folder)
    _bind(monkeypatch, folder)
    _tick(monkeypatch, _dispatcher())
    assert _status(folder, child) == "scheduled"


def test_dry_dispatch_wake_does_not_promote(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="finished", assignee="aurora")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.commit()
        child = _park(conn, "ready once the parent is done")
        kw.record_wakes(conn, child, wait_on=[parent])
    folder = tmp_path / "boards" / "chat-rescue"
    _copy_board(folder)
    _bind(monkeypatch, folder)
    line = wake_tick.wake_on_dispatch_tick(apply=False)
    assert line.endswith("apply=0")
    assert _status(folder, child) == "scheduled"
