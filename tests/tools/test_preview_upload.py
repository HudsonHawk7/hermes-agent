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
    """An isolated profile home whose config names one picture folder for the image provider and the
    provider's screen-share frames folder, which is NOT an upload root (see the screenshots test)."""
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
        "video_gen.sogni.image_input_dir",
    ]


def test_screen_share_frames_are_never_an_upload_root(home):
    """``screenshots_dir`` holds one frame a second of whatever is on the user's screen — work screens,
    customer data. The image provider may read it because its only destination is that provider;
    ``upload`` hands a file to ANY site open in the pane, so for upload that folder would be an
    exfiltration path and the setting is refused unless the file is also inside an allowed folder."""
    frame = home["shots"] / "frame-0001.png"
    frame.write_bytes(PNG)

    assert all(folder != home["shots"] for _, folder in _roots(home))
    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(frame), _roots(home))

    assert "screenshots_dir" not in str(refused.value)
    assert "image_gen.sogni.image_input_dir" in str(refused.value)


def test_the_profile_s_retained_attachments_count_once_they_exist(home):
    native = home["home"] / "cache" / "documents" / "native-inputs"
    assert all(folder != native for _, folder in _roots(home))

    native.mkdir(parents=True)

    assert ("cache/documents/native-inputs", native) in _roots(home)


def test_a_populated_legacy_document_cache_is_the_retained_attachments_folder(home):
    """An older home keeps attachments under ``document_cache/`` (the gateway's own layout rule,
    ``hermes_constants.get_hermes_dir``); the new ``cache/documents`` path only wins once the legacy one is empty."""
    legacy = home["home"] / "document_cache" / "native-inputs"
    legacy.mkdir(parents=True)
    (legacy / "kept.png").write_bytes(PNG)

    roots = _roots(home)

    assert ("document_cache/native-inputs", legacy) in roots
    assert all(not str(folder).endswith(os.path.join("cache", "documents", "native-inputs")) for _, folder in roots)
    assert up.read_upload(str(legacy / "kept.png"), roots)["name"] == "kept.png"


def test_an_empty_legacy_document_cache_does_not_shadow_the_new_layout(home):
    (home["home"] / "document_cache").mkdir(parents=True)
    native = home["home"] / "cache" / "documents" / "native-inputs"
    native.mkdir(parents=True)

    assert ("cache/documents/native-inputs", native) in _roots(home)


def test_a_file_inside_an_allowed_folder_is_read_and_described(home):
    source = home["pictures"] / "judson.png"
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
    assert str(home["shots"]) not in str(refused.value)


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

    assert [Path(folder) for _, folder in up.configured_roots()] == [home["pictures"]]


# ── the fence: where the path may point ──────────────────────────────────────


def test_a_sibling_folder_sharing_the_allowed_folder_s_prefix_is_refused(home, tmp_path):
    """``pictures2`` starts with ``pictures``; only a path-aware check tells them apart."""
    sibling = tmp_path / "pictures2" / "judson.png"
    sibling.parent.mkdir()
    sibling.write_bytes(PNG)

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(sibling), _roots(home))

    assert "image_gen.sogni.image_input_dir" in str(refused.value)


def test_dot_dot_that_leaves_the_allowed_folder_is_refused(home, tmp_path):
    outside = tmp_path / "elsewhere" / "secret.png"
    outside.parent.mkdir()
    outside.write_bytes(PNG)
    text = os.path.join(str(home["pictures"]), "..", "elsewhere", "secret.png")

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(text, _roots(home))

    assert "image_gen.sogni.image_input_dir" in str(refused.value)


def test_dot_dot_that_stays_inside_the_allowed_folder_is_accepted(home):
    source = home["pictures"] / "judson.png"
    source.write_bytes(PNG)
    text = os.path.join(str(home["pictures"]), "sub", "..", "judson.png")

    assert up.read_upload(text, _roots(home))["name"] == "judson.png"


@pytest.mark.skipif(os.path.normcase("A") == "A", reason="the path case only differs where paths are case-insensitive")
def test_a_differently_cased_path_inside_the_allowed_folder_is_accepted(home):
    source = home["pictures"] / "judson.png"
    source.write_bytes(PNG)
    text = str(home["pictures"]).upper() + os.sep + "JUDSON.PNG"

    assert up.read_upload(text, _roots(home))["name"] == "judson.png"


def test_a_path_that_is_not_a_local_file_never_touches_the_filesystem(home, monkeypatch):
    """A link, a network or device path and a relative path are judged as text: on Windows even resolving
    ``\\\\host\\share`` opens an SMB connection, so none of them may reach a filesystem call."""
    def touched(*_args, **_kwargs):
        raise AssertionError("the filesystem was touched")

    cases = {
        "judson.png": "is not a full path",
        "https://example.com/judson.png": "is a link",
        r"\\nas\share\judson.png": "network and device paths",
        "//nas/share/judson.png": "network and device paths",
        r"\\?\C:\pictures\judson.png": "network and device paths",
        r"\\.\PhysicalDrive0": "network and device paths",
    }
    roots = _roots(home)
    for text, reason in cases.items():
        with monkeypatch.context() as patched:
            patched.setattr(Path, "resolve", touched)
            patched.setattr(os, "stat", touched)
            patched.setattr("builtins.open", touched)
            with pytest.raises(up.UploadRefused) as refused:
                up.read_upload(text, roots)
        assert reason in str(refused.value), text


# ── what the filesystem can throw back ───────────────────────────────────────


def test_a_nul_in_the_path_is_refused_not_raised(home):
    text = str(home["pictures"] / "jud\x00son.png")

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(text, _roots(home))

    assert "NUL" in str(refused.value)


def test_a_file_that_cannot_be_opened_is_refused_with_the_reason(home, monkeypatch):
    """Windows holds a file another program has open; ``open`` raises PermissionError and the agent is told."""
    source = home["pictures"] / "locked.png"
    source.write_bytes(PNG)
    locked = os.path.normcase(str(source.resolve()))
    real_open = open

    def open_unless_locked(file, *args, **kwargs):
        if os.path.normcase(str(file)) == locked:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", open_unless_locked)

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(source), _roots(home))

    assert "cannot be read" in str(refused.value)
    assert "being used by another process" in str(refused.value)


def test_a_file_whose_stat_fails_is_refused_with_the_reason(home, monkeypatch):
    source = home["pictures"] / "locked.png"
    source.write_bytes(PNG)
    locked = os.path.normcase(str(source.resolve()))
    real_stat = Path.stat

    def stat_unless_locked(self, *args, **kwargs):
        if os.path.normcase(str(self)) == locked:
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_unless_locked)

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(source), _roots(home))

    assert "cannot be read" in str(refused.value)
    assert "Permission denied" in str(refused.value)


def test_a_file_that_grows_past_the_cap_while_being_read_is_refused(home, monkeypatch):
    """``stat`` sizes the file before it is opened; the bytes actually read are what the page gets, so they
    are capped again after the read."""
    source = home["pictures"] / "growing.png"
    source.write_bytes(PNG)
    grown = os.path.normcase(str(source.resolve()))
    monkeypatch.setattr(up, "MAX_UPLOAD_BYTES", len(PNG) + 8)
    real_open = open

    class Grown:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, _limit=-1):
            return PNG + b"\x00" * 16

    def open_grown(file, *args, **kwargs):
        if os.path.normcase(str(file)) == grown:
            return Grown()
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", open_grown)

    with pytest.raises(up.UploadRefused) as refused:
        up.read_upload(str(source), _roots(home))

    assert "read more than" in str(refused.value)
