"""Who is speaking: local speaker identification for the listening guard (VH5).

The listening guard (``tools.voice_guard``) decides *that* someone spoke. This module decides
*who*: every utterance the guard emits is scored against the enrolled voice on this box and
tagged with an identity, or :data:`UNKNOWN` and dropped before it can become a message.

Design, measured in the VH4 spec (``SPEC.md`` §1-§9, evidence ``calib_report.json`` /
``utter_report.json``):

* **One local model.** 3D-Speaker CAM++ ``en_voxceleb`` 16 kHz ONNX, 29,596,978 B, Apache-2.0
  (the ONNX mirror declares no licence, so :func:`install_model` fetches it once and we never
  redistribute it). CPU only, 1 thread, no torch: ~24 ms per 1.5 s window, +88 MB RSS.
* **The decision unit is the utterance**, not the window: mean cosine over 1.5 s windows at a
  0.75 s hop, and an utterance with less than 1.5 s of speech is ``too_short`` rather than
  guessed at.
* **Threshold 0.62** gives FAR 0/164 and FRR 0/20 on the fixture set, with the worst genuine
  utterance at 0.7183 and the best impostor at 0.5272. It is a CONFIGURED value
  (``voice.speaker.match_threshold``), never a constant this module invents: ``NULL`` is how the
  whole feature is off.
* **Fail closed, always.** A missing/corrupt model, a missing/corrupt enrolment file, an
  enrolment built for a different model or threshold, too little enrolled speech, an unset
  threshold or a missing backend makes the Hub refuse to listen with one of :data:`REASONS` as
  the reason. There is no fallback path, no "assume it is the GM", and no silent
  "treat everything as trusted" mode.

Scoring runs in pure Python (a 512-float dot product per window) so this module imports and is
testable on a bare interpreter; only :class:`CAMPPlusEmbedder` needs ``sherpa_onnx`` + numpy, and
it is imported lazily so that its absence is a *named* refusal instead of an ImportError at boot.

Nothing here sends anything anywhere: the model, the enrolment centroid and every score stay in
memory on this machine. The AC harness proves that from a connection log
(``tests/tools/test_voice_speaker_ac.py``), not from this paragraph.

Settings (``voice.speaker``)::

    enabled              bool    the feature's own switch; false = never checked
    enrolment_dir        path    where <identity>.enrol.json lives; empty = refuse (no derive)
    match_threshold      float   utterance cosine >= this is trusted; NULL = off
    trusted_identities   list    enrolled identities allowed to be treated as their owner
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import struct
import threading
import time
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

SETTINGS_PATH = "voice.speaker"

SAMPLE_RATE = 16000
WINDOW_S = 1.5
HOP_S = 0.75
# An utterance shorter than one window of speech cannot carry a decision (VH4 §5: at 1.0 s the
# mean genuine score falls to 0.462, below the threshold; at 1.5 s it is 0.87).
MIN_SPEECH_S = 1.5
# A window counts as speech when its RMS is at least this fraction of the utterance's own peak —
# the same crude gate the VH4 calibration used, so the published numbers describe this code.
SPEECH_RMS_RATIO = 0.04

UNKNOWN = "unknown"

ENROL_VERSION = "hermes-speaker-enrol/1"

MODEL_ID = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
MODEL_SHA256 = "357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b"
MODEL_BYTES = 29_596_978
MODEL_DIM = 192
# Where the one model lives under the data root (``speaker_model_path``). Fetched once by
# ``hermes voice setup --install-speaker-model``; never redistributed inside this project.
MODEL_SUBDIR = ("models", "speaker")
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
)

# The minimum enrolled speech a centroid may be built from. The enrolment flow asks for ~30 s
# (VH5, the GM's 30 seconds); this floor is what the gate will accept at all, and the enrolment
# file records the seconds that actually went in so VH6 can recalibrate against reality.
ENROL_MIN_SPEECH_S = 10.0
ENROL_TARGET_SPEECH_S = 30.0

# Every named refusal. The Hub refuses to listen when a gate cannot be built, and the reason is
# one of these — verbatim, from the VH4 spec §7, so a log line and a test can both depend on it.
REASONS = (
    "model_missing",
    "model_hash_mismatch",
    "enrolment_missing",
    "enrolment_corrupt",
    "enrolment_model_mismatch",
    "enrolment_threshold_mismatch",
    "enrolment_too_little_speech",
    "threshold_unset",
    "speaker_backend_unavailable",
)

# Why one utterance was dropped. A drop is not an error: the TV talking is the normal case.
DROP_REASONS = ("too_short", "low_score", "not_trusted", "embedding_failed")


class SpeakerError(RuntimeError):
    """A speaker gate could not be built. ``reason`` is one of :data:`REASONS`."""

    def __init__(self, reason: str, detail: str = "") -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown speaker refusal reason {reason!r}")
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


# ── settings ────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SpeakerSettings:
    """``voice.speaker``, read once at arm time. No defaults are invented here."""

    enabled: bool = False
    enrolment_dir: str = ""
    match_threshold: Optional[float] = None
    trusted_identities: tuple[str, ...] = ()

    def describe(self) -> str:
        """One line naming every value in force — what the arm log prints."""
        trusted = ",".join(self.trusted_identities) if self.trusted_identities else "off"
        return (
            f"enabled={self.enabled} "
            f"enrolment_dir={self.enrolment_dir or 'off'} "
            f"match_threshold={self.match_threshold if self.match_threshold is not None else 'off'} "
            f"trusted_identities={trusted}"
        )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> Optional[float]:
    """A numeric setting, else ``None``. ``True`` is not 1.0 — a bool there is a config mistake."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _identities(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(name for name in (_text(v) for v in value) if name)


def settings_from_config(cfg: Optional[dict] = None) -> SpeakerSettings:
    """Read ``voice.speaker`` out of a full config dict (or the live config when ``None``)."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("voice speaker: config unreadable: %s", exc, exc_info=True)
            return SpeakerSettings()
    block = cfg.get("voice") if isinstance(cfg, dict) else None
    speaker = block.get("speaker") if isinstance(block, dict) else None
    if not isinstance(speaker, dict):
        return SpeakerSettings()
    enabled = speaker.get("enabled")
    return SpeakerSettings(
        enabled=bool(enabled) if not isinstance(enabled, str) else enabled.strip().lower() in
        ("1", "true", "yes", "on"),
        enrolment_dir=_text(speaker.get("enrolment_dir")),
        match_threshold=_number(speaker.get("match_threshold")),
        trusted_identities=_identities(speaker.get("trusted_identities")),
    )


def wants_speaker(cfg: Optional[dict] = None) -> bool:
    """True when config asks for speaker ID at all (``voice.speaker.enabled: true``)."""
    return settings_from_config(cfg).enabled


# ── paths ──────────────────────────────────────────────────────────────────────────────────────
def speaker_model_path() -> str:
    """``<data root>/models/speaker/<MODEL_ID>`` — the one installed model file."""
    from hermes_constants import get_hermes_home

    return str(Path(get_hermes_home()).joinpath(*MODEL_SUBDIR) / MODEL_ID)


def default_enrolment_dir() -> str:
    """``<data root>/profiles/<profile>/voice/speaker`` — what ``hermes voice enrol`` writes.

    Only the enrolment command calls this. Reading the gate never derives a path: a NULL
    ``voice.speaker.enrolment_dir`` is ``enrolment_missing``, so a setting that was never made can
    never be replaced by a guess.
    """
    from hermes_constants import get_hermes_home

    return str(Path(get_hermes_home()) / "profiles" / _profile_name() / "voice" / "speaker")


def _profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        name = _text(get_active_profile_name())
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("voice speaker: profile unknown, assuming default: %s", exc)
        return "default"
    return name or "default"


# ── audio helpers (stdlib only) ────────────────────────────────────────────────────────────────
def pcm_to_floats(pcm: bytes) -> list[float]:
    """16-bit little-endian mono PCM -> floats in -1..1."""
    if len(pcm) % 2:
        pcm = pcm[: len(pcm) - 1]
    samples = array("h")
    samples.frombytes(pcm)
    if struct.pack("<h", 1) != struct.pack("=h", 1):  # pragma: no cover - big-endian host only
        samples.byteswap()
    return [s / 32768.0 for s in samples]


def floats_to_pcm(samples: Sequence[float]) -> bytes:
    """Floats in -1..1 -> 16-bit little-endian mono PCM (clipped, never wrapped)."""
    out = array("h", (max(-32768, min(32767, round(s * 32768.0))) for s in samples))
    if struct.pack("<h", 1) != struct.pack("=h", 1):  # pragma: no cover
        out.byteswap()
    return out.tobytes()


def read_wav_pcm(path: str) -> bytes:
    """Mono 16 kHz 16-bit PCM from a wav file, converting/resampling as needed.

    Enrolment reads the clips the GM recorded; ``hermes voice enrol`` writes them at the guard's
    own rate, but a hand-supplied ``--from-wav`` may be anything, so this widens to the model's
    rate instead of failing on a 44.1 kHz file.
    """
    with wave.open(path, "rb") as w:
        channels, width, rate, frames = (
            w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())
        raw = w.readframes(frames)
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM wav is supported (sampwidth {width})")
    samples = pcm_to_floats(raw)
    if channels > 1:
        samples = [sum(samples[i:i + channels]) / channels
                   for i in range(0, len(samples) - channels + 1, channels)]
    if rate != SAMPLE_RATE and samples:
        samples = _resample(samples, rate, SAMPLE_RATE)
    return floats_to_pcm(samples)


def _resample(samples: Sequence[float], src: int, dst: int) -> list[float]:
    """Linear-interpolation resample. Adequate for enrolment clips; the gate never resamples."""
    if src == dst or not samples:
        return list(samples)
    count = int(len(samples) * dst / src)
    step = src / dst
    last = len(samples) - 1
    out = []
    for i in range(count):
        pos = i * step
        left = int(pos)
        right = min(left + 1, last)
        frac = pos - left
        out.append(samples[left] * (1.0 - frac) + samples[right] * frac)
    return out


def speech_windows(samples: Sequence[float]) -> list[Sequence[float]]:
    """The windows the decision is made over: 1.5 s at a 0.75 s hop, near-silent ones dropped.

    Identical to the VH4 calibration, including the gate: a window counts when its RMS is at
    least ``SPEECH_RMS_RATIO`` of the utterance's own peak. The last window is only taken when a
    whole window fits — nothing is zero-padded, because padding would score silence as speech.
    """
    win, hop = int(WINDOW_S * SAMPLE_RATE), int(HOP_S * SAMPLE_RATE)
    if len(samples) < win:
        return []
    peak = max((abs(s) for s in samples), default=0.0)
    if peak <= 0.0:
        return []
    floor = peak * SPEECH_RMS_RATIO
    windows = []
    for start in range(0, len(samples) - win + 1, hop):
        seg = samples[start:start + win]
        mean_sq = sum(s * s for s in seg) / win
        if math.sqrt(mean_sq) >= floor:
            windows.append(seg)
    return windows


def window_span_s(count: int) -> float:
    """Seconds of speech covered by ``count`` overlapping windows (1 -> 1.5 s, 2 -> 2.25 s)."""
    if count <= 0:
        return 0.0
    return (count - 1) * HOP_S + WINDOW_S


def _unit(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        return []
    return [v / norm for v in vector]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b))


def centroid(vectors: Iterable[Sequence[float]]) -> list[float]:
    """Mean of unit vectors, re-normalised — the enrolled voice (VH4 spec §5)."""
    acc: Optional[list[float]] = None
    count = 0
    for vec in vectors:
        unit = _unit(vec)
        if not unit:
            continue
        if acc is None:
            acc = [0.0] * len(unit)
        if len(unit) != len(acc):
            raise SpeakerError("enrolment_corrupt", "embedding dimensions differ")
        for i, value in enumerate(unit):
            acc[i] += value
        count += 1
    if not count or acc is None:
        return []
    return _unit([v / count for v in acc])


# ── the model ──────────────────────────────────────────────────────────────────────────────────
def model_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(path: str) -> None:
    """Raise :class:`SpeakerError` unless *path* is exactly the model the spec measured."""
    if not path or not os.path.isfile(path):
        raise SpeakerError("model_missing", f"no speaker model at {path or '(unset)'}")
    size = os.path.getsize(path)
    if size != MODEL_BYTES:
        raise SpeakerError("model_hash_mismatch", f"{path} is {size} B, expected {MODEL_BYTES}")
    digest = model_sha256(path)
    if digest != MODEL_SHA256:
        raise SpeakerError("model_hash_mismatch", f"{path} sha256 {digest} != {MODEL_SHA256}")


class CAMPPlusEmbedder:
    """The one backend: sherpa-onnx ``SpeakerEmbeddingExtractor`` on CPU, one thread.

    Imports its wheels lazily so a machine without them gets ``speaker_backend_unavailable``
    from :func:`build_speaker_gate` rather than an ImportError somewhere at boot. Every window is
    unit-normalised here, so callers compare with a plain dot product.
    """

    def __init__(self, model_path: str) -> None:
        try:
            import sherpa_onnx
        except Exception as exc:
            raise SpeakerError(
                "speaker_backend_unavailable",
                f"sherpa_onnx not importable ({type(exc).__name__}: {exc}); "
                "install it with: hermes voice setup --install-speaker-model",
            ) from exc
        try:
            import numpy as np
        except Exception as exc:
            raise SpeakerError("speaker_backend_unavailable", f"numpy not importable: {exc}") from exc
        self._np = np
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=model_path, num_threads=1, debug=False, provider="cpu")
        try:
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        except Exception as exc:
            raise SpeakerError("speaker_backend_unavailable", f"extractor rejected: {exc}") from exc
        self.dim = int(getattr(self._extractor, "dim", MODEL_DIM))
        self.model_path = model_path
        self.calls = 0
        self.seconds = 0.0

    def embed(self, window: Sequence[float]) -> Optional[list[float]]:
        """One window -> unit vector, or ``None`` when the extractor is not ready for it."""
        extractor = self._extractor
        if extractor is None:
            return None
        np = self._np
        stream = extractor.create_stream()
        try:
            stream.accept_waveform(SAMPLE_RATE, np.asarray(window, dtype=np.float32))
            stream.input_finished()
            if not extractor.is_ready(stream):
                return None
            vector = np.asarray(extractor.compute(stream), dtype=np.float32)
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.warning("speaker embed failed: %s", exc, exc_info=True)
            return None
        finally:
            with_close = getattr(stream, "free", None)
            if callable(with_close):
                with_close()
        self.calls += 1
        self.seconds += len(window) / SAMPLE_RATE
        return _unit([float(v) for v in vector]) or None

    def close(self) -> None:
        """Drop the ONNX session (sherpa-onnx has no explicit close; freeing the ref is it)."""
        self._extractor = None


# ── the enrolment ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Enrolment:
    """One enrolled voice: a centroid plus the exact model/threshold it is valid for."""

    identity: str
    centroid_vector: tuple[float, ...]
    windows: int
    speech_s: float
    threshold: float
    model_id: str = MODEL_ID
    model_sha256: str = MODEL_SHA256
    dim: int = MODEL_DIM
    created_at: str = ""

    def score(self, vector: Sequence[float]) -> float:
        return _cosine(_unit(vector), self.centroid_vector) if self.centroid_vector else 0.0

    def to_json(self) -> dict:
        return {
            "version": ENROL_VERSION,
            "identity": self.identity,
            "created_at": self.created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": {"id": self.model_id, "sha256": self.model_sha256, "dim": self.dim},
            "window": {
                "sr": SAMPLE_RATE, "win_s": WINDOW_S, "hop_s": HOP_S,
                "min_speech_s": MIN_SPEECH_S, "rms_ratio": SPEECH_RMS_RATIO,
            },
            "threshold": self.threshold,
            "enrolment": {
                "windows": self.windows,
                "speech_s": round(self.speech_s, 2),
                "centroid": [round(v, 6) for v in self.centroid_vector],
            },
        }

    def save(self, path: str) -> str:
        """Write atomically (tmp + replace) so a half-written enrolment can never be read."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return str(target)


def load_enrolment(path: str, *, threshold: float, model_sha256: str = MODEL_SHA256,
                   model_id: str = MODEL_ID, dim: int = MODEL_DIM) -> Enrolment:
    """Load one enrolment file, refusing (never repairing) anything that does not match.

    The checks are the fail-closed list from the VH4 spec §7: a file that was enrolled under a
    different model, a different threshold, or with too little speech is not usable, and saying
    which of those it is beats guessing.
    """
    if not path or not os.path.isfile(path):
        raise SpeakerError("enrolment_missing", f"no enrolment file at {path or '(unset)'}")
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SpeakerError("enrolment_corrupt", f"{path}: unreadable ({exc})") from exc
    if not isinstance(data, dict):
        raise SpeakerError("enrolment_corrupt", f"{path}: not an object")
    if data.get("version") != ENROL_VERSION:
        raise SpeakerError("enrolment_corrupt", f"{path}: version {data.get('version')!r}")

    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    if _text(model.get("sha256")) and _text(model.get("sha256")) != model_sha256:
        raise SpeakerError("enrolment_model_mismatch",
                           f"{path}: enrolled with model {_text(model.get('sha256'))[:12]}…")
    if _text(model.get("id")) and _text(model.get("id")) != model_id:
        raise SpeakerError("enrolment_model_mismatch",
                           f"{path}: enrolled with model {_text(model.get('id'))}")

    file_threshold = _number(data.get("threshold"))
    if file_threshold is None or abs(file_threshold - threshold) > 1e-9:
        raise SpeakerError(
            "enrolment_threshold_mismatch",
            f"{path}: enrolled at {file_threshold}, config says {threshold}",
        )

    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    for key, expected in (("sr", SAMPLE_RATE), ("win_s", WINDOW_S), ("hop_s", HOP_S)):
        value = _number(window.get(key))
        if value is None or abs(value - expected) > 1e-9:
            raise SpeakerError("enrolment_corrupt", f"{path}: window {key}={window.get(key)!r}")

    body = data.get("enrolment") if isinstance(data.get("enrolment"), dict) else {}
    raw_centroid = body.get("centroid")
    speech_s = _number(body.get("speech_s")) or 0.0
    windows = _number(body.get("windows")) or 0
    if not isinstance(raw_centroid, list) or not raw_centroid:
        raise SpeakerError("enrolment_corrupt", f"{path}: no centroid")
    if len(raw_centroid) != (int(_number(model.get("dim")) or len(raw_centroid))):
        raise SpeakerError("enrolment_corrupt", f"{path}: centroid is not {model.get('dim')}-d")
    # The file can only vouch for itself. The embedder in force is the other half of the question:
    # a centroid of the wrong width scores as a plausible number instead of failing, so compare
    # both and refuse — a 4-d file must never be scored against 192-d embeddings.
    declared = int(_number((model or {}).get("dim")) or len(raw_centroid))
    if declared != int(dim):
        raise SpeakerError(
            "enrolment_corrupt",
            f"{path}: centroid is {declared}-d, this embedder produces {int(dim)}-d")
    try:
        vector = tuple(float(v) for v in raw_centroid)
    except (TypeError, ValueError) as exc:
        raise SpeakerError("enrolment_corrupt", f"{path}: centroid is not numeric") from exc
    if not all(math.isfinite(v) for v in vector) or not _unit(vector):
        raise SpeakerError("enrolment_corrupt", f"{path}: centroid is empty or non-finite")
    if speech_s < ENROL_MIN_SPEECH_S or int(windows) <= 0:
        raise SpeakerError(
            "enrolment_too_little_speech",
            f"{path}: {speech_s:.1f}s of speech, need {ENROL_MIN_SPEECH_S:.0f}s",
        )

    identity = _text(data.get("identity"))
    if not identity:
        raise SpeakerError("enrolment_corrupt", f"{path}: no identity name")
    return Enrolment(
        identity=identity, centroid_vector=vector, windows=int(windows), speech_s=speech_s,
        threshold=file_threshold, model_id=_text(model.get("id")) or model_id,
        model_sha256=_text(model.get("sha256")) or model_sha256,
        dim=len(vector), created_at=_text(data.get("created_at")),
    )


def build_enrolment(embedder: Any, utterances: Iterable[bytes], identity: str,
                    threshold: float) -> Enrolment:
    """Turn recorded utterances into a centroid. Refuses rather than shipping a thin enrolment."""
    identity = _text(identity)
    if not identity:
        raise SpeakerError("enrolment_corrupt", "no identity name given")
    if threshold is None:
        raise SpeakerError("threshold_unset", "voice.speaker.match_threshold is not set")
    vectors: list[Sequence[float]] = []
    speech_s = 0.0
    for pcm in utterances:
        windows = speech_windows(pcm_to_floats(pcm))
        speech_s += window_span_s(len(windows))
        for window in windows:
            vector = embedder.embed(window)
            if vector:
                vectors.append(vector)
    if speech_s < ENROL_MIN_SPEECH_S:
        raise SpeakerError(
            "enrolment_too_little_speech",
            f"{speech_s:.1f}s of speech, need {ENROL_MIN_SPEECH_S:.0f}s",
        )
    centre = centroid(vectors)
    if not centre:
        raise SpeakerError("enrolment_corrupt", "no usable embedding in the recorded speech")
    return Enrolment(identity=identity, centroid_vector=tuple(centre), windows=len(vectors),
                     speech_s=speech_s, threshold=float(threshold), dim=len(centre))


# ── the gate ──────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SpeakerVerdict:
    """What the gate decided about one utterance. ``label`` is what the rest of the stack uses."""

    label: str
    identity: str = ""
    score: Optional[float] = None
    drop_reason: str = ""
    windows: int = 0
    speech_s: float = 0.0
    elapsed_ms: float = 0.0

    @property
    def trusted(self) -> bool:
        return self.label != UNKNOWN

    def describe(self) -> str:
        score = f"{self.score:.3f}" if self.score is not None else "n/a"
        tail = f" ({self.drop_reason})" if self.drop_reason else ""
        return f"label={self.label} identity={self.identity or '-'} score={score}{tail}"


class SpeakerGate:
    """Scores an utterance against every enrolled voice and decides who it was.

    ``judge`` is the whole contract: give it the PCM the guard already produced, get a
    :class:`SpeakerVerdict`. ``label == UNKNOWN`` means the listener must drop the utterance —
    the TV, a guest, an empty room — and it is dropped before anything is written or transcribed.
    """

    def __init__(self, enrolments: Sequence[Enrolment], threshold: float, embedder: Any,
                 trusted: Sequence[str] = (), *, model_id: str = MODEL_ID) -> None:
        self._enrolments = {e.identity: e for e in enrolments}
        self._threshold = float(threshold)
        self._embedder = embedder
        self._trusted = frozenset(_text(t).lower() for t in trusted if _text(t))
        self._model_id = model_id
        self._lock = threading.Lock()
        self._counts = {"utterances": 0, "trusted": 0}
        self._drops = {reason: 0 for reason in DROP_REASONS}

    # ── reads ────────────────────────────────────────────────────────────────────────────────
    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def identities(self) -> tuple[str, ...]:
        return tuple(sorted(self._enrolments))

    @property
    def trusted_identities(self) -> tuple[str, ...]:
        return tuple(sorted(self._trusted))

    def state(self) -> dict:
        """Counters for the doctor read (``voice_hub.speaker``); never part of ``Hub.state()``."""
        with self._lock:
            return {
                "available": True,
                "model": self._model_id,
                "threshold": self._threshold,
                "identities": list(self.identities),
                "trusted_identities": list(self.trusted_identities),
                "utterances": self._counts["utterances"],
                "trusted": self._counts["trusted"],
                "dropped": dict(self._drops),
            }

    # ── the decision ─────────────────────────────────────────────────────────────────────────
    def judge(self, pcm: bytes) -> SpeakerVerdict:
        """Label one utterance. Never raises: a backend hiccup drops the utterance, nothing else."""
        started = time.perf_counter()
        samples = pcm_to_floats(pcm)
        windows = speech_windows(samples)
        speech_s = window_span_s(len(windows))
        with self._lock:
            self._counts["utterances"] += 1
        if speech_s < MIN_SPEECH_S:
            return self._drop("too_short", started, windows=len(windows), speech_s=speech_s)
        vectors = []
        for window in windows:
            vector = self._embedder.embed(window)
            if vector:
                vectors.append(vector)
        if not vectors:
            return self._drop("embedding_failed", started, windows=len(windows), speech_s=speech_s)
        best_identity, best_score = "", -1.0
        for identity, enrolment in self._enrolments.items():
            score = math.fsum(enrolment.score(v) for v in vectors) / len(vectors)
            if score > best_score:
                best_identity, best_score = identity, score
        verdict_kwargs = {"windows": len(windows), "speech_s": speech_s}
        if best_score < self._threshold:
            return self._drop("low_score", started, score=best_score, identity=best_identity,
                              **verdict_kwargs)
        if best_identity.lower() not in self._trusted:
            return self._drop("not_trusted", started, score=best_score, identity=best_identity,
                              **verdict_kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        with self._lock:
            self._counts["trusted"] += 1
        return SpeakerVerdict(label=best_identity, identity=best_identity, score=best_score,
                              windows=len(windows), speech_s=speech_s, elapsed_ms=elapsed)

    def _drop(self, reason: str, started: float, *, score: Optional[float] = None,
              identity: str = "", windows: int = 0, speech_s: float = 0.0) -> SpeakerVerdict:
        with self._lock:
            self._drops[reason] = self._drops.get(reason, 0) + 1
        return SpeakerVerdict(label=UNKNOWN, identity=identity, score=score, drop_reason=reason,
                              windows=windows, speech_s=speech_s,
                              elapsed_ms=(time.perf_counter() - started) * 1000)

    def close(self) -> None:
        closer = getattr(self._embedder, "close", None)
        if callable(closer):
            closer()


def _enrolment_files(directory: str) -> list[str]:
    if not directory or not os.path.isdir(directory):
        return []
    return sorted(
        str(p) for p in Path(directory).iterdir()
        if p.is_file() and p.name.endswith(".enrol.json")
    )


def build_speaker_gate(cfg: Optional[dict] = None, *, model_path: Optional[str] = None,
                       embedder: Any = None) -> Optional[SpeakerGate]:
    """The gate this config asks for, else ``None`` when ``voice.speaker.enabled`` is false.

    Raises :class:`SpeakerError` with a named reason for every way the gate cannot be trusted:
    the caller turns that into the Hub's ``speaker_id_unavailable`` refusal. Order matters and is
    documented: backend, model, threshold, enrolment directory, then each enrolment file.
    """
    settings = settings_from_config(cfg)
    if not settings.enabled:
        return None
    path = model_path or speaker_model_path()
    verify_model(path)
    if settings.match_threshold is None:
        raise SpeakerError("threshold_unset", f"{SETTINGS_PATH}.match_threshold is not set")
    if not settings.enrolment_dir:
        raise SpeakerError("enrolment_missing", f"{SETTINGS_PATH}.enrolment_dir is not set")
    files = _enrolment_files(settings.enrolment_dir)
    if not files:
        raise SpeakerError("enrolment_missing", f"no *.enrol.json in {settings.enrolment_dir}")

    real_embedder = embedder is None
    embedder = embedder if embedder is not None else CAMPPlusEmbedder(path)
    dim = int(getattr(embedder, "dim", MODEL_DIM))
    enrolments = []
    for file_path in files:
        enrolment = load_enrolment(file_path, threshold=settings.match_threshold, dim=dim)
        if len(enrolment.centroid_vector) != dim:
            if real_embedder:
                embedder.close()
            raise SpeakerError(
                "enrolment_model_mismatch",
                f"{file_path}: centroid is {len(enrolment.centroid_vector)}-d, model is {dim}-d",
            )
        enrolments.append(enrolment)
    if not settings.trusted_identities:
        logger.warning(
            "voice speaker: %s.trusted_identities is empty — every utterance will drop as "
            "not_trusted until it names who is allowed to be recognised",
            SETTINGS_PATH,
        )
    return SpeakerGate(enrolments, settings.match_threshold, embedder,
                       settings.trusted_identities)


# ── model install ────────────────────────────────────────────────────────────────────────────
def install_model(destination: Optional[str] = None, *, source: Optional[str] = None,
                  opener: Any = None) -> str:
    """Put the one model on disk, verified against the spec's sha256.

    ``source`` copies a local file (proved in the AC harness); otherwise the release URL is
    fetched once. The file is downloaded to a temp name, verified, then moved into place, so a
    truncated download can never look installed. Nothing is uploaded, ever.
    """
    target = destination or speaker_model_path()
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    if os.path.isfile(target) and os.path.getsize(target) == MODEL_BYTES:
        try:
            verify_model(target)
            return target
        except SpeakerError:
            logger.warning("voice speaker: %s is the right size but the wrong file; replacing", target)
    tmp = f"{target}.part"
    if source:
        with open(source, "rb") as src, open(tmp, "wb") as dst:
            dst.writelines(iter(lambda: src.read(1 << 20), b""))
    else:
        import urllib.request

        fetch = opener or urllib.request.urlopen
        with fetch(MODEL_URL, timeout=120) as response, open(tmp, "wb") as dst:
            dst.writelines(iter(lambda: response.read(1 << 20), b""))
    try:
        verify_model(tmp)
    except SpeakerError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.replace(tmp, target)
    return target
