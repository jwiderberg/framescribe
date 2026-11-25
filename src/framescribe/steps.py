"""Steps of the report: where one screen-and-speech episode ends and the next begins, and which frames stand for it.

Deterministic and free of model calls. A step is a run of whole visual states. Steps start at new pages
(change kind `page`), short ones are merged into the previous step and long ones are split at a point where the
speaker paused while the screen changed, or else at a sentence boundary near the middle.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

from .models import OcrArtifact, OcrLine, OcrState, TranscriptArtifact, TranscriptSegment, VisualArtifact, VisualState

PAGE_CONFIDENCE = 0.6  # a page change starts a step only when the visual stage is at least this sure
PAUSE_SECONDS = 1.5  # a gap between sentences this long counts as a pause
PAUSE_TOLERANCE = 2.0  # a screen change within this many seconds of a pause coincides with it
CHROME_SHARE = 0.6  # a line present in more than this share of all states is interface chrome
TITLE_REGION = 0.45  # titles are looked for in this upper share of the frame
EXTRA_MIN_GAP = 8.0  # seconds between the frames chosen for one step
EXTRA_KINDS = ("page", "partial", "scroll")

_SPACE = re.compile(r"\s+")


def norm(text: str) -> str:
    """Lowercased, whitespace collapsed: the form in which on-screen lines are compared."""
    return _SPACE.sub(" ", text.strip().lower())


def has_alnum(text: str) -> bool:
    return any(c.isalnum() for c in text)


def find_chrome(ocr: OcrArtifact) -> set[str]:
    """Normalised line texts present in more than 60 % of all states."""
    counts: dict[str, int] = {}
    for st in ocr.states:
        for t in {norm(line.text) for line in st.lines}:
            if t:
                counts[t] = counts.get(t, 0) + 1
    limit = CHROME_SHARE * len(ocr.states)
    return {t for t, n in counts.items() if n > limit}


@dataclass
class StepPlan:
    id: int
    first: int  # index into visual.states of the first state
    last: int  # index of the last state, inclusive
    start: float
    end: float
    states: list[VisualState]
    segments: list[TranscriptSegment]
    title: str
    title_is_fallback: bool
    main_state_id: int
    extra_state_ids: list[int]


# ---- boundaries ----------------------------------------------------------------------------------------------


def _duration(states: list[VisualState], a: int, b: int) -> float:
    """Seconds covered by states[a:b]."""
    return states[b - 1].end - states[a].start


def page_boundaries(states: list[VisualState]) -> list[int]:
    """Indices where a block starts: the first state and every confident page change."""
    out = [0]
    for i, s in enumerate(states):
        if i > 0 and s.change.kind == "page" and s.change.kind_confidence >= PAGE_CONFIDENCE:
            out.append(i)
    return out


def merge_short(blocks: list[tuple[int, int]], states: list[VisualState], min_seconds: float) -> list[tuple[int, int]]:
    """Merge every block shorter than `min_seconds` into the previous one (the first into the next)."""
    out: list[tuple[int, int]] = []
    for a, b in blocks:
        if out and _duration(states, a, b) < min_seconds:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    while len(out) > 1 and _duration(states, *out[0]) < min_seconds:
        out[0:2] = [(out[0][0], out[1][1])]
    return out


def pauses(segments: list[TranscriptSegment]) -> list[tuple[float, float]]:
    """Gaps of at least 1.5 s between consecutive sentences."""
    return [(a.end, b.start) for a, b in zip(segments, segments[1:]) if b.start - a.end >= PAUSE_SECONDS]


def _pick_split(a: int, b: int, states: list[VisualState], segments: list[TranscriptSegment], min_seconds: float) -> int | None:
    """Index of the first state of the second half when splitting states[a:b], or None when it cannot be split."""
    candidates = range(a + 1, b)
    if not candidates:
        return None
    t0, t1 = states[a].start, states[b - 1].end
    mid = (t0 + t1) / 2
    roomy = [i for i in candidates if states[i].start - t0 >= min_seconds and t1 - states[i].start >= min_seconds]
    pool = roomy or list(candidates)
    gaps = [g for g in pauses(segments) if g[1] >= t0 - PAUSE_TOLERANCE and g[0] <= t1 + PAUSE_TOLERANCE]
    at_pause = [
        i for i in pool
        if states[i].change.kind != "minor"
        and any(g[0] - PAUSE_TOLERANCE <= states[i].start <= g[1] + PAUSE_TOLERANCE for g in gaps)
    ]
    if at_pause:
        return min(at_pause, key=lambda i: (abs(states[i].start - mid), i))
    starts = [s.start for s in segments if t0 < s.start < t1]
    target = min(starts, key=lambda t: abs(t - mid)) if starts else mid
    return min(pool, key=lambda i: (abs(states[i].start - target), i))


def split_long(blocks: list[tuple[int, int]], states: list[VisualState], segments: list[TranscriptSegment],
               min_seconds: float, max_seconds: float) -> list[tuple[int, int]]:
    """Split blocks longer than `max_seconds` until none is (a single state is never split)."""
    out: list[tuple[int, int]] = []
    todo = list(reversed(blocks))
    while todo:
        a, b = todo.pop()
        cut = _pick_split(a, b, states, segments, min_seconds) if _duration(states, a, b) > max_seconds else None
        if cut is None:
            out.append((a, b))
        else:
            todo.extend([(cut, b), (a, cut)])
    return out


def step_blocks(visual: VisualArtifact, transcript: TranscriptArtifact, min_seconds: float, max_seconds: float) -> list[tuple[int, int]]:
    """Half-open state index ranges [a, b), one per step, covering every state once."""
    states = visual.states
    bounds = page_boundaries(states) + [len(states)]
    blocks = list(zip(bounds, bounds[1:]))
    blocks = merge_short(blocks, states, min_seconds)
    return split_long(blocks, states, transcript.segments, min_seconds, max_seconds)


# ---- content of a step ---------------------------------------------------------------------------------------


def speech_overlap(state: VisualState, segments: list[TranscriptSegment]) -> float:
    return sum(max(0.0, min(state.end, s.end) - max(state.start, s.start)) for s in segments)


def choose_main(states: list[VisualState], segments: list[TranscriptSegment]) -> VisualState:
    """The state with the longest overlap with speech, preferring settled ones; ties go to the longest state."""
    pool = [s for s in states if s.settled] or states
    return max(pool, key=lambda s: (round(speech_overlap(s, segments), 6), s.end - s.start, -s.id))


def choose_extras(states: list[VisualState], main: VisualState, count: int) -> list[VisualState]:
    """Up to `count` more states of kind page, partial or scroll, ranked by changed_fraction x duration,
    keeping at least 8 s between any two chosen frames (the main frame included)."""
    ranked = sorted(
        (s for s in states if s.id != main.id and s.change.kind in EXTRA_KINDS),
        key=lambda s: (-s.change.changed_fraction * (s.end - s.start), s.id),
    )
    chosen: list[VisualState] = []
    for s in ranked:
        if len(chosen) >= count:
            break
        if all(abs(s.start - o.start) >= EXTRA_MIN_GAP for o in [main, *chosen]):
            chosen.append(s)
    return sorted(chosen, key=lambda s: s.id)


def line_centre_y(line: OcrLine) -> float:
    return line.rect.y + line.rect.h / 2


def choose_title(ocr_state: OcrState, chrome: set[str], frame_height: int) -> str | None:
    """The tallest non-chrome line in the upper 45 % of the frame; ties go to the topmost. Text is kept as read."""
    candidates = [
        ln for ln in ocr_state.lines
        if norm(ln.text) not in chrome
        and sum(c.isalnum() for c in ln.text) >= 3
        and line_centre_y(ln) <= TITLE_REGION * frame_height
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda ln: (ln.rect.h, -ln.rect.y, -ln.rect.x))
    return _SPACE.sub(" ", best.text.strip())


def assign_segments(segments: list[TranscriptSegment], starts: list[float]) -> list[list[TranscriptSegment]]:
    """Each segment goes to the step in which it starts; a segment before the first step or after the last goes there."""
    out: list[list[TranscriptSegment]] = [[] for _ in starts]
    for seg in segments:
        out[max(0, bisect_right(starts, seg.start) - 1)].append(seg)
    return out


def build_steps(visual: VisualArtifact, transcript: TranscriptArtifact, ocr: OcrArtifact, chrome: set[str],
                min_seconds: float, max_seconds: float, extra_frames: int) -> list[StepPlan]:
    states = visual.states
    blocks = step_blocks(visual, transcript, min_seconds, max_seconds)
    starts = [states[a].start for a, _ in blocks]
    per_step = assign_segments(transcript.segments, starts)
    ocr_by_id = {s.state_id: s for s in ocr.states}
    plans: list[StepPlan] = []
    for n, ((a, b), segs) in enumerate(zip(blocks, per_step), start=1):
        block = states[a:b]
        main = choose_main(block, segs)
        title = choose_title(ocr_by_id[main.id], chrome, visual.content_rect.h)
        plans.append(StepPlan(
            id=n, first=a, last=b - 1, start=states[a].start, end=states[b - 1].end, states=block, segments=segs,
            title=title or f"Step {n}", title_is_fallback=title is None, main_state_id=main.id,
            extra_state_ids=[s.id for s in choose_extras(block, main, extra_frames)],
        ))
    return plans
