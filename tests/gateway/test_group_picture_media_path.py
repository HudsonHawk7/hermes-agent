"""AC8: a girl's MEDIA:/S:/ path must resolve to the drive path, not a URL."""

import os

from gateway.platforms.base import _normalize_media_tag_path


def test_windows_drive_media_tag_drops_the_leading_slash():
    normalized = _normalize_media_tag_path("/S:/renders/scene.png")
    if os.name == "nt":
        assert normalized == "S:/renders/scene.png"
    else:
        # The slash is only a Windows drive prefix. Other platforms keep it.
        assert normalized == "/S:/renders/scene.png"
