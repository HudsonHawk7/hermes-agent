"""Sample still frames from a video when the model cannot take a video part.

Ollama and other local OpenAI-compatible servers reject ``video_url`` with
``400 invalid message format``. The tool samples frames itself. Tests stub
``run_ffmpeg`` — that function is the subprocess seam.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_FRAME_COUNT = 6
_FRAME_MAX_BYTES = 2 * 1024 * 1024
_FFMPEG_TIMEOUT_SECONDS = 30
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")

MISSING_FFMPEG = (
    "ffmpeg is not on PATH, so this video cannot be sampled into frames. "
    "Install ffmpeg and retry."
)


class FfmpegMissing(ValueError):
    """ffmpeg is not installed, so frames cannot be sampled."""


class FrameExtractError(ValueError):
    """ffmpeg ran but did not produce usable frames."""


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_bin() -> str | None:
    return shutil.which("ffprobe")


def run_ffmpeg(args: list[str], timeout: int = _FFMPEG_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Run ffmpeg or ffprobe. Tests replace this function."""
    return subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def even_timestamps(duration: float, count: int = _FRAME_COUNT) -> list[float]:
    """Midpoints of ``count`` slices, clamped just inside the clip."""
    if duration <= 0:
        return [0.0]
    count = max(1, min(int(count), _FRAME_COUNT))
    last = max(0.0, duration - 0.05)
    stamps: list[float] = []
    for i in range(count):
        t = duration * (i + 0.5) / count
        stamps.append(min(max(0.0, t), last))
    return stamps


def frames_note(stamps: list[float]) -> str:
    shown = ", ".join(f"{t:.2f}s" for t in stamps)
    word = "frame" if len(stamps) == 1 else "frames"
    return f"analyzed {len(stamps)} {word} at t={shown}"


def _parse_duration(text: str) -> float | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        value = float(raw.splitlines()[0].strip())
    except ValueError:
        value = None
    if value is not None and value > 0:
        return value
    match = _DURATION_RE.search(text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe_duration_seconds(video_path: Path) -> float:
    ffprobe = ffprobe_bin()
    if ffprobe:
        result = run_ffmpeg([
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video_path),
        ])
        parsed = _parse_duration((result.stdout or b"").decode("utf-8", "replace"))
        if parsed:
            return parsed
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        raise FfmpegMissing(MISSING_FFMPEG)
    result = run_ffmpeg([ffmpeg, "-i", str(video_path)])
    text = (result.stderr or b"").decode("utf-8", "replace")
    parsed = _parse_duration(text)
    if not parsed:
        raise FrameExtractError(
            "Could not read the video duration, so frames could not be sampled."
        )
    return parsed


def extract_frames(video_path: Path, dest: Path, count: int = _FRAME_COUNT) -> list[tuple[float, Path]]:
    """Write up to ``count`` jpeg frames into ``dest``. Raises if none are written."""
    if ffmpeg_bin() is None:
        raise FfmpegMissing(MISSING_FFMPEG)
    dest.mkdir(parents=True, exist_ok=True)
    duration = probe_duration_seconds(video_path)
    stamps = even_timestamps(duration, count)
    written: list[tuple[float, Path]] = []
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        raise FfmpegMissing(MISSING_FFMPEG)
    for index, stamp in enumerate(stamps):
        out = dest / f"frame-{index:02d}.jpg"
        result = run_ffmpeg([
            ffmpeg, "-y", "-i", str(video_path),
            "-ss", f"{stamp:.3f}",
            "-frames:v", "1",
            "-vf", "scale=768:768:force_original_aspect_ratio=decrease",
            "-q:v", "5",
            str(out),
        ])
        if result.returncode != 0 or not out.is_file():
            continue
        if out.stat().st_size <= 0 or out.stat().st_size > _FRAME_MAX_BYTES:
            out.unlink(missing_ok=True)
            continue
        written.append((stamp, out))
    if not written:
        raise FrameExtractError(
            "ffmpeg could not sample frames from this video. "
            "The file may be empty or not a video."
        )
    return written
