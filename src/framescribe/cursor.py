"""Best-effort pointer tracking from frame differences. No template of any pointer is used.

When the picture is otherwise static, a moving pointer shows up in the difference of two consecutive samples as one
or two small compact blobs: where it was (it is gone in the new frame) and where it is (it is new). Two cues say
which is which: the blob that holds more structure in the new frame than in the old one is the new position, and
the new position is the blob that is not at the last known position. A sample is output only when the evidence is
clear; when the differences are not pointer-like (larger regions, many blobs, a blob in one frame only) nothing is
output for that pair. The position is the centre of the blob's bounding box, which for an arrow pointer lies
about half a pointer size down and right of the hot spot.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

DIFF_THRESHOLD = 12
MIN_BLOB_PIXELS = 10  # changed pixels in a blob; fewer is noise
MIN_DENSITY = 0.10  # changed pixels / bounding box area
MAX_PAIR_FRACTION = 0.004  # changed share of the frame (threshold 16) above which a pair is not pointer-only
CUE_MARGIN = 4.0  # gray levels of std-dev difference that make the structure cue clear
CONFIDENCE_FLOOR = 0.5
DARK_LEVEL = 60  # gray level at or below which a pixel counts as near-black
MIN_DARK_CORE = 10  # near-black pixels a pointer's blob must hold at its position in the frame that shows it
VOLATILE_HITS = 3  # this many detections at one spot within VOLATILE_SECONDS: something blinks there, it is not a pointer
VOLATILE_RADIUS = 8.0
VOLATILE_SECONDS = 10.0


@dataclass(frozen=True)
class Blob:
    cx: float
    cy: float
    x0: int
    y0: int
    x1: int
    y1: int
    pixels: int

    @property
    def size(self) -> float:
        return float(max(self.x1 - self.x0, self.y1 - self.y0))


@dataclass(frozen=True)
class Sample:
    t: float
    x: float
    y: float
    confidence: float


def find_blobs(prev: np.ndarray, cur: np.ndarray, max_side: int) -> list[Blob] | None:
    """Compact changed regions between two frames; None when the difference is not pointer-like."""
    mask = (cv2.absdiff(cur, prev) > DIFF_THRESHOLD).astype(np.uint8)
    near = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(near, connectivity=8)
    blobs: list[Blob] = []
    for i in range(1, n):
        x, y, w, h, _area = (int(v) for v in stats[i])
        sub = mask[y : y + h, x : x + w] & (labels[y : y + h, x : x + w] == i)
        pixels = int(sub.sum())
        if pixels < MIN_BLOB_PIXELS:
            continue  # speck: compression noise
        ys, xs = np.nonzero(sub)
        bx0, by0, bx1, by1 = int(xs.min()) + x, int(ys.min()) + y, int(xs.max()) + 1 + x, int(ys.max()) + 1 + y
        if max(bx1 - bx0, by1 - by0) > max_side:
            return None
        if pixels / ((bx1 - bx0) * (by1 - by0)) < MIN_DENSITY:
            return None
        blobs.append(Blob((bx0 + bx1) / 2, (by0 + by1) / 2, bx0, by0, bx1, by1, pixels))
        if len(blobs) > 2:
            return None
    return blobs


def _patch(img: np.ndarray, cx: float, cy: float, r: int) -> np.ndarray:
    h, w = img.shape
    x0, y0 = int(round(cx)) - r, int(round(cy)) - r
    out = np.zeros((2 * r, 2 * r), np.float32)
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(w, x0 + 2 * r), min(h, y0 + 2 * r)
    out[sy0 - y0 : sy1 - y0, sx0 - x0 : sx1 - x0] = img[sy0:sy1, sx0:sx1]
    return out


def dark_core(img: np.ndarray, b: Blob) -> int:
    """Near-black pixels inside the blob's box. Pointers are drawn with a black body or outline."""
    sub = img[max(0, b.y0 - 1) : b.y1 + 1, max(0, b.x0 - 1) : b.x1 + 1]
    return int((sub <= DARK_LEVEL).sum())


def _dark_gain(prev: np.ndarray, cur: np.ndarray, b: Blob) -> int:
    return dark_core(cur, b) - dark_core(prev, b)


def _structure_gain(prev: np.ndarray, cur: np.ndarray, b: Blob) -> float:
    """How much more texture the blob's region has in the new frame than in the old one."""
    pad = 2
    sl = (slice(max(0, b.y0 - pad), b.y1 + pad), slice(max(0, b.x0 - pad), b.x1 + pad))
    return float(cur[sl].std()) - float(prev[sl].std())


class CursorTracker:
    def __init__(self, frame_shape: tuple[int, int]) -> None:
        h, w = frame_shape
        self.max_side = max(24, int(0.1 * min(h, w)))
        self.max_pair_fraction = MAX_PAIR_FRACTION
        self.pos: tuple[float, float] | None = None
        self.samples: list[Sample] = []

    def feed(self, t: float, prev: np.ndarray, cur: np.ndarray, pair_fraction: float) -> Sample | None:
        """`pair_fraction` is frac16 between the two frames (computed by the caller for change detection)."""
        if pair_fraction > self.max_pair_fraction:
            return None
        blobs = find_blobs(prev, cur, self.max_side)
        if not blobs:
            return None
        sample = self._two(t, prev, cur, blobs) if len(blobs) == 2 else None  # one blob: the ends overlap, say nothing
        if sample is not None and sample.confidence >= CONFIDENCE_FLOOR:
            self.pos = (sample.x, sample.y)
            self.samples.append(sample)
            return sample
        return None

    def finish(self) -> list[Sample]:
        """The samples, minus those at spots where something keeps blinking (several hits close in time and place)."""
        keep = []
        for i, s in enumerate(self.samples):
            near = [o for o in self.samples if abs(o.x - s.x) <= VOLATILE_RADIUS and abs(o.y - s.y) <= VOLATILE_RADIUS]
            # a window of VOLATILE_SECONDS that holds VOLATILE_HITS detections at this spot, and contains s
            times = sorted(o.t for o in near)
            volatile = any(
                times[j + VOLATILE_HITS - 1] - times[j] <= VOLATILE_SECONDS and times[j] <= s.t <= times[j + VOLATILE_HITS - 1]
                for j in range(len(times) - VOLATILE_HITS + 1)
            )
            if not volatile:
                keep.append(s)
        self.samples = keep
        return keep

    def _near(self, b: Blob, radius_scale: float = 1.0) -> bool:
        if self.pos is None:
            return False
        r = max(8.0, b.size) * radius_scale
        return abs(b.cx - self.pos[0]) <= r and abs(b.cy - self.pos[1]) <= r

    def _two(self, t: float, prev: np.ndarray, cur: np.ndarray, blobs: list[Blob]) -> Sample | None:
        a, b = blobs
        ratio = a.pixels / b.pixels
        if not 0.3 <= ratio <= 3.3:
            return None  # the two ends of a pointer move look alike
        dark_a, dark_b = _dark_gain(prev, cur, a), _dark_gain(prev, cur, b)
        if abs(dark_a - dark_b) >= MIN_DARK_CORE:  # the pointer's black body appears at one end and vanishes at the other
            cue_new = a if dark_a > dark_b else b
        else:
            gain_a, gain_b = _structure_gain(prev, cur, a), _structure_gain(prev, cur, b)
            cue_new = None if abs(gain_a - gain_b) < CUE_MARGIN else (a if gain_a > gain_b else b)
        chain_new = None
        if self._near(a) and not self._near(b):
            chain_new = b
        elif self._near(b) and not self._near(a):
            chain_new = a
        if cue_new is not None and chain_new is not None:
            if cue_new is not chain_new:
                return None  # the cues disagree: say nothing
            new, conf = cue_new, 0.95
        elif chain_new is not None:
            new, conf = chain_new, 0.75
        elif cue_new is not None:
            new, conf = cue_new, 0.65
        else:
            return None
        old = b if new is a else a
        if dark_core(cur, new) < MIN_DARK_CORE or dark_core(prev, old) < MIN_DARK_CORE:
            return None  # whatever moved is not drawn like a pointer (no black body or outline)
        return Sample(t, new.cx, new.cy, conf)
