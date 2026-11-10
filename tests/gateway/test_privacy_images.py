"""AC2: a privacy picture is deleted at turn end and on the next start.

Girls' img_ files are not privacy files and must survive the sweep.
"""

import hashlib
from pathlib import Path

from gateway.privacy_images import (
    files_with_hash,
    release_session_privacy_images,
    sweep_privacy_images,
)
from tui_gateway.prompt_attachments import stage_image_bytes


def test_privacy_stage_is_gone_after_release_and_sweep_spares_img(tmp_path: Path):
    picture = b"privacy-picture-bytes"
    digest = hashlib.sha256(picture).hexdigest()
    staged = stage_image_bytes(tmp_path / "images", picture, ".png", prefix="privacy")
    kept = stage_image_bytes(tmp_path / "images", b"girl-picture", ".png", prefix="img")
    session = {"session_id": "room-1", "attached_images": [str(staged)]}

    from gateway.privacy_images import note_privacy_staged

    note_privacy_staged(session, str(staged), "room-1")
    released = release_session_privacy_images(session, "room-1")

    assert str(staged) in released
    assert not staged.exists()
    assert kept.exists()
    assert files_with_hash(tmp_path, digest) == []

    leftover = tmp_path / "cache" / "images" / "privacy_crash.png"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(picture)
    swept = sweep_privacy_images(tmp_path)

    assert str(leftover) in swept
    assert not leftover.exists()
    assert kept.exists()
    assert files_with_hash(tmp_path, digest) == []
