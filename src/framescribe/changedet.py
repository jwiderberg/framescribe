"""Visual state detection from a stream of gray frames.

Signal: `frac16`, the share of pixels whose gray value differs by more than 16 between two frames. Compression
noise and pointer movement stay below 0.002, a toast is about 0.02, scrolls 0.07 to 0.25, page switches 0.5 to 0.8
(measured in docs/MEASUREMENTS.md).

A change event opens when frac16 between consecutive samples exceeds `open_fraction` (or when the current frame
has drifted from the last kept state by that much, which catches slow fades). It closes when frac16 stays below
`settle_fraction` for `settle_pairs` consecutive pairs and no new change came within `merge_seconds`. The frame that
closes it is the settled frame. The event becomes a new state only if the settled frame differs from the last kept
state's settled frame by more than `keep_fraction`; a toast that comes and goes leaves two states, a flicker
that returns to the same picture leaves none.

Nothing here keeps more than a handful of frames.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import cv2
import numpy as np

from .models import Rect

PIXEL_THRESHOLD = 16
VISIBLE_SHARE = 0.9  # a frame on its way to the new state differs from it by less than this share of the old state's difference


@dataclass(frozen=True)
class DetectParams:
    fps: float = 2.0
    open_fraction: float = 0.01
    settle_fraction: float = 0.002
    settle_pairs: int = 2
    merge_seconds: float = 1.0
    keep_fraction: float = 0.01
    min_state_seconds: float = 1.5
    max_states_per_minute: int = 20
    max_event_seconds: float = 10.0
    # change kinds
    minor_fraction: float = 0.03
    page_fraction: float = 0.30
    scroll_min_match: float = 0.70


def mean_difference(a: np.ndarray, b: np.ndarray) -> float:
    return float(cv2.absdiff(a, b).mean())


def frac_changed(a: np.ndarray, b: np.ndarray, threshold: int = PIXEL_THRESHOLD) -> float:
    return float(np.count_nonzero(cv2.absdiff(a, b) > threshold)) / a.size


def changed_rect(a: np.ndarray, b: np.ndarray, threshold: int = PIXEL_THRESHOLD) -> Rect | None:
    """Bounding box of the changed pixels, ignoring specks smaller than a few pixels. None when nothing changed."""
    mask = (cv2.absdiff(a, b) > threshold).astype(np.uint8)
    if not mask.any():
        return None
    near = cv2.dilate(mask, np.ones((9, 9), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(near, connectivity=8)
    raw = np.bincount(labels.ravel(), weights=mask.ravel(), minlength=n)
    boxes = [stats[i] for i in range(1, n) if raw[i] >= 6]
    if not boxes:
        return None
    x0 = min(int(s[cv2.CC_STAT_LEFT]) for s in boxes)
    y0 = min(int(s[cv2.CC_STAT_TOP]) for s in boxes)
    x1 = max(int(s[cv2.CC_STAT_LEFT] + s[cv2.CC_STAT_WIDTH]) for s in boxes)
    y1 = max(int(s[cv2.CC_STAT_TOP] + s[cv2.CC_STAT_HEIGHT]) for s in boxes)
    h, w = a.shape
    x0, y0, x1, y1 = max(0, x0 + 4), max(0, y0 + 4), min(w, x1 - 4), min(h, y1 - 4)  # undo the dilation margin
    if x1 <= x0 or y1 <= y0:
        return None
    return Rect(x=x0, y=y0, w=x1 - x0, h=y1 - y0)


# ---- scroll estimation -------------------------------------------------------------------------------------
#
# A scroll moves rows of content up or down. The estimate matches rows of the previous settled frame onto rows of
# the new one inside the changed area. Blank rows match any shift, so only rows with ink count, and a row matches
# when most of its inked blocks agree (a status badge that changed in the same row does not break the match). Two
# different sparse pages share almost no inked rows at any shift, which is how they are told from a scroll.

ROW_BLOCKS = 64
BLOCK_TOL = 10.0  # block-mean gray difference within which two blocks are the same
INK_BLOCK = 4.0  # a block holds ink when its mean differs this much from the background
ROW_MIN_INK_BLOCKS = 4
ROW_MATCH = 0.65  # share of a row's inked blocks that must agree
MIN_MATCHED_ROWS = 12
SCROLL_MIN_ROWS = 24  # a scroll must be backed by this many matched rows
INK_PIXEL = 24  # gray distance from the background that makes a pixel ink
AGREE_PIXEL = 40  # gray distance within which two ink pixels agree
MIN_INK_AGREEMENT = 0.80  # share of ink pixels of the overlap that agree at full resolution


@dataclass(frozen=True)
class ScrollEvidence:
    dy: int  # content displacement in px: a row at y in the previous frame is at y + dy in the new one
    match: float  # share of the informative rows of the overlap that match at this shift
    matched_rows: int
    pixel_agreement: float  # share of the overlap's ink pixels that agree at full resolution
    ambiguous: bool  # another shift explains almost as much (periodic content)


def _row_descriptors(img: np.ndarray) -> np.ndarray:
    h, w = img.shape
    nb = max(1, min(ROW_BLOCKS, w // 4))
    edges = np.linspace(0, w, nb + 1).astype(int)
    cs = np.concatenate([np.zeros((h, 1), np.float32), np.cumsum(img, axis=1, dtype=np.float32)], axis=1)
    return (cs[:, edges[1:]] - cs[:, edges[:-1]]) / np.diff(edges)[None, :]


CANDIDATES = 8  # shifts the correlation proposes; each is then tested exactly, with its neighbours


def _candidate_shifts(da, db, bg_a, bg_b, ink_a, ink_b, max_dy: int) -> list[int]:
    """Shifts worth testing: peaks of the normalised cross-correlation of the ink of the two row-descriptor
    matrices along y (an FFT per block column), each with its +-2 neighbours. Testing every shift is ~10x slower."""
    h = da.shape[0]
    xa, xb = (da - bg_a) * ink_a, (db - bg_b) * ink_b
    n = 1 << (2 * h - 1).bit_length()
    corr = np.fft.irfft((np.fft.rfft(xb, n, axis=0) * np.conj(np.fft.rfft(xa, n, axis=0))).sum(axis=1), n)
    ea, eb = np.concatenate([[0], np.cumsum((xa * xa).sum(axis=1))]), np.concatenate([[0], np.cumsum((xb * xb).sum(axis=1))])
    shifts = np.arange(-max_dy, max_dy + 1)
    shifts = shifts[shifts != 0]
    # overlap energies: dy > 0 pairs b[dy:] with a[:h-dy]; dy < 0 pairs b[:h+dy] with a[-dy:]
    e_b = np.where(shifts > 0, eb[h] - eb[np.clip(shifts, 0, h)], eb[np.clip(h + shifts, 0, h)])
    e_a = np.where(shifts > 0, ea[np.clip(h - shifts, 0, h)], ea[h] - ea[np.clip(-shifts, 0, h)])
    score = corr[shifts % n] / np.sqrt(e_a * e_b + 1e-6)
    order = np.argsort(-score)[:CANDIDATES]
    out: set[int] = set()
    for i in order:
        for d in range(-2, 3):
            dy = int(shifts[i]) + d
            if dy != 0 and abs(dy) <= max_dy:
                out.add(dy)
    return sorted(out)


def estimate_scroll(a: np.ndarray, b: np.ndarray, region: Rect | None = None) -> ScrollEvidence | None:
    """Best vertical shift that maps rows of `a` onto rows of `b` inside `region` (default: the whole frame).

    Returns None when no shift is supported by enough inked rows.
    """
    if region is None:
        region = Rect(x=0, y=0, w=a.shape[1], h=a.shape[0])
    # blurred: frames that were just encoded after a big change are softer than the ones that sat still
    ra = cv2.GaussianBlur(np.ascontiguousarray(a[region.y : region.y1, region.x : region.x1]), (5, 5), 1.2)
    rb = cv2.GaussianBlur(np.ascontiguousarray(b[region.y : region.y1, region.x : region.x1]), (5, 5), 1.2)
    h = ra.shape[0]
    if h < 2 * MIN_MATCHED_ROWS or ra.shape[1] < 16:
        return None
    bg_b = float(np.bincount(rb.ravel(), minlength=256).argmax())
    bg_a = float(np.bincount(ra.ravel(), minlength=256).argmax())
    da, db = _row_descriptors(ra), _row_descriptors(rb)
    ink_a, ink_b = np.abs(da - bg_a) >= INK_BLOCK, np.abs(db - bg_b) >= INK_BLOCK
    informative = ink_b.sum(axis=1) >= ROW_MIN_INK_BLOCKS
    # rows that agree without any shift are fixed chrome (sticky header, toast): no evidence either way
    cells0 = ink_b | ink_a
    static = informative & (((np.abs(db - da) <= BLOCK_TOL) & cells0).sum(axis=1) >= ROW_MATCH * np.maximum(cells0.sum(axis=1), 1))
    moving = informative & ~static
    best: tuple[int, int, int] | None = None  # (matched, total, dy)
    results: dict[int, int] = {}
    max_dy = h - MIN_MATCHED_ROWS
    for dy in _candidate_shifts(da, db, bg_a, bg_b, ink_a, ink_b, max_dy):
        # b[y] corresponds to a[y - dy]
        if dy > 0:
            sb, sa = slice(dy, h), slice(0, h - dy)
        else:
            sb, sa = slice(0, h + dy), slice(-dy, h)
        cells = ink_b[sb] | ink_a[sa]
        agree = (np.abs(db[sb] - da[sa]) <= BLOCK_TOL) & cells
        n_cells = cells.sum(axis=1)
        inf = moving[sb]
        same = inf & (agree.sum(axis=1) >= ROW_MATCH * np.maximum(n_cells, 1))
        total, matched = int(inf.sum()), int(same.sum())
        results[dy] = matched
        if best is None or matched > best[0] or (matched == best[0] and abs(dy) < abs(best[2])):
            best = (matched, total, dy)
    if best is None or best[0] < MIN_MATCHED_ROWS or best[1] == 0:
        return None
    matched, total, dy = best
    # full-resolution check on the ink of the overlap
    if dy > 0:
        pb, pa = rb[dy:], ra[: h - dy]
    else:
        pb, pa = rb[: h + dy], ra[-dy:]
    ink = (np.abs(pb.astype(np.int16) - bg_b) > INK_PIXEL) | (np.abs(pa.astype(np.int16) - bg_a) > INK_PIXEL)
    ink &= (moving[dy:] if dy > 0 else moving[: h + dy])[:, None]
    n_ink = int(ink.sum())
    agreement = 0.0 if n_ink == 0 else float((cv2.absdiff(pb, pa)[ink] <= AGREE_PIXEL).sum()) / n_ink
    rivals = [m for d, m in results.items() if abs(d - dy) > 3 and m >= 0.95 * matched]
    return ScrollEvidence(dy=dy, match=matched / total, matched_rows=matched, pixel_agreement=agreement,
                          ambiguous=bool(rivals))


def is_scroll(ev: ScrollEvidence | None, params: DetectParams) -> bool:
    return (
        ev is not None
        and ev.match >= params.scroll_min_match
        and ev.matched_rows >= SCROLL_MIN_ROWS
        and ev.pixel_agreement >= MIN_INK_AGREEMENT
        and abs(ev.dy) >= 2
    )


# ---- classification ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeInfo:
    kind: str  # initial | page | partial | scroll | minor
    kind_confidence: float
    changed_fraction: float
    changed_rect: Rect | None
    scroll_dy: int | None


def _margin_confidence(value: float, threshold: float) -> float:
    """0.5 right at a decision threshold, up to 1.0 far from it (relative distance)."""
    return float(0.5 + 0.5 * min(1.0, abs(value - threshold) / max(threshold, 1e-9)))


def _pairwise_scroll(frames: list[np.ndarray], params: DetectParams) -> tuple[int, float] | None:
    """A scroll too long for the two settled frames to overlap still shows in the frames in between: add up the
    shifts between consecutive samples of the event. Returns (total dy, confidence) when most of the change is scroll."""
    total_weight = passing_weight = 0.0
    shifts: list[int] = []
    for a, b in zip(frames, frames[1:]):
        f = frac_changed(a, b)
        if f < params.open_fraction:
            continue
        total_weight += f
        if f > 0.75:
            continue
        rect = changed_rect(a, b)
        ev = estimate_scroll(a, b, rect) if rect is not None else None
        if is_scroll(ev, params):
            assert ev is not None
            passing_weight += f
            shifts.append(ev.dy)
    if not shifts or passing_weight < 0.7 * total_weight:
        return None
    if not (all(d < 0 for d in shifts) or all(d > 0 for d in shifts)):
        return None
    return sum(shifts), 0.5 + 0.3 * passing_weight / total_weight


def classify_change(prev: np.ndarray | None, cur: np.ndarray, params: DetectParams,
                    event_frames: list[np.ndarray] | None = None) -> ChangeInfo:
    if prev is None:
        return ChangeInfo("initial", 1.0, 1.0, None, None)
    f = frac_changed(prev, cur)
    rect = changed_rect(prev, cur)
    area = prev.size
    rect_share = 0.0 if rect is None else rect.w * rect.h / area
    ev = None
    if rect is not None and f >= 0.02:
        ev = estimate_scroll(prev, cur, rect)  # the scrolled pane is the area that changed
    if is_scroll(ev, params):
        assert ev is not None
        strength = 0.5 * (ev.match - params.scroll_min_match) / (1 - params.scroll_min_match) + 0.5 * min(1.0, ev.matched_rows / 60)
        conf = 0.5 + 0.5 * min(1.0, strength)
        if ev.ambiguous:
            conf *= 0.7
        return ChangeInfo("scroll", round(float(conf), 3), f, rect, ev.dy)
    if event_frames is not None and len(event_frames) >= 3 and 0.02 <= f <= 0.9:
        pw = _pairwise_scroll(event_frames, params)
        if pw is not None:
            return ChangeInfo("scroll", round(pw[1], 3), f, rect, pw[0])
    # a strong but failed scroll match lowers the confidence in "page"
    doubt = 0.0 if ev is None else max(0.0, ev.match - 0.6) * 0.5
    if f >= params.page_fraction or (rect_share >= 0.6 and f >= 0.12):
        return ChangeInfo("page", round(max(0.3, _margin_confidence(f, params.page_fraction) - doubt), 3), f, rect, None)
    if f >= params.minor_fraction:
        conf = min(_margin_confidence(f, params.minor_fraction), _margin_confidence(f, params.page_fraction))
        return ChangeInfo("partial", round(max(0.3, conf - doubt), 3), f, rect, None)
    return ChangeInfo("minor", round(_margin_confidence(f, params.minor_fraction), 3), f, rect, None)


# ---- the detector ------------------------------------------------------------------------------------------


@dataclass
class DetectedState:
    start: float
    settled_index: int  # sample number of the settled frame
    settled_time: float
    settled: bool  # False when the event was cut off (maximum length, end of video)
    change: ChangeInfo
    signature: np.ndarray  # 32x16 gray thumbnail of the settled frame, to check the colour pass against


@dataclass
class CapStats:
    forced_closes: int = 0  # events cut off at max_event_seconds
    suppressed: int = 0  # changes dropped by min_state_seconds or max_states_per_minute
    unsettled_at_end: bool = False

    @property
    def hit(self) -> bool:
        return self.forced_closes > 0 or self.suppressed > 0


def signature(gray: np.ndarray) -> np.ndarray:
    return cv2.resize(gray, (32, 16), interpolation=cv2.INTER_AREA)


class StateDetector:
    """Feed gray frames in order with `feed`; call `finish` after the last. Kept states come back as they close."""

    def __init__(self, params: DetectParams) -> None:
        self.p = params
        self.k = -1
        self.prev: np.ndarray | None = None
        self.ref: np.ndarray | None = None  # settled frame of the last kept state
        self.in_event = False
        self.ev_start_pair = 0
        self.last_on = 0
        self.quiet_run = 0
        self.kept_starts: deque[float] = deque()
        self.carry_start: float | None = None  # start time of the last change that was suppressed ...
        self.carry_frame: np.ndarray | None = None  # ... and its key frame
        self.event_frames: list[np.ndarray] = []
        self.calm: tuple[float, int, np.ndarray] | None = None  # calmest frame since the last big change
        self.states: list[DetectedState] = []
        self.cap = CapStats()
        self.last_pair_fraction = 0.0
        self._merge_pairs = max(1, round(params.merge_seconds * params.fps))
        self._max_event_pairs = max(2, round(params.max_event_seconds * params.fps))

    def time_of(self, k: int) -> float:
        return k / self.p.fps

    def feed(self, frame: np.ndarray) -> DetectedState | None:
        """Process the next sample; returns a state when one was just kept."""
        p = self.p
        self.k += 1
        k = self.k
        out = None
        if self.prev is None:
            # the first picture is the first state; it counts as an event so that a fade-in is waited out
            self.in_event, self.ev_start_pair, self.last_on, self.quiet_run = True, 0, 0, 0
            self.event_frames = [frame]
            self.calm = None
            self.last_pair_fraction = 0.0
        else:
            d = frac_changed(frame, self.prev)
            self.last_pair_fraction = d
            on = d > p.open_fraction
            drift = False
            if not self.in_event and not on and self.ref is not None:
                drift = frac_changed(frame, self.ref) > p.open_fraction
            if not self.in_event:
                if on or drift:
                    self.in_event, self.ev_start_pair, self.last_on, self.quiet_run = True, k, k, 0
                    self.event_frames = [self.prev, frame]
            else:
                self.event_frames.append(frame)
                if on:
                    self.last_on, self.quiet_run, self.calm = k, 0, None
                else:
                    if self.calm is None or d < self.calm[0]:
                        self.calm = (d, k, frame)
                if on:
                    pass
                elif d < p.settle_fraction:
                    self.quiet_run += 1
                else:
                    self.quiet_run = 0
                closing = self.quiet_run >= p.settle_pairs and k - self.last_on >= self._merge_pairs
                forced = not closing and k - self.ev_start_pair >= self._max_event_pairs
                if closing or forced:
                    if forced:
                        self.cap.forced_closes += 1
                    if forced and self.calm is not None:
                        _, ck, cframe = self.calm  # the quietest moment since the last big change, not the latest frame
                        out = self._close(cframe, ck, settled=False)
                    else:
                        out = self._close(frame, k, settled=closing)
        self.prev = frame
        return out

    def finish(self) -> DetectedState | None:
        """Close an event that the video ended in the middle of."""
        if not self.in_event or self.prev is None:
            return None
        self.cap.unsettled_at_end = self.quiet_run < self.p.settle_pairs
        return self._close(self.prev, self.k, settled=not self.cap.unsettled_at_end)

    def _visible_pair(self, frames: list[np.ndarray], k: int, frame: np.ndarray) -> int:
        """The sample at which the content of `frame` (the event's key frame, sample k) started to appear: the first
        sample from which on every frame up to the key frame is clearly closer to it (mean gray difference below
        VISIBLE_SHARE of the last kept state's) than the last kept state is. A fade counts from its first frames, but
        an animation or camera feed that ran before a real change does not move the new state's start back to the
        animation's start. `frames` are the event's samples, ending at or after sample k."""
        first_k = self.k - len(frames) + 1  # sample number of frames[0]
        upto = min(len(frames) - 1, k - first_k)
        if self.ref is None or upto < 0:
            return self.ev_start_pair
        limit = VISIBLE_SHARE * mean_difference(self.ref, frame)
        visible = upto
        for i in range(upto, -1, -1):
            if mean_difference(frames[i], frame) >= limit:
                break
            visible = i
        return max(first_k + visible, self.ev_start_pair)

    def _close(self, frame: np.ndarray, k: int, settled: bool) -> DetectedState | None:
        p = self.p
        self.in_event = False
        self.calm = None
        frames, first = self.event_frames, self.ref is None
        self.event_frames = []
        visible = self._visible_pair(frames, k, frame) if not first else 0
        start = 0.0 if first else (visible - 0.5) / p.fps  # when the content appeared: the state's start
        if not first:
            assert self.ref is not None
            if frac_changed(frame, self.ref) <= p.keep_fraction:
                self.carry_start = self.carry_frame = None
                return None  # went away and came back
            now = start  # the safeguards decide at the time of this event
            if self.carry_start is not None and visible == self.ev_start_pair and self.carry_frame is not None \
                    and frac_changed(self.carry_frame, frame) <= p.keep_fraction:
                # the content was already there when this event opened: it is the change that was suppressed,
                # and it appeared when that change happened
                start = self.carry_start
            last_start = self.kept_starts[-1] if self.kept_starts else -1e9
            # the per-minute cap counts the states kept in the minute before now, so it only acts in a minute that
            # has too many (it used to measure from the first suppressed change and so never let go)
            while self.kept_starts and self.kept_starts[0] <= now - 60.0:
                self.kept_starts.popleft()
            if now - last_start < p.min_state_seconds or len(self.kept_starts) >= p.max_states_per_minute:
                self.cap.suppressed += 1
                self.carry_start, self.carry_frame = start, frame
                return None
        change = classify_change(self.ref, frame, p, frames if not first else None)
        state = DetectedState(start=start, settled_index=k, settled_time=self.time_of(k), settled=settled, change=change,
                              signature=signature(frame))
        self.ref = frame
        self.carry_start = self.carry_frame = None
        self.kept_starts.append(start)
        self.states.append(state)
        return state
