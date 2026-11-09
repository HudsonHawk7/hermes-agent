"""A lost managed worker names the kanban run, and drops to INFO once that run ended.

HTS-035: the pipe EOF from a card's worker pair used to log a nameless WARNING.
An ended run is normal teardown (INFO). A run still marked running is a crash
(WARNING). Both lines carry the task id, run id, and profile.
"""

import asyncio
import json
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import gateway.session_managed_worker as managed


class _ExitedPair:
    """The card's process pair, already gone: the next read is a pipe EOF."""

    def __init__(self):
        self.stop = threading.Event()
        self.secrets = []
        self.closed = False

    async def next_frame(self, *args, **kwargs):
        raise EOFError('managed_worker_pipe_closed')

    def close(self):
        self.closed = True


def _board(path, *, status, ended_at):
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE task_runs ('
        'id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT, '
        'status TEXT NOT NULL, ended_at INTEGER)')
    conn.execute(
        'INSERT INTO task_runs (id, task_id, profile, status, ended_at) '
        'VALUES (?,?,?,?,?)',
        (7, 't_deadbeef', 'hera', status, ended_at),
    )
    conn.commit()
    conn.close()


async def _drive(policy):
    worker = _ExitedPair()

    async def fake_text(authority, row, bound):
        return 'go', None

    async def fake_spawn(authority, ref, row):
        authority._managed_workers = {ref.session_id: worker}
        return worker, SimpleNamespace(pid=1)

    authority = SimpleNamespace(
        adopt_agent=lambda *args, **kwargs: None,
        pending_stops={},
    )
    ref = SimpleNamespace(session_id='s1')
    row = {'admission_id': 'adm-1', 'generation': 1}
    # Patched per call so the two cases do not share a worker.
    import gateway.session_context_refs as refs
    original_text = refs.managed_turn_text
    original_spawn = managed._spawn_registered_worker
    refs.managed_turn_text = fake_text
    managed._spawn_registered_worker = fake_spawn
    try:
        with pytest.raises(EOFError):
            await managed.execute_managed(authority, ref, row, policy)
    finally:
        refs.managed_turn_text = original_text
        managed._spawn_registered_worker = original_spawn
    assert worker.closed


@pytest.mark.parametrize(
    ('status', 'ended_at', 'level', 'state'),
    [
        ('done', 1_700_000_000, logging.INFO, 'ended'),
        ('running', None, logging.WARNING, 'running'),
    ],
)
def test_lost_pair_names_the_run(tmp_path, caplog, status, ended_at, level, state):
    db = tmp_path / 'kanban.db'
    _board(db, status=status, ended_at=ended_at)
    policy = SimpleNamespace(kanban_json=json.dumps({
        'task_id': 't_deadbeef',
        'run_id': 7,
        'profile': 'hera',
        'db': str(db),
    }))
    caplog.set_level(logging.INFO, logger='gateway.session_managed_worker')
    asyncio.run(_drive(policy))
    lost = [r for r in caplog.records if r.message.startswith('Managed worker lost:')]
    assert len(lost) == 1, [r.message for r in caplog.records]
    rec = lost[0]
    expected = (
        'Managed worker lost: EOFError task_id=t_deadbeef run_id=7 '
        f'profile=hera state={state}')
    assert rec.message == expected, rec.message
    assert rec.levelno == level
