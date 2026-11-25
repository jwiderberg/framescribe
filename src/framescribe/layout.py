"""Layout detection: where the shared-screen content sits in the frame, and what else is there.

Method: take the per-pixel "ever non-black" mask over frames sampled across the video, then peel
off the largest axis-aligned rectangle of non-black pixels repeatedly. The first is the content,
the rest (webcam tiles and the like) are other regions. Nothing here depends on one video's size.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

import cv2
import numpy as np

from .ffmpeg import FfmpegError, Probe, grab_frame_rgb
from .models import Layout, Rect

METHOD = "ever-nonblack-largest-rectangles"
MIN_REGION_AREA = 0.004  # fraction of the frame; smaller regions are noise
MIN_REGION_SIDE = 16
MAX_OTHER_RECTS = 8
FRINGE_RATIO = 0.15  # an edge line darker than this fraction of the interior median is codec fringe
MAX_FRINGE = 8  # px; wider dark bands (a dark sidebar) are content, not ringing
MAX_GROW = 12  # px a rectangle may grow into dim edge lines (vignetting) that the black level cut off
GROW_RATIO = 0.5  # a line joins the rectangle if it is at least this bright relative to the edge line
GROW_MAX_MEAN = 64  # ... and only if it is dim: a bright neighbour is another region, not vignetting
LOW_CONFIDENCE = 0.5  # below this the detected layout is not trusted and the full frame is used
MIN_CONTENT_AREA = 0.25  # fraction of the frame; a smaller "content" rectangle is implausible
MIN_CONTENT_SIDE = 0.4  # fraction of the frame's width and of its height; a thinner "content" is a band, not a screen
FLOATING_PENALTY = 0.3  # content touching no frame edge is not letterboxing; it looks like a dark slide


def sample_times(duration: float, n: int) -> list[float]:
    return [duration * (i + 0.5) / n for i in range(n)]


def detect_layout(
    source: Path, info: Probe, samples: int, on_sample: Callable[[int, int], None] = lambda done, total: None
) -> Layout:
    w, h = info.video.width, info.video.height
    ever = np.zeros((h, w), np.uint8)
    for i, t in enumerate(sample_times(info.duration, samples)):
        on_sample(i, samples)
        try:
            frame = grab_frame_rgb(source, t, w, h)
        except FfmpegError:  # a sample right at the end can fall past the last frame
            frame = grab_frame_rgb(source, max(0.0, t - 1.0), w, h)
        np.maximum(ever, frame.max(axis=2), out=ever)
    on_sample(samples, samples)
    return layout_from_ever(ever)


def black_level(ever: np.ndarray) -> int:
    """Brightness at or below which a pixel counts as black.

    Estimated from the median of the outermost pixel ring (letterbox bars sit there), plus a
    margin, and capped so that a dark-themed full-frame video is not read as black.
    """
    ring = np.concatenate([ever[0], ever[-1], ever[:, 0], ever[:, -1]])
    return int(np.clip(np.median(ring) + 3, 6, 20))


def layout_from_ever(ever: np.ndarray) -> Layout:
    """Layout from the (H, W) uint8 image of per-pixel maximum brightness over the sampled frames."""
    h, w = ever.shape
    raw = ever > black_level(ever)
    if not raw.any():
        return _full_frame(w, h, 0.0, "every sampled frame is entirely black")
    mask = cv2.morphologyEx(raw.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)).astype(bool)

    level = black_level(ever)
    claimed = np.zeros_like(raw)
    rects: list[Rect] = []
    while len(rects) < 1 + MAX_OTHER_RECTS:
        found = largest_rectangle(mask)
        if found is None:
            break
        rect = found
        if rects and (rect.w < MIN_REGION_SIDE or rect.h < MIN_REGION_SIDE or rect.w * rect.h < MIN_REGION_AREA * w * h):
            break
        mask[rect.y : rect.y1, rect.x : rect.x1] = False
        rect = trim_fringe(ever, grow_edges(ever, claimed, rect, level))
        claimed[rect.y : rect.y1, rect.x : rect.x1] = True
        rects.append(rect)

    content = rects[0]
    if (content.w < MIN_CONTENT_SIDE * w or content.h < MIN_CONTENT_SIDE * h
            or content.w * content.h < MIN_CONTENT_AREA * w * h):
        # e.g. the title band of a dark slide: a screen share is a substantial part of the frame both ways
        found = f"found content {content.x},{content.y} {content.w}x{content.h}, too small a part of the {w}x{h} frame"
        return _full_frame(w, h, 0.0, found)
    fill = float(raw[content.y : content.y1, content.x : content.x1].mean())
    explained = float((raw & claimed).sum() / raw.sum())
    touches_edge = content.x == 0 or content.y == 0 or content.x1 == w or content.y1 == h
    confidence = round(fill * explained * (1.0 if touches_edge else FLOATING_PENALTY), 4)
    if confidence < LOW_CONFIDENCE:
        found = f"found content {content.x},{content.y} {content.w}x{content.h}, confidence {confidence:.2f}"
        return _full_frame(w, h, confidence, found)
    return Layout(
        frame_w=w,
        frame_h=h,
        content_rect=content,
        other_rects=rects[1:],
        method=METHOD,
        confidence=confidence,
    )


def _full_frame(w: int, h: int, confidence: float, why: str) -> Layout:
    return Layout(
        frame_w=w,
        frame_h=h,
        content_rect=Rect(x=0, y=0, w=w, h=h),
        method=METHOD + "+full-frame-fallback",
        confidence=confidence,
        warning=f"layout detection is not confident ({why}): using the full {w}x{h} frame. "
        "Pass --content-rect x,y,w,h to set it.",
    )


def largest_rectangle(mask: np.ndarray) -> Rect | None:
    """Largest all-True axis-aligned rectangle (histogram method), or None if the mask is empty."""
    h, w = mask.shape
    heights = np.zeros(w, np.int32)
    # A maximal rectangle is bounded below by a row where some column below it is False.
    blocked_below = np.ones(h, bool)
    blocked_below[:-1] = (mask[:-1] & ~mask[1:]).any(axis=1)
    best_area, best = 0, None
    for y in range(h):
        heights = np.where(mask[y], heights + 1, 0)
        if not blocked_below[y]:
            continue
        for x0, width, height in _histogram_rectangles(heights.tolist()):
            if width * height > best_area:
                best_area, best = width * height, Rect(x=x0, y=y - height + 1, w=width, h=height)
    return best


def _histogram_rectangles(heights: list[int]):
    """Yield (x, width, height) for every maximal rectangle under a histogram."""
    stack: list[tuple[int, int]] = []  # (start x, height), heights strictly increasing
    for x, cur in enumerate([*heights, 0]):
        start = x
        while stack and stack[-1][1] >= cur:
            sx, sh = stack.pop()
            if sh > 0:
                yield sx, x - sx, sh
            start = sx
        stack.append((start, cur))


def grow_edges(ever: np.ndarray, claimed: np.ndarray, rect: Rect, level: int) -> Rect:
    """Extend each side over dim but continuous lines, e.g. a webcam image that darkens towards its edge."""
    h, w = ever.shape
    x0, y0, x1, y1 = rect.x, rect.y, rect.x1, rect.y1
    for _ in range(MAX_GROW):
        grew = False
        # (edge line, line just outside it) for each side, as (rows, cols) slices
        sides = {
            "top": ((y0, y0 + 1, x0, x1), (y0 - 1, y0, x0, x1), y0 > 0),
            "bottom": ((y1 - 1, y1, x0, x1), (y1, y1 + 1, x0, x1), y1 < h),
            "left": ((y0, y1, x0, x0 + 1), (y0, y1, x0 - 1, x0), x0 > 0),
            "right": ((y0, y1, x1 - 1, x1), (y0, y1, x1, x1 + 1), x1 < w),
        }
        for side, (edge, outside, in_frame) in sides.items():
            if not in_frame:
                continue
            ya, yb, xa, xb = outside
            if claimed[ya:yb, xa:xb].any():
                continue
            outside_mean = float(ever[ya:yb, xa:xb].mean())
            edge_mean = float(ever[edge[0] : edge[1], edge[2] : edge[3]].mean())
            if level < outside_mean <= GROW_MAX_MEAN and outside_mean >= GROW_RATIO * edge_mean:
                y0, y1, x0, x1 = (
                    y0 - (side == "top"),
                    y1 + (side == "bottom"),
                    x0 - (side == "left"),
                    x1 + (side == "right"),
                )
                grew = True
        if not grew:
            break
    return Rect(x=x0, y=y0, w=x1 - x0, h=y1 - y0)


def trim_fringe(ever: np.ndarray, rect: Rect) -> Rect:
    """Drop up to MAX_FRINGE edge lines that are codec ringing next to a black bar (far darker than the interior)."""
    x0, y0, x1, y1 = rect.x, rect.y, rect.x1, rect.y1
    region = ever[y0:y1, x0:x1].astype(np.float64)
    row_ref = np.median(region.mean(axis=1))
    col_ref = np.median(region.mean(axis=0))
    dark_row = region.mean(axis=1) < FRINGE_RATIO * row_ref
    dark_col = region.mean(axis=0) < FRINGE_RATIO * col_ref
    top = _leading_true(dark_row)
    bottom = _leading_true(dark_row[::-1])
    left = _leading_true(dark_col)
    right = _leading_true(dark_col[::-1])
    return Rect(x=x0 + left, y=y0 + top, w=rect.w - left - right, h=rect.h - top - bottom)


def _leading_true(flags: np.ndarray) -> int:
    """Length of the leading run of True if it ends within MAX_FRINGE lines, else 0 (a wide dark band is content)."""
    n = 0
    for f in flags:
        if not f:
            return n
        n += 1
        if n > MAX_FRINGE:
            return 0
    return 0


def rect_from_option(value: Iterable[int]) -> Rect:
    x, y, w, h = value
    return Rect(x=x, y=y, w=w, h=h)
