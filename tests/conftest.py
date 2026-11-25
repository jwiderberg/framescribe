"""Shared fixtures: small synthetic videos built with numpy and ffmpeg."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

FPS = 10


SILENCE = ["-f", "lavfi", "-t", "3", "-i", "anullsrc=r=16000:cl=mono"]


def encode_frames(frames, w: int, h: int, dest: Path, extra_inputs: list[str] | None = None, extra_args: list[str] | None = None) -> None:
    """Encode an iterable of (h, w, 3) uint8 frames to H.264 plus AAC; extra inputs are muxed in after the video."""
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", str(FPS), "-i", "-", *(SILENCE if extra_inputs is None else extra_inputs), "-c:v", "libx264", "-crf", "12",
           "-pix_fmt", "yuv420p", "-c:a", "aac", *([] if extra_inputs else ["-shortest"]), *(extra_args or []), str(dest)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for f in frames:
            proc.stdin.write(np.ascontiguousarray(f).tobytes())
        proc.stdin.close()
    except BrokenPipeError:  # ffmpeg stopped reading early; its exit status is checked below
        pass
    err = proc.stderr.read().decode()  # type: ignore[union-attr]
    assert proc.wait() == 0, err


def letterbox_frames(seconds: float, bar: int = 0):
    """1280x720: content at x 0-959, y 102-617, a webcam-like tile at x 960-1279, y 270-449, bars elsewhere."""
    rng = np.random.default_rng(1)
    content = rng.integers(60, 255, size=(516, 960, 3), dtype=np.uint8)
    tile = np.broadcast_to(np.linspace(80, 220, 320, dtype=np.uint8)[None, :, None], (180, 320, 3))
    for i in range(int(seconds * FPS)):
        frame = np.full((720, 1280, 3), bar, np.uint8)
        frame[102:618, 0:960] = np.roll(content, i * 7, axis=1)
        frame[270:450, 960:1280] = np.roll(tile, i * 3, axis=1)
        yield frame


def fullframe_frames(seconds: float):
    rng = np.random.default_rng(2)
    base = rng.integers(40, 255, size=(360, 640, 3), dtype=np.uint8)
    for i in range(int(seconds * FPS)):
        yield np.roll(base, i * 5, axis=0)


@pytest.fixture(scope="session")
def letterbox_video(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("video") / "letterbox.mp4"
    encode_frames(letterbox_frames(3), 1280, 720, path)
    return path


@pytest.fixture(scope="session")
def graybar_video(tmp_path_factory) -> Path:
    """Like letterbox, but the bars are dark gray (17) as in real screen recordings."""
    path = tmp_path_factory.mktemp("video") / "graybar.mp4"
    encode_frames(letterbox_frames(3, bar=17), 1280, 720, path)
    return path


@pytest.fixture(scope="session")
def noaudio_video(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("video") / "noaudio.mp4"
    encode_frames(letterbox_frames(2), 1280, 720, path, extra_inputs=[])
    return path


@pytest.fixture(scope="session")
def black_video(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("video") / "black.mp4"
    encode_frames((np.zeros((360, 640, 3), np.uint8) for _ in range(30)), 640, 360, path)
    return path


@pytest.fixture(scope="session")
def fullframe_video(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("video") / "fullframe.mp4"
    encode_frames(fullframe_frames(3), 640, 360, path)
    return path


@pytest.fixture(autouse=True)
def no_whisperkit_in_environment(monkeypatch):
    monkeypatch.delenv("FRAMESCRIBE_WHISPERKIT_CLI", raising=False)
    monkeypatch.delenv("FRAMESCRIBE_WHISPERKIT_MODEL", raising=False)
