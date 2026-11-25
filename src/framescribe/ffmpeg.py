"""ffprobe and ffmpeg helpers. Failures raise with ffmpeg's own stderr.

Every call that opens the user's video goes through `input_args`: only the `file` protocol and only the container
formats framescribe reads (FORMAT_WHITELIST), so a file that is really an ffconcat script, an HLS playlist or an SDP
description is refused instead of making ffmpeg read other files or the network. The path is absolute, so a name like
`md5:x.mov` is never taken for a protocol. `probe` refuses, before anything is extracted, a video longer than
MAX_DURATION_SECONDS, a frame larger than MAX_FRAME_PX on a side, and audio whose 16 kHz WAV would exceed
MAX_AUDIO_WAV_BYTES.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from .errors import StageError
from .models import AudioFacts, VideoFacts
from .proc import child_process
from .safeio import temporary_sibling

# The ffmpeg demuxers framescribe reads: "mov" (MP4, MOV, M4V and the rest of the QuickTime family) and "matroska"
# (MKV and WebM). The README lists these containers.
FORMAT_WHITELIST = "mov,matroska"
SUPPORTED_CONTAINERS = "MP4, MOV, M4V, MKV or WebM"
PROBE_TIMEOUT_SECONDS = 120
MAX_DURATION_SECONDS = 12 * 3600
MAX_FRAME_PX = 8192
WAV_BYTES_PER_SECOND = 16000 * 2  # 16 kHz mono 16-bit
MAX_AUDIO_WAV_BYTES = MAX_DURATION_SECONDS * WAV_BYTES_PER_SECOND  # about 1.4 GB


class FfmpegError(StageError):
    pass


def input_args(source: Path) -> list[str]:
    """The arguments that open `source` as input: local files only, supported containers only, by absolute path."""
    return ["-protocol_whitelist", "file", "-format_whitelist", FORMAT_WHITELIST, "-i", os.path.abspath(source)]


def _refusal(source: Path | str, stderr: str) -> str | None:
    """The plain message for a file whose container ffmpeg was told not to read, else None."""
    if "not on whitelist" in stderr or "Protocol not on whitelist" in stderr:
        return (f"{Path(source).name}: framescribe reads only {SUPPORTED_CONTAINERS} files from the local disk, and "
                f"this file is something else (ffmpeg: {stderr.strip().splitlines()[-1][-200:]})")
    return None


def _run(cmd: list[str], source: Path | str | None = None, timeout: float | None = None) -> bytes:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FfmpegError(f"{cmd[0]} did not finish within {timeout:g} s") from None
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip()[-2000:]
        if source is not None and (refused := _refusal(source, tail)):
            raise FfmpegError(refused)
        raise FfmpegError(f"{cmd[0]} failed ({proc.returncode}): {tail}")
    return proc.stdout


@dataclass(frozen=True)
class Probe:
    duration: float
    video: VideoFacts
    audio: AudioFacts | None
    subtitle_streams: int
    longest_stream: float = 0.0  # the longest of the format's and the first video and audio streams' durations


def probe(path: Path) -> Probe:
    raw = _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", *input_args(path)],
               source=path, timeout=PROBE_TIMEOUT_SECONDS)
    try:
        result = _read_probe(json.loads(raw), path)
    except (ValueError, KeyError, TypeError, OverflowError, ZeroDivisionError) as e:  # incl. pydantic errors
        raise FfmpegError(f"{Path(path).name}: ffprobe's description of the file cannot be read "
                          f"({type(e).__name__}: {str(e)[:200]})") from None
    check_limits(result, path)
    return result


def _read_probe(info: dict, path: Path) -> Probe:
    streams = info["streams"]
    videos = [s for s in streams if s["codec_type"] == "video"]
    if not videos:
        raise FfmpegError(f"{path} has no video stream")
    v = videos[0]
    audios = [s for s in streams if s["codec_type"] == "audio"]
    a = audios[0] if audios else None
    durations = [float(info["format"]["duration"])]
    durations += [float(s["duration"]) for s in (v, *([a] if a else [])) if s.get("duration") not in (None, "N/A")]
    return Probe(
        duration=float(info["format"]["duration"]),
        video=VideoFacts(
            width=int(v["width"]),
            height=int(v["height"]),
            fps=float(Fraction(v["avg_frame_rate"])) if v.get("avg_frame_rate") not in (None, "0/0") else 0.0,
            codec=v["codec_name"],
        ),
        audio=AudioFacts(codec=a["codec_name"], sample_rate=int(a["sample_rate"]), channels=int(a["channels"]))
        if a
        else None,
        subtitle_streams=sum(1 for s in streams if s["codec_type"] == "subtitle"),
        longest_stream=max(durations),
    )


def check_limits(info: Probe, path: Path) -> None:
    """Refuse, before anything is extracted, what framescribe is not made for and what would fill memory or disk."""
    name = Path(path).name
    for seconds in (info.duration, info.longest_stream):
        if not math.isfinite(seconds) or seconds < 0:
            raise FfmpegError(f"{name}: ffprobe reports a duration of {seconds}, which is not a usable length")
    if info.longest_stream > MAX_DURATION_SECONDS:
        raise FfmpegError(f"{name} is {info.longest_stream / 3600:.1f} hours long; framescribe reads videos of up to "
                          f"{MAX_DURATION_SECONDS // 3600} hours")
    w, h = info.video.width, info.video.height
    if w <= 0 or h <= 0 or w > MAX_FRAME_PX or h > MAX_FRAME_PX:
        raise FfmpegError(f"{name} has a frame of {w}x{h} pixels; framescribe reads frames of up to "
                          f"{MAX_FRAME_PX} pixels on a side")
    if not math.isfinite(info.video.fps) or info.video.fps < 0:
        raise FfmpegError(f"{name}: ffprobe reports a frame rate of {info.video.fps}, which is not usable")
    if info.audio is not None and info.longest_stream * WAV_BYTES_PER_SECOND > MAX_AUDIO_WAV_BYTES:
        raise FfmpegError(f"{name}: its audio as 16 kHz WAV would take {info.longest_stream * WAV_BYTES_PER_SECOND / 1e9:.1f}"
                          f" GB, more than the {MAX_AUDIO_WAV_BYTES / 1e9:.1f} GB framescribe allows")


def extract_audio_wav(source: Path, dest: Path, on_progress: Callable[[float], None] = lambda seconds: None) -> None:
    """16 kHz mono 16-bit PCM, written to a temporary file and renamed, so `dest` is never half-written.

    `on_progress` gets the seconds of audio written so far.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    # a new, empty file with an unpredictable name, which ffmpeg (-y) then overwrites: a link planted at a fixed
    # name such as audio.wav.part cannot be written through
    part = temporary_sibling(dest, ".part")
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats", *input_args(source), "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-t", str(MAX_DURATION_SECONDS), "-f", "wav",
           os.path.abspath(part)]
    try:
        with child_process(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            assert proc.stdout is not None and proc.stderr is not None
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.lstrip("-").isdigit():
                    on_progress(max(0, int(value)) / 1e6)
            err = proc.stderr.read()
            if proc.wait() != 0:
                if refused := _refusal(source, err):
                    raise FfmpegError(refused)
                raise FfmpegError(f"ffmpeg failed ({proc.returncode}): {err.strip()[-2000:]}")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def extract_subtitles_srt(source: Path) -> str:
    """The whole first subtitle track as SRT. -ss/-t do not trim it, so callers filter on time (see ingest)."""
    return _run(["ffmpeg", "-nostdin", "-v", "error", *input_args(source), "-map", "0:s:0", "-f", "srt", "-"],
                source=source).decode("utf-8", errors="replace")


def grab_frame_rgb(source: Path, t: float, width: int, height: int) -> np.ndarray:
    """One decoded frame at time `t` as an (height, width, 3) uint8 array."""
    raw = _run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.3f}", *input_args(source), "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], source=source)
    if len(raw) != width * height * 3:
        raise FfmpegError(f"no frame decoded at t={t:.3f}s from {source}")
    return np.frombuffer(raw, np.uint8).reshape(height, width, 3)


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()
