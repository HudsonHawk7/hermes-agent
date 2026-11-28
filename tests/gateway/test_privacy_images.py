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


def test_sweep_deletes_only_staged_privacy_images(tmp_path: Path):
    """A home tree keeps its own module and notes; only staged privacy pictures go."""
    module = tmp_path / "hermes-agent" / "gateway" / "privacy_images.py"
    module.parent.mkdir(parents=True)
    module.write_text("def sweep_privacy_images(root):\n    pass\n", encoding="utf-8")
    staged = tmp_path / "profiles" / "x" / "cache" / "images" / "privacy_ab12.png"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"png")
    home_png = tmp_path / "cache" / "images" / "privacy_home.jpg"
    home_png.parent.mkdir(parents=True)
    home_png.write_bytes(b"jpg")
    notes = tmp_path / "privacy_notes.txt"
    notes.write_text("keep", encoding="utf-8")
    outside_png = tmp_path / "backups" / "privacy_old.png"
    outside_png.parent.mkdir()
    outside_png.write_bytes(b"png")
    decoy_py = staged.parent / "privacy_module.py"
    decoy_py.write_text("nope", encoding="utf-8")
    decoy_txt = staged.parent / "privacy_notes.txt"
    decoy_txt.write_text("keep", encoding="utf-8")
    kept_img = staged.parent / "img_girl.png"
    kept_img.write_bytes(b"img")

    swept = sweep_privacy_images(tmp_path)

    assert str(staged) in swept
    assert str(home_png) in swept
    assert not staged.exists()
    assert not home_png.exists()
    assert module.read_text(encoding="utf-8").startswith("def sweep")
    assert notes.read_text(encoding="utf-8") == "keep"
    assert outside_png.read_bytes() == b"png"
    assert decoy_py.read_text(encoding="utf-8") == "nope"
    assert decoy_txt.read_text(encoding="utf-8") == "keep"
    assert kept_img.read_bytes() == b"img"
    assert all(Path(item).parent.name == "images" for item in swept)


def test_boot_sweep_passes_home_and_stays_in_cache_folders(tmp_path: Path):
    """Boot hands the home to the sweep, and that sweep does not leave the caches."""
    source = (Path(__file__).resolve().parents[2] / "gateway" / "run_bootstrap.py").read_text(
        encoding="utf-8"
    )
    needle = "sweep_privacy_images(get_hermes_home())"
    assert needle in source
    window = source[source.index(needle): source.index(needle) + 400]
    assert "logger.warning(" in window
    assert "logger.debug(" not in window

    module = tmp_path / "hermes-agent" / "gateway" / "privacy_images.py"
    module.parent.mkdir(parents=True)
    module.write_text("module", encoding="utf-8")
    staged = tmp_path / "profiles" / "x" / "cache" / "images" / "privacy_ab12.png"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"png")
    swept = sweep_privacy_images(tmp_path)
    assert swept == [str(staged)]
    assert module.exists()
