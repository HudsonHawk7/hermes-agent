"""VH3 wiring — the far end actually reaches the canceller, through the product's own path.

The module's state machine is proved in ``test_voice_guard.py`` and the AEC3/Silero stack in
``test_voice_guard_stack.py``. Neither could catch the review finding on ``6bb9001409``: the guard
received **silence** as its far end in production, so it could not cancel anything, and the
reference carried the microphone's clock, so "present but old" could never be detected. This file
proves the wiring, not the algorithm:

* the tap is a real thread feeding the guard, and what the canceller receives is the *far end*, not
  zeros (the exact probe that failed review: 3/3 late frames, 320 zero bytes);
* ``full_duplex_listen`` starts that tap and stops it, so the device cannot outlive capture;
* a *buffered* reference latches ``degraded`` with the measured lag, which the mic-clock version
  could never do;
* ``sounddevice``'s int16 blocks are split into 10 ms frames as bytes, not as 20 ms of them.
"""

from __future__ import annotations

import os
import time

import pytest

from tools import voice_mode
from tools.voice_guard import (FRAME_BYTES, FRAME_SAMPLES, GuardSettings, LoopbackReference,
                               ReferencePump, VoiceGuard)

SPEECH = b"\x01\x00" * FRAME_SAMPLES
SILENCE = b"\x00\x00" * FRAME_SAMPLES
FAR_END = b"\x11\x00" * FRAME_SAMPLES


class _Block:
    """What ``sd.InputStream(dtype='int16').read()`` returns: samples with ``.tobytes()``, not bytes."""

    def __init__(self, frame: bytes, frames: int = 1) -> None:
        self._raw = frame * frames

    def tobytes(self) -> bytes:
        return self._raw

    def copy(self):
        return self

    def __len__(self) -> int:
        return len(self._raw) // 2


class _Echo:
    """Records exactly what the canceller was handed."""

    name = "recording-aec"

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, bytes]] = []

    def cancel(self, mic: bytes, reference: bytes) -> bytes:
        self.calls.append((mic, reference))
        return mic

    def close(self) -> None:
        pass


class _Vad:
    """Silence is silent; anything else is speech. One 30 ms block of speech == one utterance."""

    name = "energy-vad"

    def probability(self, frame: bytes) -> float:
        return 0.0 if not any(frame) else 1.0

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Source:
    """A reference source that hands over a fixed frame, and counts what was asked for."""

    name = "loopback"

    def __init__(self, frame: bytes = FAR_END) -> None:
        self.frame = frame
        self.pulls = 0

    def pull(self, samples: int) -> bytes:
        self.pulls += 1
        return self.frame[:samples * 2]

    def close(self) -> None:
        pass


def _settings(**overrides) -> GuardSettings:
    base = dict(echo_cancel="aec3", vad="silero", silero_model="m.onnx", reference="loopback",
                vad_threshold=0.1, min_speech_ms=20, min_silence_ms=10)
    base.update(overrides)
    return GuardSettings(**base)


def _guard(source=None, **overrides) -> tuple[VoiceGuard, _Echo]:
    echo = _Echo()
    guard = VoiceGuard(_settings(**overrides), echo=echo, vad=_Vad(),
                       reference=source if source is not None else _Source())
    return guard, echo


# ── the review probe: the canceller must receive the far end, not zeros ─────────────────────────


def test_the_tap_thread_feeds_the_canceller_real_far_end_audio():
    """The exact case that failed review: pump the tap, process mic blocks, inspect the canceller.

    Before this fix the far end was pulled inside the mic loop and the Hub never passed a source, so
    ``cancel()`` was handed 320 zero bytes per 30 ms block. Now the tap thread pushes real frames and
    the mic loop only consumes them.
    """
    guard, echo = _guard()
    pump = ReferencePump(guard.reference, guard)
    try:
        assert pump.start() is True
        deadline = time.monotonic() + 2.0
        while guard.reference_state()["pending_frames"] < 3 and time.monotonic() < deadline:
            time.sleep(0.005)
        utterances = voice_mode._guard_feed(guard, SILENCE * 3)  # one 30 ms block of mic silence
    finally:
        pump.stop()
        guard.close()

    assert utterances == []
    assert echo.calls, "the canceller was never called"
    fed = [reference for _mic, reference in echo.calls]
    assert fed == [FAR_END] * len(fed), "canceller received something other than the far end"
    assert guard.state()["late_reference_frames"] == 0
    assert pump.frames >= 3


def test_a_missing_far_end_is_reported_not_silently_cancelled():
    """No tap: every frame is late and ``degraded`` latches. A guard never pretends it is working."""
    guard, echo = _guard(max_late_reference_frames=2)
    for _ in range(3):
        voice_mode._guard_feed(guard, SILENCE)
    state = guard.state()
    assert len(echo.calls) == 3
    assert all(reference == b"\x00" * FRAME_BYTES for _mic, reference in echo.calls)
    assert state["late_reference_frames"] == 3
    assert state["degraded"] is True
    guard.close()


def test_a_buffered_reference_latches_degraded_with_its_measured_lag():
    """VH2 condition 1, live: an *old* reference cancels nothing, and the guard says so.

    The old wiring stamped the far end with the mic's clock, so this number was always 0.0 and the
    detector was decorative. Here the reference is captured 200 ms earlier than the mic frame.
    """
    guard, _echo = _guard(max_reference_lag_ms=50)
    guard.push_reference(FAR_END, captured_at=time.monotonic() - 0.200)  # captured 200 ms ago
    voice_mode._guard_feed(guard, SILENCE)

    state = guard.state()
    assert state["degraded"] is True
    assert state["late_reference_frames"] == 0, "the reference was present, only old"
    assert state["max_reference_lag_ms"] >= 150.0, state["max_reference_lag_ms"]
    guard.close()


def test_int16_blocks_are_split_into_ten_millisecond_frames():
    """``sounddevice`` gives 480 int16 samples. Three frames, not one 20 ms frame."""
    guard, echo = _guard()
    for _ in range(3):
        guard.push_reference(FAR_END)  # a far end for each frame, so all three are causal
    utterances = voice_mode._guard_feed(guard, _Block(SPEECH, frames=3))
    assert len(echo.calls) == 3, "a 30 ms int16 block must become three 10 ms frames"
    assert all(len(mic) == FRAME_BYTES for mic, _ref in echo.calls)
    assert guard.state()["frames"] == 3

    # The utterance closes on silence, so it is returned by the block that *ends* it.
    utterances = voice_mode._guard_feed(guard, _Block(SILENCE, frames=2))
    assert len(utterances) == 1, "one 30 ms utterance, not three"
    guard.close()


# ── the listener owns the tap's lifetime ───────────────────────────────────────────────────────


class _FakeStream:
    """Stands in for ``sounddevice.InputStream`` so the real listener loop can run headless."""

    def __init__(self, blocks, **_kwargs) -> None:
        self._blocks = blocks
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self, n: int):
        self.reads += 1
        return self._blocks[min(self.reads, len(self._blocks)) - 1], None


def test_the_listener_starts_and_stops_the_tap_and_returns_the_utterance(monkeypatch):
    """``full_duplex_listen(guard=...)``: tap started, real far end used, WAV returned, tap stopped."""
    guard, echo = _guard(min_speech_ms=10, min_silence_ms=10)
    started: list[str] = []

    real_start, real_stop = guard.start_reference, guard.stop_reference
    monkeypatch.setattr(guard, "start_reference", lambda: started.append("start") or real_start())
    monkeypatch.setattr(guard, "stop_reference", lambda: started.append("stop") or real_stop())

    speech = _Block(SPEECH, frames=1)
    silence = _Block(SILENCE, frames=1)
    fake_sd = type("FakeSd", (), {
        "InputStream": lambda **kw: _FakeStream([speech, speech, silence], **kw)})
    monkeypatch.setattr(voice_mode, "_import_audio", lambda: (fake_sd, None))

    reads = {"n": 0}

    def should_stop() -> bool:
        reads["n"] += 1
        return reads["n"] > 50  # a bug must fail fast, never spin for the runner's whole timeout

    path = voice_mode.full_duplex_listen(should_stop, guard=guard)

    try:
        assert started == ["start", "stop"], started
        assert path and path.endswith(".wav")
        assert echo.calls, "the canceller was never called"
        # The first frame may race the tap (a missing reference is counted, not hidden); later ones
        # must carry the real far end, which is the whole point.
        fed = [reference for _mic, reference in echo.calls]
        assert any(reference == FAR_END for reference in fed), fed
    finally:
        guard.close()


def test_the_listener_never_starts_a_tap_it_was_not_given(monkeypatch):
    """No guard (the default) means the loudness path, unchanged: nothing to start, nothing to stop."""
    pytest.importorskip("numpy", reason="the loudness path measures RMS with numpy")
    import numpy as np

    calls: list[str] = []
    monkeypatch.setattr(voice_mode, "_capture_until_quiet",
                        lambda *a, **k: calls.append("captured") or "/tmp/x.wav")

    speech_block = np.full(FRAME_SAMPLES * 3, 4000, dtype="int16")
    fake_sd = type("FakeSd", (), {"InputStream": lambda **kw: _FakeStream([speech_block], **kw)})
    monkeypatch.setattr(voice_mode, "_import_audio", lambda: (fake_sd, np))

    reads = {"n": 0}

    def should_stop() -> bool:
        reads["n"] += 1
        return reads["n"] > 200  # a bug must fail fast, never spin for the runner's whole timeout

    assert voice_mode.full_duplex_listen(should_stop, multiplier=1.5, sustained_ms=30,
                                         calibration_ms=30, grace_ms=0) == "/tmp/x.wav"
    assert calls == ["captured"], "the loudness path must still capture and trip on its own"


def test_the_live_loopback_tap_opens_the_real_device():
    """The hardware path, on a machine that has one: ``soundcard`` opens WASAPI loopback and the
    tap returns whole 10 ms frames at 16 kHz mono s16le.

    Gated, because it needs a speaker and cannot run in CI: set ``VOICE_GUARD_LIVE=1``. It asserts
    the *device contract* only (frames of the right size, promptly), never content, so it is safe to
    run on the GM's box with whatever is playing.
    """
    if os.environ.get("VOICE_GUARD_LIVE") != "1":
        pytest.skip("set VOICE_GUARD_LIVE=1 to exercise the real WASAPI loopback device")
    pytest.importorskip("soundcard", reason="the loopback tap needs the optional soundcard package")

    tap = LoopbackReference()
    try:
        frames = [tap.pull(FRAME_SAMPLES) for _ in range(20)]
    finally:
        tap.close()
    delivered = [len(frame) for frame in frames if frame]
    assert delivered, "the tap returned no audio at all from the default speaker"
    assert set(delivered) == {FRAME_BYTES}, delivered
