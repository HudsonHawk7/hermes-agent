"""``hermes voice`` — enrol a voice, install the model, report what the gate will do.

This is the command surface for VH5 speaker ID. Three things live here:

* ``hermes voice setup --install-speaker-model`` — fetch the one ONNX model once, verified
  against the sha256 the VH4 spec measured, into ``<data root>/models/speaker/``.
* ``hermes voice enrol --identity judson`` — record the GM's ~30 s of speech in ten short
  sentences, build the centroid, and (with ``--enable``) write the four settings that turn the
  gate on. Nothing is recorded or written unless this command is run.
* ``hermes voice status`` — what the gate would do right now, including the exact refusal reason
  when it would not start. ``hermes voice verify`` scores wav files through the same gate.

Every number goes through ``tools.voice_speaker`` — there is no second threshold, no second
windowing rule, and no second scoring function in this file.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

SETTINGS_PATH = "voice.speaker"
# What the VH4 measurement recommends. Printed, never applied silently: the GM sets it.
MEASURED_THRESHOLD = 0.62
# The one sentence the GM reads out loud when he enrols. It is printed by ``voice enrol`` and it is
# the sentence the card quotes, so the two can never drift apart.
ENROL_INVITE = "Hermes, this is my voice. Only listen to me when I am talking."


def _print(message: str = "") -> None:
    print(message, flush=True)


def _fail(message: str, code: int = 1) -> int:
    _print(f"✗ {message}")
    return code


# ── config writes ─────────────────────────────────────────────────────────────────────────────
def apply_speaker_config(*, enrolment_dir: str, threshold: float, identity: str,
                         enabled: bool = True) -> str:
    """Write ``voice.speaker.*`` into the user's config.yaml through the fail-closed writer.

    Only the four keys this feature owns are touched, and only the user's own file is written
    (``read_raw_config`` never merges defaults in, or every default would be flattened into
    config.yaml on the first enrolment).
    """
    from hermes_cli.config import (
        atomic_config_write,
        get_config_path,
        require_readable_config_before_write,
    )

    path = get_config_path()
    user_config = require_readable_config_before_write(path)
    voice = user_config.get("voice")
    if not isinstance(voice, dict):
        voice = {}
        user_config["voice"] = voice
    speaker = voice.get("speaker")
    if not isinstance(speaker, dict):
        speaker = {}
        voice["speaker"] = speaker
    speaker["enabled"] = bool(enabled)
    speaker["enrolment_dir"] = enrolment_dir
    speaker["match_threshold"] = float(threshold)
    trusted = speaker.get("trusted_identities")
    identities = [t for t in trusted if isinstance(t, str)] if isinstance(trusted, list) else []
    if identity not in identities:
        identities.append(identity)
    speaker["trusted_identities"] = identities
    atomic_config_write(path, user_config)
    return str(path)


def _current_settings():
    from tools.voice_speaker import settings_from_config

    return settings_from_config()


# ── setup ────────────────────────────────────────────────────────────────────────────────────
def cmd_setup(args: Any) -> int:
    """``hermes voice setup`` — install the speaker model (the only install step there is)."""
    from tools.voice_speaker import (
        MODEL_BYTES,
        MODEL_ID,
        MODEL_SHA256,
        SpeakerError,
        install_model,
        speaker_model_path,
    )

    if not getattr(args, "install_speaker_model", False):
        _print("Nothing to do. Options:")
        _print("  --install-speaker-model   fetch the speaker-ID model (29.6 MB, sha256 verified)")
        return 0
    source = getattr(args, "from_file", None)
    _print(f"Speaker model: {MODEL_ID}")
    _print(f"  {MODEL_BYTES:,} B, sha256 {MODEL_SHA256[:16]}…")
    _print(f"  source: {source or 'the sherpa-onnx release URL'}")
    try:
        target = install_model(source=source)
    except (OSError, RuntimeError, SpeakerError, ValueError) as exc:
        return _fail(f"install failed: {exc}")
    _print(f"✓ installed and verified: {target}")
    _print(f"  resolved path: {speaker_model_path()}")
    return 0


# ── enrol ───────────────────────────────────────────────────────────────────────────────────
def _record_chunk(seconds: float, sd: Any, np: Any) -> bytes:
    """Record one utterance at the guard's rate and return 16-bit mono PCM."""
    from tools.voice_speaker import SAMPLE_RATE

    frames = int(seconds * SAMPLE_RATE)
    recording = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    return recording.tobytes()


def _record_sentence(seconds: float, sd: Any, np: Any, *, attempts: int = 3) -> bytes:
    """One enrolment chunk that is guaranteed to contain speech.

    A silent chunk teaches the centroid nothing and would quietly weaken the enrolment, so a chunk
    with no usable window is re-recorded (up to *attempts* times) instead of being accepted.
    """
    from tools.voice_speaker import pcm_to_floats, speech_windows

    pcm = b""
    for _ in range(max(1, attempts)):
        pcm = _record_chunk(seconds, sd, np)
        if speech_windows(pcm_to_floats(pcm)):
            return pcm
        _print("    no speech in that one — again, please")
        time.sleep(0.5)
    return pcm


def cmd_enrol(args: Any) -> int:
    """``hermes voice enrol`` — the GM's 30 seconds, then a centroid on this disk only."""
    from tools.voice_speaker import (
        ENROL_TARGET_SPEECH_S,
        MODEL_ID,
        SpeakerError,
        build_enrolment,
        default_enrolment_dir,
        read_wav_pcm,
        speaker_model_path,
        verify_model,
    )

    identity = (getattr(args, "identity", None) or "").strip()
    if not identity:
        return _fail("--identity is required: the name this voice is recognised as "
                     "(for example: --identity judson)")
    threshold = getattr(args, "threshold", None)
    if threshold is None:
        threshold = _current_settings().match_threshold
    if threshold is None:
        return _fail(
            "no threshold. Pass the measured one explicitly: "
            f"--threshold {MEASURED_THRESHOLD}   (VH4: FAR 0/164, FRR 0/20 on the fixture set)")

    from_wav = list(getattr(args, "from_wav", None) or [])
    model_path = speaker_model_path()
    try:
        verify_model(model_path)
    except SpeakerError as exc:
        return _fail(f"{exc.reason}: {exc.detail}\n  Run: hermes voice setup --install-speaker-model")

    try:
        from tools.voice_speaker import CAMPPlusEmbedder

        embedder = CAMPPlusEmbedder(model_path)
    except SpeakerError as exc:
        return _fail(f"{exc.reason}: {exc.detail}")

    utterances: list[bytes] = []
    try:
        if from_wav:
            _print(f"Enrolling from {len(from_wav)} wav file(s), model {MODEL_ID}")
            for path in from_wav:
                utterances.append(read_wav_pcm(path))
                _print(f"  read {path}")
        else:
            total = float(getattr(args, "seconds", None) or ENROL_TARGET_SPEECH_S)
            count = max(1, int(getattr(args, "sentences", None) or 10))
            per = total / count
            try:
                from tools.voice_mode import _import_audio

                sd, _np = _import_audio()
            except (ImportError, OSError, RuntimeError) as exc:
                return _fail(f"no microphone backend: {exc}")
            _print(f"Recording {count} sentences of about {per:.1f} s each ({total:.0f} s total).")
            _print(f'Say this out loud, in your normal voice: "{ENROL_INVITE}"')
            _print("Then keep talking — anything you would say to Hermes. Where you normally sit.")
            for index in range(count):
                _print(f"  sentence {index + 1}/{count}: speaking starts in 2 s …")
                time.sleep(2.0)
                _print(f"    recording {per:.1f} s")
                utterances.append(_record_sentence(per, sd, _np))
                _print("    got it.")
        enrolment = build_enrolment(embedder, utterances, identity, float(threshold))
    except SpeakerError as exc:
        return _fail(f"{exc.reason}: {exc.detail}")
    finally:
        close = getattr(embedder, "close", None)
        if callable(close):
            close()

    directory = (getattr(args, "enrolment_dir", None) or default_enrolment_dir())
    target = os.path.join(directory, f"{identity}.enrol.json")
    enrolment.save(target)
    _print()
    _print(f"✓ enrolled {identity!r}: {enrolment.windows} windows, "
           f"{enrolment.speech_s:.1f} s of speech, threshold {enrolment.threshold}")
    _print(f"  written to {target} (centroid only — no audio is stored)")
    if not getattr(args, "enable", False):
        _print()
        _print("Not enabled yet. This is the config that turns the gate on:")
        _print(f"  {SETTINGS_PATH}.enabled: true")
        _print(f"  {SETTINGS_PATH}.enrolment_dir: {directory}")
        _print(f"  {SETTINGS_PATH}.match_threshold: {enrolment.threshold}")
        _print(f"  {SETTINGS_PATH}.trusted_identities: [{identity}]")
        _print("  (or re-run with --enable to write them)")
        return 0
    path = apply_speaker_config(enrolment_dir=directory, threshold=enrolment.threshold,
                                identity=identity)
    _print(f"✓ enabled in {path}: {SETTINGS_PATH}.enabled=true, "
           f"trusted_identities=[{identity}]")
    _print("  Nothing is listening yet — the gate starts the next time a surface arms the mic.")
    return 0


# ── status ──────────────────────────────────────────────────────────────────────────────────
def cmd_status(args: Any) -> int:
    """``hermes voice status`` — can the gate start, and if not, exactly why not."""
    from tools.voice_speaker import (
        MODEL_ID,
        SpeakerError,
        build_speaker_gate,
        speaker_model_path,
    )

    settings = _current_settings()
    as_json = bool(getattr(args, "json", False))
    report: dict[str, Any] = {"settings": {
        "enabled": settings.enabled,
        "enrolment_dir": settings.enrolment_dir,
        "match_threshold": settings.match_threshold,
        "trusted_identities": list(settings.trusted_identities),
    }, "model_path": speaker_model_path(), "model": MODEL_ID}
    gate = None
    if not settings.enabled:
        report["state"] = "off"
        report["reason"] = ""
    else:
        try:
            gate = build_speaker_gate()
        except SpeakerError as exc:
            report["state"] = "speaker_id_unavailable"
            report["reason"] = exc.reason
            report["detail"] = exc.detail
        else:
            report["state"] = "ready"
            report["reason"] = ""
    try:
        if gate is not None:
            report["gate"] = gate.state()
    finally:
        if gate is not None:
            gate.close()
    if as_json:
        _print(json.dumps(report, indent=2))
        return 0
    if report["state"] == "off":
        _print("Speaker ID: off (voice.speaker.enabled is false). The guard decides THAT someone")
        _print("spoke; it does not check who. Enrol with: hermes voice enrol --identity <name>")
        return 0
    if report["state"] == "speaker_id_unavailable":
        _print(f"Speaker ID: REFUSES TO LISTEN — {report['reason']}")
        _print(f"  {report.get('detail', '')}")
        _print(f"  model: {report['model_path']}")
        return 1
    gate_state = report.get("gate") or {}
    _print("Speaker ID: ready")
    _print(f"  model: {MODEL_ID}")
    _print(f"  threshold: {gate_state.get('threshold')}")
    _print(f"  enrolled: {', '.join(gate_state.get('identities') or []) or '(none)'}")
    _print(f"  trusted: {', '.join(gate_state.get('trusted_identities') or []) or '(none)'}")
    _print(f"  utterances judged: {gate_state.get('utterances', 0)} "
           f"(trusted {gate_state.get('trusted', 0)})")
    return 0


# ── verify ──────────────────────────────────────────────────────────────────────────────────
def cmd_verify(args: Any) -> int:
    """``hermes voice verify`` — score wav files through the production gate, one row each."""
    from tools.voice_speaker import SpeakerError, build_speaker_gate, read_wav_pcm

    try:
        gate = build_speaker_gate()
    except SpeakerError as exc:
        return _fail(f"{exc.reason}: {exc.detail}")
    if gate is None:
        return _fail("speaker ID is off (voice.speaker.enabled is false)")
    rows = []
    try:
        for kind, paths in (("genuine", getattr(args, "genuine", None) or []),
                            ("impostor", getattr(args, "impostor", None) or [])):
            for path in paths:
                verdict = gate.judge(read_wav_pcm(path))
                rows.append({"kind": kind, "file": os.path.basename(path),
                             "label": verdict.label, "score": verdict.score,
                             "drop_reason": verdict.drop_reason})
                _print(f"{kind:<8} {os.path.basename(path):<34} {verdict.describe()}")
    finally:
        gate.close()
    if getattr(args, "json", False):
        _print(json.dumps({"rows": rows, "gate": gate.state()}, indent=2))
    return 0


# ── dispatch ────────────────────────────────────────────────────────────────────────────────
def run_voice_command(args: Any) -> Optional[int]:
    """Route ``hermes voice <command>``; ``None`` means "no command, print help"."""
    command = getattr(args, "voice_command", None) or ""
    handlers = {
        "setup": cmd_setup,
        "enrol": cmd_enrol,
        "status": cmd_status,
        "verify": cmd_verify,
    }
    handler = handlers.get(command)
    if handler is None:
        return None
    return handler(args)


if __name__ == "__main__":  # pragma: no cover - manual smoke
    sys.exit("run me through `hermes voice`, not directly")
