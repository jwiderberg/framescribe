"""Difference between the text of two consecutive states, tolerant of OCR jitter and of scrolling.

A line of the new state matches a line of the old one when the text is nearly the same (digits must be identical:
"24%" and "25%" are different facts, however similar they look) and the position agrees after allowing for one
common vertical shift, the scroll. The shift is the most common vertical offset among text-matched pairs; it is
cross-checked against the visual stage's `scroll_dy`. Lines that scrolled out of view are `removed` and lines that
scrolled in are `added`; lines that merely moved with the scroll are neither.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

from .models import OcrLine, OcrScroll

MIN_TEXT_SIMILARITY = 0.85
SHORT_TEXT = 5  # normalised texts shorter than this must be identical
SHIFT_BIN = 3  # px
MIN_SHIFT_PAIRS = 3


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def text_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if min(len(a), len(b)) < SHORT_TEXT or _digits(a) != _digits(b):
        return 0.0
    m = SequenceMatcher(None, a, b, autojunk=False)
    if m.real_quick_ratio() < MIN_TEXT_SIMILARITY or m.quick_ratio() < MIN_TEXT_SIMILARITY:
        return 0.0
    r = m.ratio()
    return r if r >= MIN_TEXT_SIMILARITY else 0.0


def diff_lines(
    prev: list[OcrLine],
    cur: list[OcrLine],
    frame_w: int,
    frame_h: int,
    visual_dy: int | None = None,
) -> tuple[list[OcrLine], list[OcrLine], OcrScroll | None, int]:
    """(added, removed, scroll check, vertical shift used for matching)."""
    tol_x = max(10.0, 0.02 * frame_w)
    tol_y = max(6.0, 0.015 * frame_h)
    pn = [normalise(p.text) for p in prev]
    cn = [normalise(c.text) for c in cur]
    pairs: list[tuple[int, int, float, float, float]] = []  # (cur idx, prev idx, similarity, dx, dy)
    for i, c in enumerate(cur):
        for j, p in enumerate(prev):
            sim = text_similarity(cn[i], pn[j])
            if sim > 0:
                pairs.append((i, j, sim, c.rect.x - p.rect.x, c.rect.y - p.rect.y))
    in_column = [p for p in pairs if abs(p[3]) <= tol_x and len(cn[p[0]]) >= SHORT_TEXT]
    shift = 0
    ocr_dy: int | None = None
    if in_column:
        counts = Counter(round(p[4] / SHIFT_BIN) for p in in_column)
        bin_, n = counts.most_common(1)[0]
        if n >= MIN_SHIFT_PAIRS and n >= 0.25 * min(len(cur), len(prev)):
            ocr_dy = int(round(sum(p[4] for p in in_column if round(p[4] / SHIFT_BIN) == bin_) / n))
            shift = 0 if abs(ocr_dy) <= SHIFT_BIN else ocr_dy  # a shift of a pixel or two is jitter
    if ocr_dy is None and visual_dy is not None:
        shift = visual_dy
    usable = sorted(
        (p for p in pairs if abs(p[3]) <= tol_x and abs(p[4] - shift) <= tol_y),
        key=lambda p: (-p[2], abs(p[4] - shift)),
    )
    used_c: set[int] = set()
    used_p: set[int] = set()
    for i, j, *_ in usable:
        if i not in used_c and j not in used_p:
            used_c.add(i)
            used_p.add(j)
    added = [c for i, c in enumerate(cur) if i not in used_c]
    removed = [p for j, p in enumerate(prev) if j not in used_p]
    scroll = None
    if visual_dy is not None:
        consistent = sum(1 for p in in_column if abs(p[4] - visual_dy) <= tol_y)
        scroll = OcrScroll(visual_dy=visual_dy, ocr_dy=ocr_dy, matched_lines=len(used_c), consistent_lines=consistent)
    return added, removed, scroll, shift
