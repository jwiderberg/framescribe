"""The chronological events of every step: what was said, what the screen did, and where the pointer was.

Everything here is taken from the artifacts as it is; spoken text is never edited and nothing is interpreted.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass

from .models import (
    OcrArtifact,
    OcrLine,
    OcrState,
    Rect,
    TimelineArtifact,
    TimelineEvent,
    TimelineStep,
    TranscriptArtifact,
    TranscriptSegment,
    VisualArtifact,
    VisualState,
)
from .steps import StepPlan, has_alnum, norm

SAID_MAX_SECONDS = 30.0  # a SAID paragraph stops growing after this long
POINTER_WINDOW = 3.0  # seconds either side of a SAID paragraph in which a pointer sample counts
POINTER_MAX_PX = 40.0  # an on-screen line this near the pointer (or containing it) counts as "near"
SCROLL_MIN_PX = 25  # smaller scrolls are not given an amount
SCROLL_TOLERANCE_PX = 8  # the OCR text must agree with the measured scroll this closely
MAX_APPEARED = 8
MAX_GONE = 4
MINOR_TEXT_LINES = 2  # a minor change that brings at least this many new lines of text is shown on its own
CHANGE_MARGIN_PX = 6  # on a partial change, lines further than this from the changed pixels did not visibly change

CUE = re.compile(r"\b(this|that|these|those|here|there|click|press|button|tab|link|open|select|hover)\b", re.IGNORECASE)


def reading_order(lines: list[OcrLine]) -> list[OcrLine]:
    return sorted(lines, key=lambda ln: (ln.rect.y, ln.rect.x))


def touches(line: OcrLine, region: Rect, margin: int = CHANGE_MARGIN_PX) -> bool:
    r = line.rect
    return r.x < region.x1 + margin and r.x1 > region.x - margin and r.y < region.y1 + margin and r.y1 > region.y - margin


def evidence_lines(lines: list[OcrLine], chrome: set[str], limit: int, region: Rect | None = None) -> list[str]:
    """Non-chrome lines that contain at least one letter or digit, in reading order, at most `limit`;
    with a `region`, only lines that touch it."""
    out = [ln.text.strip() for ln in reading_order(lines)
           if norm(ln.text) not in chrome and has_alnum(ln.text) and (region is None or touches(ln, region))]
    return out[:limit]


def scroll_amount(state: VisualState, ocr_state: OcrState) -> int | None:
    """The scroll in px when it is large enough and the OCR text agrees with it, else None."""
    dy = state.change.scroll_dy
    sc = ocr_state.scroll
    if state.change.kind != "scroll" or dy is None or abs(dy) <= SCROLL_MIN_PX or sc is None or sc.ocr_dy is None:
        return None
    return dy if abs(sc.ocr_dy - dy) <= SCROLL_TOLERANCE_PX else None


def screen_event(state: VisualState, ocr_state: OcrState, chrome: set[str]) -> TimelineEvent:
    # Re-reading an unchanged header with a different misread would otherwise fill the appeared list on every partial
    # (or minor) change, so only lines that touch the changed pixels count there.
    region = state.change.changed_rect if state.change.kind in ("partial", "minor") else None
    return TimelineEvent(
        type="screen", time=state.start, state_id=state.id, state_ids=[state.id], kind=state.change.kind,
        scroll_dy=scroll_amount(state, ocr_state),
        appeared=evidence_lines(ocr_state.added, chrome, MAX_APPEARED, region),
        gone=evidence_lines(ocr_state.removed, chrome, MAX_GONE, region),
    )


def shows_text(state: VisualState, ocr_state: OcrState, chrome: set[str]) -> bool:
    """A minor change that brings new text (a panel, a table): at least MINOR_TEXT_LINES non-chrome lines appeared
    where the pixels changed."""
    region = state.change.changed_rect
    return len(evidence_lines(ocr_state.added, chrome, MINOR_TEXT_LINES, region)) >= MINOR_TEXT_LINES


def is_collapsed(e: TimelineEvent) -> bool:
    """A screen event that stands for a run of minor changes without new text."""
    return e.type == "screen" and e.kind == "minor" and e.end is not None


def screen_events(states: list[VisualState], ocr_by_id: dict[int, OcrState], chrome: set[str]) -> list[TimelineEvent]:
    """One event per state, except that each run of consecutive minor states without new text collapses into one
    event (with `end` set). A minor state with new text is an event of its own."""
    out: list[TimelineEvent] = []
    run: list[VisualState] = []

    def flush() -> None:
        if run:
            out.append(TimelineEvent(type="screen", time=run[0].start, end=run[-1].end, state_id=run[0].id,
                                     state_ids=[s.id for s in run], kind="minor"))
            run.clear()

    for s in states:
        if s.change.kind == "minor" and not shows_text(s, ocr_by_id[s.id], chrome):
            run.append(s)
        else:
            flush()
            out.append(screen_event(s, ocr_by_id[s.id], chrome))
    flush()
    return out


def said_paragraphs(segments: list[TranscriptSegment], break_times: list[float]) -> list[list[TranscriptSegment]]:
    """Consecutive segments merged until a screen event (other than a collapsed run of minor changes) falls between two
    of them (after the first's start, up to the second's start), the speaker changes, or 30 s have accumulated."""
    times = sorted(break_times)
    out: list[list[TranscriptSegment]] = []
    for seg in segments:
        if out:
            cur = out[-1]
            prev = cur[-1]
            screen_between = bisect_right(times, seg.start) > bisect_right(times, prev.start)
            if not (screen_between or seg.speaker != prev.speaker or seg.end - cur[0].start > SAID_MAX_SECONDS):
                cur.append(seg)
                continue
        out.append([seg])
    return out


def said_event(paragraph: list[TranscriptSegment]) -> TimelineEvent:
    return TimelineEvent(
        type="said", time=paragraph[0].start, end=paragraph[-1].end, speaker=paragraph[0].speaker,
        text=" ".join(s.text.strip() for s in paragraph), segment_ids=[s.id for s in paragraph],
        filled_words=sum(1 for s in paragraph for w in s.words if w.filled),
    )


@dataclass(frozen=True)
class Sample:
    t: float
    x: float
    y: float
    state_id: int


def pointer_samples(visual: VisualArtifact) -> list[Sample]:
    return sorted((Sample(c.t, c.x, c.y, s.id) for s in visual.states for c in s.cursor), key=lambda c: c.t)


def _distance_to_rect(x: float, y: float, line: OcrLine) -> float:
    r = line.rect
    dx = max(r.x - x, 0.0, x - r.x1)
    dy = max(r.y - y, 0.0, y - r.y1)
    return (dx * dx + dy * dy) ** 0.5


def nearest_line(sample: Sample, lines: list[OcrLine]) -> OcrLine | None:
    """The on-screen line containing the point or within 40 px of it (nearest first, smaller first on a tie)."""
    near = [(d, ln.rect.w * ln.rect.h, ln) for ln in lines if has_alnum(ln.text)
            if (d := _distance_to_rect(sample.x, sample.y, ln)) <= POINTER_MAX_PX]
    if not near:
        return None
    return min(near, key=lambda c: (c[0], c[1], c[2].rect.y, c[2].rect.x))[2]


def pointer_event(paragraph: TimelineEvent, samples: list[Sample], ocr_by_id: dict[int, OcrState],
                  used: set[float] | None = None) -> TimelineEvent | None:
    """A pointer hint for a SAID paragraph with a pointing or action cue, from a sample within 3 s of it.
    A sample already listed in `used` is not offered twice (neighbouring paragraphs would repeat the same hint)."""
    if paragraph.text is None or not CUE.search(paragraph.text):
        return None
    start, end = paragraph.time, paragraph.end if paragraph.end is not None else paragraph.time
    mid = (start + end) / 2
    near = [c for c in samples if start - POINTER_WINDOW <= c.t <= end + POINTER_WINDOW]
    near.sort(key=lambda c: (max(start - c.t, c.t - end, 0.0), abs(c.t - mid), c.t))
    for c in near:
        if used is not None and c.t in used:
            continue
        line = nearest_line(c, ocr_by_id[c.state_id].lines)
        if line is not None:
            if used is not None:
                used.add(c.t)
            return TimelineEvent(type="pointer", time=c.t, text=line.text.strip(), x=c.x, y=c.y, state_id=c.state_id)
    return None


def step_events(plan: StepPlan, ocr_by_id: dict[int, OcrState], chrome: set[str], samples: list[Sample],
                used: set[float] | None = None) -> list[TimelineEvent]:
    screens = screen_events(plan.states, ocr_by_id, chrome)
    breaks = [e.time for e in screens if not is_collapsed(e)]
    said = [said_event(p) for p in said_paragraphs(plan.segments, breaks)]
    items = sorted([(e.time, 0, i, e) for i, e in enumerate(screens)] + [(e.time, 1, i, e) for i, e in enumerate(said)],
                   key=lambda it: it[:3])
    out: list[TimelineEvent] = []
    for _, _, _, e in items:
        out.append(e)
        if e.type == "said":
            p = pointer_event(e, samples, ocr_by_id, used)
            if p is not None:
                out.append(p)
    return out


def count_words(segments: list[TranscriptSegment]) -> int:
    return sum(len(s.words) if s.words else len(s.text.split()) for s in segments)


def build_timeline(source: str, asr_backend: str, visual: VisualArtifact, transcript: TranscriptArtifact,
                   ocr: OcrArtifact, plans: list[StepPlan], chrome: set[str], min_seconds: float, max_seconds: float,
                   on_step: Callable[[int, int], None] = lambda done, total: None) -> TimelineArtifact:
    ocr_by_id = {s.state_id: s for s in ocr.states}
    samples = pointer_samples(visual)
    used: set[float] = set()
    steps: list[TimelineStep] = []
    for plan in plans:
        steps.append(TimelineStep(
            id=plan.id, start=plan.start, end=plan.end, title=plan.title, title_is_fallback=plan.title_is_fallback,
            main_state_id=plan.main_state_id, extra_state_ids=plan.extra_state_ids,
            state_ids=[s.id for s in plan.states], segment_ids=[s.id for s in plan.segments],
            words=count_words(plan.segments), events=step_events(plan, ocr_by_id, chrome, samples, used),
        ))
        on_step(plan.id, len(plans))
    return TimelineArtifact(
        source=source, duration=visual.duration, asr_backend=asr_backend, step_min_seconds=min_seconds,
        step_max_seconds=max_seconds, chrome=sorted(chrome), steps=steps,
    )

