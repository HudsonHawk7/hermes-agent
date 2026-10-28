"""Speaker ID contract: windowing, scoring, every named refusal, and the drop-before-delivery seam.

These tests need no model, no numpy and no microphone — they drive the production code
(``tools.voice_speaker``, ``tools.voice_mode.full_duplex_listen``) with a scripted embedder and a
fake input stream, exactly the way VH3's guard tests drive the real guard without a device. The
real model's numbers are proved separately in ``test_voice_speaker_ac.py``.

What is proved here:
  * the windowing/scoring contract is the one the VH4 spec measured (1.5 s window, 0.75 s hop,
    utterance = mean cosine, threshold decides);
  * each of the nine fail-closed reasons actually comes out of ``build_speaker_gate``;
  * an utterance that is not the enrolled voice is dropped inside the listener, before the
    trigger callback and before any WAV exists — so nothing downstream can ever see it.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from tools import voice_mode, voice_speaker
from tools.voice_speaker import (
    ENROL_MIN_SPEECH_S,
    MODEL_BYTES,
    MODEL_DIM,
    MODEL_ID,
    MODEL_SHA256,
    SAMPLE_RATE,
    UNKNOWN,
    WINDOW_S,
    SpeakerError,
    SpeakerGate,
    SpeakerSettings,
    build_enrolment,
    build_speaker_gate,
    centroid,
    floats_to_pcm,
    install_model,
    load_enrolment,
    pcm_to_floats,
    read_wav_pcm,
    settings_from_config,
    speech_windows,
    wants_speaker,
    window_span_s,
)

THRESHOLD = 0.62


# ── helpers ──────────────────────────────────────────────────────────────────────────────────
def tone(seconds: float, freq: float = 220.0, amp: float = 0.3) -> bytes:
    """A signal loud enough for every window to count as speech (RMS well above the gate)."""
    count = int(seconds * SAMPLE_RATE)
    return floats_to_pcm([amp * math.sin(2 * math.pi * freq * i / SAMPLE_RATE) for i in range(count)])


def silence(seconds: float) -> bytes:
    return floats_to_pcm([0.0] * int(seconds * SAMPLE_RATE))


def _unit(values) -> tuple[float, ...]:
    norm = math.sqrt(sum(v * v for v in values))
    return tuple(v / norm for v in values)


class ScriptedEmbedder:
    """Returns the vector it was given for every window (the tests own the score)."""

    def __init__(self, vector=(1.0, 0.0, 0.0, 0.0), dim: int = 4, *, fail: bool = False) -> None:
        self.vector = _unit(vector)
        self.dim = dim
        self.calls = 0
        self.windows = 0
        self.closed = False
        self._fail = fail

    def embed(self, window):
        self.calls += 1
        if self._fail:
            return None
        self.windows += 1
        return list(self.vector)

    def close(self) -> None:
        self.closed = True


def make_enrolment(identity: str = "judson", *, threshold=THRESHOLD, seconds: float = 11.0,
                   embedder=None, **kwargs):
    """A real enrolment file's worth of speech, built through the production path."""
    embedder = embedder or ScriptedEmbedder()
    return build_enrolment(embedder, [tone(seconds)], identity, threshold)


def write_enrolment(directory: Path, enrolment) -> Path:
    path = Path(directory) / f"{enrolment.identity}.enrol.json"
    enrolment.save(str(path))
    return path


def gate_config(enrolment_dir, *, threshold=THRESHOLD, trusted=("judson",),
                enabled=True) -> dict:
    return {"voice": {"speaker": {
        "enabled": enabled,
        "enrolment_dir": str(enrolment_dir),
        "match_threshold": threshold,
        "trusted_identities": list(trusted),
    }}}


@pytest.fixture()
def model_file(tmp_path, monkeypatch):
    """The tiny stand-in model, with the module's expected size/hash patched to match it."""
    blob = b"not-really-a-model" * 64
    import hashlib

    digest = hashlib.sha256(blob).hexdigest()
    monkeypatch.setattr(voice_speaker, "MODEL_BYTES", len(blob))
    monkeypatch.setattr(voice_speaker, "MODEL_SHA256", digest)
    path = tmp_path / f"models/{MODEL_ID}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return str(path)


@pytest.fixture()
def enrolled_dir(tmp_path):
    directory = tmp_path / "voice" / "speaker"
    directory.mkdir(parents=True)
    write_enrolment(directory, make_enrolment())
    return directory


# ── windowing: the contract the spec measured ────────────────────────────────────────────────
def test_window_is_1_5s_at_0_75s_hop():
    windows = speech_windows(pcm_to_floats(tone(3.0)))
    # 3.0 s of audio: windows start at 0.00, 0.75, 1.50 → three full windows fit.
    assert len(windows) == 3
    assert all(len(window) == int(WINDOW_S * SAMPLE_RATE) for window in windows)


def test_nothing_is_zero_padded():
    # 2.0 s holds one full 1.5 s window; the second would need padding, so it is not taken.
    assert len(speech_windows(pcm_to_floats(tone(2.0)))) == 1


def test_near_silent_windows_are_dropped():
    quiet = pcm_to_floats(silence(3.0))
    assert speech_windows(quiet) == []


def test_speech_inside_silence_keeps_only_the_speech_windows():
    pcm = floats_to_pcm(pcm_to_floats(silence(1.5)) + pcm_to_floats(tone(3.0)) + pcm_to_floats(silence(1.5)))
    windows = speech_windows(pcm_to_floats(pcm))
    assert 1 <= len(windows) < len(speech_windows(pcm_to_floats(tone(6.0))))


def test_window_span_matches_the_overlap():
    assert window_span_s(0) == 0.0
    assert window_span_s(1) == pytest.approx(WINDOW_S)
    assert window_span_s(2) == pytest.approx(WINDOW_S + 0.75)
    assert window_span_s(13) == pytest.approx(10.5)


def test_pcm_round_trip_clips_instead_of_wrapping():
    samples = [1.5, -1.5, 0.5, -0.5]
    assert pcm_to_floats(floats_to_pcm(samples)) == pytest.approx([32767 / 32768, -1.0, 16384 / 32768, -16384 / 32768], abs=1e-4)


def test_odd_length_pcm_is_truncated_to_whole_samples():
    assert len(pcm_to_floats(b"\x01\x02\x03")) == 1


def test_read_wav_pcm_downmixes_and_resamples(tmp_path):
    import wave

    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(floats_to_pcm([0.1] * (48000 * 2)))
    pcm = read_wav_pcm(str(path))
    assert len(pcm_to_floats(pcm)) == pytest.approx(SAMPLE_RATE, abs=2)


def test_centroid_is_the_mean_of_unit_vectors():
    centre = centroid([(1.0, 1.0), (1.0, -1.0)])
    assert centre == pytest.approx([1.0, 0.0], abs=1e-9)


def test_centroid_of_nothing_is_empty():
    assert centroid([]) == []


# ── settings ─────────────────────────────────────────────────────────────────────────────────
def test_settings_read_every_key_and_default_to_off():
    assert settings_from_config({}) == SpeakerSettings()
    assert not wants_speaker({})
    settings = settings_from_config(gate_config(Path("/tmp/x"), threshold="0.62", trusted=["judson", "", "a"]))
    assert settings.enabled is True
    assert settings.match_threshold == pytest.approx(0.62)
    assert settings.trusted_identities == ("judson", "a")
    assert settings.enrolment_dir == str(Path("/tmp/x"))


def test_string_booleans_and_junk_numbers():
    config = {"voice": {"speaker": {"enabled": "true", "match_threshold": "not a number"}}}
    settings = settings_from_config(config)
    assert settings.enabled is True
    assert settings.match_threshold is None  # junk is off, never a silent 0.0
    assert settings_from_config({"voice": {"speaker": {"enabled": True, "match_threshold": True}}}).match_threshold is None


def test_voice_block_of_the_wrong_shape_is_off():
    assert not wants_speaker({"voice": "loud"})
    assert not wants_speaker({"voice": {"speaker": []}})


# ── fail-closed: one test per named reason ───────────────────────────────────────────────────– 
def test_disabled_means_no_gate(tmp_path):
    assert build_speaker_gate(gate_config(tmp_path, enabled=False), model_path="ignored") is None


def test_refuses_when_the_model_is_missing(tmp_path, enrolled_dir):
    with pytest.raises(SpeakerError) as caught:
        build_speaker_gate(gate_config(enrolled_dir), model_path=str(tmp_path / "nope.onnx"),
                           embedder=ScriptedEmbedder())
    assert caught.value.reason == "model_missing"


def test_refuses_when_the_model_hash_is_wrong(tmp_path, enrolled_dir, monkeypatch):
    path = tmp_path / "model.onnx"
    path.write_bytes(b"x" * 10)
    monkeypatch.setattr(voice_speaker, "MODEL_BYTES", 10)
    monkeypatch.setattr(voice_speaker, "MODEL_SHA256", "0" * 64)
    with pytest.raises(SpeakerError) as caught:
        build_speaker_gate(gate_config(enrolled_dir), model_path=str(path),
                           embedder=ScriptedEmbedder())
    assert caught.value.reason == "model_hash_mismatch"


def test_refuses_without_a_threshold(tmp_path, model_file, enrolled_dir):
    with pytest.raises(SpeakerError) as caught:
        build_speaker_gate(gate_config(enrolled_dir, threshold=None), model_path=model_file,
                           embedder=ScriptedEmbedder())
    assert caught.value.reason == "threshold_unset"


def test_refuses_without_an_enrolment_directory(model_file):
    config = {"voice": {"speaker": {"enabled": True, "match_threshold": THRESHOLD,
                                    "enrolment_dir": None, "trusted_identities": ["judson"]}}}
    with pytest.raises(SpeakerError) as caught:
        build_speaker_gate(config, model_path=model_file, embedder=ScriptedEmbedder())
    assert caught.value.reason == "enrolment_missing"


def test_refuses_on_an_empty_enrolment_directory(tmp_path, model_file):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SpeakerError) as caught:
        build_speaker_gate(gate_config(empty), model_path=model_file, embedder=ScriptedEmbedder())
    assert caught.value.reason == "enrolment_missing"


@pytest.mark.parametrize("mutate, reason", [
    (lambda data: data.update(version="something-else"), "enrolment_corrupt"),
    (lambda data: data["enrolment"].pop("centroid"), "enrolment_corrupt"),
    (lambda data: data["enrolment"].update(centroid=[0.0, 0.0, 0.0, 0.0]), "enrolment_corrupt"),
    (lambda data: data["window"].update(sr=8000), "enrolment_corrupt"),
    (lambda data: data.update(model={"id": MODEL_ID, "sha256": "f" * 64, "dim": 4}),
     "enrolment_model_mismatch"),
    (lambda data: data.update(threshold=0.5), "enrolment_threshold_mismatch"),
    (lambda data: data["enrolment"].update(speech_s=1.0), "enrolment_too_little_speech"),
    (lambda data: data["enrolment"].update(windows=0), "enrolment_too_little_speech"),
])
def test_every_enrolment_refusal(tmp_path, model_file, mutate, reason):
    directory = tmp_path / "enrol"
    directory.mkdir()
    path = write_enrolment(directory, make_enrolment())
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SpeakerError) as caught:
        build_speaker_gate(gate_config(directory), model_path=model_file,
                           embedder=ScriptedEmbedder())
    assert caught.value.reason == reason


def test_unreadable_enrolment_is_corrupt_not_missing(tmp_path, model_file):
    directory = tmp_path / "enrol"
    directory.mkdir()
    (directory / "judson.enrol.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SpeakerError) as caught:
        build_speaker_gate(gate_config(directory), model_path=model_file,
                           embedder=ScriptedEmbedder())
    assert caught.value.reason == "enrolment_corrupt"


def test_missing_backend_is_named(tmp_path, model_file, enrolled_dir, monkeypatch):
    def explode(*_args, **_kwargs):
        raise SpeakerError("speaker_backend_unavailable", "no sherpa here")

    monkeypatch.setattr(voice_speaker, "CAMPPlusEmbedder", explode)
    with pytest.raises(SpeakerError) as caught:
        build_speaker_gate(gate_config(enrolled_dir), model_path=model_file)
    assert caught.value.reason == "speaker_backend_unavailable"


def test_a_bad_reason_is_a_programming_error():
    with pytest.raises(ValueError):
        SpeakerError("because-i-said-so")


# ── the decision ────────────────────────────────────────────────────────────────────────────
def _gate(vector=(1.0, 0, 0, 0), *, trusted=("judson",), threshold=THRESHOLD,
          identity="judson") -> SpeakerGate:
    enrolment = make_enrolment(identity, threshold=threshold, embedder=ScriptedEmbedder(vector))
    return SpeakerGate([enrolment], threshold, ScriptedEmbedder(vector), trusted)


def test_the_enrolled_voice_is_trusted():
    verdict = _gate().judge(tone(3.0))
    assert verdict.label == "judson"
    assert verdict.trusted
    assert verdict.score == pytest.approx(1.0, abs=1e-6)
    assert verdict.drop_reason == ""
    assert verdict.speech_s == pytest.approx(3.0)  # three overlapping windows cover 3.0 s


def test_a_different_voice_is_unknown_and_low_score():
    enrolment = make_enrolment(embedder=ScriptedEmbedder((1.0, 0, 0, 0)))
    different = SpeakerGate([enrolment], THRESHOLD, ScriptedEmbedder((0.0, 1.0, 0.0, 0.0)),
                            ("judson",))
    verdict = different.judge(tone(3.0))
    assert verdict.label == UNKNOWN
    assert verdict.drop_reason == "low_score"
    assert verdict.score == pytest.approx(0.0, abs=1e-6)


def test_the_right_voice_but_not_on_the_trusted_list_is_not_trusted():
    verdict = _gate(trusted=()).judge(tone(3.0))
    assert verdict.label == UNKNOWN
    assert verdict.drop_reason == "not_trusted"
    assert verdict.score == pytest.approx(1.0, abs=1e-6)


def test_a_short_utterance_is_dropped_without_scoring():
    enrolment = make_enrolment()  # its own embedder: this one starts from zero calls
    embedder = ScriptedEmbedder()
    gate = SpeakerGate([enrolment], THRESHOLD, embedder, ("judson",))
    verdict = gate.judge(tone(1.0))
    assert verdict.drop_reason == "too_short"
    assert embedder.calls == 0  # nothing was embedded: a fragment is never scored


def test_silence_is_too_short_not_a_ghost_utterance():
    verdict = _gate().judge(silence(4.0))
    assert verdict.label == UNKNOWN
    assert verdict.drop_reason == "too_short"
    assert verdict.score is None


def test_a_dead_backend_drops_the_utterance_instead_of_raising():
    embedder = ScriptedEmbedder(fail=True)
    gate = SpeakerGate([make_enrolment(embedder=ScriptedEmbedder())], THRESHOLD, embedder,
                       ("judson",))
    verdict = gate.judge(tone(3.0))
    assert verdict.drop_reason == "embedding_failed"
    assert verdict.label == UNKNOWN


def test_state_counts_what_happened():
    gate = _gate()
    gate.judge(tone(3.0))
    gate.judge(tone(1.0))
    state = gate.state()
    assert state["available"] is True
    assert state["identities"] == ["judson"]
    assert state["threshold"] == pytest.approx(THRESHOLD)
    assert state["utterances"] == 2
    assert state["trusted"] == 1
    assert state["dropped"]["too_short"] == 1


# ── enrolment files ─────────────────────────────────────────────────────────────────────────
def test_enrolment_round_trip_is_centroid_only(tmp_path):
    enrolment = make_enrolment()
    path = write_enrolment(tmp_path, enrolment)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["version"] == voice_speaker.ENROL_VERSION
    assert written["threshold"] == pytest.approx(THRESHOLD)
    assert written["model"]["sha256"] == MODEL_SHA256
    assert "pcm" not in path.read_text(encoding="utf-8").lower()
    loaded = load_enrolment(str(path), threshold=THRESHOLD, dim=len(enrolment.centroid_vector))
    assert loaded.identity == "judson"
    assert loaded.centroid_vector == pytest.approx(enrolment.centroid_vector, abs=1e-6)
    assert loaded.speech_s == pytest.approx(enrolment.speech_s, abs=0.01)


def test_an_enrolment_is_rejected_when_its_centroid_does_not_match_the_model(tmp_path):
    path = write_enrolment(tmp_path, make_enrolment())  # a 4-d centroid, like a 192-d model refuses
    with pytest.raises(SpeakerError) as caught:
        load_enrolment(str(path), threshold=THRESHOLD, dim=MODEL_DIM)
    assert caught.value.reason == "enrolment_corrupt"


def test_enrolment_refuses_too_little_speech():
    with pytest.raises(SpeakerError) as caught:
        build_enrolment(ScriptedEmbedder(), [tone(2.0)], "judson", THRESHOLD)
    assert caught.value.reason == "enrolment_too_little_speech"
    assert f"{ENROL_MIN_SPEECH_S:.0f}s" in caught.value.detail


def test_enrolment_refuses_without_an_identity():
    with pytest.raises(SpeakerError) as caught:
        build_enrolment(ScriptedEmbedder(), [tone(11.0)], "  ", THRESHOLD)
    assert caught.value.reason == "enrolment_corrupt"


def test_enrolment_refuses_without_a_threshold():
    with pytest.raises(SpeakerError) as caught:
        build_enrolment(ScriptedEmbedder(), [tone(11.0)], "judson", None)
    assert caught.value.reason == "threshold_unset"


def test_save_leaves_no_temp_file(tmp_path):
    write_enrolment(tmp_path, make_enrolment())
    assert [p.name for p in tmp_path.iterdir() if p.suffix in (".json", ".tmp")] == ["judson.enrol.json"]


# ── model install ───────────────────────────────────────────────────────────────────────────
def test_install_from_a_local_file_verifies_and_moves_into_place(tmp_path, monkeypatch):
    import hashlib

    blob = b"model-bytes" * 500
    source = tmp_path / "downloaded.onnx"
    source.write_bytes(blob)
    monkeypatch.setattr(voice_speaker, "MODEL_BYTES", len(blob))
    monkeypatch.setattr(voice_speaker, "MODEL_SHA256", hashlib.sha256(blob).hexdigest())
    target = tmp_path / "models" / "speaker" / MODEL_ID
    assert install_model(str(target), source=str(source)) == str(target)
    assert target.read_bytes() == blob
    assert not target.with_suffix(target.suffix + ".part").exists()


def test_install_refuses_a_wrong_file_and_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_speaker, "MODEL_BYTES", 10)
    monkeypatch.setattr(voice_speaker, "MODEL_SHA256", "a" * 64)
    source = tmp_path / "bad.onnx"
    source.write_bytes(b"0123456789")
    target = tmp_path / "models" / "speaker" / MODEL_ID
    with pytest.raises(SpeakerError):
        install_model(str(target), source=str(source))
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_an_installed_model_is_not_downloaded_again(tmp_path, monkeypatch):
    blob = b"already-here" * 100
    import hashlib

    monkeypatch.setattr(voice_speaker, "MODEL_BYTES", len(blob))
    monkeypatch.setattr(voice_speaker, "MODEL_SHA256", hashlib.sha256(blob).hexdigest())
    target = tmp_path / "m.onnx"
    target.write_bytes(blob)

    def boom(*_args, **_kwargs):
        raise AssertionError("install_model fetched a model that was already installed")

    assert install_model(str(target), opener=boom) == str(target)


# ── the seam that keeps a non-GM voice out of the message stream ────────────────────────────
class _Utterance:
    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm


class _Block:
    """A mic block: the listener only ever calls ``len()`` and ``copy()`` on it."""

    __slots__ = ("size",)

    def __init__(self, size: int) -> None:
        self.size = size

    def __len__(self) -> int:
        return self.size

    def copy(self):
        return self


class _FakeStream:
    def __init__(self, blocks) -> None:
        self._blocks = list(blocks)
        self._index = 0

    def read(self, _count):
        block = self._blocks[min(self._index, len(self._blocks) - 1)]
        self._index += 1
        return block, False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeSD:
    """Just enough of ``sounddevice`` for the listener loop: one InputStream, 30 ms blocks."""

    def __init__(self, blocks) -> None:
        self._blocks = blocks

    def InputStream(self, **_kwargs):
        return _FakeStream(self._blocks)


class _FakeGuard:
    """The listening guard as the listener uses it: reference tap on/off, nothing else."""
    started = 0

    def start_reference(self) -> None:
        self.started += 1

    def stop_reference(self) -> None:
        pass

    def close(self) -> None:
        pass


def _drive_listener(monkeypatch, *, pcm: bytes, speaker, blocks: int = 6, trigger_at: int = 2):
    """Run ``full_duplex_listen`` over a fake mic that yields one utterance on *trigger_at*."""
    block = _Block(int(SAMPLE_RATE * 0.03))
    monkeypatch.setattr(voice_mode, "_import_audio", lambda: (_FakeSD([block]), None))
    seen = {"n": 0}

    def fake_feed(_guard, _data):
        seen["n"] += 1
        return [_Utterance(pcm)] if seen["n"] == trigger_at else []

    monkeypatch.setattr(voice_mode, "_guard_feed", fake_feed)
    state = {"blocks": 0}

    def should_stop() -> bool:
        state["blocks"] += 1
        return state["blocks"] > blocks

    triggers: list[str] = []
    return voice_mode.full_duplex_listen(
        should_stop, on_trigger=triggers.append, guard=_FakeGuard(), speaker=speaker), triggers


def test_listener_drops_an_unknown_voice_before_anything_is_written(monkeypatch, tmp_path):
    """The TV talks: no trigger, no WAV, no transcription — and the loop keeps listening."""
    written: list[bytes] = []
    monkeypatch.setattr(voice_mode, "_write_wav_frames_to_temp",
                        lambda pcm: written.append(pcm) or str(tmp_path / "x.wav"))
    gate = _gate(trusted=())  # right voice, not trusted → same drop as a stranger
    wav, triggers = _drive_listener(monkeypatch, pcm=tone(2.0), speaker=gate)
    assert wav is None
    assert triggers == []
    assert written == []
    assert gate.state()["dropped"]["not_trusted"] == 1


def test_listener_hands_a_trusted_utterance_on(monkeypatch, tmp_path):
    written: list[bytes] = []
    monkeypatch.setattr(voice_mode, "_write_wav_frames_to_temp",
                        lambda pcm: written.append(pcm) or str(tmp_path / "x.wav"))
    wav, triggers = _drive_listener(monkeypatch, pcm=tone(2.0), speaker=_gate())
    assert wav == str(tmp_path / "x.wav")
    assert triggers == ["generation"]
    assert len(written) == 1


def test_listener_without_a_speaker_gate_behaves_exactly_as_before(monkeypatch, tmp_path):
    written: list[bytes] = []
    monkeypatch.setattr(voice_mode, "_write_wav_frames_to_temp",
                        lambda pcm: written.append(pcm) or str(tmp_path / "x.wav"))
    wav, triggers = _drive_listener(monkeypatch, pcm=tone(2.0), speaker=None)
    assert wav == str(tmp_path / "x.wav")
    assert triggers == ["generation"]
