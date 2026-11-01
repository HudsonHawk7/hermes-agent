"""The file half of ``drive_preview`` action='upload': which local files may be handed to a page.

Every test builds its own Hermes home under ``tmp_path``; the real one is never read.
"""

import base64
import os
import sys
from pathlib import Path

import pytest

from tools import preview_upload as up

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 28
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24


@pytest.fixture
def home(tmp_path):
    """An isolated profile home whose config names one picture folder and one screenshot folder."""
    pictures = tmp_path / "pictures"
    shots = tmp_path / "shots"
    pictures.mkdir()
    shots.mkdir()
    cfg = {
        "image_gen": {
            "provider": "sogni",
            "sogni": {"image_input_dir": str(pictures), "screenshots_dir": str(shots)},
        },
    }
    return {"home": tmp_path / "hermes", "cfg": cfg, "pictures": pictures, "shots": shots}


def _roots(fixture):
    return up.upload_roots(fixture["cfg"], fixture["home"])


def test_roots_come_from_every_configured_provider_folder(home):
    home["cfg"]["video_gen"] = {"sogni": {"image_input_dir": str(home["pictures"])}, "provider": "sogni"}
    roots = _roots(home)

    assert [name for name, _ in roots] == [
        "image_gen.sogni.image_input_dir",
        "image_gen.sogni.screenshots_dir",
        "video_gen.sogni.image_input_dir",
    ]


def test_the_profile_s_retained_attachments_count_once_they_exist(home):
    native = home["home"] / "cache" / "documents" / "native-inputs"
    assert all(folder != native for _, folder in _roots(home))

    native.mkdir(parents=True)

    assert (up.NATIVE_INPUTS, native) in _roots(home)


def test_a_file_inside_an_allowed_folder_is_read_and_described(home):
    source = home["shots"] / "judson.png"
    source.write_bytes(PNG)

    got = up.read_upload(str(source), _roots(home))

    assert got == {"name": "judson.png", "mime": "image/png", "size": len(PNG), "data": base64.b64encode(PNG).decode()}


def test_a_video_inside_an_allowed_folder_is_accepted(home):
    source = home["pictures"] / "clip.mp4"
    source.write_bytes(MP4)

    assert up.read_upload(str(source), _roots(home))["mime"] == "video/mp4"


def test_a_file_outside_every_allowed_folder_is_refused_naming_them(home, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "secret.png"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(PNG)

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(elsewhere), _roots(home))

    assert "image_gen.sogni.image_input_dir" in str(refused.value)
    assert str(home["pictures"]) in str(refused.value)
    assert str(home["shots"]) in str(refused.value)


def test_a_relative_path_a_url_and_a_network_path_never_reach_the_filesystem(home):
    for text in ("judson.png", "https://example.com/judson.png", r"\\nas\share\judson.png", "//nas/share/judson.png"):
        with pytest.raises(up.UploadRefused):
            up.read_upload(text, _roots(home))


def test_a_symlink_escaping_the_folder_is_refused(home, tmp_path):
    real = tmp_path / "outside.png"
    real.write_bytes(PNG)
    link = home["pictures"] / "looks-inside.png"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(link), _roots(home))

    assert "image_gen.sogni.image_input_dir" in str(refused.value)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows reparse point")
def test_a_junction_escaping_the_folder_is_refused(home, tmp_path):
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "judson.png").write_bytes(PNG)
    _winapi.CreateJunction(str(outside), str(home["pictures"] / "jump"))

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(home["pictures"] / "jump" / "judson.png"), _roots(home))

    assert "image_gen.sogni.image_input_dir" in str(refused.value)


def test_an_unknown_extension_is_refused_before_the_file_is_opened(home):
    source = home["pictures"] / "notes.txt"
    source.write_bytes(b"hello")

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(source), _roots(home))

    assert ".png" in str(refused.value)


def test_an_extension_that_lies_about_its_bytes_is_refused(home):
    source = home["pictures"] / "really-a-jpeg.png"
    source.write_bytes(JPEG)

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(source), _roots(home))

    assert "not a PNG" in str(refused.value)


def test_an_oversize_file_is_refused_without_being_read(home, monkeypatch):
    source = home["pictures"] / "huge.png"
    source.write_bytes(PNG)
    monkeypatch.setattr(up, "MAX_UPLOAD_BYTES", len(PNG) - 1)

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(source), _roots(home))

    assert "larger than" in str(refused.value)


def test_a_missing_file_inside_an_allowed_folder_is_named(home):
    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(home["pictures"] / "nope.png"), _roots(home))

    assert "nope.png" in str(refused.value)


def test_no_configured_folder_refuses_and_names_the_setting(tmp_path):
    roots = up.upload_roots({"image_gen": {"provider": "sogni", "sogni": {"model": "x"}}}, tmp_path / "hermes")

    assert roots == []
    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(tmp_path / "judson.png"), roots)

    assert "image_gen.<provider>.image_input_dir" in str(refused.value)


def test_a_folder_setting_that_is_not_text_is_ignored(tmp_path):
    cfg = {"image_gen": {"sogni": {"image_input_dir": None, "screenshots_dir": 3}}, "video_gen": "off"}

    assert up.upload_roots(cfg, tmp_path) == []


def test_configured_roots_read_the_running_profile(home, monkeypatch):
    """The live seam: ``load_config`` and ``get_hermes_home`` of the running profile, nothing else."""
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: home["cfg"])
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home["home"])

    assert [Path(folder) for _, folder in up.configured_roots()] == [home["pictures"], home["shots"]]
