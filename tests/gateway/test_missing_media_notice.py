"""A reply that names a media file which does not exist must say so in chat.

The gateway used to log ``Skipping MEDIA directive path (not found on this host)``
and drop the tag. The user saw the promise and no file. The outgoing message
must carry a plain notice instead, and the agent's next turn must be told the
file was never made. An eval session key is used on purpose — never a live
profile.
"""

import asyncio
import os
from types import SimpleNamespace

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource

MISSING_NOTICE = "That file doesn't exist; nothing was made."
EVAL_SESSION = "agent:eval:desktop:dm:eval"


class _EvalAdapter(BasePlatformAdapter):
    """Captures the text the gateway would send. Not a live profile."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.LOCAL)
        self.sent: list[str] = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        from gateway.platforms.base import SendResult
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {"name": "eval", "type": "dm"}


def _event() -> MessageEvent:
    return MessageEvent(
        text="Did you make a video from that last scene?",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.LOCAL, chat_id="eval-chat", chat_type="dm"),
        message_id="m-eval",
    )


def _outgoing(reply: str, runner):
    adapter = _EvalAdapter()
    adapter.gateway_runner = runner

    async def _run():
        return await adapter._extract_response_content(
            reply, _event(), EVAL_SESSION, is_ephemeral_response=False,
        )

    return asyncio.run(_run())


def test_missing_media_directive_renders_notice_and_queues_next_turn_note(tmp_path, caplog):
    missing = tmp_path / "beach_surfer_session_video.mp4"
    reply = (
        "Totally. I've got it ready for you here\n"
        f"MEDIA:{missing.as_posix()}"
    )
    runner = SimpleNamespace()
    with caplog.at_level("WARNING"):
        extracted = _outgoing(reply, runner)

    assert MISSING_NOTICE in extracted.text_content
    assert "MEDIA:" not in extracted.text_content
    assert missing.name not in extracted.text_content
    assert extracted.media_files == []
    note = getattr(runner, "_pending_missing_media_notes", {}).get(EVAL_SESSION)
    assert note and "does not exist" in note
    assert "not found on this host" in caplog.text


def test_real_media_file_still_sends(tmp_path):
    good = tmp_path / "real-clip.mp4"
    good.write_bytes(b"\x00\x00\x00\x18ftyp")
    reply = f"Here is the clip\nMEDIA:{good.as_posix()}"
    runner = SimpleNamespace()
    extracted = _outgoing(reply, runner)

    assert extracted.media_files and os.path.isfile(extracted.media_files[0][0])
    assert MISSING_NOTICE not in extracted.text_content
    assert not getattr(runner, "_pending_missing_media_notes", {})
