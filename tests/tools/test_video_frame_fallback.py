"""video_analyze / vision_analyze sample frames when the model cannot take video.

The girls' local Ollama rejects a video part with ``400 invalid message format``.
Telling the model to run ffmpeg itself did not work, so the tool does it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.vision_tools import (
    _handle_vision_analyze,
    video_analyze_tool,
    vision_analyze_tool,
)

# Structurally valid 1x1 PNG. Image-path tests must not be rejected as non-images.
_VALID_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Minimal JPEG magic. The frame path base64s the file; it does not decode it.
_TINY_JPEG = b"\xff\xd8\xff\xd9"


def _ok_response(text: str = "A person at a desk."):
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=text))]
    return response


def _install_ffmpeg_stub(monkeypatch):
    """Replace the subprocess seam when the fix is present.

    On the base tree the seam does not exist yet. The assertions still fail
    because the tool returns the old hint instead of a frames analysis.
    """
    try:
        import tools.vision_video_frames as frames
    except ImportError:
        return
    if not hasattr(frames, "run_ffmpeg"):
        return

    def fake_run(args, timeout=60):
        argv = [str(part) for part in args]
        if "-show_entries" in argv:
            return subprocess.CompletedProcess(argv, 0, b"12.0\n", b"")
        out = Path(argv[-1])
        if out.suffix.lower() in {".jpg", ".jpeg", ".png"}:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(_TINY_JPEG)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(
            argv, 1, b"", b"Duration: 00:00:12.00, start: 0.000000, bitrate: 1 kb/s"
        )

    monkeypatch.setattr(frames, "run_ffmpeg", fake_run)
    if hasattr(frames, "ffmpeg_bin"):
        monkeypatch.setattr(frames, "ffmpeg_bin", lambda: "ffmpeg")
    if hasattr(frames, "ffprobe_bin"):
        monkeypatch.setattr(frames, "ffprobe_bin", lambda: "ffprobe")


def _hide_ffmpeg(monkeypatch):
    try:
        import tools.vision_video_frames as frames
    except ImportError:
        return
    if hasattr(frames, "ffmpeg_bin"):
        monkeypatch.setattr(frames, "ffmpeg_bin", lambda: None)
    if hasattr(frames, "ffprobe_bin"):
        monkeypatch.setattr(frames, "ffprobe_bin", lambda: None)


def _patch_extract(monkeypatch):
    """Pull the mock's content the same way the real extractor would."""
    def fake_extract(response, **_kwargs):
        try:
            text = response.choices[0].message.content
        except Exception:
            return ""
        return text or ""
    monkeypatch.setattr("tools.vision_tools.extract_content_or_reasoning", fake_extract)


def _content_types(kwargs) -> list:
    content = kwargs["messages"][0]["content"]
    return [part.get("type") for part in content if isinstance(part, dict)]


def test_invalid_message_format_returns_frame_analysis(tmp_path, monkeypatch):
    """A provider 400 'invalid message format' must come back as a frames analysis."""
    video = tmp_path / "office_call.mp4"
    video.write_bytes(b"\x00" * 64)
    ollama_400 = RuntimeError(
        "Error code: 400 - {'error': {'message': 'invalid message format'}}"
    )
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        if "video_url" in _content_types(kwargs):
            raise ollama_400
        return _ok_response("A person at a desk on a call.")

    _install_ffmpeg_stub(monkeypatch)
    _patch_extract(monkeypatch)
    with patch("tools.vision_tools.async_call_llm", new=fake_llm):
        payload = json.loads(asyncio.run(video_analyze_tool(str(video), "What happens?")))

    assert payload["success"] is True, payload
    analysis = payload["analysis"]
    assert "A person at a desk on a call." in analysis
    assert "analyzed 6 frames at t=" in analysis
    assert len(calls) == 2
    assert "video_url" in _content_types(calls[0])
    assert "video_url" not in _content_types(calls[1])
    assert _content_types(calls[1]).count("image_url") == 6


def test_local_route_skips_the_video_part(tmp_path, monkeypatch):
    """A local OpenAI-compatible route is known to reject video parts. Don't send one."""
    video = tmp_path / "office_call.mp4"
    video.write_bytes(b"\x00" * 64)
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        return _ok_response("Frames only.")

    _install_ffmpeg_stub(monkeypatch)
    _patch_extract(monkeypatch)
    monkeypatch.setattr(
        "tools.vision_tools._aux_route_rejects_video_parts", lambda: True
    )
    with patch("tools.vision_tools.async_call_llm", new=fake_llm):
        payload = json.loads(asyncio.run(video_analyze_tool(str(video), "What happens?")))

    assert payload["success"] is True, payload
    assert "analyzed 6 frames at t=" in payload["analysis"]
    assert len(calls) == 1
    assert "video_url" not in _content_types(calls[0])
    assert _content_types(calls[0]).count("image_url") == 6


def test_native_fast_path_attaches_frames(tmp_path, monkeypatch):
    video = tmp_path / "office_call.mp4"
    video.write_bytes(b"\x00" * 64)
    ollama_400 = RuntimeError("Error code: 400 - invalid message format")

    async def fake_llm(**kwargs):
        if "video_url" in _content_types(kwargs):
            raise ollama_400
        raise AssertionError("native path must not send frames to the aux model")

    _install_ffmpeg_stub(monkeypatch)
    monkeypatch.setattr(
        "tools.vision_tools._should_use_native_vision_fast_path", lambda: True
    )
    with patch("tools.vision_tools.async_call_llm", new=fake_llm):
        result = asyncio.run(video_analyze_tool(str(video), "What happens?"))

    assert isinstance(result, dict), result
    assert result.get("_multimodal") is True
    types = [part.get("type") for part in result["content"] if isinstance(part, dict)]
    assert types.count("image_url") == 6
    text = " ".join(
        part.get("text", "") for part in result["content"] if part.get("type") == "text"
    )
    assert "analyzed 6 frames at t=" in text


def test_vision_analyze_mp4_goes_to_frames(tmp_path, monkeypatch):
    video = tmp_path / "office_call.mp4"
    video.write_bytes(b"\x00" * 64)
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        return _ok_response("Still frames of the call.")

    _install_ffmpeg_stub(monkeypatch)
    _patch_extract(monkeypatch)
    with patch("tools.vision_tools.async_call_llm", new=fake_llm):
        payload = json.loads(
            asyncio.run(_handle_vision_analyze({
                "image_url": str(video),
                "question": "What is in this?",
            }))
        )

    assert payload["success"] is True, payload
    assert "not a recognized image" not in json.dumps(payload)
    assert "analyzed 6 frames at t=" in payload["analysis"]
    assert calls and "video_url" not in _content_types(calls[0])
    assert _content_types(calls[0]).count("image_url") == 6


def test_vision_analyze_tool_mp4_goes_to_frames(tmp_path, monkeypatch):
    video = tmp_path / "clip.webm"
    video.write_bytes(b"\x00" * 32)
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        return _ok_response("Webm frames.")

    _install_ffmpeg_stub(monkeypatch)
    _patch_extract(monkeypatch)
    with patch("tools.vision_tools.async_call_llm", new=fake_llm):
        payload = json.loads(asyncio.run(vision_analyze_tool(str(video), "Describe.")))

    assert payload["success"] is True, payload
    assert "not a recognized image" not in json.dumps(payload)
    assert "analyzed 6 frames at t=" in payload["analysis"]
    assert calls and _content_types(calls[0]).count("image_url") == 6


def test_missing_ffmpeg_is_a_clear_error(tmp_path, monkeypatch):
    video = tmp_path / "office_call.mp4"
    video.write_bytes(b"\x00" * 64)
    ollama_400 = RuntimeError("Error code: 400 - invalid message format")

    async def fake_llm(**kwargs):
        if "video_url" in _content_types(kwargs):
            raise ollama_400
        raise AssertionError("missing ffmpeg must not send a frames request")

    _hide_ffmpeg(monkeypatch)
    with patch("tools.vision_tools.async_call_llm", new=fake_llm):
        payload = json.loads(asyncio.run(video_analyze_tool(str(video), "What happens?")))

    assert payload["success"] is False, payload
    error = payload.get("error") or ""
    assert "ffmpeg is not on PATH" in error
    assert "extract a few frames with ffmpeg and look at them with vision_analyze" not in error


def test_image_path_does_not_sample_frames(tmp_path, monkeypatch):
    image = tmp_path / "still.png"
    image.write_bytes(_VALID_PNG)
    seam_calls = []
    try:
        import tools.vision_video_frames as frames

        if hasattr(frames, "run_ffmpeg"):
            monkeypatch.setattr(
                frames,
                "run_ffmpeg",
                lambda *args, **kwargs: seam_calls.append(args)
                or subprocess.CompletedProcess(args, 1, b"", b"should not run"),
            )
    except ImportError:
        pass

    with patch(
        "tools.vision_tools.async_call_llm",
        new_callable=AsyncMock,
        return_value=_ok_response("A png."),
    ), patch("tools.vision_tools.extract_content_or_reasoning", side_effect=lambda response, **_k: response.choices[0].message.content):
        payload = json.loads(asyncio.run(vision_analyze_tool(str(image), "Describe.")))

    assert payload["success"] is True, payload
    assert "A png." in payload["analysis"]
    assert "frames at t=" not in payload["analysis"]
    assert seam_calls == []


def test_read_guard_runs_before_ffmpeg(tmp_path, monkeypatch):
    secret = tmp_path / ".env"
    secret.write_text("OPENAI_API_KEY=«redacted:sk-…»\n", encoding="utf-8")
    disguised = tmp_path / "clip.mp4"
    try:
        disguised.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    seam_calls = []
    try:
        import tools.vision_video_frames as frames

        if hasattr(frames, "run_ffmpeg"):
            monkeypatch.setattr(
                frames,
                "run_ffmpeg",
                lambda *args, **kwargs: seam_calls.append(args),
            )
    except ImportError:
        pass

    with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock) as mock_llm:
        payload = json.loads(asyncio.run(video_analyze_tool(str(disguised), "What is in this?")))

    assert payload["success"] is False
    assert "secret-bearing environment file" in payload["error"]
    mock_llm.assert_not_awaited()
    assert seam_calls == []
