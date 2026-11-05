"""The Hub's speaker-ID half (VH5): a refusal is visible, and nothing arms through it.

Numpy-free on purpose, like ``test_voice_hub.py``: the Hub never touches audio, so these tests run
in the plain gate without any of the voice extras installed.
"""

from __future__ import annotations

import pytest

from tui_gateway.voice_hub import SPEAKER_UNAVAILABLE, VoiceHub


class FakeSpeaker:
    """Stands in for ``tools.voice_speaker.SpeakerGate`` (the Hub only reads ``state()``)."""

    def __init__(self, **state) -> None:
        self._state = state or {"available": True, "identities": ["judson"], "utterances": 0}

    def state(self) -> dict:
        return dict(self._state)


class ExplodingSpeaker(FakeSpeaker):
    def state(self) -> dict:
        raise RuntimeError("onnx session went away")


@pytest.fixture()
def hub():
    return VoiceHub()


def test_a_gate_is_reported_under_its_own_state(hub):
    hub.attach_speaker(FakeSpeaker(identities=["judson"], utterances=3, trusted=2,
                                   dropped={"low_score": 1}))
    hub.armed("tui", object())
    state = hub.speaker_state()
    assert state["available"] is True
    assert state["state"] == "ready"
    assert state["identities"] == ["judson"]
    assert state["dropped"] == {"low_score": 1}
    assert hub.can_listen() == (True, "")


def test_speaker_id_off_reports_nothing_at_all(hub):
    assert hub.speaker_state() is None
    assert hub.can_listen() == (True, "")


def test_a_refusal_is_named_and_stops_the_listening_light(hub):
    hub.attach_speaker(FakeSpeaker())
    hub.armed("tui", object())
    hub.speaker_unavailable("enrolment_missing", "no enrolment file at …")
    state = hub.state()
    assert state["status"] == "idle"
    assert state["stop_reason"] == f"{SPEAKER_UNAVAILABLE}:enrolment_missing"
    # The four-field contract is untouched (VH1 AC4) — the reason rides in stop_reason.
    assert set(state) == {"status", "owner_surface", "since", "stop_reason"}
    assert hub.speaker_state() == {
        "available": False, "state": SPEAKER_UNAVAILABLE, "reason": "enrolment_missing",
        "detail": "no enrolment file at …",
    }
    assert hub.can_listen() == (False, f"{SPEAKER_UNAVAILABLE}:enrolment_missing")


def test_arming_through_a_refusal_is_impossible(hub):
    hub.speaker_unavailable("model_hash_mismatch", "sha256 differs")
    state = hub.armed("tui", object())
    assert state["status"] == "idle"
    assert state["stop_reason"] == f"{SPEAKER_UNAVAILABLE}:model_hash_mismatch"


def test_resuming_through_a_refusal_is_impossible(hub):
    transport = object()
    hub.armed("tui", transport)
    hub.pause(transport, "phrase")
    hub.speaker_unavailable("threshold_unset", "voice.speaker.match_threshold is null")
    assert hub.resume(transport) is False
    assert hub.state()["status"] == "idle"
    assert hub.state()["stop_reason"] == f"{SPEAKER_UNAVAILABLE}:threshold_unset"


def test_a_new_arm_after_a_fixed_enrolment_is_allowed_again(hub):
    transport = object()
    hub.speaker_unavailable("enrolment_missing", "")
    assert hub.can_listen()[0] is False
    hub.release(transport)  # the surface gave the mic up; nothing is claimed
    hub.attach_speaker(FakeSpeaker())  # a fresh gate built after the file was restored
    assert hub.can_listen() == (True, "")
    assert hub.armed("tui", transport)["status"] == "armed"
    assert hub.speaker_state()["state"] == "ready"


def test_reset_clears_the_refusal(hub):
    hub.speaker_unavailable("model_missing", "")
    hub.reset()
    assert hub.speaker_state() is None
    assert hub.can_listen() == (True, "")
    assert hub.state()["stop_reason"] is None


def test_a_broken_gate_reports_the_backend_rather_than_pretending_to_be_ready(hub):
    hub.attach_speaker(ExplodingSpeaker())
    state = hub.speaker_state()
    assert state["available"] is False
    assert state["reason"] == "speaker_backend_unavailable"
    assert "onnx session went away" in state["detail"]


def test_attaching_a_gate_clears_an_old_refusal(hub):
    hub.speaker_unavailable("enrolment_corrupt", "bad json")
    hub.attach_speaker(FakeSpeaker())
    assert hub.can_listen() == (True, "")
    assert hub.speaker_state()["state"] == "ready"
