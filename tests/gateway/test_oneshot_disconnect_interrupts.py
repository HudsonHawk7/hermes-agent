"""A killed ``hermes chat --oneshot`` client must stop its running turn.

The measured failure: the client dies on timeout and ``AuthorityConnection.close``
only detaches. The turn stays ``started``, keeps calling tools, and holds the GPU.
Desktop (``source='gui'``) is not oneshot. Its client disconnects so it can
reattach, and that turn must keep running.
"""
import json

import pytest

from tests.gateway.test_session_follow_profile_config import _owner

pytestmark = pytest.mark.usefixtures("authority_cwd_in_tmp")  # symbolic profile ids are relative homes

CAPS = ['session:create', 'session:read', 'session:submit', 'session:control',
        'session:approve', 'session:respond']


class _StopAgent:
    def __init__(self):
        self.calls = []

    def interrupt(self, message=None):
        self.calls.append(message)


def _keep_bootstrap_off_the_live_gitdir(monkeypatch, home):
    """A linked worktree's git dir lives inside the live install. Bootstrap stats it on import."""
    import hermes_cli._early_recovery as recovery
    git = home / "fake-git"
    git.mkdir(exist_ok=True)
    monkeypatch.setattr(recovery, "_git_dir", lambda root: git)


def _native(authority, user):
    from gateway.session_controls import AuthorityConnection
    return AuthorityConnection(authority, object(), {
        'user_id': user, 'provider': 'local', 'profile_id': authority.profile_id,
        'instance_id': authority.instance_id, 'capabilities': CAPS, 'native_bootstrap': True})


async def _rpc(connection, method, **params):
    return await connection.dispatch({'id': method, 'method': method, 'params': params})


def _stamp_running(authority, session_id):
    """The drain is not running in this test. A started row is what ``_handle`` calls running.

    This tree's authority stores rows on persistence, not the force/carry ``.db`` handle.
    """
    from persistence.sqlite.authority._admission_store import list_session_admissions
    from tests.fixtures.storage_holder import raw_holder
    from tests.fixtures.storage_read import session_row
    holder = raw_holder(authority.persistence)
    rows = list_session_admissions(holder, session_id=session_id)
    assert rows and rows[0]['status'] == 'queued', rows
    session = session_row(authority.persistence, session_id)
    assert session is not None, session_id
    generation = session['runtime_generation'] + 1
    admission_id = rows[0]['admission_id']

    def write(conn):
        conn.execute('UPDATE sessions SET runtime_generation=? WHERE id=?', (generation, session_id))
        changed = conn.execute(
            "UPDATE session_admissions SET status='started',owner_epoch=?,generation=? "
            "WHERE admission_id=? AND status='queued'",
            (authority.epoch, generation, admission_id))
        if changed.rowcount != 1:
            raise AssertionError('could not stamp the turn started')

    holder._execute_write(write)
    from gateway.session_contract import SessionRef
    handle = authority._handle(SessionRef(authority.profile_id, session_id))
    assert handle.execution_state == 'running', handle
    agent = _StopAgent()
    authority.adopted[session_id] = (handle.execution_generation, agent)
    return handle, agent


async def _running_session(connection, authority, home, source, request_id):
    created = await _rpc(connection, 'session.create', request_id=request_id, source=source,
                         cwd=str(home), toolsets=[])
    assert 'result' in created, created
    session_id = created['result']['session_id']
    from tests.fixtures.storage_read import session_row
    row = session_row(authority.persistence, session_id)
    assert row is not None and row['source'] == source
    submitted = await _rpc(connection, 'prompt.submit', session_id=session_id,
                           input_id=request_id + '-turn', text='keep going')
    assert 'result' in submitted, submitted
    handle, agent = _stamp_running(authority, session_id)
    return session_id, handle, agent


@pytest.mark.asyncio
async def test_oneshot_disconnect_interrupts_the_running_turn(monkeypatch):
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(json.dumps({'model': {'provider': 'custom', 'default': 'm',
                                                            'base_url': 'http://127.0.0.1:1/v1'}}))
    _keep_bootstrap_off_the_live_gitdir(monkeypatch, home)
    store, _runner, authority, _dashboard = _owner(home)
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    client = _native(authority, 'me')
    try:
        session_id, handle, agent = await _running_session(client, authority, home, 'oneshot', 'oneshot')
        await client.close()
        assert agent.calls, 'a killed oneshot client left its turn running'
        from gateway.session_contract import SessionRef
        after = authority._handle(SessionRef(authority.profile_id, session_id))
        assert after.execution_generation == handle.execution_generation
    finally:
        await client.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_desktop_disconnect_leaves_the_turn_running_for_reattach(monkeypatch):
    from gateway.session_contract import SessionRef
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(json.dumps({'model': {'provider': 'custom', 'default': 'm',
                                                            'base_url': 'http://127.0.0.1:1/v1'}}))
    _keep_bootstrap_off_the_live_gitdir(monkeypatch, home)
    store, _runner, authority, _dashboard = _owner(home)
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    client = _native(authority, 'me')
    try:
        session_id, handle, agent = await _running_session(client, authority, home, 'gui', 'desktop')
        await client.close()
        assert agent.calls == [], 'a desktop disconnect must not stop the turn'
        still = authority._handle(SessionRef(authority.profile_id, session_id))
        assert still.execution_state == 'running'
        assert still.execution_generation == handle.execution_generation
        again = _native(authority, 'me')
        resumed = await _rpc(again, 'session.resume', session_id=session_id)
        assert resumed['result']['running'] is True, resumed
        assert resumed['result']['execution_generation'] == handle.execution_generation
        await again.close()
    finally:
        await client.close()
        store.close_all_db_handles()
