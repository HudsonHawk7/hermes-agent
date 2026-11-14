"""AC2 wire: a group picture's privacy flag must reach the handler, not 4001.

Desktop always sends privacy as the string 'true' or 'false' on image.attach_bytes.
The canonical dispatcher refuses an undeclared key before session_images.attach_bytes.
"""
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch
from tui_gateway.contracts.canonical_projections import ImageAttachBytesParams
from tui_gateway.contracts.registry import CANONICAL_METHODS, canonical_param_problems

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nL8AAAAASUVORK5CYII='
)


def test_privacy_string_is_a_declared_attach_bytes_param():
    contract = CANONICAL_METHODS['image.attach_bytes']
    assert contract.params is ImageAttachBytesParams
    for privacy in ('true', 'false', True, False, None):
        params = {
            'session_id': 's',
            'content_base64': base64.b64encode(PNG).decode(),
            'filename': 'shot.png',
            'privacy': privacy,
        }
        problems = canonical_param_problems(contract, params)
        assert problems == [], problems


@pytest.mark.asyncio
async def test_privacy_string_reaches_the_handler_not_4001(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    home = root / 'profiles' / 'probe'
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setattr('hermes_constants.get_default_hermes_root', lambda: root)
    with SessionDB(db_path=home / 'state.db') as db:
        db.create_session('s', source='test')
        authority = SessionAuthority(
            SimpleNamespace(_draining=False),
            profile_id=str(home),
            instance_id='current',
            db=db,
            epoch=begin_runtime_epoch(db, instance_id='current'),
        )
        owner = AuthorityConnection(
            authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'}
        )
        live = authority.sessions['s'] = LiveSession(
            SimpleNamespace(platform=Platform.LOCAL, user_id=owner.actor.subject), 'route'
        )
        owner.subscriptions['s'] = 'test-viewer'
        live.subscribers['test-viewer'] = owner.actor
        params = {
            'session_id': 's',
            'content_base64': base64.b64encode(PNG).decode(),
            'filename': 'shot.png',
            'profile': 'probe',
        }

        private = await owner.dispatch({
            'id': 1,
            'method': 'image.attach_bytes',
            'params': {**params, 'privacy': 'true'},
        })
        assert private.get('error', {}).get('code') != 4001, private
        assert 'result' in private, private
        private_path = Path(private['result']['path'])
        assert private_path.name.startswith('privacy_'), private
        assert private_path.read_bytes() == PNG

        plain = await owner.dispatch({
            'id': 2,
            'method': 'image.attach_bytes',
            'params': {**params, 'privacy': 'false'},
        })
        assert plain.get('error', {}).get('code') != 4001, plain
        assert 'result' in plain, plain
        assert Path(plain['result']['path']).name.startswith('upload_'), plain
        await owner.close()
