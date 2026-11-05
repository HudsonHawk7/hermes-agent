"""Local STT backends.

faster-whisper loading (CUDA->CPU fallback, Apple Silicon pinning), the
anti-hallucination transcribe kwargs and segment gate, and the local whisper CLI
(``local_command``) provider. The cached-model singleton and idle-unload watcher
stay in ``transcription_tools`` (module state) and are read from it lazily.
"""

from __future__ import annotations

import logging
import os
import ctypes
import platform
import shlex
import subprocess
import tempfile
import importlib.util as _ilu
from pathlib import Path
from typing import Any, Dict, Optional

from tools.transcription_audio import _find_whisper_binary, _prepare_local_audio, _run_quiet
from tools.transcription_common import (
    DEFAULT_LOCAL_MODEL, DEFAULT_LOCAL_STT_LANGUAGE, GROQ_MODELS, LOCAL_STT_COMMAND_ENV, RETIRED_GROQ_MODELS,
    OPENAI_MODELS, STT_MODEL_CATALOG, _config_number, _error_result, _log_prompt_unsupported, _ok_result,
    _process_error_detail)

# Log-record parity with the origin module.
logger = logging.getLogger("tools.transcription_tools")


_LOCAL_LANGUAGE_ALIASES = {
    "繁體中文": "zh",
    "繁体中文": "zh",
    "简体中文": "zh",
    "簡體中文": "zh",
}
_THREE_LETTER_WHISPER_CODES = frozenset({"haw", "yue"})


def _normalize_local_stt_language(
    language: Optional[str], supported_languages: object = None
) -> Optional[str]:
    """Return a Whisper language code, or None so the caller can fall back safely."""
    if not isinstance(language, str) or not language.strip():
        return None
    raw = language.strip()
    folded = raw.casefold().replace("_", "-")
    candidate = _LOCAL_LANGUAGE_ALIASES.get(raw, folded.split("-", 1)[0])
    code_shape_is_valid = len(candidate) == 2 or candidate in _THREE_LETTER_WHISPER_CODES
    if not (candidate.isascii() and candidate.isalpha() and code_shape_is_valid):
        logger.warning("Local STT language %r is not a language code; using fallback", raw)
        return None

    if isinstance(supported_languages, (list, tuple, set, frozenset)):
        supported_codes = {str(code).casefold() for code in supported_languages}
        if candidate not in supported_codes:
            logger.warning("Local STT language %r is unsupported; using fallback", raw)
            return None
    return candidate


def _get_local_command_template() -> Optional[str]:
    configured = os.getenv(LOCAL_STT_COMMAND_ENV, "").strip()
    if configured:
        return configured
    from tools.transcription_whisper_cpp import whisper_cpp_command
    managed_command = whisper_cpp_command()
    if managed_command:
        return managed_command
    whisper_binary = _find_whisper_binary()
    return (f"{shlex.quote(whisper_binary)} {{input_path}} --model {{model}} --output_format txt "
            "--output_dir {output_dir} --language {language}") if whisper_binary else None


def _has_local_command() -> bool:
    return _get_local_command_template() is not None


def _normalize_local_model(model_name: Optional[str]) -> str:
    """Return a valid faster-whisper size; cloud-only names (``whisper-1`` …) fall back to the default with a warning."""
    if not model_name:
        return DEFAULT_LOCAL_MODEL
    if model_name in OPENAI_MODELS | GROQ_MODELS | RETIRED_GROQ_MODELS:
        logger.warning(
            "STT model '%s' is a cloud-only name and cannot be used with the local "
            "provider. Falling back to '%s'. Set stt.local.model to a valid "
            "faster-whisper size (%s).",
            model_name, DEFAULT_LOCAL_MODEL, ", ".join(STT_MODEL_CATALOG["local"]))
        return DEFAULT_LOCAL_MODEL
    return model_name


def _try_lazy_install_stt() -> bool:
    """Install faster-whisper and re-check dynamically so it's usable without a restart.

    ACTION paths only (``_transcribe_local``). Nothing that merely *resolves* or *reports* a
    provider may call this: the install takes the per-install lock for as long as a full extra-set
    rebuild, and a status probe must never start one."""
    try:
        # pm installs are gated by security.allow_lazy_installs; never a blocking
        # prompt mid-session. See #40490.
        import pm
        pm.ensure_import("stt-whisper")
        if _ilu.find_spec("faster_whisper"):
            return True
        logger.warning("faster-whisper was installed but importlib still cannot find it (may require Python restart)")
    except Exception as exc:
        logger.warning(
            "Lazy install of faster-whisper failed: %s. "
            "When the message names a restart, this process selected its dependency generation at "
            "boot and a new one cannot take effect in-flight; otherwise the Hermes process user "
            "may not be able to write to the dependency environment. Run `hermes tools` as the "
            "Hermes installation owner and select Local Whisper under Speech-to-Text.",
            exc)
    return False


# Substrings identifying a missing/unloadable CUDA runtime library: the "auto" device
# picker has already committed to CUDA, so we fall back to CPU and reload. Deliberately
# narrow (library names + dlopen phrasing): a genuine decode failure must surface, not
# silently re-run on CPU — VRAM exhaustion is the one exception, see _CUDA_OOM_MARKERS.
_CUDA_LIB_ERROR_MARKERS = (
    "libcublas", "libcudnn", "libcudart", "cannot be loaded", "cannot open shared object",
    "no kernel image is available", "CUBLAS_STATUS_NOT_SUPPORTED", "no CUDA-capable device",
    "CUDA driver version is insufficient")


def _looks_like_cuda_lib_error(exc: BaseException) -> bool:
    """Heuristic: is this a missing/broken CUDA runtime library (not a legitimate runtime failure)?"""
    return any(marker in str(exc) for marker in _CUDA_LIB_ERROR_MARKERS)


# The 3090 is shared with ComfyUI and Ollama, so a GPU transcription can lose the VRAM race.
# Losing the GPU for one utterance is fine; losing the words is not, so an out-of-memory error
# retries that utterance on the CPU behind a WARNING that says OOM. It is NOT the missing-library
# case: nothing is broken, so the cached CUDA model stays and the next utterance tries the GPU
# again.
_CUDA_OOM_MARKERS = (
    "out of memory", "cublas_status_alloc_failed", "cudnn_status_alloc_failed",
    "cuda_error_out_of_memory", "cudaerrormemoryallocation")


def _looks_like_cuda_oom_error(exc: BaseException) -> bool:
    """Heuristic: did this failure run out of GPU memory (as opposed to a missing library)?"""
    text = str(exc).casefold()
    return any(marker in text for marker in _CUDA_OOM_MARKERS)


# cuBLAS 12 is the CUDA library ctranslate2's wheel does NOT ship — it bundles cudnn64_9.dll and
# libiomp5md.dll and expects the rest of the CUDA runtime to be installed — and since Python 3.8
# the loader no longer searches PATH for an extension module's dependencies. So `device: cuda`
# died with "Library cublas64_12.dll is not found or cannot be loaded" and the CPU fallback hid it
# behind 6-8 s per utterance, however short the speech; conversation mode was unusable on Windows
# (2026-10-18..21, three profiles pinned back to the CPU). The nvidia-cublas-cu12 wheel (the
# `stt-whisper` / `voice` extras) carries cublas64_12 + cublasLt64_12 under nvidia/cublas/bin: we
# add that directory to the DLL search path and load the pair in dependency order, the same
# pattern ctranslate2/__init__.py uses for its own bundled libraries.
_CUDA_DEVICE_NAMES = frozenset({"cuda", "auto"})
_CUBLAS12_DLLS = ("cublasLt64_12.dll", "cublas64_12.dll")
_CUBLAS_EXTRA = "stt-whisper"
# ``os.add_dll_directory`` hands back a cookie that the loader only honours while it is
# referenced; keep every one we open alive for the life of the process.
_cuda_dll_dir_cookies: list[Any] = []
# None = not attempted in this process yet; then True (loaded) or False (reported as unusable).
_cublas_preload: Optional[bool] = None


def _cuda_preload_needed(device: str, host: str) -> bool:
    """Pure: does loading *device* on *host* need cuBLAS put on the DLL search path?"""
    return host == "Windows" and str(device or "").strip().casefold() in _CUDA_DEVICE_NAMES


def _cublas_bin_dir() -> Optional[Path]:
    """The nvidia-cublas-cu12 wheel's DLL directory, or None when that package is absent."""
    try:
        spec = _ilu.find_spec("nvidia.cublas")
    except (ImportError, ValueError):
        return None
    locations = list(getattr(spec, "submodule_search_locations", None) or ()) if spec else []
    for location in locations:
        bin_dir = Path(location) / "bin"
        if bin_dir.is_dir():
            return bin_dir
    return None


def _preload_cublas(bin_dir: Path) -> list[str]:
    """Load cuBLAS 12's DLLs in dependency order so ctranslate2's own load resolves them."""
    cookie = os.add_dll_directory(str(bin_dir))
    _cuda_dll_dir_cookies.append(cookie)
    for name in _CUBLAS12_DLLS:
        ctypes.CDLL(str(bin_dir / name))
    return list(_CUBLAS12_DLLS)


def _ensure_cublas_preloaded(device: str, *, host: Optional[str] = None) -> bool:
    """Put cuBLAS 12 on the DLL search path before a GPU load.

    Returns False when it cannot be done, after ONE WARNING naming the extra to install: the
    caller's CPU fallback then costs 6-8 s an utterance, which must never be a silent surprise.
    """
    global _cublas_preload
    if not _cuda_preload_needed(device, platform.system() if host is None else host):
        return False
    if _cublas_preload is not None:
        return _cublas_preload
    bin_dir = _cublas_bin_dir()
    if bin_dir is None:
        _cublas_preload = False
        logger.warning(
            "Local whisper is pinned to device=%r but the CUDA 12 cuBLAS runtime is not installed, so "
            "ctranslate2 cannot load cublas64_12.dll and every transcription runs on the CPU (6-8 s per "
            "utterance however short the speech). Install the '%s' extra (nvidia-cublas-cu12) — "
            "`hermes tools` → Speech-to-Text — to transcribe on the GPU.", device, _CUBLAS_EXTRA)
        return False
    try:
        _preload_cublas(bin_dir)
    except OSError as exc:
        _cublas_preload = False
        logger.warning("Cannot load the CUDA 12 cuBLAS runtime from %s (%s) — the GPU path will fall "
                       "back to the CPU (int8).", bin_dir, exc)
        return False
    _cublas_preload = True
    logger.info("cuBLAS 12 preloaded from %s (%s)", bin_dir, ", ".join(_CUBLAS12_DLLS))
    return True


def _sysctl_value(name: str) -> str:
    """Return a sysctl value, or an empty string when unavailable."""
    try:
        return subprocess.check_output(["/usr/sbin/sysctl", "-n", name], stderr=subprocess.DEVNULL,
                                       stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                       timeout=2).strip()
    except Exception:
        return ""


def _should_force_faster_whisper_cpu() -> bool:
    """Force CPU on Apple Silicon (incl. x86_64 under Rosetta), where ctranslate2's
    ``device="auto"`` can abort inside native code before Python can catch it."""
    if platform.system() != "Darwin":
        return False
    if platform.machine().lower() in {"arm64", "aarch64"}:
        return True
    # Under Rosetta platform.machine() reports x86_64; sysctl.proc_translated
    # flags translation and hw.optional.arm64 distinguishes Apple Silicon hosts.
    return _sysctl_value("sysctl.proc_translated") == "1" or _sysctl_value("hw.optional.arm64") == "1"


def _get_idle_unload_seconds(local_cfg: dict[str, Any]) -> int:
    """Resolve the idle unload timeout from config; 0 = never (default), negatives clamp to 0."""
    return max(_config_number(local_cfg, "unload_after_idle_seconds", 0, int), 0)


def _hub_cache_miss_error() -> type:
    """Exception faster-whisper raises for a model missing from the local Hub cache.

    ``huggingface_hub`` is an optional dependency (it arrives with faster-whisper); when it is
    absent, its ``LocalEntryNotFoundError`` base class ``OSError`` is the closest match.
    """
    try:
        from huggingface_hub.errors import LocalEntryNotFoundError
    except ImportError:
        return OSError
    return LocalEntryNotFoundError


def _create_whisper_model(model_name: str, *, device: str, compute_type: str):
    """Use a cached model without contacting the Hub, downloading only on a cache miss."""
    _ensure_cublas_preloaded(device)
    from faster_whisper import WhisperModel

    kwargs = {"device": device, "compute_type": compute_type}
    try:
        return WhisperModel(model_name, local_files_only=True, **kwargs)
    except (_hub_cache_miss_error(), RuntimeError) as exc:
        # An interrupted first download leaves a snapshot folder without the weights;
        # snapshot_download still returns it and ctranslate2 raises "Unable to open file".
        if isinstance(exc, RuntimeError) and "Unable to open file" not in str(exc):
            raise
        logger.info("faster-whisper model '%s' is not cached; downloading it from the Hugging Face Hub", model_name)

    # huggingface_hub surfaces every Hub/network failure as an OSError subclass
    # (LocalEntryNotFoundError wrapping the ConnectTimeout, HfHubHTTPError). Anything else
    # (CUDA runtime, invalid model size) is not a download problem and propagates untouched.
    try:
        return WhisperModel(model_name, local_files_only=False, **kwargs)
    except OSError as exc:
        raise RuntimeError(
            f"Unable to download faster-whisper model '{model_name}': {exc}. "
            "If huggingface.co is unreachable, set HF_ENDPOINT to an accessible mirror; "
            "when using a mirror with hf-xet installed, also set HF_HUB_DISABLE_XET=1."
        ) from exc


def _load_local_whisper_model(model_name: str, device: str = "auto", compute_type: str = "auto"):
    """Load faster-whisper with graceful CUDA → CPU fallback. ``device="auto"`` picks CUDA
    whenever the ctranslate2 wheel ships CUDA libs, even on hosts without the NVIDIA runtime (WSL2,
    headless servers): try the requested config first; on a CUDA library load failure fall back to
    CPU + int8. Pass ``stt.local.device`` / ``compute_type`` to pin.

    ``device`` / ``compute_type`` default to ``"auto"`` so the historical behaviour is unchanged; pass
    explicit values from ``stt.local.device`` / ``stt.local.compute_type`` to pin a configuration (#9088).
    """
    force_cpu = _should_force_faster_whisper_cpu()
    if force_cpu:
        # Importing ctranslate2 can itself abort on Apple Silicon/Rosetta when
        # multiple Intel OpenMP runtimes are loaded — set before the import.
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    if force_cpu:
        logger.info("Apple Silicon/Rosetta detected — loading faster-whisper on CPU "
                    "(int8) to avoid native device autodetection crashes")
        return _create_whisper_model(model_name, device="cpu", compute_type="int8")
    try:
        return _create_whisper_model(model_name, device=device, compute_type=compute_type)
    except Exception as exc:
        if _looks_like_cuda_oom_error(exc):
            logger.warning("faster-whisper ran out of GPU memory loading '%s' (%s) — the 3090 is shared "
                           "with other GPU work, so this loads on the CPU (int8).", model_name, exc)
            return _create_whisper_model(model_name, device="cpu", compute_type="int8")
        if not _looks_like_cuda_lib_error(exc):
            raise
        logger.warning("faster-whisper CUDA load failed (%s) — falling back to CPU (int8). "
                       "Install the NVIDIA CUDA runtime (libcublas/libcudnn) to use GPU.", exc)
        return _create_whisper_model(model_name, device="cpu", compute_type="int8")


# Silence-hallucination hardening for local faster-whisper (whisper decodes junk like
# "You"/"Thank you." from pure silence). Three layers, all tunable under ``stt.local``:
# Silero VAD so silence never reaches the model (``vad: false`` restores raw behaviour
# for music/ambient audio); condition_on_previous_text=False so one hallucinated token
# can't seed a run; and the segment confidence gate in _is_hallucinated_segment.
_VAD_MIN_SILENCE_MS_DEFAULT = 500
_NO_SPEECH_PROB_THRESHOLD_DEFAULT = 0.6
_LOGPROB_THRESHOLD_DEFAULT = -1.0


def build_local_transcribe_kwargs(stt_config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Kwargs for EVERY local faster-whisper ``model.transcribe`` call — single owner of the anti-hallucination hardening."""
    from tools.transcription_tools import _load_stt_config, _resolve_stt_language
    stt_config = stt_config if isinstance(stt_config, dict) else _load_stt_config()
    local_cfg = stt_config.get("local") or {}
    # ``vad: null`` in YAML means "default on".
    vad_enabled = local_cfg.get("vad", True)
    kwargs: dict[str, Any] = {
        "beam_size": 5,
        "condition_on_previous_text": False,
        "vad_filter": vad_enabled is None or bool(vad_enabled)}
    if kwargs["vad_filter"]:
        kwargs["vad_parameters"] = {
            "min_silence_duration_ms": _config_number(local_cfg, "vad_min_silence_ms", _VAD_MIN_SILENCE_MS_DEFAULT, int)
        }
    # Push the confidence gate into faster-whisper itself: its internal defaults drop
    # low-confidence segments BEFORE our post-filter sees them, so the ``stt.local``
    # threshold knobs were dead for that first gate (non-English speech decodes at
    # lower avg_logprob and was silently discarded). Same values feed both gates.
    kwargs["no_speech_threshold"], kwargs["log_prob_threshold"] = _confidence_thresholds(local_cfg)
    forced_lang = _resolve_stt_language("local", stt_config)
    if forced_lang:
        kwargs["language"] = forced_lang
    initial_prompt = local_cfg.get("initial_prompt")
    if isinstance(initial_prompt, str) and initial_prompt.strip():
        kwargs["initial_prompt"] = initial_prompt
    return kwargs


def _confidence_thresholds(local_cfg: dict[str, Any]) -> tuple[float, float]:
    """Resolve (no_speech_prob, avg_logprob) gate thresholds from config."""
    return (_config_number(local_cfg, "no_speech_prob_threshold", _NO_SPEECH_PROB_THRESHOLD_DEFAULT),
            _config_number(local_cfg, "logprob_threshold", _LOGPROB_THRESHOLD_DEFAULT))


def _is_hallucinated_segment(segment: Any, no_speech_threshold: float, logprob_threshold: float) -> bool:
    """True when a segment is very likely a silence hallucination. Conservative AND gate
    (openai-whisper's own heuristic): non-speech AND low decode confidence, so quiet-but-real speech
    survives. Unknown segment shapes are never dropped."""
    try:
        return (float(segment.no_speech_prob) > no_speech_threshold
                and float(segment.avg_logprob) < logprob_threshold)
    except (AttributeError, TypeError, ValueError):
        return False


def _join_confident_segments(segments: Any, local_cfg: dict[str, Any]) -> str:
    """Join segment texts, dropping probable silence hallucinations."""
    no_speech_threshold, logprob_threshold = _confidence_thresholds(local_cfg)
    kept: list[str] = []
    for segment in segments:
        if _is_hallucinated_segment(segment, no_speech_threshold, logprob_threshold):
            logger.debug("Dropping probable hallucinated segment %r (no_speech_prob=%.3f, avg_logprob=%.3f)",
                         getattr(segment, "text", ""), getattr(segment, "no_speech_prob", float("nan")),
                         getattr(segment, "avg_logprob", float("nan")))
            continue
        kept.append(segment.text.strip())
    return " ".join(kept).strip()


def _transcribe_local_command(
    file_path: str, model_name: str, *, language: Optional[str] = None, prompt: Optional[str] = None
) -> dict[str, Any]:
    """Run the configured local STT command template and read back a .txt transcript."""
    from tools.transcription_tools import _resolve_stt_language
    if prompt:
        _log_prompt_unsupported("STT provider 'local_command'")
    command_template = _get_local_command_template()
    if not command_template:
        return _error_result(f"{LOCAL_STT_COMMAND_ENV} not configured and no local whisper binary was found")
    # Language: hook override > stt.local.language > stt.language > env > "en".
    configured_language = language or _resolve_stt_language("local")
    language = _normalize_local_stt_language(configured_language) or DEFAULT_LOCAL_STT_LANGUAGE
    normalized_model = _normalize_local_model(model_name)
    try:
        if not os.getenv(LOCAL_STT_COMMAND_ENV, "").strip():
            from tools.transcription_whisper_cpp import ensure_whisper_cpp_models, whisper_cpp_command
            if command_template == whisper_cpp_command():
                ensure_whisper_cpp_models(normalized_model)
        with tempfile.TemporaryDirectory(prefix="hermes-local-stt-") as output_dir:
            prepared_input, prep_error = _prepare_local_audio(file_path, output_dir)
            if prep_error:
                return _error_result(prep_error)
            command = command_template.format(
                input_path=shlex.quote(prepared_input), output_dir=shlex.quote(output_dir),
                language=shlex.quote(language), model=shlex.quote(normalized_model))
            # Scrub Hermes secrets from the child env (same policy as _run_command_stt).
            # Scrub Hermes secrets from the child env (sibling path to #56332 / _run_command_stt — this
            # local-whisper path previously inherited the full process environment).
            from tools.environments.local import hermes_subprocess_env
            _run_quiet(shlex.split(command), timeout=300, env=hermes_subprocess_env(inherit_credentials=False))
            txt_files = sorted(Path(output_dir).glob("*.txt"))
            if not txt_files:
                return _error_result("Local STT command completed but did not produce a .txt transcript")
            transcript_text = txt_files[0].read_text(encoding="utf-8-sig").strip()
            logger.info("Transcribed %s via local STT command (%s, %d chars)",
                        Path(file_path).name, normalized_model, len(transcript_text))
            return _ok_result(transcript_text, "local_command")
    except KeyError as e:
        return _error_result(f"Invalid {LOCAL_STT_COMMAND_ENV} template, missing placeholder: {e}")
    except subprocess.CalledProcessError as e:
        details = _process_error_detail(e)
        logger.error("Local STT command failed for %s: %s", file_path, details)
        return _error_result(f"Local STT failed: {details}")
    except Exception as e:
        logger.error("Unexpected error during local command transcription: %s", e, exc_info=True)
        return _error_result(f"Local transcription failed: {e}")
