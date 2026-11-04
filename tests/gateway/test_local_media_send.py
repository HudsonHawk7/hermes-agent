"""Local (Desktop) platform native image/MEDIA send.

Before this fix, LocalSessionAdapter inherited base send_image_file, which only
posted a fallback \"couldn't send\" notice through the no-op send() and logged
``native image send unavailable``. Cron MEDIA also depended on the companion
copying script paths into her reply.

These unit tests pin:
1. Valid on-disk image → send_image_file succeeds (no fallback notice path).
2. Missing file → named error published into the live session stream.
3. Cron inbound MEDIA tags are auto-appended even when the reply omits/mangles them.
4. Post-stream delivery reports dropped paths via report_media_drop on Local.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner, _collect_cron_inbound_media_tags
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.session_local import LocalSessionAdapter


class _FakeStream:
    def __init__(self):
        self.frames = []

    def publish(self, session_id, payload, *, event_type="message.complete", execution=None):
        self.frames.append({"session_id": session_id, "event_type": event_type, "payload": payload})


def _allowed_png(tmp_path, monkeypatch, name="shot.png"):
    root = tmp_path / "media-cache"
    media_file = root / name
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr("gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS", (root,))
    return media_file.resolve()


def _local_adapter_with_stream(chat_id="local-chat"):
    stream = _FakeStream()
    live = SimpleNamespace(event_stream=stream, source=SimpleNamespace(chat_id=chat_id))
    authority = SimpleNamespace(sessions={chat_id: live})
    adapter = LocalSessionAdapter(authority)
    return adapter, stream, chat_id


@pytest.mark.asyncio
async def test_local_send_image_file_succeeds_for_existing_file(tmp_path, monkeypatch):
    """Native Local image send: existing allowed file → success, no fallback notice."""
    path = _allowed_png(tmp_path, monkeypatch)
    adapter, stream, chat_id = _local_adapter_with_stream()

    result = await adapter.send_image_file(chat_id, str(path))

    assert result.success is True
    assert result.error is None
    # Valid files rely on MEDIA tags already in the transcript; no extra delta spam.
    assert stream.frames == []


@pytest.mark.asyncio
async def test_local_send_image_file_names_missing_file_in_chat(tmp_path):
    """Missing path → failure + named reason published into the live session stream."""
    missing = tmp_path / "gone.png"
    adapter, stream, chat_id = _local_adapter_with_stream()

    result = await adapter.send_image_file(chat_id, str(missing))

    assert result.success is False
    assert "gone.png" in (result.error or "")
    assert "not found" in (result.error or "").lower()
    assert len(stream.frames) == 1
    frame = stream.frames[0]
    assert frame["event_type"] == "message.delta"
    text = frame["payload"]["text"]
    assert "gone.png" in text
    assert "Couldn't show the image" in text


@pytest.mark.asyncio
async def test_local_send_image_file_no_live_session_still_fails_cleanly(tmp_path):
    """No live stream: still return a named failure (log-only publish path)."""
    missing = tmp_path / "orphan.png"
    authority = SimpleNamespace(sessions={})
    adapter = LocalSessionAdapter(authority)

    result = await adapter.send_image_file("no-session", str(missing))

    assert result.success is False
    assert "orphan.png" in (result.error or "")


def test_collect_cron_inbound_media_tags_from_bot_chat_admission(tmp_path, monkeypatch):
    """Script MEDIA on a Cronjob user message is collected for auto-append."""
    path = _allowed_png(tmp_path, monkeypatch, "cron1.png")
    content = (
        f'[Cronjob "Daily Photos" output — scheduled job, not the user. '
        f"Review it, act on anything that needs action, and summarize for the chat.]\n\n"
        f"Here are today's shots.\nMEDIA:{path}\n"
    )
    messages = [
        {"role": "user", "content": "old turn"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": content},
        {"role": "assistant", "content": "Nice set."},
    ]
    tags = _collect_cron_inbound_media_tags(messages, history_offset=2, history_media_paths=set())
    assert tags == [f"MEDIA:{path}"]


def test_collect_cron_inbound_media_tags_ignores_ordinary_user_media(tmp_path, monkeypatch):
    """Ordinary user messages with MEDIA are not treated as cron script output."""
    path = _allowed_png(tmp_path, monkeypatch, "user.png")
    messages = [{"role": "user", "content": f"look MEDIA:{path}"}]
    assert _collect_cron_inbound_media_tags(messages, history_offset=0) == []


def test_append_auto_media_tags_merges_cron_when_reply_omits(tmp_path, monkeypatch):
    """Companion omits MEDIA → cron paths still land on the final response."""
    path = _allowed_png(tmp_path, monkeypatch, "garden.png")
    cron_user = (
        f'[Cronjob "Sexy Time Prep" output — scheduled job, not the user. '
        f"Review it and summarize.]\n\nMEDIA:{path}\n"
    )
    result = {
        "messages": [
            {"role": "user", "content": cron_user},
            {"role": "assistant", "content": "Your set is ready."},
        ],
        "final_response": "Your set is ready.",
    }
    runner = TurnRunner.__new__(TurnRunner)
    out = runner._append_auto_media_tags(
        "Your set is ready.", result, agent_history=[], history_media_paths=set(),
    )
    assert f"MEDIA:{path}" in out
    assert out.startswith("Your set is ready.")


def test_append_auto_media_tags_merges_when_reply_has_wrong_media(tmp_path, monkeypatch):
    """Companion mangles one path but cron still supplies the real ones."""
    good = _allowed_png(tmp_path, monkeypatch, "real.png")
    bad = tmp_path / "typo-real.png"  # does not need to exist
    cron_user = (
        f'[Cronjob "Photos" output — scheduled job, not the user. Summarize.]\n\n'
        f"MEDIA:{good}\n"
    )
    result = {
        "messages": [
            {"role": "user", "content": cron_user},
            {"role": "assistant", "content": f"MEDIA:{bad}"},
        ],
        "final_response": f"Here you go\nMEDIA:{bad}",
    }
    runner = TurnRunner.__new__(TurnRunner)
    out = runner._append_auto_media_tags(
        result["final_response"], result, agent_history=[], history_media_paths=set(),
    )
    assert f"MEDIA:{good}" in out
    assert f"MEDIA:{bad}" in out  # keep what she wrote; deliver path still filters it


@pytest.mark.asyncio
async def test_post_stream_dropped_path_uses_local_report_media_drop(tmp_path):
    """Filter-dropped MEDIA gets a named Local chat notice via report_media_drop."""
    missing = tmp_path / "missing-cron.png"
    adapter, stream, chat_id = _local_adapter_with_stream(chat_id="C-local")
    adapter.extract_media = BasePlatformAdapter.extract_media
    adapter.extract_images = BasePlatformAdapter.extract_images
    adapter.send_multiple_images = AsyncMock(return_value=SendResult(success=True))
    adapter.send_voice = AsyncMock()
    adapter.send_video = AsyncMock()
    adapter.send_document = AsyncMock()

    source = SessionSource(platform=Platform.LOCAL, chat_id=chat_id, chat_type="dm")
    event = MessageEvent(text="hi", message_type=MessageType.TEXT, source=source, message_id="1")

    class _FakeRunner:
        def _thread_metadata_for_source(self, source, anchor=None):
            return {}

        def _reply_anchor_for_event(self, event):
            return None

        async def _report_dropped_media_paths(self, *args, **kwargs):
            return await GatewayRunner._report_dropped_media_paths(self, *args, **kwargs)

    await GatewayRunner._deliver_media_from_response(
        _FakeRunner(),
        f"Ready\nMEDIA:{missing}",
        event,
        adapter,
    )

    # No successful image send for the missing file.
    adapter.send_multiple_images.assert_not_awaited()
    assert any(
        "missing-cron.png" in (frame["payload"].get("text") or "")
        and "Couldn't show the image" in (frame["payload"].get("text") or "")
        for frame in stream.frames
    )


@pytest.mark.asyncio
async def test_base_fallback_not_used_for_local_existing_file(tmp_path, monkeypatch):
    """Regression: Local must not take the base 'native image send unavailable' path."""
    path = _allowed_png(tmp_path, monkeypatch, "ok.png")
    adapter, _stream, chat_id = _local_adapter_with_stream()
    # Spy: if base fallback ran it would call emit_media_warning / send.
    adapter.emit_media_warning = AsyncMock(
        return_value=SendResult(success=False, error="fallback"),
    )
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="x"))

    result = await adapter.send_image_file(chat_id, str(path), caption="hi")

    assert result.success is True
    adapter.emit_media_warning.assert_not_awaited()
    adapter.send.assert_not_awaited()
