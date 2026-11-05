"""RPC-level tests for ``session.note`` — the plugin voice door's transcript line.

A desktop plugin appends the "Call ended · 2:14" line through this method, so the
contract has to hold at the gateway, not just in the SDK:

* the line is validated here (non-empty, length-capped) and never persisted, so
  it cannot become model input on the next turn or replay on resume;
* an unknown runtime id is refused like every other session-scoped RPC;
* a client that does not own the session's transport is refused, so one window
  cannot inject a line into another window's call.
"""

import pytest

import tui_gateway.server as srv
import tui_gateway.methods_session
from tui_gateway.transport import bind_transport, reset_transport

SID = "note-runtime-1"


class _OtherTransport:
    """Stand-in for a second client's transport (identity is what matters)."""

    def write(self, frame) -> bool:
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def live_session():
    session = {"agent": None, "id": SID, "session_key": "stored-note-1", "transport": None}
    srv._sessions[SID] = session
    try:
        yield session
    finally:
        srv._sessions.pop(SID, None)


@pytest.fixture
def frames(monkeypatch):
    """Capture every event frame the gateway writes."""
    written: list[dict] = []
    monkeypatch.setattr(srv, "write_json", lambda frame: written.append(frame))
    return written


def _call(params: dict) -> dict:
    return srv._methods["session.note"](1, params)


def _notes(frames: list[dict]) -> list[dict]:
    return [f["params"] for f in frames if f["params"].get("type") == "session.note"]


def _as_owner(session: dict):
    """Bind the session's transport as the caller, the way a desktop RPC arrives."""
    transport = _OtherTransport()
    session["transport"] = transport
    return bind_transport(transport)


def test_note_emits_one_system_line_for_the_owning_session(live_session, frames):
    token = _as_owner(live_session)
    try:
        envelope = _call({"session_id": SID, "text": "  Call ended · 2:14  "})
    finally:
        reset_transport(token)

    assert "error" not in envelope, envelope
    assert envelope["result"] == {"ok": True, "session_id": SID}

    notes = _notes(frames)
    assert len(notes) == 1
    assert notes[0]["session_id"] == SID
    assert notes[0]["payload"]["text"] == "Call ended · 2:14"


def test_note_carries_an_optional_source_label(live_session, frames):
    token = _as_owner(live_session)
    try:
        envelope = _call({"session_id": SID, "text": "Call ended", "source": "video-call"})
    finally:
        reset_transport(token)

    assert "error" not in envelope, envelope
    assert _notes(frames)[0]["payload"]["source"] == "video-call"


def test_note_refuses_an_empty_line(live_session, frames):
    envelope = _call({"session_id": SID, "text": "   "})

    assert envelope["error"]["code"] == 4031
    assert _notes(frames) == []


def test_note_refuses_an_over_long_line(live_session, frames):
    envelope = _call({"session_id": SID, "text": "x" * 501})

    assert envelope["error"]["code"] == 4032
    assert _notes(frames) == []


def test_note_refuses_an_unknown_session(frames):
    envelope = _call({"session_id": "no-such-session", "text": "Call ended"})

    assert envelope["error"]["code"] == 4001
    assert _notes(frames) == []


def test_note_refuses_a_client_that_does_not_own_the_session(live_session, frames):
    live_session["transport"] = _OtherTransport()
    token = bind_transport(object())
    try:
        envelope = _call({"session_id": SID, "text": "Call ended"})
    finally:
        reset_transport(token)

    assert envelope["error"]["code"] == 4033
    assert _notes(frames) == []


def test_note_refuses_when_the_caller_has_no_transport(live_session, frames):
    live_session["transport"] = _OtherTransport()

    envelope = _call({"session_id": SID, "text": "Call ended"})

    assert envelope["error"]["code"] == 4033
    assert _notes(frames) == []


def test_note_refuses_a_session_with_no_owner(live_session, frames):
    token = bind_transport(_OtherTransport())
    try:
        envelope = _call({"session_id": SID, "text": "Call ended"})
    finally:
        reset_transport(token)

    assert envelope["error"]["code"] == 4033
    assert _notes(frames) == []
