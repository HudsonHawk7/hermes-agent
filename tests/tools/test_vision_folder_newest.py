"""vision_analyze on a local folder loads only the folder's newest image.

Watching a camera roll or screenshots folder used to mean listing it first (hundreds of
file names re-sent on every later turn) or guessing a file name. A folder source now
resolves to its newest image by modified time, and the result names the file it picked.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from io import BytesIO
from unittest.mock import patch

import pytest

from tools.vision_tools import _handle_vision_analyze, _run_analysis

_COLORS = {"z_oldest.png": (255, 0, 0), "m_middle.png": (0, 255, 0), "a_newest.png": (0, 0, 255)}


def _png_bytes(color) -> bytes:
    from PIL import Image

    out = BytesIO()
    Image.new("RGB", (4, 4), color).save(out, format="PNG")
    return out.getvalue()


def _write(path, data: bytes, mtime: int):
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def camera_roll(tmp_path):
    """Names sort the opposite way to age, so a name-ordered pick would choose the oldest."""
    roll = tmp_path / "camera-roll"
    roll.mkdir()
    _write(roll / "z_oldest.png", _png_bytes(_COLORS["z_oldest.png"]), 1_000)
    _write(roll / "m_middle.png", _png_bytes(_COLORS["m_middle.png"]), 2_000)
    _write(roll / "a_newest.png", _png_bytes(_COLORS["a_newest.png"]), 3_000)
    _write(roll / "notes.txt", b"newer, but not an image", 4_000)
    (roll / "subfolder").mkdir()
    return roll


def _run(coro):
    return asyncio.run(coro)


def _embedded_color(result: dict):
    from PIL import Image

    url = next(p["image_url"]["url"] for p in result["content"] if p.get("type") == "image_url")
    data = base64.b64decode(url.split(",", 1)[1])
    return Image.open(BytesIO(data)).convert("RGB").getpixel((0, 0))


def test_native_path_embeds_only_the_newest_image_and_names_it(camera_roll):
    with patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=True):
        result = _run(_handle_vision_analyze({"image_url": str(camera_roll), "question": "what do you see?"}))

    assert isinstance(result, dict) and result.get("_multimodal") is True, str(result)[:300]
    images = [p for p in result["content"] if p.get("type") == "image_url"]
    assert len(images) == 1
    assert _embedded_color(result) == _COLORS["a_newest.png"]
    text = next(p["text"] for p in result["content"] if p.get("type") == "text")
    assert "a_newest.png" in text
    assert "z_oldest.png" not in text and "m_middle.png" not in text
    assert "a_newest.png" in result["text_summary"]


def test_aux_path_gets_the_newest_file_and_its_note(camera_roll):
    captured = {}

    async def _aux(image_url, prompt, model=None, **kwargs):
        captured.update(image_url=image_url, **kwargs)
        return json.dumps({"success": True, "analysis": "ok"})

    with patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=False), \
            patch("tools.vision_tools.vision_analyze_tool", side_effect=_aux):
        _run(_handle_vision_analyze({"image_url": str(camera_roll), "question": "?"}))

    assert captured["image_url"] == str(camera_roll / "a_newest.png")
    assert "a_newest.png" in captured["source_note"]


def test_folder_with_no_images_returns_an_error_not_a_listing(tmp_path):
    empty = tmp_path / "empty-roll"
    empty.mkdir()
    (empty / "readme.txt").write_text("no pictures here")

    with patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=True):
        result = _run(_handle_vision_analyze({"image_url": str(empty), "question": "?"}))

    payload = json.loads(result)
    assert payload["success"] is False
    assert "No images in folder" in payload["error"]


def test_file_url_and_data_sources_are_not_treated_as_folders(camera_roll):
    from tools.vision_tools import _newest_image_in_folder

    assert _newest_image_in_folder(str(camera_roll / "a_newest.png")) is None
    assert _newest_image_in_folder("https://example.com/camera-roll/") is None
    assert _newest_image_in_folder("data:image/png;base64,AAAA") is None
    assert _newest_image_in_folder(f"file://{camera_roll}").path == str(camera_roll / "a_newest.png")


def test_non_local_backend_leaves_folder_sources_alone(camera_roll):
    from tools.vision_tools import _newest_image_in_folder

    with patch("tools.image_source._is_local_terminal_backend", return_value=False):
        assert _newest_image_in_folder(str(camera_roll)) is None


def test_aux_result_json_carries_the_source_note():
    async def _stage(prompt, debug_call_data, temp_paths):
        return "a blue square", None

    payload = json.loads(_run(_run_analysis("image", "x", "q", None, _stage, source_note="Newest image in x: a.png")))
    assert payload["success"] is True
    assert payload["source"] == "Newest image in x: a.png"
