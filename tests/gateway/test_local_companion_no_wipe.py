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


class _Store:
    """Matches session_store.rewrite_transcript. No **kwargs, so today's extra arg fails."""

    def __init__(self, messages, *, wrote=True):
        self.messages = list(messages)
        self.wrote = wrote
        self.calls = []

    def load_transcript(self, session_id):
        assert session_id == "sess-keep"
        return list(self.messages)

    def rewrite_transcript(self, session_id, messages, *, active_only=False, reject_active_turn_lease=False):
        self.calls.append({
            "session_id": session_id,
            "active_only": active_only,
            "reject_active_turn_lease": reject_active_turn_lease,
        })
        if self.wrote:
            self.messages = list(messages)
        return self.wrote


def _runner(store):
    runner = GatewayRunner(GatewayConfig())
    runner.session_store = store

    async def boom(*args, **kwargs):
        raise AssertionError("a local companion chat must not be rotated")

    runner._hmwa_rotate_exhausted_session = boom
    return runner


def _entry():
    source = SessionSource(platform=Platform.LOCAL, chat_id="eval-chat", user_id="gm", chat_type="dm")
    now = _now()
    entry = SessionEntry("local:eval-chat", "sess-keep", now, now, origin=source, platform=Platform.LOCAL)
    return source, entry


def test_trim_drops_the_old_middle_and_keeps_the_recent_tail():
    trimmed = trim_oldest_middle_turns(_chat())
    texts = [m.get("content") for m in trimmed]
    assert "OLD PICTURE PROMPT draw the castle" not in texts
    assert "recent answer" in texts
    assert LOCAL_EXHAUSTION_TRIM_NOTE in texts
    assert texts[0] == "You are the eval companion."


def test_trim_keeps_a_tool_call_with_its_result():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old picture prompt"},
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "middle"},
        {"role": "assistant", "content": "call", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "tool result"},
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent answer"},
    ]
    trimmed = trim_oldest_middle_turns(messages, keep_tail=4)
    roles = [(m.get("role"), bool(m.get("tool_calls"))) for m in trimmed]
    tool_at = next(i for i, row in enumerate(roles) if row[0] == "tool")
    assert roles[tool_at - 1][1] is True
    assert "tool result" in [m.get("content") for m in trimmed]


@pytest.mark.asyncio
async def test_local_exhaustion_writes_through_the_store_and_says_so():
    store = _Store(_chat(), wrote=True)
    runner = _runner(store)
    source, entry = _entry()
    exhausted = {"failed": True, "compression_exhausted": True}
    response, kept = await runner._hmwa_compression_exhaustion_reset(
        exhausted, "still here", entry, entry.session_key, source,
    )
    assert kept.session_id == "sess-keep"
    assert "still here" in response
    assert LOCAL_EXHAUSTION_TRIM_NOTE in response
    assert "Auto-reset" not in response
    assert store.calls == [{
        "session_id": "sess-keep",
        "active_only": False,
        "reject_active_turn_lease": False,
    }]
    texts = [m.get("content") for m in store.messages]
    assert "recent answer" in texts
    assert "OLD PICTURE PROMPT draw the castle" not in texts


@pytest.mark.asyncio
async def test_local_exhaustion_does_not_claim_a_trim_the_store_refused():
    store = _Store(_chat(), wrote=False)
    runner = _runner(store)
    source, entry = _entry()
    exhausted = {"failed": True, "compression_exhausted": True}
    response, kept = await runner._hmwa_compression_exhaustion_reset(
        exhausted, "still here", entry, entry.session_key, source,
    )
    assert kept.session_id == "sess-keep"
    assert LOCAL_EXHAUSTION_TRIM_NOTE not in response
    assert store.messages[1]["content"] == "OLD PICTURE PROMPT draw the castle"


def _wipe_double(store, entry):
    """What the old auto-reset did: drop the chat and hand back an empty session."""
    called = {"n": 0}

    async def wipe(session_entry, session_key, source):
        called["n"] += 1
        store.messages.clear()
        if hasattr(session_entry, "messages"):
            session_entry.messages = []
        return SessionEntry(
            session_entry.session_key, "sess-wiped", session_entry.created_at, session_entry.updated_at,
            origin=session_entry.origin, platform=session_entry.platform,
        )

    return called, wipe


@pytest.mark.asyncio
async def test_summarize_exhausted_does_not_clear_messages():
    """A failed summary that exhausts the budget must leave a trimmed chat, not an empty one.

    The live path is the gateway reset after compression_exhausted. A wipe that returns
    a fresh empty session still has to fail this test.
    """
    pre = _chat()
    store = _Store(pre)
    source, entry = _entry()
    entry.messages = [dict(row) for row in pre]
    wiped, wipe = _wipe_double(store, entry)
    runner = GatewayRunner(GatewayConfig())
    runner.session_store = store
    runner._hmwa_rotate_exhausted_session = wipe
    exhausted = {"failed": True, "compression_exhausted": True}
    response, kept = await runner._hmwa_compression_exhaustion_reset(
        exhausted, "Context length exceeded", entry, entry.session_key, source,
    )
    assert wiped["n"] == 0
    assert kept.session_id == "sess-keep"
    texts = [row.get("content") for row in store.messages]
    assert texts
    assert "recent answer" in texts
    assert entry.messages
    assert any("recent answer" in str(row.get("content")) for row in entry.messages)
    assert "Auto-reset" not in (response or "")


@pytest.mark.asyncio
async def test_companion_chat_exhausted_does_not_wipe():
    """The companion session still has messages after the exhausted path."""
    pre = _chat()
    store = _Store(pre)
    source, entry = _entry()
    entry.messages = [dict(row) for row in pre]
    wiped, wipe = _wipe_double(store, entry)
    runner = GatewayRunner(GatewayConfig())
    runner.session_store = store
    runner._hmwa_rotate_exhausted_session = wipe
    exhausted = {"failed": True, "compression_exhausted": True}
    _response, kept = await runner._hmwa_compression_exhaustion_reset(
        exhausted, "still here", entry, entry.session_key, source,
    )
    assert wiped["n"] == 0
    assert kept.session_id == entry.session_id
    assert store.messages
    assert any("recent answer" in str(row.get("content")) for row in store.messages)
    assert entry.messages
    assert any("recent answer" in str(row.get("content")) for row in entry.messages)
