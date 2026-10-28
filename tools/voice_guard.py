"""The listening guard: echo-cancelled, voice-activity-gated utterances.

Design t_e850e163 point 2 ("echo cancellation against the system-audio loopback + voice activity
detection — that is how built-in systems ignore the TV and their own voice"), measured by the VH2
spike (t_5e633028), built here as VH3 (t_feb1a147).

The chain, per 10 ms frame of 16 kHz mono s16le PCM:

    loopback reference ─┐
                        ├─▶ AEC3 (libwebrtc, via livekit.rtc) ─▶ Silero VAD ─▶ utterance assembler
    mic ────────────────┘

Three rules, each of which is why this module exists rather than another RMS threshold:

* **The reference must be causally early.** VH2 measured 33.8 dB of echo return loss with the true
  far-end, and 2.9 dB with that same far-end delayed 300 ms — i.e. a buffered reference cancels
  nothing. So a frame the canceller has no reference for is counted (``late_reference_frames``)
  and latches ``degraded`` past a named limit: the guard says it is not working instead of
  silently passing the TV through.
* **Nothing here has a threshold of its own.** Every number comes from
  ``voice.listening_guard`` in config.yaml. An empty or missing key turns that piece OFF and the
  arm log names the key that is missing — no default constant decides how sensitive the guard is.
* **Silence is not an utterance.** The VAD, not a rolling noise floor, answers "is this speech";
  the assembler only ever emits a segment that held at least ``min_speech_ms`` of speech.

Importing this module pulls in nothing heavy: ``livekit.rtc``, ``onnxruntime`` and ``numpy`` are
imported by the backends that need them, so a host without the wheels gets a named refusal from
:func:`build_guard` instead of an ImportError at import time.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import suppress
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# ── Protocol constants (not tunables: they are what the two backends process) ───────────────────
# The rate AEC3 and Silero both run at; VH2 AC3 proved 16 kHz works through livekit.rtc with no GPU
# and no silent resample. A capture rate other than this must be resampled before it reaches here.
SAMPLE_RATE = 16000
# One 10 ms frame — the APM's processing unit. ``set_stream_format`` does not exist in livekit.rtc;
# the rate comes from the frame, so every frame handed over must be exactly this size.
FRAME_SAMPLES = 160
SAMPLE_WIDTH = 2  # s16le
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH
_SILENCE = b"\x00" * FRAME_BYTES
# Silero v6's fixed window at 16 kHz, and the 64 samples of history its ONNX graph expects prepended
# (mirrors the package's own ``OnnxWrapper.__call__``; the graph input is 64 + 512 = 576 samples).
VAD_WINDOW_SAMPLES = 512
VAD_CONTEXT_SAMPLES = 64

SETTINGS_PATH = "voice.listening_guard"


class GuardError(RuntimeError):
    """A backend asked for by config could not be built (missing wheel, missing model file)."""


# ── settings ────────────────────────────────────────────────────────────────────────────────────
# Each field is one named setting under ``voice.listening_guard``. ``None``/``""`` means that piece
# is OFF; there is deliberately no fallback value in this module (see the module docstring).

REQUIRED_KEYS = ("echo_cancel", "vad", "silero_model", "vad_threshold", "min_speech_ms", "min_silence_ms")


@dataclass(frozen=True)
class GuardSettings:
    """The guard's whole configuration, read once at arm time from ``voice.listening_guard``."""

    echo_cancel: str = ""
    vad: str = ""
    silero_model: str = ""
    vad_threshold: Optional[float] = None
    min_speech_ms: Optional[int] = None
    min_silence_ms: Optional[int] = None
    # Optional pieces: unset = off, and the arm log says so.
    max_utterance_ms: Optional[int] = None
    stream_delay_ms: Optional[int] = None
    onnx_threads: Optional[int] = None
    max_late_reference_frames: Optional[int] = None
    max_reference_lag_ms: Optional[int] = None
    max_pending_reference_frames: Optional[int] = None
    reference: Optional[str] = None

    def missing(self) -> list[str]:
        """Fully-qualified names of the required settings that are not set."""
        missing = []
        for key in REQUIRED_KEYS:
            value = getattr(self, key)
            if value is None or value == "":
                missing.append(f"{SETTINGS_PATH}.{key}")
        return missing

    @property
    def complete(self) -> bool:
        return not self.missing()

    def describe(self) -> str:
        """One line naming every value in force — what the arm log prints (VH3 AC4)."""
        parts = []
        for key in ("echo_cancel", "vad", "silero_model", "vad_threshold", "min_speech_ms",
                    "min_silence_ms", "max_utterance_ms", "stream_delay_ms", "onnx_threads",
                    "max_late_reference_frames", "max_reference_lag_ms",
                    "max_pending_reference_frames", "reference"):
            value = getattr(self, key)
            parts.append(f"{key}={value if value not in (None, '') else 'off'}")
        return " ".join(parts)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any, cast):
    """Numeric setting, else ``None``; bools are rejected (``stream_delay_ms: true`` is not 60)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return cast(value)
    if isinstance(value, str) and value.strip():
        try:
            return cast(float(value.strip()))
        except ValueError:
            return None
    return None


def settings_from_config(cfg: Optional[dict] = None) -> GuardSettings:
    """Read ``voice.listening_guard`` out of a config dict (``None`` = load the live config).

    Shape-safe at every level, like every other ``voice.*`` reader: a hand-edited
    ``voice: true`` or a scalar ``listening_guard`` yields an all-off GuardSettings rather than
    taking the caller down.
    """
    block: Any = None
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly()
        except (ImportError, OSError, TypeError, ValueError) as exc:
            # Config unreadable: nothing is configured, so the guard is off — and the reason is
            # in the log with its traceback, because a silent all-off guard looks like config.
            logger.debug("voice guard: config unreadable (%s); nothing configured", exc, exc_info=True)
            cfg = {}
    voice = cfg.get("voice") if isinstance(cfg, dict) else None
    block = voice.get("listening_guard") if isinstance(voice, dict) else None
    if not isinstance(block, dict):
        block = {}
    return GuardSettings(
        echo_cancel=_text(block.get("echo_cancel")),
        vad=_text(block.get("vad")),
        silero_model=_text(block.get("silero_model")),
        vad_threshold=_number(block.get("vad_threshold"), float),
        min_speech_ms=_number(block.get("min_speech_ms"), int),
        min_silence_ms=_number(block.get("min_silence_ms"), int),
        max_utterance_ms=_number(block.get("max_utterance_ms"), int),
        stream_delay_ms=_number(block.get("stream_delay_ms"), int),
        onnx_threads=_number(block.get("onnx_threads"), int),
        max_late_reference_frames=_number(block.get("max_late_reference_frames"), int),
        max_reference_lag_ms=_number(block.get("max_reference_lag_ms"), int),
        max_pending_reference_frames=_number(block.get("max_pending_reference_frames"), int),
        reference=_text(block.get("reference")),
    )


def wants_guard(cfg: Optional[dict] = None) -> bool:
    """True when config asks for the guard at all (``voice.listening_guard.enabled``)."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly()
        except (ImportError, OSError, TypeError, ValueError) as exc:
            logger.debug("voice guard: config unreadable (%s); guard off", exc, exc_info=True)
            return False
    voice = cfg.get("voice") if isinstance(cfg, dict) else None
    block = voice.get("listening_guard") if isinstance(voice, dict) else None
    enabled = block.get("enabled") if isinstance(block, dict) else None
    if isinstance(enabled, str):
        return enabled.strip().lower() in ("1", "true", "yes", "on")
    return enabled is True


# ── backends ────────────────────────────────────────────────────────────────────────────────────
# Both are protocols so tests can drive the state machine with a known canceller, and so a host
# without the wheels still exercises the rest of the guard.


class EchoCanceller(Protocol):
    name: str

    def cancel(self, mic: bytes, reference: bytes) -> bytes: ...

    def close(self) -> None: ...


class Vad(Protocol):
    name: str

    def probability(self, frame: bytes) -> float: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


class Aec3Canceller:
    """libwebrtc's AEC3 through ``livekit.rtc.AudioProcessingModule`` (VH2 AC4 recommendation).

    The module mutates the near-end ``AudioFrame`` in place, so the cancelled frame is read back
    off the same object that was submitted. ``set_stream_delay_ms`` is declared honestly when
    configured, but VH2 measured that AEC3's own delay estimator absorbs an error there (27.03 dB
    at every declared delay from 0 to 200 ms), so it is not the knob that decides whether this
    works — the reference's arrival time is.
    """

    name = "aec3"

    def __init__(self, *, stream_delay_ms: Optional[int] = None) -> None:
        try:
            from livekit import rtc
        except ImportError as exc:  # pragma: no cover - exercised by the missing-wheel test
            raise GuardError(f"livekit.rtc is not installed ({exc})") from exc
        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(echo_cancellation=True)
        if stream_delay_ms is not None:
            self._apm.set_stream_delay_ms(int(stream_delay_ms))

    def cancel(self, mic: bytes, reference: bytes) -> bytes:
        near = self._frame(mic)
        self._apm.process_reverse_stream(self._frame(reference))
        self._apm.process_stream(near)
        return bytes(near.data)

    def _frame(self, pcm: bytes):
        pcm = pcm[:FRAME_BYTES]
        if len(pcm) < FRAME_BYTES:
            pcm = pcm + b"\x00" * (FRAME_BYTES - len(pcm))
        return self._rtc.AudioFrame(pcm, sample_rate=SAMPLE_RATE, num_channels=1,
                                    samples_per_channel=FRAME_SAMPLES)

    def close(self) -> None:
        self._apm = None


class SileroVad:
    """Silero VAD v6 through ``onnxruntime`` only — torch-free, as VH2 measured it.

    The model file is a path from config (``voice.listening_guard.silero_model``), never a bundled
    binary: the repo stays free of model assets and a host without the file gets a named refusal.
    """

    name = "silero"

    def __init__(self, model_path: str, *, threads: Optional[int] = None) -> None:
        import os
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - exercised by the missing-wheel test
            raise GuardError(f"onnxruntime/numpy is not installed ({exc})") from exc
        if not os.path.isfile(model_path):
            raise GuardError(f"silero model not found at {SETTINGS_PATH}.silero_model={model_path!r}")
        self._np = np
        options = ort.SessionOptions()
        if threads is not None:
            options.inter_op_num_threads = options.intra_op_num_threads = int(threads)
        self._sess = ort.InferenceSession(model_path, sess_options=options,
                                          providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        np = self._np
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._ctx = np.zeros((1, VAD_CONTEXT_SAMPLES), dtype=np.float32)
        self._history = np.zeros((VAD_WINDOW_SAMPLES - FRAME_SAMPLES,), dtype=np.float32)

    def probability(self, frame: bytes) -> float:
        """Speech probability for the 512-sample window ending at this frame's end."""
        np = self._np
        x = np.frombuffer(frame[:FRAME_BYTES], dtype="<i2").astype(np.float32) / 32768.0
        if len(x) < FRAME_SAMPLES:
            x = np.concatenate([x, np.zeros((FRAME_SAMPLES - len(x),), dtype=np.float32)])
        window = np.concatenate([self._history, x])
        self._history = window[-(VAD_WINDOW_SAMPLES - FRAME_SAMPLES):]
        payload = np.concatenate([self._ctx, window.reshape(1, -1)], axis=1).astype(np.float32)
        out, state = self._sess.run(None, {"input": payload, "state": self._state,
                                           "sr": np.array(SAMPLE_RATE, dtype=np.int64)})
        self._state = state
        self._ctx = payload[:, -VAD_CONTEXT_SAMPLES:]
        return float(out[0][0])

    def close(self) -> None:
        self._sess = None


class PassThroughCanceller:
    """Off: the mic frame is handed on unchanged. Only for tests and for ``echo_cancel: off``."""

    name = "off"

    def cancel(self, mic: bytes, reference: bytes) -> bytes:
        return mic

    def close(self) -> None:
        pass


# ── the reference: system audio, taken from the loopback so it is causally early ────────────────


class ReferenceSource(Protocol):
    """Far-end audio for the canceller: ``pull(n)`` returns *n* samples of s16le, 16 kHz mono.

    ``pullable`` is what tells :class:`ReferencePump` whether there is a device to read at all:
    ``off`` and ``manual`` have no tap (``manual`` is fed by the caller through
    :meth:`VoiceGuard.push_reference`), and pumping them only floods the queue with silence.
    """

    name: str
    pullable: bool

    def pull(self, samples: int) -> bytes: ...

    def close(self) -> None: ...


class SilentReference:
    """Off: nothing to cancel against, so the canceller is a pass-through in practice."""

    name = "off"
    pullable = False

    def pull(self, samples: int) -> bytes:
        return _SILENCE[:samples * SAMPLE_WIDTH]

    def close(self) -> None:
        pass


class ManualReference:
    """A far end the caller pushes itself, frame by frame (``push_reference``).

    For callers that already hold the loopback audio — a test fixture, or a host that taps the
    speaker in its own capture callback. Named ``manual`` so a config that says ``reference: manual``
    is honest about who supplies the audio, and so the arm log still tells the truth.

    There is no tap behind it, so ``pullable`` is False and no pump starts: a pump here would pull
    ``b""`` forever and push silence ahead of the caller's real far end (measured in review: 6 of 6
    canceller frames empty, queue depth 19 over a limit of 5, real reference dropped).
    """

    name = "manual"
    pullable = False

    def pull(self, samples: int) -> bytes:
        return b""

    def close(self) -> None:
        pass


class LoopbackReference:
    """The default speaker's own output, captured with WASAPI loopback (``soundcard``).

    This is the tap the card means by "system-audio loopback": it hears the television *and* the
    reply ``:8790`` is playing, because both leave through the same device — and it hears them
    **before** the microphone hears the room, which is the only arrangement in which AEC3 can
    cancel (VH2 condition 1). ``soundcard`` is not a Hermes dependency; without it the guard
    refuses to pretend, and the refusal names the package to install.

    The device is *resolved* here (at arm time, so a missing speaker is a named refusal) but the
    recorder is *opened on first pull*, i.e. on the pump thread: WASAPI wants the stream opened
    and read by the same thread, and the pump is the thread that reads it.
    """

    name = "loopback"
    pullable = True

    def __init__(self, device: Optional[str] = None) -> None:
        try:
            import soundcard
        except ImportError as exc:
            raise GuardError(f"reference: loopback needs the 'soundcard' package ({exc})") from exc
        self._sc = soundcard
        try:
            speaker = soundcard.default_speaker()
            self._mic = (soundcard.get_microphone(str(device), include_loopback=True)
                         if device else soundcard.get_microphone(str(speaker.name), include_loopback=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise GuardError(f"reference: loopback device unavailable ({exc})") from exc
        self._recorder = None
        self._pending = b""

    def pull(self, samples: int) -> bytes:
        """Return exactly ``samples`` of s16le mono; short reads are topped up with silence."""
        need = samples * SAMPLE_WIDTH
        if self._recorder is None:  # first pull: open on this thread, the one that will read it
            self._recorder = self._mic.recorder(samplerate=SAMPLE_RATE, channels=1,
                                                blocksize=FRAME_SAMPLES)
            self._recorder.__enter__()
        while len(self._pending) < need:
            chunk = self._recorder.record(numframes=samples)
            self._pending += _to_s16le(chunk)
        out, self._pending = self._pending[:need], self._pending[need:]
        return out

    def close(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            with suppress(RuntimeError, OSError):
                recorder.__exit__(None, None, None)


def _to_s16le(chunk: Any) -> bytes:
    """``soundcard`` hands back float32 in [-1, 1]; the guard works in s16le."""
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - numpy ships with the audio extras
        raise GuardError(f"reference: loopback needs numpy ({exc})") from exc
    data = np.asarray(chunk, dtype="float32").reshape(-1)
    return np.clip(data * 32767.0, -32768, 32767).astype("<i2").tobytes()


class ReferencePump:
    """Read the far end on its own thread and stamp every frame when it was captured.

    Why a thread and not "pull one frame per mic frame": the tap is a real-time device, so pulling
    it inside the mic loop (a) blocks the reader for another 10 ms per 10 ms frame, which makes the
    mic drift behind realtime, and (b) leaves the far end carrying the *mic's* clock, so
    ``max_reference_lag_ms`` could never fire — a buffered reference looked exactly like an early
    one. Here the timestamp is taken at capture and compared with the mic frame that consumes it:
    a tap that falls behind shows up as lag, not as silence.

    ``pull`` may block (WASAPI) or return at once (a file, a test source); the loop paces itself
    to one frame per 10 ms either way, so a fast source cannot flood the queue.
    """

    def __init__(self, source: ReferenceSource, guard: VoiceGuard,
                 *, clock: Callable[[], float] = time.monotonic) -> None:
        self._source, self._guard, self._clock = source, guard, clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.frames = 0
        self.errors = 0
        self.last_error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._stop.is_set()

    def start(self) -> bool:
        """Begin pumping. False when the source has no tap to read (``off``, ``manual``)."""
        if not getattr(self._source, "pullable", True):
            logger.debug("voice guard: reference %s has no tap to pump — the caller supplies the "
                         "far end with push_reference", getattr(self._source, "name", "?"))
            return False
        if self._thread is not None:
            return True
        # Context-bound (never a bare Thread): the tap outlives the RPC that armed it, and a
        # profile-scoped read from the wrong scope is a silent default-profile leak.
        from agent.memory_provider import spawn_context_thread

        self._thread = spawn_context_thread(self._run, name="voice-guard-reference", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        next_due = self._clock()
        while not self._stop.is_set():
            try:
                frame = self._source.pull(FRAME_SAMPLES)
            except (GuardError, OSError, RuntimeError, ValueError) as exc:
                self.errors += 1
                self.last_error = str(exc)
                logger.warning("voice guard: reference tap failed (%s) — the guard will report "
                               "every frame as a missing reference", exc)
                self._stop.set()  # the tap is gone; missing-reference counting takes over
                return
            self._guard.push_reference(frame, captured_at=self._clock())
            self.frames += 1
            next_due += FRAME_SAMPLES / SAMPLE_RATE
            delay = next_due - self._clock()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_due = self._clock()  # fell behind by more than a frame: re-base, never burst

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    def state(self) -> dict:
        return {"running": self.running, "frames": self.frames, "errors": self.errors,
                "last_error": self.last_error}


# ── utterances ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Utterance:
    """One stretch of the GM's speech, echo-cancelled, ready for STT."""

    index: int
    start_frame: int
    end_frame: int
    speech_frames: int
    pcm: bytes  # s16le, 16 kHz mono, AEC output

    @property
    def speech_ms(self) -> int:
        return self.speech_frames * FRAME_SAMPLES * 1000 // SAMPLE_RATE

    @property
    def duration_ms(self) -> int:
        return (self.end_frame - self.start_frame + 1) * FRAME_SAMPLES * 1000 // SAMPLE_RATE


# ── the guard ──────────────────────────────────────────────────────────────────────────────────


class VoiceGuard:
    """Frames in, utterances out. One instance per armed mic, never a process-wide default.

    Not a detector and not an owner: ``tui_gateway.voice_hub`` owns the lease and decides who gets
    the mic; this object only turns an already-owned capture stream into speech segments. It is
    built at arm time and dropped at release, so a restarted gateway has no guard until a surface
    asks for one (VH1 AC1/AC3 still hold).
    """

    def __init__(self, settings: GuardSettings, *, echo: Optional[EchoCanceller] = None,
                 vad: Optional[Vad] = None, reference: Optional[ReferenceSource] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not settings.complete and (echo is None or vad is None):
            raise GuardError("guard settings incomplete: missing " + ", ".join(settings.missing()))
        self.settings = settings
        self._clock = clock
        self._lock = threading.RLock()
        # The far end the canceller subtracts. Resolved before the canceller is built so a
        # canceller with nothing to subtract is refused at arm time, by name, instead of arming,
        # holding the loopback device open and subtracting silence — which VH2 measured as useless
        # in this room (66-77% of TV silence frames called speech).
        self.reference: ReferenceSource = (reference if reference is not None
                                           else _make_reference(settings))
        real_canceller = (echo is None
                          and (settings.echo_cancel or "").lower() not in ("", "off", "none"))
        if real_canceller and (getattr(self.reference, "name", "") or "off") == "off":
            raise GuardError(
                f"a canceller with no far end is not a guard: set {SETTINGS_PATH}.reference "
                "(loopback) or turn echo_cancel off")
        self._echo = echo if echo is not None else _make_echo(settings)
        self._vad = vad if vad is not None else _make_vad(settings)
        self._pump: Optional[ReferencePump] = None
        self._refs: deque[bytes] = deque()
        self._ref_times: deque[Optional[float]] = deque()
        self._mic_index = 0
        self._late_reference_frames = 0
        self._dropped_reference_frames = 0
        self._max_reference_lag_ms = 0.0
        self._degraded = False
        self._frames = 0
        self._speech_frames = 0
        self._cancelled_frames = 0
        # assembler
        self._buf: list[bytes] = []
        self._buf_speech = 0
        self._silence_run = 0
        self._start = 0
        self._last_speech = 0
        self._emitted = 0
        self._dropped_short = 0
        self._closed = False

    # ── reference side ──────────────────────────────────────────────────────────────────────

    def start_reference(self) -> bool:
        """Start the far-end tap. The listener calls this when capture opens; idempotent.

        Returns False when there is no tap to start: ``reference: off`` (nothing to cancel
        against) or ``reference: manual`` (the caller hands the far end over with
        :meth:`push_reference`; a pump there would flood the queue with silence ahead of it).
        Nothing here decides to run on its own.
        """
        with self._lock:
            if self._closed:
                return False
            if self._pump is None:
                self._pump = ReferencePump(self.reference, self, clock=self._clock)
            pump = self._pump
        started = pump.start()
        logger.debug("voice guard reference tap %s: %s", "started" if started else "off",
                     pump.state())
        return started

    def stop_reference(self) -> None:
        """Stop the tap (capture closed). Safe to call twice, and after :meth:`close`."""
        with self._lock:
            pump, self._pump = self._pump, None
        if pump is not None:
            pump.stop()

    def push_reference(self, frame: bytes, captured_at: Optional[float] = None) -> None:
        """Hand over one 10 ms frame of the system-audio loopback (the far-end, causally early).

        ``captured_at`` is when that audio reached the tap, on the same clock as the ``captured_at``
        passed to :meth:`process`. Supply it and the guard can notice a *buffered* reference — the
        case VH2 measured at 2.9 dB ERLE — not just a missing one.

        ``max_pending_reference_frames`` (optional) trims the queue to that depth, oldest first: a
        backlog deeper than that is audio the canceller can no longer use, and keeping it would
        only grow for the life of the arm.
        """
        with self._lock:
            if self._closed:
                return
            self._refs.append(bytes(frame[:FRAME_BYTES]))
            self._ref_times.append(captured_at)
            cap = self.settings.max_pending_reference_frames
            while cap is not None and len(self._refs) > cap:
                self._refs.popleft()
                self._ref_times.popleft()
                self._dropped_reference_frames += 1

    def reference_state(self) -> dict:
        """Whether the reference is arriving in time (VH2 condition 1)."""
        with self._lock:
            pump = self._pump.state() if self._pump is not None else {"running": False}
            return {"late_frames": self._late_reference_frames, "degraded": self._degraded,
                    "max_lag_ms": round(self._max_reference_lag_ms, 1),
                    "pending_frames": len(self._refs),
                    "dropped_frames": self._dropped_reference_frames,
                    "tap": getattr(self.reference, "name", "?"), "pump": pump}

    # ── mic side ────────────────────────────────────────────────────────────────────────────

    def process(self, mic_frame: bytes, captured_at: Optional[float] = None) -> list[Utterance]:
        """Consume one mic frame; return the utterances that completed on this frame (usually none)."""
        with self._lock:
            if self._closed:
                return []
            frame = bytes(mic_frame[:FRAME_BYTES])
            reference = self._next_reference(captured_at)
            clean = self._echo.cancel(frame, reference)
            self._frames += 1
            self._mic_index += 1
            if reference is not _SILENCE and clean != frame:
                self._cancelled_frames += 1
            return self._assemble(clean)

    def flush(self) -> list[Utterance]:
        """Close any open utterance (stop of capture) and return it if it qualifies."""
        with self._lock:
            out = self._finish()
            return out

    def state(self) -> dict:
        """Counters for the doctor/log: what the guard actually did, not what it was told to do."""
        with self._lock:
            pump = self._pump.state() if self._pump is not None else {"running": False}
            return {
                "frames": self._frames,
                "speech_frames": self._speech_frames,
                "cancelled_frames": self._cancelled_frames,
                "utterances": self._emitted,
                "dropped_short": self._dropped_short,
                "late_reference_frames": self._late_reference_frames,
                "max_reference_lag_ms": round(self._max_reference_lag_ms, 1),
                "degraded": self._degraded,
                "reference": getattr(self.reference, "name", "?"),
                "reference_pump": pump,
                "dropped_reference_frames": self._dropped_reference_frames,
                "echo_cancel": getattr(self._echo, "name", "?"),
                "vad": getattr(self._vad, "name", "?"),
                "settings": self.settings.describe(),
            }

    def close(self) -> None:
        """Stop the tap, then release every backend. Never raises: the mic thread owns this."""
        self.stop_reference()
        with self._lock:
            self._closed = True
            backends = (self._echo, self._vad, self.reference)
        for backend in backends:
            try:
                backend.close()
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("voice guard backend close failed: %s", exc, exc_info=True)

    # ── internals ───────────────────────────────────────────────────────────────────────────

    def _next_reference(self, mic_time: Optional[float]) -> bytes:
        """The reference frame for this mic frame, and whether the reference is keeping up.

        Two failures are named separately because they need different fixes:

        * **missing** — no reference frame at all (the loopback tap is not running or is behind by
          more than the queue). Counted, and ``degraded`` latches past ``max_late_reference_frames``.
        * **stale** — a frame is there but its audio is older than the mic frame by more than
          ``max_reference_lag_ms``: a *buffered* reference. AEC3 cannot cancel with it (VH2: 33.8 dB
          early, 2.9 dB with the same far-end 300 ms late), so this latches ``degraded`` too.
        """
        if not self._refs:
            return self._missing_reference()
        reference = self._refs.popleft()
        reference_time = self._ref_times.popleft()
        lag_limit = self.settings.max_reference_lag_ms
        if lag_limit is not None and mic_time is not None and reference_time is not None:
            lag_ms = (mic_time - reference_time) * 1000.0
            self._max_reference_lag_ms = max(self._max_reference_lag_ms, lag_ms)
            if lag_ms > lag_limit and not self._degraded:
                self._degraded = True
                logger.warning(
                    "voice guard DEGRADED: the loopback reference is %.0f ms behind the mic "
                    "(%s.max_reference_lag_ms=%d) — echo cancellation cannot work with a buffered "
                    "reference (VH2: 33.8 dB early vs 2.9 dB 300 ms late)",
                    lag_ms, SETTINGS_PATH, lag_limit)
        return reference

    def _missing_reference(self) -> bytes:
        """No reference for this frame: count it, latch ``degraded``, and cancel nothing."""
        self._late_reference_frames += 1
        limit = self.settings.max_late_reference_frames
        if limit is not None and not self._degraded and self._late_reference_frames > limit:
            self._degraded = True
            logger.warning(
                "voice guard DEGRADED: %d frames arrived with no loopback reference "
                "(%s.max_late_reference_frames=%d) — echo cancellation is not working; "
                "the reference is buffered/late (VH2: 33.8 dB early vs 2.9 dB late)",
                self._late_reference_frames, SETTINGS_PATH, limit)
        return _SILENCE

    def _is_speech(self, frame: bytes) -> bool:
        probability = float(self._vad.probability(frame))
        return probability >= float(self.settings.vad_threshold)  # type: ignore[arg-type]

    def _assemble(self, clean: bytes) -> list[Utterance]:
        """VAD + endpointing: speech frames open/extend an utterance, a long enough gap closes it."""
        out: list[Utterance] = []
        speech = self._is_speech(clean)
        self._speech_frames += int(speech)
        min_speech_frames = max(1, round((self.settings.min_speech_ms or 0) / 10))
        min_silence_frames = max(1, round((self.settings.min_silence_ms or 0) / 10))
        max_frames = (round(self.settings.max_utterance_ms / 10)
                      if self.settings.max_utterance_ms else 0)

        if speech:
            if not self._buf:
                self._start = self._mic_index
            self._buf.append(clean)
            self._buf_speech += 1
            self._silence_run = 0
            self._last_speech = self._mic_index
        elif self._buf:
            self._buf.append(clean)
            self._silence_run += 1
            if self._silence_run >= min_silence_frames:
                out.extend(self._finish(min_speech_frames))
                return out

        if max_frames and self._buf and len(self._buf) >= max_frames:
            out.extend(self._finish(min_speech_frames))
        return out

    def _finish(self, min_speech_frames: Optional[int] = None) -> list[Utterance]:
        """Close the open buffer: emit it, or count it as too short (silence is not an utterance)."""
        if not self._buf:
            return []
        if min_speech_frames is None:
            min_speech_frames = max(1, round((self.settings.min_speech_ms or 0) / 10))
        frames, speech = self._buf, self._buf_speech
        start, end = self._start, self._last_speech
        self._buf, self._buf_speech, self._silence_run = [], 0, 0
        if speech < min_speech_frames:
            self._dropped_short += 1
            return []
        # Trim the trailing gap: an utterance ends on its last speech frame, not after the endpoint.
        keep = frames[: end - start + 1] if end >= start else frames
        self._emitted += 1
        return [Utterance(index=self._emitted, start_frame=start, end_frame=end,
                          speech_frames=speech, pcm=b"".join(keep))]


def _make_echo(settings: GuardSettings) -> EchoCanceller:
    name = (settings.echo_cancel or "").lower()
    if name in ("off", "none"):
        return PassThroughCanceller()
    if name == "aec3":
        return Aec3Canceller(stream_delay_ms=settings.stream_delay_ms)
    raise GuardError(f"unknown {SETTINGS_PATH}.echo_cancel={settings.echo_cancel!r} (known: aec3)")


def _make_vad(settings: GuardSettings) -> Vad:
    name = (settings.vad or "").lower()
    if name == "silero":
        return SileroVad(settings.silero_model, threads=settings.onnx_threads)
    raise GuardError(f"unknown {SETTINGS_PATH}.vad={settings.vad!r} (known: silero)")


def _make_reference(settings: GuardSettings) -> ReferenceSource:
    """The far-end tap named by ``reference``; empty means none (the canceller gets silence)."""
    name = (settings.reference or "").lower()
    if name in ("", "off", "none"):
        return SilentReference()
    if name == "loopback":
        return LoopbackReference()
    if name == "manual":
        return ManualReference()
    raise GuardError(
        f"unknown {SETTINGS_PATH}.reference={settings.reference!r} (known: loopback, manual, off)")


def build_guard(cfg: Optional[dict] = None) -> Optional[VoiceGuard]:
    """The guard this config asks for, or ``None`` with the reason logged and named.

    Called at arm time only. A host without the wheels is a named refusal, never a half-built
    guard: the caller keeps the pre-VH3 behaviour it was already running.
    """
    if not wants_guard(cfg):
        logger.debug("voice guard off: %s.enabled is not true", SETTINGS_PATH)
        return None
    settings = settings_from_config(cfg)
    missing = settings.missing()
    if missing:
        logger.warning("voice guard off: %s is empty (empty means off) — set it to arm the guard",
                       ", ".join(missing))
        return None
    try:
        guard = VoiceGuard(settings)
    except GuardError as exc:
        logger.warning("voice guard off: %s", exc)
        return None
    logger.info("voice guard armed: %s", settings.describe())
    return guard
