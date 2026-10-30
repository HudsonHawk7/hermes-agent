"""Live proof that local faster-whisper really transcribes on the GPU.

Opt-in (``local-assets``): needs a CUDA device, the faster-whisper + nvidia-cublas-cu12 wheels
and the model in the Hugging Face cache, so pyproject's ``addopts`` deselects it. Run it with

    scripts/run_tests.sh tests/tools/test_transcription_local_cuda_live.py -m local-assets

The regression this pins: without the cuBLAS 12 preload, ``device: cuda`` in a Windows venv dies
with "Library cublas64_12.dll is not found or cannot be loaded", the CPU fallback swallows it,
and conversation mode pays 6-8 s per utterance however short the speech.
"""

import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# A dashed marker name can't be spelled `pytest.mark.local-assets` (that parses as subtraction).
pytestmark = pytest.mark.__getattr__("local-assets")

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "audio" / "short_speech_16k.wav"
MODEL = "base"


def _loaded_model_device() -> str:
    """The device ctranslate2 is actually running the cached model on ("cuda" / "cpu")."""
    from tools import transcription_tools
    return str(getattr(getattr(transcription_tools._local_model, "model", None), "device", ""))


def test_cuda_device_transcribes_on_the_gpu(caplog):
    pytest.importorskip("faster_whisper")
    ctranslate2 = pytest.importorskip("ctranslate2")
    if ctranslate2.get_cuda_device_count() < 1:
        pytest.skip("no CUDA device on this host")
    assert FIXTURE.is_file(), f"missing committed fixture {FIXTURE}"

    from tools import transcription_tools

    caplog.set_level(logging.WARNING, logger="tools.transcription_tools")
    with patch.object(transcription_tools, "_local_model", None), \
            patch.object(transcription_tools, "_local_model_name", None), \
            patch.object(transcription_tools, "_load_stt_config",
                         return_value={"local": {"device": "cuda", "compute_type": "int8_float16"}}):
        started = time.perf_counter()
        result = transcription_tools._transcribe_local(str(FIXTURE), MODEL)
        elapsed = time.perf_counter() - started
        # Read the device before the patch restores the pre-test `_local_model` (None).
        loaded_device = _loaded_model_device()

    warnings = [record.getMessage() for record in caplog.records]
    assert result["success"] is True, result.get("error")
    assert "quick brown fox" in result["transcript"].lower(), result["transcript"]
    assert loaded_device == "cuda", f"transcription ran on the CPU; warnings={warnings}"
    print(f"\nCUDA transcribe: {elapsed:.2f}s wall for {FIXTURE.name} "
          f"(model={MODEL}, device=cuda, compute_type=int8_float16) -> {result['transcript']!r}")
