"""VH3 — the listening guard's contract (design t_e850e163, spike t_5e633028, card t_feb1a147).

Two things are proved here, and only these two:

* **Nothing is on unless config says so, and nothing invents a threshold.** Every value the guard
  uses comes from ``voice.listening_guard``; an empty key is ``off`` and the arm log names the
  values in force (VH3 AC4).
* **The state machine.** Silence makes no utterance, one 300 ms segment makes exactly one, a
  too-short blip makes none, a long segment is cut, and a reference that arrives late is *reported*
  rather than silently cancelling nothing (VH2 condition 1).

The real AEC3 + Silero stack against an echo fixture lives in ``test_voice_guard_stack.py``,
because it needs wheels this checkout does not ship. The backends here are fakes on purpose:
they make the state machine observable, and they keep this file runnable on any host.
"""

from __future__ import annotations

import logging

import pytest

from tools import voice_guard
from tools.voice_guard import (
    GuardError,
    GuardSettings,
    VoiceGuard,
    build_guard,
    settings_from_config,
    wants_guard,
)

SETTINGS_KEYS = ("echo_cancel", "vad", "silero_model", "vad_threshold", "min_speech_ms", "min_silence_ms")

FULL_CONFIG = {
    "voice": {
        "listening_guard": {
            "enabled": True,
            "echo_cancel": "aec3",
            "vad": "silero",
            "silero_model": "S:/models/silero_vad.onnx",
            "vad_threshold": 0.1,
            "min_speech_ms": 200,
            "min_silence_ms": 500,
            "max_utterance_ms": 30000,
            "stream_delay_ms": 60,
            "max_late_reference_frames": 5,
        }
    }
}


# ── fakes: the two backends are the only things that are not real ───────────────────────────────


class _RecordingEcho:
    """Pass-through canceller that records what it was handed."""

    name = "fake-aec"

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, bytes]] = []
        self.closed = False

    def cancel(self, mic: bytes, reference: bytes) -> bytes:
        self.calls.append((mic, reference))
        return mic

    def close(self) -> None:
        self.closed = True


class _EnergyVad:
    """Silence is all-zero frames; anything else is speech. Deterministic, no model."""

    name = "fake-vad"

    def __init__(self) -> None:
        self.closed = False

    def probability(self, frame: bytes) -> float:
        return 0.0 if not any(frame) else 1.0

    def reset(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


SPEECH = b"\x01\x00" * voice_guard.FRAME_SAMPLES
SILENCE = b"\x00\x00" * voice_guard.FRAME_SAMPLES


def _settings(**overrides) -> GuardSettings:
    base: dict = dict(echo_cancel="aec3", vad="silero", silero_model="model.onnx", vad_threshold=0.1,
                      min_speech_ms=200, min_silence_ms=500)
    base.update(overrides)
    return GuardSettings(**base)


def _guard(**overrides) -> tuple[VoiceGuard, _RecordingEcho]:
    echo = _RecordingEcho()
    guard = VoiceGuard(_settings(**overrides), echo=echo, vad=_EnergyVad())
    return guard, echo


def _run(guard: VoiceGuard, pattern: str, *, with_reference: bool = True) -> list:
    """Feed one frame per character: 'S' speech, '.' silence. Returns every utterance emitted."""
    out = []
    for ch in pattern:
        frame = SPEECH if ch == "S" else SILENCE
        if with_reference:
            guard.push_reference(SILENCE)
        out.extend(guard.process(frame))
    return out


# ── AC4: configured, never defaulted ────────────────────────────────────────────────────────────


def test_no_config_means_no_guard():
    assert wants_guard({}) is False
    assert build_guard({}) is None


def test_enabled_with_empty_keys_is_off_and_names_every_missing_key(caplog):
    caplog.set_level(logging.WARNING, logger="tools.voice_guard")
    assert build_guard({"voice": {"listening_guard": {"enabled": True}}}) is None
    logged = caplog.text
    for key in SETTINGS_KEYS:
        assert f"voice.listening_guard.{key}" in logged, f"{key} was not named: {logged}"


def test_every_value_in_force_is_logged_at_arm(caplog, monkeypatch):
    monkeypatch.setattr(voice_guard, "_make_echo", lambda settings: _RecordingEcho())
    monkeypatch.setattr(voice_guard, "_make_vad", lambda settings: _EnergyVad())
    caplog.set_level(logging.INFO, logger="tools.voice_guard")
    guard = build_guard(FULL_CONFIG)
    assert guard is not None
    armed = [r.getMessage() for r in caplog.records if "voice guard armed" in r.getMessage()]
    assert armed, caplog.text
    line = armed[-1]
    for fragment in ("echo_cancel=aec3", "vad=silero", "vad_threshold=0.1", "min_speech_ms=200",
                     "min_silence_ms=500", "max_utterance_ms=30000", "stream_delay_ms=60",
                     "max_late_reference_frames=5", "onnx_threads=off"):
        assert fragment in line, f"{fragment} missing from arm log: {line}"


def test_optional_keys_are_off_not_defaulted():
    settings = settings_from_config({"voice": {"listening_guard": {
        "echo_cancel": "aec3", "vad": "silero", "silero_model": "m.onnx",
        "vad_threshold": 0.1, "min_speech_ms": 200, "min_silence_ms": 500}}})
    assert settings.complete
    assert settings.max_utterance_ms is None and settings.onnx_threads is None
    assert "max_utterance_ms=off" in settings.describe()


def test_malformed_shapes_are_off_not_crashes():
    for cfg in ({"voice": True}, {"voice": []}, {"voice": {"listening_guard": 3}}, {}, None):
        if cfg is None:
            continue
        settings = settings_from_config(cfg)
        assert not settings.complete
    # a bool is not a number, and a bad string is not a number either
    settings = settings_from_config({"voice": {"listening_guard": {
        "stream_delay_ms": True, "vad_threshold": "loud"}}})
    assert settings.stream_delay_ms is None and settings.vad_threshold is None


def test_missing_wheel_is_a_named_refusal_not_a_half_guard(monkeypatch):
    def _boom(settings):
        raise GuardError("livekit.rtc is not installed (No module named 'livekit')")

    monkeypatch.setattr(voice_guard, "_make_echo", _boom)
    assert build_guard(FULL_CONFIG) is None


def test_unknown_backend_name_is_refused():
    with pytest.raises(GuardError, match="echo_cancel"):
        voice_guard._make_echo(_settings(echo_cancel="webrtc-ns"))


# ── AC3: silence is not an utterance ──────────────────────────────────────────────────────────


def test_ten_minutes_of_silence_makes_zero_utterances():
    guard, _ = _guard()
    assert _run(guard, "." * 60_000) == []
    state = guard.state()
    assert state["frames"] == 60_000
    assert state["utterances"] == 0


def test_one_300ms_segment_makes_exactly_one_utterance():
    guard, _ = _guard()
    utterances = _run(guard, "." * 50 + "S" * 30 + "." * 100)
    assert len(utterances) == 1
    assert utterances[0].speech_ms == 300
    assert guard.state()["utterances"] == 1


def test_a_150ms_blip_is_dropped_not_delivered():
    guard, _ = _guard(min_speech_ms=200)
    assert _run(guard, "." * 20 + "S" * 15 + "." * 100) == []
    assert guard.state()["dropped_short"] == 1


def test_a_gap_shorter_than_min_silence_is_bridged_into_one_utterance():
    guard, _ = _guard(min_silence_ms=500)
    # 200 ms silence between two 300 ms segments is less than the 500 ms endpoint
    utterances = _run(guard, "S" * 30 + "." * 20 + "S" * 30 + "." * 100)
    assert len(utterances) == 1
    assert utterances[0].speech_ms == 600


def test_a_gap_longer_than_min_silence_splits_two_utterances():
    guard, _ = _guard(min_silence_ms=500)
    utterances = _run(guard, "S" * 30 + "." * 60 + "S" * 30 + "." * 100)
    assert len(utterances) == 2
    assert utterances[0].end_frame < utterances[1].start_frame


def test_max_utterance_cuts_a_long_segment():
    guard, _ = _guard(max_utterance_ms=500)
    utterances = _run(guard, "S" * 70 + "." * 100)
    assert len(utterances) >= 2
    assert all(u.duration_ms <= 500 for u in utterances)


def test_flush_closes_an_open_utterance():
    guard, _ = _guard()
    assert _run(guard, "S" * 30) == []
    flushed = guard.flush()
    assert len(flushed) == 1 and flushed[0].speech_ms == 300


# ── VH2 condition 1: the reference must be causally early ──────────────────────────────────────


def test_reference_arriving_with_every_frame_is_not_degraded():
    guard, echo = _guard(max_late_reference_frames=5)
    _run(guard, "." * 100)
    assert guard.reference_state()["late_frames"] == 0
    assert guard.reference_state()["degraded"] is False
    # the canceller really was given the reference for every frame
    assert len(echo.calls) == 100 and all(ref == SILENCE for _mic, ref in echo.calls)


def test_a_reference_that_never_arrives_is_counted_and_latches_degraded(caplog):
    caplog.set_level(logging.WARNING, logger="tools.voice_guard")
    guard, _ = _guard(max_late_reference_frames=5)
    _run(guard, "." * 20, with_reference=False)
    state = guard.reference_state()
    assert state["late_frames"] == 20
    assert state["degraded"] is True
    assert "max_late_reference_frames=5" in caplog.text


def test_an_unknown_reference_is_refused_not_ignored():
    echo, vad = _RecordingEcho(), _EnergyVad()
    with pytest.raises(GuardError) as err:
        VoiceGuard(_settings(reference="loopback-ish"), echo=echo, vad=vad)
    assert ".reference=" in str(err.value)


def test_the_reference_is_off_unless_named():
    guard, _ = _guard()
    assert guard.reference.name == "off"
    assert guard.reference.pull(160) == b"\x00" * 320


def test_degraded_is_not_latched_when_the_limit_is_off():
    guard, _ = _guard(max_late_reference_frames=None)
    _run(guard, "." * 20, with_reference=False)
    assert guard.reference_state()["late_frames"] == 20
    assert guard.reference_state()["degraded"] is False


def test_a_buffered_reference_is_reported_as_stale(caplog):
    """A reference that is present but 300 ms old cancels nothing — say so, don't pretend."""
    caplog.set_level(logging.WARNING, logger="tools.voice_guard")
    guard, _ = _guard(max_reference_lag_ms=50)
    for index in range(100):
        guard.push_reference(SILENCE, captured_at=index * 0.01 - 0.3)
        guard.process(SILENCE, captured_at=index * 0.01)
    state = guard.reference_state()
    assert state["degraded"] is True
    assert state["max_lag_ms"] >= 290
    assert "max_reference_lag_ms=50" in caplog.text


# ── the Hub owns the guard's lifetime (VH1 AC1/AC3 must still hold) ─────────────────────────────


def test_hub_never_builds_a_guard_by_itself_and_drops_it_on_release():
    from tui_gateway.voice_hub import VoiceHub

    hub = VoiceHub()
    assert hub.guard_state() is None  # a fresh hub (a restart) has no guard
    guard, echo = _guard()
    transport = object()
    hub.armed("tui", transport, guard=guard)
    in_force = hub.guard_state()
    assert in_force is not None and in_force["echo_cancel"] == "fake-aec"
    assert hub.release(transport) is True
    assert hub.guard_state() is None
    assert echo.closed is True  # backends released with the arm, not leaked


def test_hub_reset_drops_the_guard():
    from tui_gateway.voice_hub import VoiceHub

    hub = VoiceHub()
    guard, echo = _guard()
    hub.armed("tui", object(), guard=guard)
    hub.reset()
    assert hub.guard_state() is None and echo.closed is True
