"""Media path errors tell the model what is actually wrong.

On 11/07 Solara asked for `S:/.../aura/office_call.mp4`, a path that did not exist (the file was
under renders/movie_10/). read_file answered "Cannot read binary file", which implies the file is
there; video_analyze answered "Invalid video source". She retried the same wrong path four times.
Once she had the right path, her local Ollama model rejected the video part with a 400
"invalid message format", and the error text gave her no way forward.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from tools.file_tools import read_file_tool
from tools.vision_tools import video_analyze_tool


def test_read_file_on_a_missing_video_path_says_not_found(tmp_path):
    missing = tmp_path / "aura" / "office_call.mp4"

    result = json.loads(read_file_tool(str(missing), task_id="media-missing-read"))

    assert "Cannot read binary file" not in result.get("error", "")
    assert "not found" in result.get("error", "").lower()


def test_read_file_on_an_existing_video_still_points_at_video_analyze(tmp_path):
    video = tmp_path / "office_call.mp4"
    video.write_bytes(b"\x00" * 64)

    result = json.loads(read_file_tool(str(video), task_id="media-binary-read"))

    assert "Cannot read binary file" in result["error"]
    assert "video_analyze" in result["error"]


def test_video_analyze_on_a_missing_local_path_says_not_found(tmp_path):
    missing = tmp_path / "aura" / "office_call.mp4"

    with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock) as llm:
        payload = json.loads(asyncio.run(video_analyze_tool(str(missing), "What happens?")))

    assert payload["success"] is False
    assert "video file not found" in payload["error"]
    assert "office_call.mp4" in payload["error"]
    llm.assert_not_awaited()


def test_local_model_rejecting_video_parts_is_sampled_as_frames(tmp_path, monkeypatch):
    """A 400 invalid message format is no longer a hint. The tool samples frames."""
    video = tmp_path / "office_call.mp4"
    video.write_bytes(b"\x00" * 64)
    ollama_400 = RuntimeError(
        "Error code: 400 - {'error': {'message': 'invalid message format', 'type': 'invalid_request_error'}}")

    async def fake_llm(**kwargs):
        content = kwargs["messages"][0]["content"]
        types = [part.get("type") for part in content if isinstance(part, dict)]
        if "video_url" in types:
            raise ollama_400
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="Frames of the call."))]
        return response

    from tests.tools.test_video_frame_fallback import _install_ffmpeg_stub
    _install_ffmpeg_stub(monkeypatch)
    monkeypatch.setattr(
        "tools.vision_tools.extract_content_or_reasoning",
        lambda response, **_k: response.choices[0].message.content,
    )
    with patch("tools.vision_tools.async_call_llm", new=fake_llm):
        payload = json.loads(asyncio.run(video_analyze_tool(str(video), "What happens?")))

    assert payload["success"] is True, payload
    assert "analyzed 6 frames at t=" in payload["analysis"]
    assert "extract a few frames with ffmpeg and look at them with vision_analyze" not in payload["analysis"]
