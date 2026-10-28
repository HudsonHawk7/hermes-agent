"""VH5 acceptance criteria, measured with the real model on this machine.

These are the card's ACs, not a mock:

  AC1  five held-out clips of the enrolled voice → at least four tagged ``judson``
  AC2  five non-GM clips (a different voice, plus that voice through a room) → five ``unknown``,
       dropped with a named reason. The control flow that turns a drop into "no message row" is
       proved in ``test_voice_speaker.py::test_listener_drops_an_unknown_voice_before_anything_is_written``
       — here the verdicts themselves are real model output.
  AC3  zero outbound connections during the whole measurement, read from a connection log
  AC4  deleting the enrolment file → the gate refuses with ``enrolment_missing`` and the Hub says
       ``speaker_id_unavailable`` instead of guessing

The fixtures are Windows SAPI voices (no cloud TTS, no stored chat audio): voice A enrols and is
held out on new sentences, voice B is the stranger, and two of B's clips go through a synthetic
room so "TV in the corner" is represented. The VH4 spec measured the same stand-in set;
real-voice recalibration is VH6's job, on the GM's own enrolment.

Skips (never silently passes) when numpy/sherpa-onnx or the model file are not on this host.
"""

from __future__ import annotations

import math
import os
import subprocess
import unittest
import wave
from pathlib import Path

import pytest

try:
    import numpy as np
except ImportError:  # pragma: no cover - the gate env without the voice extras
    raise unittest.SkipTest("numpy is not installed: the speaker model cannot be run here") from None

try:
    import sherpa_onnx
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("sherpa_onnx is not installed here") from None

from tools import voice_speaker
from tools.voice_speaker import (
    MODEL_ID,
    SAMPLE_RATE,
    UNKNOWN,
    SpeakerError,
    build_enrolment,
    build_speaker_gate,
    floats_to_pcm,
    pcm_to_floats,
    read_wav_pcm,
    speech_windows,
)

pytestmark = [pytest.mark.platforms("windows")]

THRESHOLD = 0.62
ENROL_SENTENCES = [
    "The hyperdrive needs a new motivator before the next jump.",
    "I keep the engine running while the others fly.",
    "Bring the ship around and hold this heading.",
    "The gauges look steady, but watch the coolant line.",
    "Run the checks again and read me the numbers.",
    "That is not what the manual says about the coupling.",
    "Hand me the small wrench and step back.",
    "We are two degrees off and drifting slowly.",
    "Log the fault, then we will try it once more.",
    "Good, that holds. Now let us take it apart.",
]
HELD_OUT = [
    "Something is rattling under the deck plating again.",
    "Do not touch that panel while the power is on.",
    "I want the report on my desk before the watch ends.",
    "The bay doors are sticking in the cold.",
    "Cut the feed and we will look at it in the morning.",
]
STRANGER = [
    "Coming up next, a story about the weather on the coast.",
    "Our guest tonight studies deep ocean currents.",
    "The price of copper rose again this quarter.",
    "Let us take a short break before the next segment.",
    "Thank you for watching, and good night.",
]


# ── fixtures: voices, room, model ────────────────────────────────────────────────────────────
def _installed_voices() -> list[str]:
    script = ("Add-Type -AssemblyName System.Speech; "
              "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices()"
              " | ForEach-Object { $_.VoiceInfo.Name }")
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def _sapi_wav(text: str, path: Path, voice: str) -> Path:
    """Windows SAPI at 16 kHz mono — the model's own rate, no resampling in the way."""
    if path.exists():
        return path
    safe = text.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.SelectVoice('{voice}');"
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{path.as_posix()}', $f);"
        f"$s.Speak('{safe}');"
        "$s.Dispose();"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, capture_output=True, timeout=180)
    if not path.exists() or path.stat().st_size < 1000:
        pytest.skip(f"SAPI could not speak with voice {voice!r} on this host")
    return path


def _as_array(pcm: bytes):
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def _room(pcm: bytes, *, delay_ms: int = 45, gain: float = 0.55, seed: int = 7) -> bytes:
    """The clip as a microphone across the room hears it: late, quieter, noisy, one reflection."""
    far = _as_array(pcm)
    delay = delay_ms * SAMPLE_RATE // 1000
    out = np.zeros(len(far) + delay + 900, dtype=np.float32)
    tap = far * gain
    out[delay:delay + len(tap)] += tap
    out[delay + 260:delay + 260 + len(tap)] += tap * 0.35
    rng = np.random.default_rng(seed)
    out += rng.normal(0.0, 0.004, len(out)).astype(np.float32)  # about -48 dBFS of room tone
    return floats_to_pcm([float(v) for v in np.clip(out, -1.0, 1.0)])


def _model_path() -> str:
    candidates = []
    env = os.environ.get("HERMES_SPEAKER_MODEL")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(voice_speaker.speaker_model_path()))
    candidates.append(Path("S:/ForceAgents/architect/work/vh4-spike/dl") / MODEL_ID)
    candidates.append(Path("S:/ForceAgents/architect/work/vh4-spike") / MODEL_ID)
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size == voice_speaker.MODEL_BYTES:
            return str(candidate)
    pytest.skip(f"the verified {MODEL_ID} is not on this host "
                "(set HERMES_SPEAKER_MODEL, or run `hermes voice setup --install-speaker-model`)")


class ConnectionLog:
    """Every Python-level outbound attempt, recorded so AC3 is read off a log, not asserted."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._saved: list[tuple[object, str, object]] = []

    def __enter__(self) -> ConnectionLog:
        import socket
        import urllib.request

        def record(kind, target):
            def wrapper(*args, **kwargs):
                self.attempts.append(f"{kind} {target(args, kwargs)}")
                raise OSError(f"outbound connection refused during the AC run: {kind}")
            return wrapper

        pairs = [
            (socket.socket, "connect", record("socket.connect", lambda a, k: a[1:])),
            (socket.socket, "connect_ex", record("socket.connect_ex", lambda a, k: a[1:])),
            (socket, "create_connection", record("create_connection", lambda a, k: a[:1])),
            (socket, "getaddrinfo", record("getaddrinfo", lambda a, k: a[:1])),
            (urllib.request, "urlopen", record("urlopen", lambda a, k: a[:1])),
        ]
        for owner, name, wrapper in pairs:
            self._saved.append((owner, name, getattr(owner, name)))
            setattr(owner, name, wrapper)
        return self

    def __exit__(self, *_exc) -> None:
        for owner, name, original in self._saved:
            setattr(owner, name, original)

    def outbound(self) -> list[str]:
        """Loopback is not "leaving the machine" (pytest-xdist talks to its own workers)."""
        return [entry for entry in self.attempts if "127.0.0.1" not in entry and "::1" not in entry]


# ── the measurement ─────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    voices = _installed_voices()
    if len(voices) < 2:
        pytest.skip(f"need two SAPI voices to enrol one and test another; found {voices}")
    speaker_voice, stranger_voice = voices[0], voices[1]
    model = _model_path()
    work = tmp_path_factory.mktemp("vh5-ac")

    genuine = [_sapi_wav(text, work / f"enrol_{i:02d}.wav", speaker_voice)
               for i, text in enumerate(ENROL_SENTENCES)]
    held = [_sapi_wav(text, work / f"held_{i:02d}.wav", speaker_voice)
            for i, text in enumerate(HELD_OUT)]
    stranger_files = [_sapi_wav(text, work / f"stranger_{i:02d}.wav", stranger_voice)
                      for i, text in enumerate(STRANGER)]
    strangers: list[tuple[str, bytes]] = [
        (path.name, read_wav_pcm(str(path))) for path in stranger_files
    ]
    # Two of the five strangers are heard across the room: a TV, not a person at the desk.
    strangers[3] = ("stranger_03_room.wav",
                    _room(read_wav_pcm(str(stranger_files[3]))))
    strangers[4] = ("stranger_04_far_room.wav",
                    _room(read_wav_pcm(str(stranger_files[4])), delay_ms=90, gain=0.4, seed=11))

    embedder = voice_speaker.CAMPPlusEmbedder(model)
    with ConnectionLog() as log:
        enrolment = build_enrolment(embedder, [read_wav_pcm(str(p)) for p in genuine],
                                    "judson", THRESHOLD)
        enrolment_path = work / "enrolment" / "judson.enrol.json"
        enrolment.save(str(enrolment_path))
        config = {"voice": {"speaker": {
            "enabled": True,
            "enrolment_dir": str(enrolment_path.parent),
            "match_threshold": THRESHOLD,
            "trusted_identities": ["judson"],
        }}}
        gate = build_speaker_gate(config, model_path=model, embedder=embedder)
        assert gate is not None
        genuine_verdicts = [(p.name, gate.judge(read_wav_pcm(str(p)))) for p in held]
        stranger_verdicts = [(name, gate.judge(pcm)) for name, pcm in strangers]
    embedder.close()

    return {
        "voices": (speaker_voice, stranger_voice),
        "model": model,
        "enrolment": enrolment,
        "enrolment_path": enrolment_path,
        "config": config,
        "gate_state": gate.state(),
        "genuine": genuine_verdicts,
        "strangers": stranger_verdicts,
        "connections": log.attempts,
        "outbound": log.outbound(),
        "log_lines": list(log.attempts),
    }


def _report(rows) -> str:
    return "\n".join(
        f"    {name}: label={verdict.label} score={verdict.score} "
        f"drop={verdict.drop_reason or '-'} speech={verdict.speech_s:.2f}s"
        for name, verdict in rows)


def test_ac1_four_of_five_held_out_clips_are_the_enrolled_voice(measured):
    rows = measured["genuine"]
    trusted = [name for name, verdict in rows if verdict.trusted]
    print(f"\nAC1 enrolment: {measured['enrolment'].speech_s:.1f}s of speech, "
          f"{measured['enrolment'].windows} windows, identity "
          f"{measured['enrolment'].identity}, model {MODEL_ID}")
    print(f"AC1 held-out clips ({len(rows)}):\n{_report(rows)}")
    print(f"AC1 tagged judson: {len(trusted)}/{len(rows)} "
          f"({', '.join(trusted) or 'none'}); failed: "
          f"{', '.join(n for n, _ in rows if n not in trusted) or 'none'}")
    assert len(trusted) >= 4, f"only {len(trusted)} of {len(rows)} held-out clips matched:\n{_report(rows)}"


def test_ac2_all_five_non_gm_clips_are_unknown(measured):
    rows = measured["strangers"]
    print(f"\nAC2 non-GM clips ({len(rows)}), stranger voice {measured['voices'][1]!r}:\n"
          f"{_report(rows)}")
    for name, verdict in rows:
        assert verdict.label == UNKNOWN, f"{name} was tagged {verdict.label} ({verdict.score})"
        assert not verdict.trusted
        assert verdict.drop_reason, f"{name} was not dropped with a named reason"
    assert len(rows) == 5
    print(f"AC2 dropped before delivery: {[v.drop_reason for _, v in rows]}")


def test_ac3_nothing_left_the_machine(measured):
    print(f"\nAC3 connection log ({len(measured['connections'])} entries): "
          f"{measured['connections']}")
    assert measured["outbound"] == [], f"outbound attempts: {measured['outbound']}"


def test_ac4_a_deleted_enrolment_refuses_by_name(measured):
    from tui_gateway.voice_hub import VoiceHub

    path = Path(measured["enrolment_path"])
    path.unlink()
    embedder = voice_speaker.CAMPPlusEmbedder(measured["model"])
    try:
        with pytest.raises(SpeakerError) as caught:
            build_speaker_gate(measured["config"], model_path=measured["model"], embedder=embedder)
    finally:
        embedder.close()
    assert caught.value.reason == "enrolment_missing"
    print(f"\nAC4 deleted {path.name} → {caught.value.reason}: {caught.value.detail}")

    hub = VoiceHub()
    hub.armed("tui", object())
    hub.speaker_unavailable(caught.value.reason, caught.value.detail)
    state = hub.state()
    assert state["status"] == "idle"
    assert state["stop_reason"] == "speaker_id_unavailable:enrolment_missing"
    assert hub.can_listen() == (False, "speaker_id_unavailable:enrolment_missing")
    print(f"AC4 hub: status={state['status']} stop_reason={state['stop_reason']} "
          f"can_listen={hub.can_listen()}")


def test_the_gate_reports_what_it_did(measured):
    state = measured["gate_state"]
    print(f"\nstate: {state}")
    assert state["available"] is True
    assert state["identities"] == ["judson"]
    assert state["threshold"] == pytest.approx(THRESHOLD)
    assert state["utterances"] == 10
    assert state["trusted"] == 5
    assert sum(state["dropped"].values()) == 5
