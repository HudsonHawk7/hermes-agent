"""VH3 acceptance against the real stack: AEC3 + Silero on an echo fixture (card t_feb1a147).

This file measures the guard the way the card asks for it, with real audio and real words:

* AC1 — a clip plays through the loopback while speech plays on the input; every utterance the
  guard emits is transcribed and must contain **zero words from the clip**, across 10 trials, with
  the near speech present in at least one utterance (otherwise "zero clip words" would be free).
  A negative control repeats the run with the reference buffered 300 ms late, where VH2 measured
  echo cancellation collapsing (33.8 dB → 2.9 dB ERLE), and the clip words must then appear.
* AC2 — a 30 s synthesised reply is what the mic hears (its own voice): the guard must emit no
  utterance at all, across 5 trials. Zero utterances is stricter than "no utterance matches the
  reply at ≥ 0.8 similarity".
* AC3 — ten minutes of silence emits nothing; one 300 ms speech segment emits exactly one.

Speech is generated locally with Windows SAPI (16 kHz mono), never from anyone's chat audio. It
skips, with the reason named, on a host without livekit/onnxruntime/faster-whisper, without the
Silero model, or off Windows — the guard's own state machine is proved in
``test_voice_guard.py`` where nothing has to be installed.

Reproduce the full measurement (the exact command this was run with, from a venv holding
``livekit onnxruntime numpy faster-whisper pytest`` and a Silero v6 ``silero_vad.onnx``)::

    PYTHONPATH=<repo> VOICE_GUARD_SILERO_MODEL=<path to silero_vad.onnx> \\
        python -m pytest tests/tools/test_voice_guard_stack.py -q --noconftest
"""

from __future__ import annotations

import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from tools.voice_guard import GuardSettings, VoiceGuard, FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE

pytestmark = pytest.mark.platforms("windows")

np = pytest.importorskip("numpy")
pytest.importorskip("livekit.rtc")
pytest.importorskip("onnxruntime")

SILERO_MODEL = os.environ.get("VOICE_GUARD_SILERO_MODEL", "")
if not SILERO_MODEL or not os.path.isfile(SILERO_MODEL):
    pytest.skip(f"set VOICE_GUARD_SILERO_MODEL to a silero_vad.onnx (got {SILERO_MODEL!r})",
                allow_module_level=True)

# Distinct, unambiguous words: nothing the near-end says can be mistaken for the clip and vice versa.
CLIP_TEXT = "The television forecast mentioned heavy traffic and the weather."
NEAR_TEXT = "Bicycle harmonica umbrella."
REPLY_TEXT = (
    "Here is the long answer you asked for. The gateway keeps one lease per microphone, so a "
    "second surface cannot take the device while the first is speaking. When the reply finishes, "
    "the lease is released and the microphone returns to the surface that asked for it. Nothing "
    "starts listening on its own after a restart, and no threshold here was chosen by me. "
    "The listening guard runs the loopback reference through echo cancellation before voice "
    "activity detection, which is why the television in the next room is not taken for a person. "
    "If the reference is buffered the guard says so instead of pretending the room is quiet. "
    "That is the whole design, and every number in it comes from configuration rather than from "
    "a constant somebody picked at three in the morning."
)
CLIP_WORDS = ("television", "forecast", "traffic", "weather")
NEAR_WORDS = ("bicycle", "harmonica", "umbrella")
REPLY_WORDS = ("lease", "microphone", "restart", "threshold")

TRIALS = 10
REPLY_TRIALS = 5
ECHO_DELAY_MS = 60       # the echo path VH2's fixture used
ECHO_GAIN = 0.5          # about −6 dB of clip leaking into the mic
NOISE_DBFS = -55.0       # room floor, so the mic is never digital silence
FRAME_MS = 1000.0 * FRAME_SAMPLES / SAMPLE_RATE


# ── audio helpers ──────────────────────────────────────────────────────────────────────────────


def _sapi_wav(text: str, path: Path) -> Path:
    """Windows SAPI, 16 kHz 16-bit mono — no cloud TTS, no stored chat audio."""
    if path.exists():
        return path
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{path.as_posix()}', $f);"
        f"$s.Speak('{text}');"
        "$s.Dispose();"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, capture_output=True)
    if not path.exists():
        pytest.skip("SAPI produced no wav (no speech voice installed on this host)")
    return path


def _read_pcm(path: Path):
    with wave.open(str(path), "rb") as handle:
        assert handle.getframerate() == SAMPLE_RATE, handle.getframerate()
        assert handle.getnchannels() == 1
        if handle.getsampwidth() != 2:
            pytest.skip("SAPI wrote a non-16-bit wav")
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")


def _echo_path(far, delay_ms: int, gain: float):
    """Linear room: the clip arrives ``delay_ms`` later, quieter, plus a short tail."""
    delay = delay_ms * SAMPLE_RATE // 1000
    out = np.zeros(len(far) + delay + 400, dtype=np.float32)
    tap = far.astype(np.float32) * gain
    out[delay:delay + len(tap)] += tap
    # one reflection, so this is not a single-tap delay line
    out[delay + 240:delay + 240 + len(tap)] += tap * 0.3
    return out


def _mix(far, near, delay_ms: int, seed: int):
    """What the microphone hears: near speech + delayed clip + room noise."""
    room = _echo_path(far, delay_ms, ECHO_GAIN)
    length = max(len(room), len(near))
    mic = np.zeros(length, dtype=np.float32)
    mic[:len(room)] += room
    mic[:len(near)] += near.astype(np.float32)
    rng = np.random.default_rng(seed)
    floor = 32768.0 * 10 ** (NOISE_DBFS / 20.0)
    mic += rng.normal(0.0, floor, size=length).astype(np.float32)
    return np.clip(mic, -32768, 32767).astype("<i2")


def _frames(pcm) -> list:
    return [pcm[i:i + FRAME_SAMPLES].tobytes()
            for i in range(0, len(pcm) - FRAME_SAMPLES + 1, FRAME_SAMPLES)]


def _settings(**overrides) -> GuardSettings:
    base: dict = dict(echo_cancel="aec3", vad="silero", silero_model=SILERO_MODEL, vad_threshold=0.1,
                      min_speech_ms=200, min_silence_ms=500, max_utterance_ms=30000,
                      stream_delay_ms=ECHO_DELAY_MS, max_late_reference_frames=5,
                      max_reference_lag_ms=50)
    base.update(overrides)
    return GuardSettings(**base)


def _guard(**overrides) -> VoiceGuard:
    return VoiceGuard(_settings(**overrides))


def _run_guard(mic_pcm, ref_pcm, *, ref_lag_frames: int = 0, **overrides):
    """Feed mic and reference frame by frame and return ``(utterances, state)``.

    ``ref_lag_frames = 0`` is the causal case: the loopback frame for an instant is in hand before
    that mic frame is processed, exactly as a live loopback tap delivers it. A positive value
    reproduces a *buffered* reference: the tap hands over audio that is that many 10 ms frames old,
    which is VH2's 300 ms failure (33.8 dB → 2.9 dB). The reference is padded with silence to the
    mic's length, so a short clip is never miscounted as a missing reference.
    """
    guard = _guard(**overrides)
    if len(ref_pcm) < len(mic_pcm):
        ref_pcm = np.concatenate([ref_pcm, np.zeros(len(mic_pcm) - len(ref_pcm), dtype="<i2")])
    mic_frames, ref_frames = _frames(mic_pcm), _frames(ref_pcm)
    utterances = []
    for index, mic in enumerate(mic_frames):
        available = index - ref_lag_frames
        if 0 <= available < len(ref_frames):
            guard.push_reference(ref_frames[available], captured_at=available * FRAME_MS)
        utterances.extend(guard.process(mic, captured_at=index * FRAME_MS))
    utterances.extend(guard.flush())
    state = guard.state()
    guard.close()
    return utterances, state


# ── transcription: the words are what the card measures ────────────────────────────────────────


@pytest.fixture(scope="module")
def transcriber():
    pytest.importorskip("faster_whisper")
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8")

    def transcribe(pcm) -> str:
        audio = pcm.astype(np.float32) / 32768.0
        segments, _info = model.transcribe(audio, language="en", beam_size=1)
        return " ".join(segment.text for segment in segments).lower()

    return transcribe


def _hits(text: str, words) -> list[str]:
    return [word for word in words if word in text]


# ── AC1 ────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def wavs(tmp_path_factory):
    base = tmp_path_factory.mktemp("vh3")
    return {
        "clip": _read_pcm(_sapi_wav(CLIP_TEXT, base / "clip.wav")),
        "near": _read_pcm(_sapi_wav(NEAR_TEXT, base / "near.wav")),
        "reply": _read_pcm(_sapi_wav(REPLY_TEXT, base / "reply.wav")),
    }


def test_ac1_zero_words_from_the_clip_across_ten_trials(wavs, transcriber):
    """AC1: the clip is the loudest thing in the room and not one of its words gets through."""
    clip_words_heard: list[str] = []
    near_words_heard: list[str] = []
    transcripts: list[str] = []
    for trial in range(TRIALS):
        mic = _mix(wavs["clip"], wavs["near"], ECHO_DELAY_MS, seed=20261028 + trial)
        ref = wavs["clip"]  # the loopback tap: the clip, before the room
        utterances, state = _run_guard(mic, ref)
        assert state["echo_cancel"] == "aec3" and state["vad"] == "silero"
        assert state["late_reference_frames"] == 0, state
        for utterance in utterances:
            text = transcriber(np.frombuffer(utterance.pcm, dtype="<i2"))
            transcripts.append(text)
            clip_words_heard.extend(_hits(text, CLIP_WORDS))
            near_words_heard.extend(_hits(text, NEAR_WORDS))
    quoted = f"trials={TRIALS} clip_words={len(clip_words_heard)} {clip_words_heard} " \
             f"near_words={len(near_words_heard)} {sorted(set(near_words_heard))} " \
             f"utterance_transcripts={transcripts}"
    assert near_words_heard, f"the near speech was never heard, so this proves nothing: {quoted}"
    assert len(clip_words_heard) == 0, quoted
    print("\nAC1 " + quoted)


class _Tap:
    """Wraps a canceller so a test can measure what it actually did (ERLE)."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.pairs: list[tuple[bytes, bytes]] = []

    @property
    def name(self) -> str:
        return getattr(self.inner, "name", "?")

    def cancel(self, mic: bytes, reference: bytes) -> bytes:
        out = self.inner.cancel(mic, reference)
        self.pairs.append((mic, out))
        return out

    def close(self) -> None:
        self.inner.close()


def _erle_db(pairs) -> float:
    """Echo return loss enhancement over the frames where the clip dominates the mic."""
    mic = np.frombuffer(b"".join(mic for mic, _out in pairs), dtype="<i2").astype(np.float64)
    out = np.frombuffer(b"".join(out for _mic, out in pairs), dtype="<i2").astype(np.float64)
    frame = FRAME_SAMPLES
    usable = [(mic[i:i + frame], out[i:i + frame]) for i in range(0, len(mic) - frame, frame)]
    loud = [(near, clean) for near, clean in usable
            if np.sqrt(np.mean(near ** 2)) > 32768.0 * 10 ** (-30 / 20.0)]
    near = np.concatenate([near for near, _clean in loud])
    clean = np.concatenate([clean for _near, clean in loud])
    return 10.0 * np.log10(max(float(np.sum(near ** 2)), 1e-9) / max(float(np.sum(clean ** 2)), 1e-9))


def _run_with_tap(mic_pcm, ref_pcm, *, ref_lag_frames: int, **overrides):
    from tools.voice_guard import Aec3Canceller

    tap = _Tap(Aec3Canceller(stream_delay_ms=ECHO_DELAY_MS))
    guard = VoiceGuard(_settings(**overrides), echo=tap)
    if len(ref_pcm) < len(mic_pcm):
        ref_pcm = np.concatenate([ref_pcm, np.zeros(len(mic_pcm) - len(ref_pcm), dtype="<i2")])
    mic_frames, ref_frames = _frames(mic_pcm), _frames(ref_pcm)
    utterances = []
    for index, mic in enumerate(mic_frames):
        available = index - ref_lag_frames
        if 0 <= available < len(ref_frames):
            guard.push_reference(ref_frames[available], captured_at=available * FRAME_MS)
        utterances.extend(guard.process(mic, captured_at=index * FRAME_MS))
    utterances.extend(guard.flush())
    state = guard.state()
    guard.close()
    return utterances, state, tap.pairs


def test_ac1_negative_control_a_buffered_reference_cancels_nothing(wavs, transcriber):
    """The same room with the reference 300 ms late — VH2's failure case, measured as ERLE.

    Without this, "zero clip words" could just mean the fixture had no echo in it. The clip alone
    is played into the mic (no near speech, so this is single-talk), once with the reference in
    hand and once with it buffered 300 ms, exactly as VH2 compared 33.8 dB against 2.9 dB.
    """
    mic = _mix(wavs["clip"], np.zeros(1, dtype="<i2"), ECHO_DELAY_MS, seed=7)
    _clean_utts, causal_state, causal_pairs = _run_with_tap(mic, wavs["clip"], ref_lag_frames=0)
    _late_utts, late_state, late_pairs = _run_with_tap(mic, wavs["clip"], ref_lag_frames=30)

    causal_erle = _erle_db(causal_pairs)
    late_erle = _erle_db(late_pairs)
    quoted = (f"causal={causal_erle:.1f} dB buffered300ms={late_erle:.1f} dB "
              f"(VH2 measured 33.8 dB vs 2.9 dB)")
    assert causal_state["degraded"] is False, causal_state
    assert late_state["degraded"] is True, late_state
    assert causal_erle >= 15.0, quoted
    assert late_erle <= 8.0, quoted
    print("\nAC1-control " + quoted)


# ── AC2 ────────────────────────────────────────────────────────────────────────────────────────


def test_ac2_her_own_voice_is_not_an_utterance(wavs):
    """AC2: while a 30 s reply is playing, the guard creates no utterance at all, 5/5 trials."""
    reply = wavs["reply"]
    seconds = len(reply) / SAMPLE_RATE
    assert seconds >= 25, f"the reply fixture is only {seconds:.1f}s"
    counts = []
    for trial in range(REPLY_TRIALS):
        mic = _mix(reply, np.zeros(1, dtype="<i2"), ECHO_DELAY_MS, seed=99 + trial)
        utterances, state = _run_guard(mic, reply)
        counts.append(len(utterances))
        assert state["late_reference_frames"] == 0, state
    quoted = f"reply={seconds:.1f}s trials={REPLY_TRIALS} utterances_created={counts}"
    assert counts == [0] * REPLY_TRIALS, quoted


# ── AC3 with the real VAD ─────────────────────────────────────────────────────────────────────


def test_ac3_ten_minutes_of_silence_with_the_real_vad(wavs):
    silence = np.zeros(SAMPLE_RATE * 600, dtype="<i2")
    utterances, state = _run_guard(silence, silence)
    assert len(utterances) == 0
    assert state["utterances"] == 0 and state["frames"] == 60000


def test_ac3_one_300ms_segment_is_exactly_one_utterance(wavs):
    near = wavs["near"]
    speech = near[SAMPLE_RATE // 2: SAMPLE_RATE // 2 + SAMPLE_RATE * 3 // 10]  # 300 ms of real speech
    lead = np.zeros(SAMPLE_RATE, dtype="<i2")
    tail = np.zeros(SAMPLE_RATE, dtype="<i2")
    mic = np.concatenate([lead, speech, tail])
    utterances, state = _run_guard(mic, np.zeros(len(mic), dtype="<i2"))
    assert len(utterances) == 1, f"{len(utterances)} utterances ({state})"
