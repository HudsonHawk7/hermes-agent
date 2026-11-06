"""A local companion chat is trimmed, never wiped, when compression is exhausted."""
import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.run_turn_persistence import LOCAL_EXHAUSTION_TRIM_NOTE, trim_oldest_middle_turns
from gateway.session import SessionEntry, SessionSource
from gateway.session_lifecycle import _now


def _chat():
    messages = [
        {"role": "system", "content": "You are the eval companion."},
        {"role": "user", "content": "OLD PICTURE PROMPT draw the castle"},
        {"role": "assistant", "content": "old reply"},
    ]
    for i in range(6):
        messages.append({"role": "user", "content": f"middle {i}"})
        messages.append({"role": "assistant", "content": f"middle reply {i}"})
    messages.append({"role": "user", "content": "recent question about the garden"})
    messages.append({"role": "assistant", "content": "recent answer"})
    return messages


def test_trim_drops_the_old_middle_and_keeps_the_recent_tail():
    trimmed = trim_oldest_middle_turns(_chat())
    texts = [m.get("content") for m in trimmed]
    assert "OLD PICTURE PROMPT draw the castle" not in texts
    assert "recent answer" in texts
    assert LOCAL_EXHAUSTION_TRIM_NOTE in texts
    assert texts[0] == "You are the eval companion."


@pytest.mark.asyncio
async def test_local_exhaustion_does_not_rotate_the_session():
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(platform=Platform.LOCAL, chat_id="eval-chat", user_id="gm", chat_type="dm")
    now = _now()
    entry = SessionEntry("local:eval-chat", "sess-keep", now, now, origin=source, platform=Platform.LOCAL)
    stored = {"messages": _chat(), "rewritten": False}

    def load_transcript(session_id):
        assert session_id == "sess-keep"
        return list(stored["messages"])

    def rewrite_transcript(session_id, new_messages, **kwargs):
        assert session_id == "sess-keep"
        stored["messages"] = list(new_messages)
        stored["rewritten"] = True
        return True

    runner.load_transcript = load_transcript
    runner.rewrite_transcript = rewrite_transcript

    async def boom(*args, **kwargs):
        raise AssertionError("a local companion chat must not be rotated")

    runner._hmwa_rotate_exhausted_session = boom
    exhausted = {"failed": True, "compression_exhausted": True}
    response, kept = await runner._hmwa_compression_exhaustion_reset(
        exhausted, "still here", entry, entry.session_key, source,
    )
    assert kept.session_id == "sess-keep"
    assert "still here" in response
    assert LOCAL_EXHAUSTION_TRIM_NOTE in response
    assert "Auto-reset" not in response
    assert stored["rewritten"] is True
    texts = [m.get("content") for m in stored["messages"]]
    assert "recent answer" in texts
    assert "OLD PICTURE PROMPT draw the castle" not in texts
