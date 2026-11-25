"""ffprobe and ffmpeg helpers. Failures raise with ffmpeg's own stderr."""

from __future__ import annotations

import json
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


class FfmpegError(StageError):
    pass


def _run(cmd: list[str]) -> bytes:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip()[-2000:]
        raise FfmpegError(f"{cmd[0]} failed ({proc.returncode}): {tail}")
    return proc.stdout


@dataclass(frozen=True)
class Probe:
    duration: float
    video: VideoFacts
    audio: AudioFacts | None
    subtitle_streams: int


def probe(path: Path) -> Probe:
    raw = _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    info = json.loads(raw)
    streams = info["streams"]
    videos = [s for s in streams if s["codec_type"] == "video"]
    if not videos:
        raise FfmpegError(f"{path} has no video stream")
    v = videos[0]
    audios = [s for s in streams if s["codec_type"] == "audio"]
    a = audios[0] if audios else None
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
    )


def extract_audio_wav(source: Path, dest: Path, on_progress: Callable[[float], None] = lambda seconds: None) -> None:
    """16 kHz mono 16-bit PCM, written to a temporary file and renamed, so `dest` is never half-written.

    `on_progress` gets the seconds of audio written so far.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats", "-i", str(source), "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(part)]
    try:
        with child_process(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            assert proc.stdout is not None and proc.stderr is not None
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.lstrip("-").isdigit():
                    on_progress(max(0, int(value)) / 1e6)
            err = proc.stderr.read()
            if proc.wait() != 0:
                raise FfmpegError(f"ffmpeg failed ({proc.returncode}): {err.strip()[-2000:]}")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def extract_subtitles_srt(source: Path) -> str:
    """The whole first subtitle track as SRT. -ss/-t do not trim it, so callers filter on time (see ingest)."""
    return _run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:s:0", "-f", "srt", "-"]).decode(
        "utf-8", errors="replace"
    )


def grab_frame_rgb(source: Path, t: float, width: int, height: int) -> np.ndarray:
    """One decoded frame at time `t` as an (height, width, 3) uint8 array."""
    raw = _run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.3f}", "-i", str(source), "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    if len(raw) != width * height * 3:
        raise FfmpegError(f"no frame decoded at t={t:.3f}s from {source}")
    return np.frombuffer(raw, np.uint8).reshape(height, width, 3)


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()
