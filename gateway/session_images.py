"""Staging-only image uploads on the authenticated session authority."""
import asyncio
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError


async def attach_bytes(connection, ref, params):
    if (not isinstance(ref.session_id, str) or not ref.session_id
            or set(params) - {'session_id', 'profile', 'content_base64', 'data', 'filename', 'ext', 'privacy'}):
        raise RuntimeStoreError('invalid_params')
    privacy = params.get('privacy')
    if privacy not in (None, True, False, 'true', 'false', '1', '0'):
        raise RuntimeStoreError('invalid_params')
    string_params = {key: value for key, value in params.items() if key != 'privacy'}
    if any(not isinstance(value, str) for value in string_params.values()):
        raise RuntimeStoreError('invalid_params')
    if ref.session_id not in connection.subscriptions:
        raise RuntimeStoreError('permission_denied')
    # The declared ``profile`` selector names this owner's own home or is refused (dokterdok N73).
    from gateway.session_busy_controls import authorize
    authorize(connection, ref, params, 'session:submit')
    # The owner, never the caller or ambient process profile, selects staging.
    directory = Path(connection.authority.profile_id) / 'cache' / 'images'
    return await asyncio.to_thread(_stage, directory, params, ref.session_id)


def _stage(directory, params, session_id=None):
    from tui_gateway.prompt_attachments import (
        _ATTACH_BYTES_MAX_BYTES, _decode_attach_base64, _sniff_image_ext, stage_image_bytes,
    )
    data = _decode_attach_base64(params.get('content_base64') or params.get('data') or '', mime_prefix='image/')
    if not data or len(data) > _ATTACH_BYTES_MAX_BYTES:
        raise RuntimeStoreError('invalid_params')
    hint = params.get('ext', '').strip().lower().lstrip('.')
    ext = _sniff_image_ext(data, params.get('filename') or (f'x.{hint}' if hint else ''))
    mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.gif': 'image/gif', '.webp': 'image/webp'}.get(ext)
    if mime is None:
        raise RuntimeStoreError('invalid_params')
    privacy = params.get('privacy') in (True, 'true', '1')
    try:
        path = stage_image_bytes(directory, data, ext, prefix='privacy' if privacy else 'upload')
    except OSError as exc:
        raise RuntimeStoreError('storage_unavailable') from exc
    if privacy:
        from gateway.privacy_images import note_privacy_staged
        note_privacy_staged(None, str(path), session_id=session_id)
    return {'attached': True, 'path': str(path), 'mime': mime, 'name': path.name,
            'count': 1, 'remainder': '', 'text': f'[User attached image: {path.name}]', 'bytes': len(data)}
