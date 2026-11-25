"""Frames out of ffmpeg, one at a time: nothing here holds more than one frame.

Two passes use the same filter prefix (`fps`, then `crop`), so frame number n of one pass is frame number n
of the other: the first decodes every sample as gray for change detection, the second decodes only the chosen
samples in colour. Decoding is software; VideoToolbox is 7 to 9 times slower on screen recordings.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterable, Iterator

import numpy as np

from .errors import StageError
from .ffmpeg import FfmpegError
from .models import Rect
from .proc import child_process

MAX_SELECT_TERMS = 4000  # indices per ffmpeg run; the expression is a balanced tree, a flat sum overflows ffmpeg at ~1500 terms


def _base_filter(rect: Rect, fps: float) -> str:
    return f"fps={fps:g},crop={rect.w}:{rect.h}:{rect.x}:{rect.y}:exact=1"


def _read_frames(cmd: list[str], frame_bytes: int) -> Iterator[bytearray]:
    with tempfile.TemporaryFile() as err, child_process(cmd, stdout=subprocess.PIPE, stderr=err) as proc:
        assert proc.stdout is not None
        while True:
            buf = bytearray(frame_bytes)
            got = proc.stdout.readinto(buf)
            if got == 0:
                break
            if got < frame_bytes:  # readinto on a pipe may return short; fill the rest
                view = memoryview(buf)
                while got < frame_bytes:
                    n = proc.stdout.readinto(view[got:])
                    if not n:
                        break
                    got += n
                if got < frame_bytes:
                    break
            yield buf
        proc.stdout.close()
        code = proc.wait()
        if code != 0:
            err.seek(0)
            tail = err.read().decode(errors="replace").strip()[-2000:]
            raise FfmpegError(f"ffmpeg failed ({code}): {tail}")


def gray_frames(source, rect: Rect, fps: float) -> Iterator[np.ndarray]:
    """Every sample (`fps` per second) of the `rect` crop as an (h, w) uint8 array, in order. Arrays are not reused."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:v:0", "-vf", _base_filter(rect, fps),
           "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    for buf in _read_frames(cmd, rect.w * rect.h):
        yield np.frombuffer(buf, np.uint8).reshape(rect.h, rect.w)


def _select_expr(indices: list[int]) -> str:
    """`select` expression for sorted distinct sample numbers; runs of consecutive numbers become one `between`.

    The terms are summed as a balanced tree: ffmpeg's expression parser fails on a long flat sum.
    """
    terms: list[str] = []
    i = 0
    while i < len(indices):
        j = i
        while j + 1 < len(indices) and indices[j + 1] == indices[j] + 1:
            j += 1
        terms.append(f"eq(n\\,{indices[i]})" if i == j else f"between(n\\,{indices[i]}\\,{indices[j]})")
        i = j + 1

    def tree(items: list[str]) -> str:
        if len(items) == 1:
            return items[0]
        mid = len(items) // 2
        return f"({tree(items[:mid])}+{tree(items[mid:])})"

    return tree(terms)


def rgb_frames(source, rect: Rect, fps: float, indices: Iterable[int]) -> Iterator[tuple[int, np.ndarray]]:
    """The chosen samples in colour as (index, (h, w, 3) uint8), ascending. Raises when ffmpeg yields fewer."""
    wanted = sorted(set(indices))
    for start in range(0, len(wanted), MAX_SELECT_TERMS):
        chunk = wanted[start : start + MAX_SELECT_TERMS]
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:v:0", "-vf",
               f"{_base_filter(rect, fps)},select='{_select_expr(chunk)}'", "-fps_mode", "passthrough",
               "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        cmd[cmd.index("-f"):cmd.index("-f")] = ["-frames:v", str(len(chunk))]  # stop decoding after the last one
        got = 0
        for buf in _read_frames(cmd, rect.w * rect.h * 3):
            if got >= len(chunk):
                break
            yield chunk[got], np.frombuffer(buf, np.uint8).reshape(rect.h, rect.w, 3)
            got += 1
        if got != len(chunk):
            raise StageError(
                f"ffmpeg returned {got} of {len(chunk)} requested key frames; the video's frame timing changed between passes"
            )
